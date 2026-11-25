# YouTube Downloader & Translator

Downloads a YouTube video (720p), its thumbnail, and its transcript translated
into a language you choose. It can also dub the video with a cloned voice.

## Setup

1. Install Python 3.9+
2. Install the packages:

       pip install -r requirements.txt

3. Install **ffmpeg** (needed to merge video + audio) and make sure it is on your PATH.
   - Windows: `winget install ffmpeg`
   - macOS: `brew install ffmpeg`
   - Linux: `sudo apt install ffmpeg`

## Run

    python app.py

Paste the URL, pick the target language, then press Start.
Everything is saved in `<Save to>/<video title>/`:

- `<title>.mp4`
- `thumbnail.jpg` (the original thumbnail, not translated)
- `transcript_<lang>.srt`, `transcript_<lang>.txt`, `transcript_original_<src>.txt`

## Notes

- The video is always downloaded in 720p (or the best lower quality if 720p is not available).
- Translation uses Google Translate through `deep-translator` (no API key needed).
- YouTube changes often; if downloads fail, run `pip install -U yt-dlp youtube-transcript-api`.
