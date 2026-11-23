name: Make paradox videos

on:
  workflow_dispatch:          # lets you click "Run workflow" manually
  schedule:
    - cron: "0 14 * * *"      # daily at 14:00 UTC; delete these two lines to run manually only

jobs:
  render:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install ffmpeg, fonts, Python packages
        run: |
          sudo apt-get update -y
          sudo apt-get install -y ffmpeg fonts-dejavu-core
          pip install edge-tts requests pillow

      - name: Render approved scripts
        run: python make_videos.py
        env:
          SHEET_CSV_URL: ${{ secrets.SHEET_CSV_URL }}
          POLLINATIONS_TOKEN: ${{ secrets.POLLINATIONS_TOKEN }}
          VOICE: en-US-AndrewNeural
          MAX_VIDEOS: "3"

      - name: Save videos
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: videos
          path: output/
          if-no-files-found: ignore
          retention-days: 7
