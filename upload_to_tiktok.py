#!/usr/bin/env python3
"""Send freshly rendered videos to your TikTok drafts (inbox) using the Content Posting API.

Uses only the Python standard library, so the workflow needs no pip install.

Environment variables (set as GitHub secrets):
  TIKTOK_TOKEN_URL     the Apps Script web app URL that hands out a fresh access token
  TIKTOK_TOKEN_SECRET  the shared secret that web app checks (TT_WORKFLOW_SECRET)

Usage:
  python upload_to_tiktok.py [folder] [--max-age-hours 3] [--dry-run]

A video is sent only if it is a recently written .mp4 with a caption .txt file next to it
(same name start, "caption" in the file name). Repo files like stock clips are skipped.
TikTok's draft upload cannot carry a caption, so the caption is printed in the log and in
the run summary for you to paste when you finish the post in the TikTok app.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = os.environ.get("TIKTOK_API_BASE", "https://open.tiktokapis.com")
TOKEN_URL = os.environ.get("TIKTOK_TOKEN_URL", "")
TOKEN_SECRET = os.environ.get("TIKTOK_TOKEN_SECRET", "")
POLL_SECONDS = int(os.environ.get("TIKTOK_POLL_SECONDS", "12"))   # TikTok allows 6 requests/min per token
POLL_TRIES = int(os.environ.get("TIKTOK_POLL_TRIES", "5"))
PAUSE_BETWEEN_VIDEOS = int(os.environ.get("TIKTOK_PAUSE_SECONDS", "20"))


def http(method, url, headers=None, data=None, timeout=300):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def parse(raw):
    try:
        return json.loads(raw)
    except Exception:
        return {}


def get_token():
    if not TOKEN_URL or not TOKEN_SECRET:
        raise RuntimeError("TIKTOK_TOKEN_URL and TIKTOK_TOKEN_SECRET must be set as GitHub secrets")
    sep = "&" if "?" in TOKEN_URL else "?"
    url = TOKEN_URL + sep + urllib.parse.urlencode({"action": "tiktok_token", "secret": TOKEN_SECRET})
    status, raw = http("GET", url, timeout=60)  # the URL holds the secret, so never print it
    data = parse(raw)
    if not data:
        raise RuntimeError(
            "token service sent a non-JSON reply (HTTP %s). Check that the Apps Script web app is "
            "deployed as a NEW VERSION and set to 'Anyone'." % status)
    if "access_token" not in data:
        raise RuntimeError("token service said: %s" % data.get("error", "unknown error"))
    return data["access_token"]


def api_headers(token):
    return {"Authorization": "Bearer " + token, "Content-Type": "application/json; charset=UTF-8"}


def find_videos(root, max_age_seconds):
    now = time.time()
    found, skipped = [], []
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules")]
        for name in files:
            if not name.lower().endswith(".mp4"):
                continue
            path = os.path.join(folder, name)
            if now - os.path.getmtime(path) > max_age_seconds:
                continue
            stem = name[:-4]
            captions = sorted(
                c for c in os.listdir(folder)
                if c.startswith(stem) and "caption" in c.lower() and c.lower().endswith(".txt"))
            if captions:
                found.append((path, os.path.join(folder, captions[0])))
            else:
                skipped.append(path)
    return sorted(found), sorted(skipped)


def upload_video(path, token):
    size = os.path.getsize(path)
    if size > 64 * 1024 * 1024:
        print("  warning: file is %.0f MB; TikTok may require chunked upload for very large files" % (size / 1e6))
    body = {"source_info": {"source": "FILE_UPLOAD", "video_size": size,
                            "chunk_size": size, "total_chunk_count": 1}}
    status, raw = http("POST", API_BASE + "/v2/post/publish/inbox/video/init/",
                       api_headers(token), json.dumps(body).encode())
    data = parse(raw)
    err = data.get("error") or {}
    if status != 200 or err.get("code") not in (None, "ok"):
        raise RuntimeError("init failed (HTTP %s): %s %s" % (status, err.get("code"), err.get("message")))
    publish_id = data["data"]["publish_id"]
    upload_url = data["data"]["upload_url"]  # contains a token, never print it

    with open(path, "rb") as f:
        blob = f.read()
    status, raw = http("PUT", upload_url,
                       {"Content-Type": "video/mp4",
                        "Content-Range": "bytes 0-%d/%d" % (size - 1, size)}, blob)
    if status not in (200, 201):
        raise RuntimeError("file transfer failed (HTTP %s): %s" % (status, raw[:200]))
    return publish_id


def wait_for_status(publish_id, token):
    last = "unknown"
    for _ in range(POLL_TRIES):
        time.sleep(POLL_SECONDS)
        status, raw = http("POST", API_BASE + "/v2/post/publish/status/fetch/",
                           api_headers(token), json.dumps({"publish_id": publish_id}).encode(), timeout=60)
        info = parse(raw).get("data") or {}
        state = info.get("status")
        if state:
            last = state
        if state in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
            return state
        if state == "FAILED":
            raise RuntimeError("TikTok reported FAILED: %s" % info.get("fail_reason"))
    return last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--max-age-hours", type=float, default=3.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    videos, skipped = find_videos(args.root, args.max_age_hours * 3600)
    for p in skipped:
        print("skipped (no caption file next to it): %s" % p)
    if not videos:
        print("No new videos with captions found under %s." % args.root)
        return 0

    summary = ["## TikTok drafts", ""]
    failures = 0
    token = None
    for i, (video, cap_path) in enumerate(videos):
        with open(cap_path, encoding="utf-8") as f:
            caption = f.read().strip()
        print("\n%s" % video)
        print("  caption: %s" % caption)
        if args.dry_run:
            print("  dry run: would upload")
            continue
        try:
            if token is None:
                token = get_token()
            if i > 0:
                time.sleep(PAUSE_BETWEEN_VIDEOS)
            publish_id = upload_video(video, token)
            state = wait_for_status(publish_id, token)
            print("  uploaded, TikTok status: %s" % state)
            summary += ["**%s** - status `%s`. Caption to paste in TikTok:" % (os.path.basename(video), state),
                        "", "```", caption, "```", ""]
        except Exception as e:
            failures += 1
            print("  FAILED: %s" % e)
            summary += ["**%s** - FAILED: %s" % (os.path.basename(video), e), ""]

    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path and not args.dry_run:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(summary) + "\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
