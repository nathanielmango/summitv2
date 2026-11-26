# Summit

A Windows desktop app that takes a YouTube link and gives you a translated package:
the video, a translated thumbnail, a translated transcript (`.srt` and `.txt`), and
optionally a dubbed video in a cloned voice.

**Target languages:** Czech, German, Polish, Spanish.

## What it makes

Everything is saved in `<Save to>/<video title>/`:

| File | What it is |
| --- | --- |
| `<title>.mp4` | The video, 720p (or the best lower quality available) |
| `thumbnail_<language>.jpg` | The translated thumbnail (the original is removed) |
| `thumbnail_prompt_<language>.txt` | The prompt used to generate the new thumbnail |
| `transcript_<code>.srt` | Translated subtitles with timings |
| `transcript_<code>.txt` | Translated transcript as plain text |
| `<title>_<code>_dubbed.mp4` | The video with a dubbed voice track (only when **Dub Video** is on; the plain video is deleted once the dub succeeds) |

`<code>` is `cs`, `de`, `pl` or `es`.

## Install (easiest)

Download `SummitSetup.exe` from the **Releases** page and run it. No admin rights needed.
It installs for the current user and creates a desktop shortcut if you tick the box.

## Run from source

1. Install Python 3.9 or newer.
2. Install the packages:

       pip install -r requirements.txt

3. Install **ffmpeg** and make sure it is on your PATH (or put `ffmpeg.exe` next to `app.py`):
   - Windows: `winget install ffmpeg`
   - macOS: `brew install ffmpeg`
   - Linux: `sudo apt install ffmpeg`
4. Start the app:

       python app.py

Paste a YouTube link anywhere in the window, pick a language, choose what to include, press **Start**.

## Gemini API key (recommended)

Open the settings (gear icon) and paste a Gemini API key. A free key from Google AI Studio works.
Tick **Remember** to save it in `gemini_key.txt` next to the app, or set the `GEMINI_API_KEY`
environment variable.

With a key, Summit:

- translates the transcript in a natural spoken style (numbers, units and foreign names written
  the way they are pronounced, YouTube plugs removed), using your language-specific prompt;
- creates the translated thumbnail. If image generation is not available for your key, it
  translates the text on the original thumbnail and repaints it instead.

Without a key, the transcript is translated with Google Translate and the thumbnail falls back to
OCR (needs [Tesseract](https://github.com/UB-Mannheim/tesseract/wiki) installed). The result is
more literal and less polished.

## Dubbing (optional)

Dubbing uses the free **XTTS v2** voice-cloning model and needs two extra things:

1. **A voice sample** named `tomas_Nikl.mp3` in the app folder (next to `app.py` or `Summit.exe`).
   It is not included in this repository.
2. **A separate Python environment with the `TTS` package installed** (Coqui XTTS). Use Python
   3.9 to 3.11 for the original `TTS` package. In the settings, set **XTTS Python** to that
   environment's `python.exe`, for example `D:\xtts-env\Scripts\python.exe`.

The first dub downloads about 1.8 GB of model files into `xtts_data` next to the app. A GPU makes
it much faster, but it also runs on CPU.

## Build the installer

Run `build_windows.bat` in the folder that holds `app.py`. It builds `dist\Summit\Summit.exe` and,
if [Inno Setup 6](https://jrsoftware.org/isinfo.php) is installed, compiles `Summit.iss` into
`Output\SummitSetup.exe`.

Put these next to `app.py` first: `summit.ico`, `summit.png`, `xtts_worker.py`, and optionally
`tomas_Nikl.mp3`, `ffmpeg.exe` and `aria2c.exe` (aria2c makes video downloads faster).

The XTTS environment is not bundled. Each computer that wants dubbing needs its own.

## Notes

- YouTube changes often. If downloads fail, run `pip install -U yt-dlp youtube-transcript-api`.
- The app writes its settings, saved key, log (`last_run_log.txt`) and downloads next to itself.
- Never share `gemini_key.txt` or commit it. It is listed in `.gitignore`.
