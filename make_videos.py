"""Paradox TikTok video maker: Google Sheet (published CSV) -> AI voice + AI images + captions -> looping 9:16 MP4.
Runs on GitHub Actions. Only rows with status APPROVED and loop_pass TRUE are rendered."""
import asyncio, csv, io, os, random, re, subprocess, sys, time, urllib.parse
from pathlib import Path
import requests
from PIL import Image, ImageDraw

W, H = 1080, 1920
FPS = 30
VOICE = os.environ.get("VOICE", "en-US-AndrewNeural")
MAX_VIDEOS = int(os.environ.get("MAX_VIDEOS", "3"))
POLLI_TOKEN = os.environ.get("POLLINATIONS_TOKEN", "").strip()
IMAGE_TRIES = int(os.environ.get("IMAGE_TRIES", "3"))
RETRY_WAIT = int(os.environ.get("RETRY_WAIT", "15"))
STYLE = ("dark moody surreal digital illustration, high contrast, cinematic lighting, "
         "deep blue and purple palette, vertical composition, no text, no words, no letters, no people's faces")
OUT, WORK = Path("output"), Path("work")


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("Command failed: " + " ".join(map(str, cmd[:4])) + "\n" + r.stderr[-800:])
    return r.stdout


def sentences(text):
    return [s for s in re.split(r"(?<=[.?!])\s+", text.strip()) if s]


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def loop_ok(row):
    if row.get("loop_pass", "").strip().upper() != "TRUE":
        return False, "loop_pass is not TRUE"
    s = sentences(row.get("final_script", ""))
    if len(s) < 3:
        return False, "script too short"
    if norm(s[0]) == norm(s[-1]):
        return False, "last sentence repeats the hook"
    n = len(row["final_script"].split())
    if not 30 <= n <= 90:
        return False, "word count %d outside 40-90" % n
    return True, ""


def speakable(text):
    t = re.sub(r"\s+", " ", text.strip())
    # trailing comma/ellipsis removed so the voice does not drop into a "full stop" tone before the loop
    return re.sub(r"(?:\.\.\.|[,;:\u2026])+$", "", t).strip()


async def tts(text, mp3):
    import edge_tts
    try:
        comm = edge_tts.Communicate(text, VOICE, boundary="WordBoundary")
    except TypeError:
        comm = edge_tts.Communicate(text, VOICE)
    words = []
    with open(mp3, "wb") as f:
        async for ch in comm.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                words.append((ch["offset"] / 1e7, (ch["offset"] + ch["duration"]) / 1e7, ch["text"]))
    return words


def audio_seconds(path):
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                      "-of", "default=nw=1:nk=1", str(path)]).strip())


def ass_time(t):
    cs = int(round(t * 100))
    return "%d:%02d:%02d.%02d" % (cs // 360000, cs // 6000 % 60, cs // 100 % 60, cs % 100)


def write_ass(words, script, duration, path, per=3):
    if not words:  # fallback: spread script words evenly across the audio
        toks = script.split()
        step = duration / max(len(toks), 1)
        words = [(i * step, (i + 1) * step, w) for i, w in enumerate(toks)]
    groups = [words[i:i + per] for i in range(0, len(words), per)]
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding\n"
            "Style: Default,DejaVu Sans,86,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,2,5,80,80,0,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n") % (W, H)
    lines = []
    for i, g in enumerate(groups):
        start = 0.0 if i == 0 else g[0][0]
        end = groups[i + 1][0][0] if i + 1 < len(groups) else duration
        text = " ".join(w[2] for w in g).replace("{", "(").replace("}", ")")
        lines.append("Dialogue: 0,%s,%s,Default,,0,0,0,,%s" % (ass_time(start), ass_time(end), text))
    Path(path).write_text(head + "\n".join(lines) + "\n", encoding="utf-8")


def fallback_image(seed, path):
    rnd = random.Random(seed)
    c1 = (rnd.randint(5, 40), rnd.randint(5, 30), rnd.randint(40, 110))
    c2 = (rnd.randint(40, 110), rnd.randint(5, 40), rnd.randint(60, 140))
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(c1[k] * (1 - t) + c2[k] * t) for k in range(3)))
    img.save(path, quality=92)


def fetch_image(prompt, seed, path):
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt) +
           "?width=768&height=1344&nologo=true&model=flux&seed=%d" % seed)
    headers = {"Authorization": "Bearer " + POLLI_TOKEN} if POLLI_TOKEN else {}
    for attempt in range(IMAGE_TRIES):
        try:
            r = requests.get(url, headers=headers, timeout=120)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                raw = Path(str(path) + ".raw")
                raw.write_bytes(r.content)
                img = Image.open(raw).convert("RGB")
                scale = max(W / img.width, H / img.height)  # cover-crop to 1080x1920
                img = img.resize((int(img.width * scale) + 1, int(img.height * scale) + 1), Image.LANCZOS)
                l, t = (img.width - W) // 2, (img.height - H) // 2
                img.crop((l, t, l + W, t + H)).save(path, quality=92)
                return True
            print("  image HTTP", r.status_code)
        except Exception as e:
            print("  image error:", str(e)[:150])
        time.sleep(RETRY_WAIT * (attempt + 1))
    return False


def beats(row):
    parts = [p.strip() for p in row.get("on_screen_text", "").split("|") if p.strip()]
    if len(parts) < 3:
        parts = sentences(row["final_script"])[:5]
    return parts[:6]


def clip(img, frames, zoom_in, path):
    z = ("1+0.15*on/%d" % frames) if zoom_in else ("1.15-0.15*on/%d" % frames)
    vf = ("scale=1296:2304,zoompan=z='%s':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2':d=%d:s=%dx%d:fps=%d,format=yuv420p"
          % (z, frames, W, H, FPS))
    run(["ffmpeg", "-y", "-i", str(img), "-vf", vf, "-frames:v", str(frames), "-r", str(FPS),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", str(path)])


def render(row):
    vid = re.sub(r"[^A-Za-z0-9_-]", "_", row["video_id"])
    wd = WORK / vid
    wd.mkdir(parents=True, exist_ok=True)
    script = speakable(row["final_script"])
    mp3 = wd / "voice.mp3"
    words = asyncio.run(tts(script, mp3))
    dur = audio_seconds(mp3)
    print("  voice: %.1fs, %d word timings" % (dur, len(words)))

    phrases = beats(row)
    n = len(phrases)
    loop_visual = n >= 3                      # last scene reuses image 1 so last frame == first frame
    unique = n - 1 if loop_visual else n
    imgs = []
    for i in range(unique):
        p = "%s. %s. %s" % (row["topic"].split(":")[0], phrases[i], STYLE)
        path = wd / ("img%d.jpg" % i)
        if i > 0 and not POLLI_TOKEN:
            time.sleep(8)                     # be gentle with the free image service
        ok = fetch_image(p, random.randint(1, 99999), path)
        if not ok:
            print("  using gradient fallback for scene", i + 1)
            fallback_image(i + hash(vid) % 1000, path)
        imgs.append(path)
    scenes = imgs + ([imgs[0]] if loop_visual else [])

    total_frames = int(round(dur * FPS))
    per = total_frames // len(scenes)
    clips = []
    for i, img in enumerate(scenes):
        frames = per if i < len(scenes) - 1 else total_frames - per * (len(scenes) - 1)
        cp = wd / ("clip%d.mp4" % i)
        clip(img, frames, zoom_in=not (loop_visual and i == len(scenes) - 1), path=cp)
        clips.append(cp)
    (wd / "list.txt").write_text("".join("file '%s'\n" % c.name for c in clips))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(wd / "list.txt"), "-c", "copy", str(wd / "video.mp4")])

    ass = wd / "captions.ass"
    write_ass(words, script, dur, ass)
    OUT.mkdir(exist_ok=True)
    final = OUT / (vid + ".mp4")
    run(["ffmpeg", "-y", "-i", str(wd / "video.mp4"), "-i", str(mp3),
         "-vf", "eq=brightness=-0.08,ass=%s" % ass.as_posix(),
         "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-c:a", "aac", "-b:a", "160k", "-t", "%.3f" % dur, "-movflags", "+faststart", str(final)])
    return final, wd, dur


def main():
    url = os.environ.get("SHEET_CSV_URL", "").strip()
    if not url:
        sys.exit("SHEET_CSV_URL secret is missing.")
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig"))))
    todo = []
    for row in rows:
        if row.get("status", "").strip().upper() != "APPROVED":
            continue
        ok, why = loop_ok(row)
        if not ok:
            print("SKIP %s: %s" % (row.get("video_id"), why))
            continue
        todo.append(row)
    print("%d FACT-CHECKED rows with loop_pass TRUE to render (max %d)" % (len(todo), MAX_VIDEOS))
    for row in todo[:MAX_VIDEOS]:
        print("Rendering", row["video_id"], "-", row["topic"])
        try:
            final, wd, dur = render(row)
            (OUT / (final.stem + "_caption.txt")).write_text(
                (row.get("caption", "") + "\n" + row.get("hashtag", "")).strip() + "\n", encoding="utf-8")
            print("  done:", final)
        except Exception as e:
            print("  FAILED:", str(e)[:600])


if __name__ == "__main__":
    main()
