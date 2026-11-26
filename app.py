"""
Summit — YouTube Downloader, Translator & Dubber
- Downloads a YouTube video (720p mp4) and its original thumbnail
- Downloads the transcript and translates it (.srt and .txt)
- Optionally dubs the video with your cloned XTTS voice

Run:  python app.py
"""

import base64
import ctypes
import io
import json
import math
import os
import queue
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import wave
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog, messagebox, ttk
from urllib.parse import quote, urlencode

# The heavy libraries are NOT imported here. They are loaded by preload() while the splash
# screen is showing, so the window opens instantly and the first video starts faster.
yt_dlp = GoogleTranslator = YouTubeTranscriptApi = None
Image = ImageDraw = ImageFilter = ImageFont = ImageOps = None

IS_WIN = os.name == "nt"
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN else {}

if IS_WIN:  # own taskbar identity + sharp High-DPI rendering
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Summit.App")
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
LANGUAGES = {"Czech": "cs", "German": "de", "Polish": "pl", "Spanish": "es"}
LANG_NAMES = {v: k for k, v in LANGUAGES.items()}

FROZEN = getattr(sys, "frozen", False)  # True when running as the packaged .exe
APP_DIR = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
os.environ["PATH"] = APP_DIR + os.pathsep + os.environ.get("PATH", "")  # ffmpeg.exe next to the app is found
ICON_ICO = os.path.join(APP_DIR, "summit.ico")
ICON_PNG = os.path.join(APP_DIR, "summit.png")
LOG_FILE = os.path.join(APP_DIR, "last_run_log.txt")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
KEY_FILE = os.path.join(APP_DIR, "gemini_key.txt")
PREVIEW_W, PREVIEW_H = 224, 126

# share of the single progress bar each step gets (unpicked steps are left out)
STAGE_WEIGHTS = {"video": 28, "thumb": 12, "trans": 10, "dub": 50}
STAGE_LABELS = {"video": "Downloading video", "thumb": "Translating thumbnail",
                "trans": "Translating transcript", "dub": "Dubbing video"}
VIDEO_HEIGHT = 720
DUB_RATE = 24000

# voice cloning with the free XTTS v2 model (runs in a separate process)
XTTS_LANGS = {"ar", "zh-cn", "cs", "nl", "en", "fr", "de", "hi", "hu",
              "it", "ja", "ko", "pl", "pt", "ru", "es", "tr"}  # no Tagalog
XTTS_WORKER = os.path.join(APP_DIR, "xtts_worker.py")
XTTS_HOME = os.path.join(APP_DIR, "xtts_data")  # model files go here (not on C:)
VOICE_FILE = os.path.join(APP_DIR, "tomas_Nikl.mp3")  # the ONE voice used for every dub

# Default + automatic fallback models; newer ones are only used if picked by hand.
GEMINI_DEFAULT_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
GEMINI_MODELS = GEMINI_DEFAULT_MODELS + ["gemini-3.6-flash", "gemini-3.8-flash",
                                         "gemini-4-hydrogen"]

THEME = {
    "bg": "#FFFFFF", "titlebar": "#FFF9F5", "card": "#FFF5EE", "card_alt": "#FFEBE0",
    "input_bg": "#FFFFFF", "border": "#F3C8B2", "border_subtle": "#F7DFD2",
    "accent": "#F4845F", "accent_hover": "#E66D45", "accent_press": "#D45B34",
    "accent_soft": "#FFE6D8", "accent_text": "#C8522B", "text": "#2B211E",
    "muted": "#7D6860", "placeholder": "#B9A39B", "white": "#FFFFFF",
    "success": "#2E8B57", "danger": "#C94A38",
}
UI_FONT = "Segoe UI" if IS_WIN else ("SF Pro Display" if sys.platform == "darwin" else "DejaVu Sans")
MONO_FONT = "Consolas" if IS_WIN else ("Menlo" if sys.platform == "darwin" else "DejaVu Sans Mono")


def default_xtts_python():
    guess = r"D:\xtts-env\Scripts\python.exe"
    return guess if os.path.isfile(guess) else ("" if FROZEN else sys.executable)


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Windows notification (toast, bottom-right corner)
# --------------------------------------------------------------------------- #
TOAST_APP_ID = "Summit.App"
_TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml($env:SUMMIT_TOAST_XML)
$n = New-Object Windows.UI.Notifications.ToastNotification $x
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:SUMMIT_TOAST_ID).Show($n)
"""


def _register_toast_app():
    """Tell Windows the notification comes from 'Summit' (name + icon)."""
    try:
        import winreg
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Classes\AppUserModelId" + "\\" + TOAST_APP_ID)
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "Summit")
        if os.path.isfile(ICON_PNG):
            winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, ICON_PNG)
        winreg.CloseKey(key)
    except Exception:
        pass


def windows_toast(title, line1="", line2="", open_path=None):
    """Show a Windows 10/11 toast; clicking opens `open_path`. False if it couldn't start."""
    if not IS_WIN:
        return False
    try:
        import base64
        from pathlib import Path
        from xml.sax.saxutils import escape, quoteattr

        _register_toast_app()
        launch = ""
        if open_path and os.path.exists(open_path):
            launch = (' activationType="protocol" launch='
                      + quoteattr(Path(open_path).resolve().as_uri()))
        img = ""
        if os.path.isfile(ICON_PNG):
            img = ('<image placement="appLogoOverride" src='
                   + quoteattr(Path(ICON_PNG).resolve().as_uri()) + "/>")
        texts = "".join(f"<text>{escape(t)}</text>" for t in (title, line1, line2) if t)
        xml = (f'<toast{launch}><visual><binding template="ToastGeneric">{img}{texts}'
               f'</binding></visual>'
               f'<audio src="ms-winsoundevent:Notification.Default"/></toast>')
        encoded = base64.b64encode(_TOAST_PS.encode("utf-16-le")).decode("ascii")
        env = dict(os.environ, SUMMIT_TOAST_XML=xml, SUMMIT_TOAST_ID=TOAST_APP_ID)
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **NO_WINDOW)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Plain translation (Google Translate and friends)
# --------------------------------------------------------------------------- #
class MultiTranslator:
    """Tries several free translation routes and remembers the one that works."""

    def __init__(self, target, source="auto"):
        self.target, self.source = target, source
        self.backends = [self._gtx, self._google_web, self._mymemory]
        self.good = 0

    def translate(self, text):
        last = None
        for k in range(len(self.backends)):
            idx = (self.good + k) % len(self.backends)
            try:
                res = self.backends[idx](text)
                if res and res.strip():
                    self.good = idx
                    return res
            except Exception as e:
                last = e
        raise last or RuntimeError("translation failed")

    def _gtx(self, text):
        query = urlencode({"client": "gtx", "sl": self.source, "tl": self.target, "dt": "t"})
        req = urllib.request.Request(
            "https://translate.googleapis.com/translate_a/single?" + query,
            data=urlencode({"q": text}).encode("utf-8"),
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            js = json.loads(r.read().decode("utf-8"))
        return "".join(p[0] for p in js[0] if p and p[0])

    def _google_web(self, text):
        return GoogleTranslator(source=self.source, target=self.target).translate(text)

    def _mymemory(self, text):
        pair = f"{'Autodetect' if self.source == 'auto' else self.source}|{self.target}"
        out = []
        for line in text.split("\n"):
            if not line.strip():
                out.append(line)
                continue
            url = ("https://api.mymemory.translated.net/get?"
                   + urlencode({"q": line[:450], "langpair": pair}))
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                out.append(json.loads(r.read().decode("utf-8"))["responseData"]["translatedText"])
        return "\n".join(out)


def _translate_with_retry(translator, text, tries=5, log=None):
    """Translate one string; back off and retry if the service rate-limits us."""
    for attempt in range(tries):
        try:
            return translator.translate(text) or text
        except Exception as e:
            if attempt == tries - 1:
                raise
            wait = min(60, 3 * 2 ** attempt)
            if log:
                log(f"  translation error ({type(e).__name__}); retrying in {wait}s...")
            time.sleep(wait)
    return text


def _enc_len(s):
    """Length of a line once URL-encoded (the translator sends text in the URL)."""
    return len(quote(s)) + 3


def translate_list(texts, target, log=None, max_chars=3000, progress=None, workers=2):
    """Translate a list of strings, keeping order and length (batched, a few at a time)."""
    translator = MultiTranslator(target)
    out = list(texts)
    total = sum(1 for t in texts if t.strip())

    batches, cur, size = [], [], 0
    for i, t in enumerate(texts):
        if not t.strip():
            continue
        if cur and size + _enc_len(t) > max_chars:
            batches.append(cur)
            cur, size = [], 0
        cur.append((i, t))
        size += _enc_len(t)
    if cur:
        batches.append(cur)

    state, lock = {"done": 0}, threading.Lock()

    def do_batch(batch):
        lines = _translate_with_retry(translator, "\n".join(t for _, t in batch),
                                      log=log).split("\n")
        if len(lines) == len(batch):
            for (i, _), line in zip(batch, lines):
                out[i] = line.strip() or texts[i]
        else:  # line count changed: redo this block line by line
            for i, t in batch:
                out[i] = _translate_with_retry(translator, t, log=log)
                time.sleep(0.2)
        with lock:
            state["done"] += len(batch)
            done = state["done"]
        if log:
            log(f"  translated {done}/{total}")
        if progress:
            progress(done / max(1, total) * 100)
        time.sleep(0.2)

    if len(batches) <= 1:
        for bt in batches:
            do_batch(bt)
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(batches))) as ex:
            list(ex.map(do_batch, batches))
    return out


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def fmt_srt_time(seconds):
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def looks_like_youtube(text):
    return bool(re.search(r"(youtube\.com/|youtu\.be/)", text or "", re.I))


def fmt_duration(seconds):
    try:
        t = int(seconds)
    except (TypeError, ValueError):
        return ""
    h, rem = divmod(t, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02}:{sec:02}" if h else f"{m}:{sec:02}"


def run_ffmpeg(args):
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error"] + args,
                       capture_output=True, text=True, **NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "ffmpeg failed")


# --------------------------------------------------------------------------- #
# Video + thumbnail
# --------------------------------------------------------------------------- #
def download_video(url, height, folder, log, progress):
    fmt = (f"bestvideo[height={height}][ext=mp4]+bestaudio[ext=m4a]/"
           f"bestvideo[height={height}]+bestaudio/"
           f"bestvideo[height<={height}]+bestaudio/best[height<={height}]")
    streams = {"done": 0}  # video stream first (0-85%), then the small audio stream (85-100%)

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                pct = d["downloaded_bytes"] / total * 100
                progress(pct * 0.85 if streams["done"] == 0 else 85 + pct * 0.15)
        elif d["status"] == "finished":
            streams["done"] += 1
            progress(85 if streams["done"] == 1 else 100)

    opts = {
        "format": fmt, "merge_output_format": "mp4",
        "outtmpl": os.path.join(folder, "%(title)s.%(ext)s"),
        "progress_hooks": [hook], "quiet": True, "no_warnings": True, "noplaylist": True,
        "http_chunk_size": 10 * 1024 * 1024, "concurrent_fragment_downloads": 16,
        "retries": 10, "fragment_retries": 10,
    }
    log(f"Downloading video ({height}p preferred)...")

    def run(options):
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            rd = info.get("requested_downloads")
            path = rd[0].get("filepath") if rd else None
            if not path or not os.path.exists(path):
                path = os.path.splitext(ydl.prepare_filename(info))[0] + ".mp4"
        return info, path

    # aria2c opens many connections at once, which is the biggest speed-up on a fast
    # line. Used automatically when aria2c.exe sits next to app.py (or is on PATH).
    if shutil.which("aria2c"):
        fast = dict(opts)
        fast["external_downloader"] = {"default": "aria2c"}
        fast["external_downloader_args"] = {"aria2c": [
            "-x16", "-s16", "-k1M", "--file-allocation=none", "--summary-interval=0",
            "--console-log-level=error", "--download-result=hide"]}
        fast.pop("http_chunk_size")  # aria2c splits the file itself
        try:
            log("Using aria2c: 16 connections, no speed limit.")
            info, path = run(fast)
        except Exception as e:
            log(f"aria2c download failed ({e}); using the normal downloader instead.")
            info, path = run(opts)
    else:
        info, path = run(opts)
    progress(100)
    log(f"Video saved ({info.get('height')}p actual).")
    return path


def thumbnail_path(folder, code, suffix=".jpg", prefix="thumbnail"):
    """e.g. thumbnail_czech.jpg / thumbnail_prompt_polish.txt (language name, not 'cs')."""
    return os.path.join(folder, f"{prefix}_{LANG_NAMES.get(code, code).lower()}{suffix}")


def download_thumbnail(info, folder, log):
    path = os.path.join(folder, "_thumbnail_original.jpg")  # temporary: removed after use
    for u in (f"https://i.ytimg.com/vi/{info['id']}/maxresdefault.jpg", info.get("thumbnail")):
        if not u:
            continue
        try:
            urllib.request.urlretrieve(u, path)
            log("Thumbnail downloaded.")
            return path
        except Exception:
            continue
    raise RuntimeError("Could not download thumbnail.")


# --------------------------------------------------------------------------- #
# Free fallback: read the text with OCR, translate it, paint it back on the thumbnail
# --------------------------------------------------------------------------- #
FONT_CANDIDATES = [
    "ariblk.ttf", "C:/Windows/Fonts/ariblk.ttf",  # Arial Black: closest to thumbnail fonts
    "DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def load_font(size):
    for name in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _boxes_from_data(data, scale):
    lines = {}
    for i, txt in enumerate(data["text"]):
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1
        if not str(txt).strip() or conf < 60:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(i)

    boxes = []
    for idxs in lines.values():
        text = " ".join(str(data["text"][i]).strip() for i in idxs)
        # drop junk: need several real letters, mostly letters, high confidence
        letters = len(re.findall(r"[^\W\d_]", text))
        compact = len(text.replace(" ", ""))
        avg_conf = sum(float(data["conf"][i]) for i in idxs) / len(idxs)
        if letters < 4 or letters / max(1, compact) < 0.7 or avg_conf < 70:
            continue
        l = min(data["left"][i] for i in idxs) // scale
        t = min(data["top"][i] for i in idxs) // scale
        r = max(data["left"][i] + data["width"][i] for i in idxs) // scale
        b = max(data["top"][i] + data["height"][i] for i in idxs) // scale
        boxes.append((l, t, r, b, text))
    return boxes


def find_text_boxes(img, ocr_lang):
    """Try several image variants / OCR modes and keep the best result."""
    import pytesseract  # imported here so the app still runs without it

    W, H = img.size
    scale = 2 if W < 1800 else 1
    big = img.resize((W * scale, H * scale), Image.LANCZOS) if scale > 1 else img
    gray = ImageOps.autocontrast(ImageOps.grayscale(big))
    variants = [big, gray, ImageOps.invert(gray)]

    best, best_score = [], 0
    for v in variants:
        for psm in (11, 3):
            data = pytesseract.image_to_data(
                v, lang=ocr_lang, config=f"--psm {psm}",
                output_type=pytesseract.Output.DICT,
            )
            boxes = _boxes_from_data(data, scale)
            score = sum(len(re.sub(r"\W", "", b[4])) for b in boxes)
            if score > best_score:
                best, best_score = boxes, score
    return best


def _hex_rgb(h):
    try:
        h = str(h).strip().lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, TypeError):
        return None


def _lum(c):
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def _estimate_text_color(crop):
    """Colour of the letters: the pixels that differ most from the box edge colour."""
    w, h = crop.size
    px = crop.load()
    edge = [px[x, 0] for x in range(w)] + [px[x, h - 1] for x in range(w)] \
        + [px[0, y] for y in range(h)] + [px[w - 1, y] for y in range(h)]
    bg = tuple(int(statistics.median(c[k] for c in edge)) for k in range(3))
    pix = sorted((sum((p[k] - bg[k]) ** 2 for k in range(3)), p)
                 for p in (px[x, y] for y in range(0, h, 2) for x in range(0, w, 2)))
    top = [p for _, p in pix[-max(1, len(pix) // 10):]]
    return tuple(int(sum(p[k] for p in top) / len(top)) for k in range(3))


def _fill_masked(crop, mask):
    """Paint the masked pixels from their surroundings (no extra libraries needed)."""
    w, h = crop.size
    px, mp = crop.load(), mask.load()

    def runs(length, get_mask, get_px):
        res, i = {}, 0
        while i < length:
            if get_mask(i):
                i0 = i
                while i < length and get_mask(i):
                    i += 1
                a = get_px(i0 - 1) if i0 > 0 else None
                z = get_px(i) if i < length else None
                for k in range(i0, i):
                    if a and z:
                        f = (k - i0 + 1) / (i - i0 + 1)
                        res[k] = tuple(int(a[c] * (1 - f) + z[c] * f) for c in range(3))
                    else:
                        res[k] = a or z
            else:
                i += 1
        return res

    hor = {}
    for y in range(h):
        for x, c in runs(w, lambda i: mp[i, y], lambda i: px[i, y]).items():
            hor[(x, y)] = c
    ver = {}
    for x in range(w):
        for y, c in runs(h, lambda i: mp[x, i], lambda i: px[x, i]).items():
            ver[(x, y)] = c
    out = crop.copy()
    op = out.load()
    for key in set(hor) | set(ver):
        a, b = hor.get(key), ver.get(key)
        if a and b:
            op[key] = tuple((a[c] + b[c]) // 2 for c in range(3))
        elif a or b:
            op[key] = a or b
    return out


def _erase_text(img, box, color):
    """Remove the letters inside `box` and rebuild the background behind them."""
    W, H = img.size
    l, t, r, b = box
    pad = max(4, (b - t) // 8)
    L, T, R, B = max(0, l - pad), max(0, t - pad), min(W, r + pad), min(H, b + pad)
    if R - L < 4 or B - T < 4:
        return color or (255, 255, 255)
    crop = img.crop((L, T, R, B))
    color = color or _estimate_text_color(crop)
    w, h = crop.size
    px = crop.load()
    mask = Image.new("L", (w, h), 0)
    mp = mask.load()
    for y in range(h):
        for x in range(w):
            p = px[x, y]
            if (p[0] - color[0]) ** 2 + (p[1] - color[1]) ** 2 + (p[2] - color[2]) ** 2 < 100 ** 2:
                mp[x, y] = 255
    mask = mask.filter(ImageFilter.MaxFilter(7))  # also cover the soft edges of the letters
    covered = 1 - mask.histogram()[0] / (w * h)  # share of pixels that will be repainted
    if covered < 0.85:
        try:  # best result when OpenCV is installed
            import cv2
            import numpy as np
            bgr = cv2.cvtColor(np.array(crop), cv2.COLOR_RGB2BGR)
            res = cv2.inpaint(bgr, np.array(mask), 5, cv2.INPAINT_TELEA)
            fixed = Image.fromarray(cv2.cvtColor(res, cv2.COLOR_BGR2RGB))
        except Exception:
            fixed = _fill_masked(crop, mask)
        img.paste(fixed, (L, T))
    return color


def _draw_fitted(img, box, text, color, upper):
    """Write `text` centred on `box`, as big as fits, with an outline for contrast."""
    draw = ImageDraw.Draw(img)
    l, t, r, b = box
    text = text.upper() if upper else text
    max_w = (r - l) * 1.12
    size = max(12, int((b - t) * 1.4))
    while size > 12:
        bb = draw.textbbox((0, 0), text, font=load_font(size))
        if bb[2] - bb[0] <= max_w:
            break
        size -= 2
    font = load_font(size)
    bb = draw.textbbox((0, 0), text, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    x = (l + r) / 2 - tw / 2 - bb[0]
    y = (t + b) / 2 - th / 2 - bb[1]
    outline = (15, 15, 15) if _lum(color) > 140 else (250, 250, 250)
    draw.text((x, y), text, font=font, fill=color,
              stroke_width=max(2, size // 18), stroke_fill=outline)


def paint_translations(img, items, log):
    """items: [{'box': (l, t, r, b), 'orig': str, 'text': str, 'color': (r, g, b) or None}]"""
    for it in items:
        if not it["text"].strip():
            continue
        color = _erase_text(img, it["box"], it.get("color"))
        letters = [c for c in it.get("orig", "") if c.isalpha()]
        upper = bool(letters) and all(c.isupper() for c in letters)
        _draw_fitted(img, it["box"], it["text"], color, upper)


def translate_thumbnail(src_path, target_code, ocr_lang, out_path, log):
    """Last-resort method: OCR the text, translate it, erase it and draw the translation."""
    img = Image.open(src_path).convert("RGB")
    boxes = find_text_boxes(img, ocr_lang)
    if not boxes:
        raise RuntimeError("no text could be read in the thumbnail")
    log("Detected text: " + " | ".join(b[4] for b in boxes))
    translated = translate_list([b[4] for b in boxes], target_code, log)
    log("Translated:    " + " | ".join(translated))
    paint_translations(img, [{"box": bx[:4], "orig": bx[4], "text": tr, "color": None}
                             for bx, tr in zip(boxes, translated)], log)
    img.save(out_path, quality=95)
    log("Translated thumbnail saved.")


# --------------------------------------------------------------------------- #
# AI thumbnail (Gemini writes the prompt, an image model draws the new thumbnail)
# --------------------------------------------------------------------------- #
# Image models are tried in this order. Image generation is often a paid-only
# feature of the Gemini API; if your key can't use it, the prompt is still saved.
THUMB_IMAGE_MODELS = ["gemini-3.1-flash-lite-image", "gemini-3.1-flash-image"]

THUMB_PROMPT = """You are a YouTube thumbnail designer. Look at the attached thumbnail (the video is titled "{title}").
Create a thumbnail prompt with this format:
Thumbnail Prompt (in English):
Text overlays (in {language})

Rules:
- Under "Thumbnail Prompt (in English):" describe the whole thumbnail in English so an image model can recreate it: 16:9 layout, main subject, people or objects, colours, lighting, background and style.
- Under "Text overlays (in {language}):" write every piece of text that appears in the thumbnail, translated into short, punchy, natural {language} (thumbnail style), one per line, each with its position, colour and font style.
- If the thumbnail has no text, write: Text overlays (in {language}): none.
Output only these two sections."""


def _gemini_generate(key, model, parts, system=None, want_image=False, log=None, tries=3,
                     json_mode=False):
    """One Gemini call that may return text and/or an image. Returns (text, image_bytes)."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {"contents": [{"role": "user", "parts": parts}]}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if want_image:
        body["generationConfig"] = {"responseModalities": ["TEXT", "IMAGE"]}
    elif json_mode:
        body["generationConfig"] = {"temperature": 0.3, "responseMimeType": "application/json"}
    data = json.dumps(body).encode("utf-8")
    for attempt in range(tries):
        last_try = attempt == tries - 1
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                js = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                msg = json.loads(raw)["error"]["message"]
            except Exception:
                msg = raw[:300]
            low = msg.lower()
            if e.code in (400, 403) and any(w in low for w in ("location", "api key", "api_key")):
                raise GeminiError(e.code, msg, fatal=True)
            if e.code in (429, 500, 503) and not last_try and "limit: 0" not in low:
                wait = 5 * (attempt + 1)
                if log:
                    log(f"  Gemini busy/limit ({e.code}); retrying in {wait}s...")
                time.sleep(wait)
                continue
            raise GeminiError(e.code, msg)
        except (urllib.error.URLError, TimeoutError) as e:
            if not last_try:
                time.sleep(3)
                continue
            raise GeminiError(0, f"cannot reach Gemini ({e})", fatal=True)

        texts, image = [], None
        cands = js.get("candidates") or []
        for part in (cands[0].get("content", {}).get("parts", []) if cands else []):
            if part.get("thought"):
                continue
            if "text" in part:
                texts.append(part["text"])
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                image = base64.b64decode(inline["data"])
        if not texts and image is None:
            why = (cands[0].get("finishReason") if cands else None) \
                or (js.get("promptFeedback") or {}).get("blockReason") or "empty answer"
            raise GeminiError(0, f"Gemini returned nothing ({why})")
        return "\n".join(texts).strip(), image


LOCATE_PROMPT = """You localise YouTube thumbnails into {language}.
Find every piece of text that is part of the thumbnail artwork (ignore small logos and watermarks).
Return a JSON array, one item per visual line of text, in reading order:
- "text": the original text of that line
- "translation": its {language} translation
- "box_2d": [ymin, xmin, ymax, xmax] tightly around the letters, each number 0-1000 (fractions of image height/width)
- "color": the colour of the letters as a hex code like "#FFFFFF"
Translate the thumbnail's text as one message into short, punchy, natural {language} (thumbnail style, keep the meaning), then split that translation over the same lines in reading order so each line is about as long as its original line. Use correct {language} spelling with all accents.
If there is no text, return []. Output only the JSON array."""


def locate_thumbnail_text(orig_path, title, code, gemini, log):
    """Ask Gemini where the text is, what colour it has and how it translates."""
    language = LANG_NAMES.get(code, code)
    img = Image.open(orig_path).convert("RGB")
    W, H = img.size
    with open(orig_path, "rb") as f:
        part = {"inline_data": {"mime_type": "image/jpeg",
                                "data": base64.b64encode(f.read()).decode("ascii")}}
    last = None
    for model in gemini["models"]:
        try:
            txt, _ = _gemini_generate(key=gemini["key"], model=model,
                                      parts=[{"text": f'Video title: "{title}"'}, part],
                                      system=LOCATE_PROMPT.format(language=language),
                                      log=log, json_mode=True)
            data = json.loads(txt.strip().removeprefix("```json").removesuffix("```").strip())
            items = []
            for d in data:
                y0, x0, y1, x1 = (float(v) for v in d["box_2d"])
                box = (int(min(x0, x1) / 1000 * W), int(min(y0, y1) / 1000 * H),
                       int(max(x0, x1) / 1000 * W), int(max(y0, y1) / 1000 * H))
                if box[2] - box[0] < 8 or box[3] - box[1] < 8:
                    continue
                items.append({"box": box, "orig": str(d.get("text", "")),
                              "text": str(d.get("translation", "")).strip(),
                              "color": _hex_rgb(d.get("color"))})
            return items
        except GeminiError as e:
            last = e
            if e.fatal:
                raise
            log(f"  {model} failed ({e}); trying the next model...")
        except (ValueError, KeyError, TypeError) as e:
            last = e
            log(f"  {model} gave an unreadable answer; trying the next model...")
    raise RuntimeError(f"Gemini could not locate the text ({last})")


def redraw_thumbnail(orig_path, title, code, gemini, out_path, log):
    """
    Free way to make the translated thumbnail: Gemini (text model) finds and translates
    the text, the old letters are erased from the picture and the new text is drawn
    in the same place and colour. Falls back to OCR if Gemini can't be used.
    """
    img = Image.open(orig_path).convert("RGB")
    items = None
    if gemini and gemini.get("key"):
        try:
            log("Asking Gemini to find and translate the thumbnail text...")
            items = locate_thumbnail_text(orig_path, title, code, gemini, log)
        except Exception as e:
            log(f"  Gemini couldn't read the thumbnail ({e}); using OCR instead.")
    if items is None:
        translate_thumbnail(orig_path, code, "eng", out_path, log)
        return
    if not items:
        raise RuntimeError("no text found in the thumbnail")
    log("Thumbnail text: " + " | ".join(f"{i['orig']} -> {i['text']}" for i in items))
    paint_translations(img, items, log)
    img.save(out_path, quality=95)
    log("Translated thumbnail saved.")


def translate_thumbnail_ai(orig_path, title, code, gemini, folder, log, progress=None):
    """
    Gemini looks at the thumbnail and writes the prompt
        Thumbnail Prompt (in English): ... / Text overlays (in <language>): ...
    then an image model draws the new thumbnail. Returns the new .jpg path.
    The prompt is always saved as thumbnail_prompt_<lang>.txt.
    """
    key = (gemini or {}).get("key")
    if not key:
        raise RuntimeError("no Gemini key entered (needed to translate the thumbnail)")
    language = LANG_NAMES.get(code, code)
    with open(orig_path, "rb") as f:
        img_part = {"inline_data": {"mime_type": "image/jpeg",
                                    "data": base64.b64encode(f.read()).decode("ascii")}}

    log(f"Asking Gemini to write the thumbnail prompt ({language})...")
    prompt_text, last = "", None
    for model in gemini["models"]:
        try:
            prompt_text, _ = _gemini_generate(
                key, model, [{"text": "Here is the thumbnail."}, img_part],
                system=THUMB_PROMPT.format(title=title or "", language=language), log=log)
            if prompt_text:
                break
        except GeminiError as e:
            last = e
            if e.fatal:
                raise
            log(f"  {model} failed ({e}); trying the next model...")
    if not prompt_text:
        raise RuntimeError(f"Gemini could not write the thumbnail prompt ({last})")
    prompt_path = thumbnail_path(folder, code, ".txt", "thumbnail_prompt")
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(prompt_text)
    log(f"Thumbnail prompt saved: {os.path.basename(prompt_path)}")
    if progress:
        progress(40)

    instruction = (
        f"Recreate the attached YouTube thumbnail as a new 16:9 image. Keep the same "
        f"composition, subjects, colours and style as the original, but every piece of text "
        f"in the image must be the {language} text overlays below, spelled exactly and "
        f"correctly (keep all accents). No other text.\n\n{prompt_text}")
    log("Generating the new thumbnail image...")
    image, last = None, None
    for model in THUMB_IMAGE_MODELS:
        try:
            _, image = _gemini_generate(key, model, [{"text": instruction}, img_part],
                                        want_image=True, log=log)
            if image:
                break
            last = GeminiError(0, "no image in the answer")
        except GeminiError as e:
            last = e
            if e.fatal:
                raise
            log(f"  {model} failed ({e})")
    if not image:
        raise RuntimeError(
            f"the image model could not draw it ({last}). Image generation may need a paid "
            f"Gemini plan. The prompt was saved - paste it into Gemini with the original "
            f"thumbnail.")
    out = thumbnail_path(folder, code)
    pic = Image.open(io.BytesIO(image)).convert("RGB")
    ImageOps.fit(pic, (1280, 720), Image.LANCZOS).save(out, quality=92)
    log(f"New thumbnail saved: {os.path.basename(out)}")
    if progress:
        progress(100)
    return out


# --------------------------------------------------------------------------- #
# Gemini translation (natural spoken style)
# --------------------------------------------------------------------------- #
STYLE_PROMPTS = {
    "cs": """You are a professional translator and voice-over script writer for Czech audio content.
Translate the transcript lines below into natural, fluent Czech. The result will be read out loud by a Czech text-to-speech voice, so it must sound like a native Czech narrator speaking, not like a written translation.

RULES:
Natural spoken Czech
- Do not translate word by word. Rewrite sentences so they sound natural when spoken in Czech.
- Use a calm, engaging storytelling tone, like a documentary narrator.
- Keep the meaning, facts and order of the original. Do not add or remove information.
Write everything the way it is pronounced in Czech
- Foreign names and words must be written phonetically, the way a Czech person would pronounce them, so the Czech voice reads them correctly.
Examples: Shakespeare → Šejkspír, Jesus → Ježíš, Washington → Vošinktn, George → Džordž, Michael → Majkl, Isaac Newton → Ajzek Ňůtn, Nikola Tesla → Nikola Tesla
- If a name has an established Czech form, use it (Moses → Mojžíš, Egypt → Egypt, John → Jan when it's a biblical name).
- Numbers and years must be written out in Czech words.
Examples: 1945 → devatenáct set čtyřicet pět, 3 000 → tři tisíce, 12th century → dvanácté století
- Write out abbreviations and symbols in full words (e.g. "%" → procent, "km" → kilometrů, "BC" → před naším letopočtem, "Dr." → doktor).
- Convert units to the ones used in Czech (miles → kilometry, feet → metry, Fahrenheit → stupně Celsia).
Clean text only
- No headings, bullet points, asterisks, emojis, timestamps or speaker names.
- Remove filler and YouTube phrases like "subscribe", "like this video", "link in the description", "comment below".
- Use normal punctuation so the voice makes natural pauses. Prefer shorter sentences.""",

    "pl": """You are a professional English-to-Polish translator preparing text for voiceover (read by TTS such as ElevenLabs, or by a voice actor). The result must sound as if it were written by a native Polish speaker, never like a translation.

TRANSLATION
- Do not translate word for word. Translate the meaning, tone and emotion the way a Pole would naturally say it.
- Replace English idioms with Polish equivalents, or rephrase them.
- Use natural word order and everyday spoken Polish, not bookish or machine-like language.
- Split long sentences into shorter ones so they are easy to read aloud without running out of breath.
- Keep the style of the original (suspenseful, narrative, documentary, etc.).
- Pay close attention to correct cases, grammatical gender and verb aspect.
PRONUNCIATION (for voiceover)
- Write foreign names, titles, brands and English words phonetically using Polish spelling, exactly as they are pronounced. For example:
Shakespeare → Szekspir, iPhone → ajfon, YouTube → jutub, Washington → Łoszyngton, George → Dżordż, Michael → Majkel, Netflix → Netfliks, CIA → si-aj-ej, FBI → ef-bi-aj
- If a name has an established Polish form, use it (London → Londyn, Egypt → Egipt, Pharaoh → faraon, Jesus → Jezus).
- Expand abbreviations or write them the way they are read aloud (USA → ju-es-ej or Stany Zjednoczone, NATO → nato, UFO → ufo).
NUMBERS AND SYMBOLS → ALWAYS IN WORDS
- Write every number in words, in the CORRECT case and form for the sentence:
"in 1945" → "w tysiąc dziewięćset czterdziestym piątym roku"
"5 people" → "pięć osób", "22 years" → "dwadzieścia dwa lata"
- Ordinals and Roman numerals in words: XIV century → "czternasty wiek", Louis XVI → "Ludwik Szesnasty"
- Dates: "March 3, 2020" → "trzeciego marca dwa tysiące dwudziestego roku"
- Time: "7:30" → "siódma trzydzieści" or "wpół do ósmej"
- Decimals: "3.5" → "trzy i pół" or "trzy przecinek pięć"
- Symbols and units in words: % → procent, $ → dolarów, € → euro, & → i, + → plus, km → kilometrów, kg → kilogramów, °C → stopni Celsjusza
- Expand Polish abbreviations: np. → na przykład, itd. → i tak dalej, ok. → około, tys. → tysięcy, mln → milionów, mld → miliardów, r. → roku, w. → wieku
- The output must contain NO digits and NO special characters (#, @, /, *, %, $, etc.).
TEXT FORMAT
- Clean Polish text, ready to record. No comments, notes, headings or markdown (no **, #, bullet points).
- Use punctuation to control pauses: comma = short pause, period = longer pause, "…" = dramatic pause. Avoid brackets or quotation marks that cannot be read aloud.
- Remove filler and YouTube phrases like "subscribe", "like this video", "link in the description", "comment below".
Before answering, mentally read the text aloud and check that: 1) it sounds natural in Polish, 2) it contains no digits or symbols, 3) all foreign words are written phonetically.""",

    "de": """Du bist ein professioneller Übersetzer und Sprecher-Texter. Übersetze die folgenden englischen Zeilen ins Deutsche. Der Text wird von einer KI-Stimme (Text-to-Speech) als Hörbuch bzw. Video-Narration vorgelesen.

WICHTIGSTE REGELN:
NATÜRLICHES DEUTSCH
- Übersetze nicht wörtlich. Schreibe so, wie ein deutscher Muttersprachler erzählen würde – flüssig, lebendig, spannend.
- Passe Satzbau, Redewendungen und Bilder an die deutsche Sprache an. Keine englischen Satzkonstruktionen.
- Sprich den Hörer mit "du" an (nicht "Sie"), außer im Original ist es eindeutig formell.
- Ton: wie ein fesselnder Erzähler einer Dokumentation, nicht wie ein Schulbuch.
ZAHLEN IMMER AUSSCHREIBEN
- Alle Zahlen als Wörter: "1650" → "eintausendsechshundertfünfzig", "20 000" → "zwanzigtausend".
- Jahreszahlen so, wie man sie spricht: "1847" → "achtzehnhundertsiebenundvierzig", "2003" → "zweitausenddrei".
- "3000 BC" → "dreitausend vor Christus", "AD 79" → "neunundsiebzig nach Christus".
- Ordinalzahlen ausschreiben: "19th century" → "neunzehnten Jahrhundert", "1st" → "erste".
- Dezimalzahlen: "2.5" → "zweieinhalb" oder "zwei Komma fünf".
- Brüche: "1/3" → "ein Drittel".
ZEICHEN, EINHEITEN UND ABKÜRZUNGEN AUSSCHREIBEN
- "%" → "Prozent", "°C" → "Grad Celsius", "&" → "und", "+" → "plus", "€" → "Euro", "$" → "Dollar".
- Einheiten: "km" → "Kilometer", "m" → "Meter", "kg" → "Kilogramm", "t" → "Tonnen", "mm" → "Millimeter".
- Imperiale Einheiten in metrische umrechnen und ausschreiben: "10 feet" → "etwa drei Meter", "5 miles" → "etwa acht Kilometer", "100 pounds" → "etwa fünfundvierzig Kilogramm".
- Abkürzungen auflösen: "z. B." → "zum Beispiel", "ca." → "circa", "Dr." → "Doktor", "St." → "Sankt", "USA" → "U-S-A" nur wenn nötig, sonst "die Vereinigten Staaten".
- Keine Klammern, Schrägstriche, Bindestrich-Aufzählungen oder Sonderzeichen, die eine Stimme nicht vorlesen kann. Formuliere sie in ganze Sätze um.
AUSSPRACHE FÜR FREMDE NAMEN
- Englische und andere fremde Namen, die eine deutsche KI-Stimme falsch aussprechen würde, phonetisch auf Deutsch schreiben: "John" → "Dschon", "Washington" → "Woschington", "Jamestown" → "Dschejmstaun", "Smithsonian" → "Smissounien".
- Bekannte Namen, die im Deutschen eine eigene Form haben, in der deutschen Form verwenden: "Egypt" → "Ägypten", "Stonehenge" bleibt "Stonehenge" (in Deutschland bekannt), "Cairo" → "Kairo".
- Im Zweifel: Schreibe es so, wie ein Deutscher es aussprechen würde, damit die Stimme es richtig liest.
WEBSEITEN
- Domains so schreiben, wie sie gesprochen werden: "steinelugennicht.com" → "steine lügen nicht punkt com".
FORMAT
- Keine Erklärungen, keine Anmerkungen, keine Überschriften, keine Regieanweisungen, keine Aufzählungszeichen.
- Entferne Füllwörter und YouTube-Floskeln wie "abonniere den Kanal", "gefällt dir das Video", "Link in der Beschreibung", "schreib einen Kommentar".
- Alle anderen Fakten, Zahlen und Namen müssen erhalten bleiben.""",

    "es": """You are a professional translator and voice-over script writer for Spanish audio content.
Translate the transcript lines below into natural, fluent Spanish. The result will be read out loud by a Spanish text-to-speech voice, so it must sound like a native Spanish narrator speaking, not like a written translation. Use neutral Spanish that works for all Spanish-speaking audiences, avoiding regional slang.

RULES:
Natural spoken Spanish
- Do not translate word by word. Rewrite sentences so they sound natural when spoken in Spanish.
- Use a calm, engaging storytelling tone, like a documentary narrator.
- Keep the meaning, facts and order of the original. Do not add or remove information.
- Replace English idioms with natural Spanish equivalents.
Write everything the way it is pronounced in Spanish
- Foreign names and words must be written phonetically, the way a Spanish speaker would pronounce them, so the Spanish voice reads them correctly.
Examples: Shakespeare → Séspir, Washington → Guóshington, George → Yorch, Michael → Máikel, Isaac Newton → Áisac Niúton, Nikola Tesla → Nikola Tesla
- If a name has an established Spanish form, use it (Moses → Moisés, Egypt → Egipto, Jesus → Jesús, John → Juan when it's a biblical name, London → Londres).
- Numbers and years must be written out in Spanish words.
Examples: 1945 → mil novecientos cuarenta y cinco, 3,000 → tres mil, 12th century → siglo doce
- Write out abbreviations and symbols in full words (e.g. "%" → por ciento, "km" → kilómetros, "BC" → antes de Cristo, "AD" → después de Cristo, "Dr." → doctor, "$" → dólares, "&" → y).
- Convert units to the metric units used in Spanish (miles → kilómetros, feet → metros, pounds → kilogramos, Fahrenheit → grados Celsius).
Clean text only
- No headings, bullet points, asterisks, emojis, timestamps or speaker names.
- Remove filler and YouTube phrases like "subscribe", "like this video", "link in the description", "comment below".
- Use normal punctuation so the voice makes natural pauses. Prefer shorter sentences.
- No digits, brackets, slashes or special characters that a voice cannot read aloud.""",
}

# Added to every language prompt: how the app sends lines and wants them back.
PROMPT_IO = """

HOW THE TEXT IS SENT (this overrides any instruction above about output format):
- Input: a JSON array of N consecutive transcript lines from one video titled "{title}" (the title is context only, do not translate it).
- Output: a JSON array of exactly N strings, the translation of each line in the same order. Do not merge or split lines. Output only the JSON array, no notes or explanations.
- Translate every line, never summarize or shorten. If a whole line is only a YouTube phrase (subscribe, like, link below), return an empty string for that line.
- Keep each translated line about as long, when spoken, as its original line, so it fits the video's timing.
- Use the same spelling for the same name in every line."""


def build_style_prompt(code, title):
    base = STYLE_PROMPTS.get(code) or (
        "You are a professional translator. Translate the transcript lines into natural, "
        f"fluent, spoken {LANG_NAMES.get(code, code)}. Spell out numbers and symbols as words.")
    return (base + PROMPT_IO).replace("{title}", title or "")


class GeminiError(Exception):
    def __init__(self, status, message, fatal=False):
        super().__init__(message)
        self.status, self.fatal = status, fatal


def load_saved_key():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        try:
            with open(KEY_FILE, encoding="utf-8") as f:
                key = f.read().strip()
        except OSError:
            pass
    return key


def _gemini_call(key, model, prompt, lines, log=None, tries=4):
    """Ask Gemini to translate a list of lines; returns a list of strings."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": prompt}]},
        "contents": [{"role": "user",
                      "parts": [{"text": json.dumps(lines, ensure_ascii=False)}]}],
        "generationConfig": {
            "temperature": 0.3,
            "responseMimeType": "application/json",
            "responseSchema": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
    }
    data = json.dumps(body).encode("utf-8")
    for attempt in range(tries):
        last_try = attempt == tries - 1
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                js = json.loads(r.read().decode("utf-8"))
            return json.loads(js["candidates"][0]["content"]["parts"][0]["text"])
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                msg = json.loads(raw)["error"]["message"]
            except Exception:
                msg = raw[:300]
            low = msg.lower()
            if e.code in (400, 403) and any(w in low for w in ("location", "api key", "api_key")):
                raise GeminiError(e.code, msg, fatal=True)
            if e.code in (429, 500, 503) and not last_try:
                wait = 5 * (attempt + 1)
                if log:
                    log(f"  Gemini busy/limit ({e.code}); retrying in {wait}s...")
                time.sleep(wait)
                continue
            raise GeminiError(e.code, msg)
        except (urllib.error.URLError, TimeoutError) as e:
            if not last_try:
                time.sleep(3)
                continue
            raise GeminiError(0, f"cannot reach Gemini ({e})", fatal=True)
        except (KeyError, IndexError, ValueError) as e:
            if not last_try:
                time.sleep(2)
                continue
            raise GeminiError(0, f"unreadable Gemini answer ({type(e).__name__})")


def translate_with_gemini(texts, target_code, key, models, title, log,
                          max_lines=80, max_chars=6000, progress=None, workers=3):
    """Big batches, several sent at the same time (a few requests instead of many)."""
    prompt = build_style_prompt(target_code, title)
    out = [None] * len(texts)
    state = {"model": 0, "done": 0, "stop": False}
    lock = threading.Lock()

    def ask(idxs):
        if state["stop"]:
            return
        lines = [texts[i] for i in idxs]
        while True:
            with lock:
                mi = state["model"]
            model = models[mi]
            try:
                res = _gemini_call(key, model, prompt, lines, log)
                break
            except GeminiError as e:
                if e.fatal:
                    state["stop"] = True
                    raise
                if e.status in (0, 400, 403, 404, 429, 500, 503):
                    with lock:
                        if state["model"] == mi and mi + 1 < len(models):
                            state["model"] = mi + 1
                            log(f"  {model} failed ({e}); trying {models[mi + 1]}...")
                        moved = state["model"] > mi  # we (or another batch) switched model
                    if moved:
                        continue
                state["stop"] = True  # no other model left to try
                raise
        if isinstance(res, list) and len(res) == len(lines) \
                and all(isinstance(x, str) for x in res):
            with lock:
                for i, t in zip(idxs, res):
                    out[i] = t.strip()
                state["done"] += len(idxs)
                done = state["done"]
            log(f"  Gemini translated {done}/{len(texts)}")
            if progress:
                progress(done / max(1, len(texts)) * 100)
        elif len(idxs) > 1:  # wrong line count: split the block and try the halves
            mid = len(idxs) // 2
            ask(idxs[:mid])
            ask(idxs[mid:])

    chunks, chunk, size = [], [], 0
    for i, t in enumerate(texts):
        if chunk and (len(chunk) >= max_lines or size + len(t) > max_chars):
            chunks.append(chunk)
            chunk, size = [], 0
        chunk.append(i)
        size += len(t)
    if chunk:
        chunks.append(chunk)

    def run_chunk(c):
        try:
            ask(c)
        except GeminiError as e:
            log(f"Gemini stopped: {e}")

    if chunks:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(chunks)))) as ex:
            list(ex.map(run_chunk, chunks))
    return out


# --------------------------------------------------------------------------- #
# Transcript
# --------------------------------------------------------------------------- #
def get_transcript(video_id, video_lang=None):
    """Pick the transcript in the video's ORIGINAL language (not a translation)."""
    tlist = list(YouTubeTranscriptApi().list(video_id))
    if not tlist:
        raise RuntimeError("No transcript available for this video.")

    def same_lang(t):
        return bool(video_lang) and (
            t.language_code.lower().split("-")[0] == video_lang.lower().split("-")[0])

    pool = [t for t in tlist if same_lang(t)] or [t for t in tlist if t.is_generated] or tlist
    pool.sort(key=lambda t: t.is_generated)
    return pool[0].fetch(), pool[0].language_code


def group_segments(snippets, texts, max_len=8.0, max_gap=1.0):
    """Merge caption fragments into sentence-sized phrases (better translation + voice)."""
    groups, cur = [], None
    for s, text in zip(snippets, texts):
        text = text.strip()
        if not text:
            continue
        start, end = s.start, s.start + s.duration
        closed = bool(cur) and cur["text"].rstrip()[-1:] in ".?!…。！？" \
            and cur["end"] - cur["start"] >= 3.0
        if cur and not closed and start - cur["end"] <= max_gap and end - cur["start"] <= max_len:
            cur["text"] += " " + text
            cur["end"] = max(cur["end"], end)
        else:
            if cur:
                groups.append(cur)
            cur = {"start": start, "end": end, "text": text}
    if cur:
        groups.append(cur)
    for i, g in enumerate(groups):
        nxt = groups[i + 1]["start"] if i + 1 < len(groups) else g["end"] + 2
        g["slot"] = max(0.5, nxt - g["start"])
    return groups


def save_transcript(video_id, target_code, folder, log, video_lang=None, gemini=None,
                    progress=None):
    log("Fetching transcript...")
    fetched, src_lang = get_transcript(video_id, video_lang)
    snippets = list(fetched)
    groups = group_segments(snippets, [s.text.replace("\n", " ") for s in snippets])
    log(f"Translating transcript ({src_lang} -> {target_code}), {len(groups)} phrases...")

    src_texts = [g["text"] for g in groups]
    translated = [None] * len(groups)
    has_key = bool(gemini and gemini.get("key"))
    if has_key:
        log("Translating with Gemini (natural spoken style)...")
        translated = translate_with_gemini(
            src_texts, target_code, gemini["key"], gemini["models"],
            gemini.get("title", ""), log, progress=progress)
    else:
        log("No Gemini key entered - using plain Google Translate.")

    missing = [i for i, t in enumerate(translated) if t is None]
    if missing:
        if has_key:
            log(f"Using Google Translate for {len(missing)} phrase(s) Gemini could not do.")
        base = (len(groups) - len(missing)) / max(1, len(groups))  # part Gemini already did
        cb = (lambda p: progress((base + (1 - base) * p / 100) * 100)) if progress else None
        fixed = translate_list([src_texts[i] for i in missing], target_code, log, progress=cb)
        for i, t in zip(missing, fixed):
            translated[i] = t

    for g, t in zip(groups, translated):
        g["orig"], g["text"] = g["text"], t
    groups = [g for g in groups if g["text"].strip()]

    with open(os.path.join(folder, f"transcript_{target_code}.srt"), "w", encoding="utf-8") as f:
        for n, g in enumerate(groups, 1):
            f.write(f"{n}\n{fmt_srt_time(g['start'])} --> {fmt_srt_time(g['end'])}"
                    f"\n{g['text']}\n\n")
    with open(os.path.join(folder, f"transcript_{target_code}.txt"), "w", encoding="utf-8") as f:
        f.write(" ".join(g["text"] for g in groups))
    log("Transcript saved (.srt and .txt).")
    return groups


# --------------------------------------------------------------------------- #
# Dubbing (cloned voice replaces the original audio)
# --------------------------------------------------------------------------- #
def prepare_reference(sample, out_wav, log):
    """Cut up to 30 s of clean speech (pauses removed) from the voice sample."""
    run_ffmpeg(["-i", sample, "-ac", "1", "-ar", "24000",
                "-af", "silenceremove=start_periods=1:start_threshold=-45dB"
                       ":stop_periods=-1:stop_duration=0.5:stop_threshold=-45dB",
                "-t", "30", out_wav])
    with wave.open(out_wav, "rb") as w:
        secs = w.getnframes() / w.getframerate()
    if secs < 3:
        raise RuntimeError(f"voice sample has only {secs:.1f}s of speech - need at least 3s")
    if secs < 8:
        log(f"  Note: only {secs:.0f}s of speech in the sample; 10-30 s of clean "
            f"speech clones better.")
    else:
        log(f"  Using {secs:.0f}s of your voice sample.")


def xtts_all(texts, lang, ref_wav, work_dir, python_exe, log, progress):
    """Run xtts_worker.py in the XTTS Python and collect the .wav it writes per phrase."""
    job = {"language": lang, "reference": ref_wav, "out_dir": work_dir,
           "items": [{"id": i, "text": t} for i, t in enumerate(texts)]}
    job_path = os.path.join(work_dir, "xtts_job.json")
    with open(job_path, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False)

    os.makedirs(XTTS_HOME, exist_ok=True)
    env = dict(os.environ, COQUI_TOS_AGREED="1", PYTHONIOENCODING="utf-8",
               PYTHONUNBUFFERED="1", TTS_HOME=XTTS_HOME,
               HF_HOME=os.path.join(XTTS_HOME, "hf"),
               TORCH_HOME=os.path.join(XTTS_HOME, "torch"))
    proc = subprocess.Popen(
        [python_exe, XTTS_WORKER, job_path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=env, **NO_WINDOW)

    results, tail = [None] * len(texts), []
    for line in proc.stdout:
        line = line.rstrip()
        tail = (tail + [line])[-15:]
        try:
            if line.startswith("OK "):
                i = int(line.split()[1])
                results[i] = os.path.join(work_dir, f"x{i:05}.wav")
            elif line.startswith("PROGRESS "):
                _, n, total = line.split()
                progress(int(n) / int(total) * 50)
                if int(n) % 10 == 0 or n == total:
                    log(f"  voice: {n}/{total} phrases done")
            elif line.startswith("LOG "):
                log("  " + line[4:])
            elif line.startswith("FAIL "):
                log("  " + line)
        except (ValueError, IndexError):
            pass
    proc.wait()
    if not any(results):
        fails = [t for t in tail if t.startswith("FAIL ")]
        raise RuntimeError("the XTTS voice engine could not speak any phrase. Last message: "
                           + (fails[-1] if fails else "\n".join(tail)))
    return results


def _to_wav(src, dst, tempo=None):
    args = ["-i", src] + (["-filter:a", f"atempo={tempo:.3f}"] if tempo else [])
    run_ffmpeg(args + ["-ar", str(DUB_RATE), "-ac", "1", "-c:a", "pcm_s16le", dst])


def _read_wav(path):
    with wave.open(path, "rb") as w:
        n = w.getnframes()
        return w.readframes(n), n


def _write_silence(out, frames):
    while frames > 0:
        n = min(DUB_RATE, frames)
        out.writeframes(b"\x00\x00" * n)
        frames -= n


def make_dub(video_path, groups, duration, out_path, work_dir, log, progress, engine):
    """Dub the video with the cloned voice (XTTS). No other voice is ever used."""
    os.makedirs(work_dir, exist_ok=True)
    try:
        ref = os.path.join(work_dir, "reference.wav")
        log("Preparing your voice sample...")
        prepare_reference(engine["sample"], ref, log)
        log(f"Generating the voice with XTTS for {len(groups)} phrases. "
            f"This can take a long time on a normal CPU...")
        clips = xtts_all([g["text"] for g in groups], engine["lang"], ref, work_dir,
                         engine["python"], log, progress)
        failed = sum(1 for c in clips if not c)
        if failed:
            log(f"Warning: {failed} phrase(s) could not be voiced and are silent.")

        log("Building the audio track...")
        dub_wav = os.path.join(work_dir, "dub.wav")
        with wave.open(dub_wav, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(DUB_RATE)
            pos = 0
            for n, (g, clip) in enumerate(zip(groups, clips)):
                progress(50 + n / len(groups) * 45)
                if n % 50 == 0:
                    log(f"  assembling audio: {n}/{len(groups)}")
                if not clip:
                    continue
                wav = clip + ".conv.wav"
                _to_wav(clip, wav)
                frames, nfr = _read_wav(wav)
                dur = nfr / DUB_RATE
                if dur > g["slot"] * 1.05:  # too long for its slot: speed it up
                    _to_wav(clip, wav, tempo=min(1.6, dur / g["slot"]))
                    frames, nfr = _read_wav(wav)
                start = int(g["start"] * DUB_RATE)
                if start > pos:
                    _write_silence(out, start - pos)
                    pos = start
                out.writeframes(frames)
                pos += nfr
            total = int((duration + 1) * DUB_RATE)
            if total > pos:
                _write_silence(out, total - pos)

        log("Replacing the original audio...")
        run_ffmpeg(["-i", video_path, "-i", dub_wav, "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", out_path])
        progress(100)
        log(f"Dubbed video saved: {os.path.basename(out_path)}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Custom Peach & White UI widgets
# --------------------------------------------------------------------------- #
def _rounded_rect(canvas, x1, y1, x2, y2, r, **kw):
    """Draw a smooth rounded rectangle on a Tkinter Canvas."""
    r = max(1, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, splinesteps=24, **kw)


def label(parent, text="", fg=None, font=None, bg=None, **kw):
    """tk.Label that defaults to the parent's background and the normal text colour."""
    return tk.Label(parent, text=text, bg=bg or parent.cget("bg"),
                    fg=fg or THEME["text"], font=font or (UI_FONT, 9), **kw)


def link(parent, text, command):
    """Underlined clickable text."""
    lbl = label(parent, text, fg=THEME["accent_text"], font=(UI_FONT, 9, "underline"),
                cursor="hand2")
    lbl.bind("<Button-1>", lambda e: command())
    return lbl


def field(parent, font, **kw):
    """Plain entry with the peach focus border (API key / Python path)."""
    return tk.Entry(parent, bd=0, highlightthickness=1, highlightbackground=THEME["border"],
                    highlightcolor=THEME["accent"], bg=THEME["white"], fg=THEME["text"],
                    font=font, **kw)


class RoundedButton(tk.Canvas):
    """Sleek rounded pill/rectangle button."""

    def __init__(self, parent, text="", command=None, width=116, height=44, radius=12,
                 bg_color=None, hover_color=None, press_color=None, fg_color=None,
                 border_color=None, font=None, parent_bg=None):
        super().__init__(parent, width=width, height=height, bg=parent_bg or THEME["bg"],
                         highlightthickness=0, bd=0, cursor="hand2")
        self.text, self.command, self.radius = text, command, radius
        self.colors = {"normal": bg_color or THEME["accent"],
                       "hover": hover_color or THEME["accent_hover"],
                       "press": press_color or THEME["accent_press"]}
        self.fg_color = fg_color or THEME["white"]
        self.border_color = border_color or ""
        self.font = font or (UI_FONT, 11, "bold")
        self._state, self._disabled = "normal", False

        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", lambda e: self._set_state("hover"))
        self.bind("<Leave>", lambda e: self._set_state("normal"))
        self.bind("<ButtonPress-1>", lambda e: self._set_state("press"))
        self.bind("<ButtonRelease-1>", self._on_release)
        self._draw()

    def _set_state(self, state):
        if not self._disabled:
            self._state = state
            self._draw()

    def set_text(self, text):
        self.text = text
        self._draw()

    def set_disabled(self, disabled):
        self._disabled = disabled
        self.configure(cursor="arrow" if disabled else "hand2")
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 2 or h <= 2:
            w, h = int(self["width"]), int(self["height"])
        fill, fg = ("#F5C4B3", "#FFFDFC") if self._disabled \
            else (self.colors[self._state], self.fg_color)
        _rounded_rect(self, 1, 1, w - 1, h - 1, self.radius, fill=fill,
                      outline=self.border_color, width=1 if self.border_color else 0)
        self.create_text(w / 2, h / 2, text=self.text, fill=fg, font=self.font)

    def _on_release(self, event):
        if self._disabled:
            return
        inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
        self._set_state("hover" if inside else "normal")
        if inside and self.command:
            self.command()


class RoundedEntry(tk.Canvas):
    """Rounded white input field with peach focus border and a placeholder."""

    def __init__(self, parent, placeholder="", height=44, radius=12, show="", font=None,
                 parent_bg=None, on_submit=None):
        super().__init__(parent, height=height, bg=parent_bg or THEME["bg"],
                         highlightthickness=0, bd=0)
        self.placeholder, self.radius, self.show_char = placeholder, radius, show
        self._focused = self._has_placeholder = False
        self.entry = tk.Entry(
            self, bd=0, highlightthickness=0, relief="flat", bg=THEME["input_bg"],
            fg=THEME["text"], insertbackground=THEME["accent"],
            selectbackground=THEME["accent_soft"], selectforeground=THEME["text"],
            font=font or (UI_FONT, 11))
        self._win_id = self.create_window(14, height // 2, window=self.entry, anchor="w")

        self.entry.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.entry.bind("<FocusOut>", lambda e: self._set_focus(False))
        if on_submit:
            self.entry.bind("<Return>", lambda e: on_submit())
        self.bind("<Configure>", self._on_resize)
        self.bind("<Button-1>", lambda e: self.entry.focus_set())
        self._apply_placeholder()
        self._draw()

    def _on_resize(self, event):
        self.coords(self._win_id, 14, event.height // 2)
        self.itemconfigure(self._win_id, width=max(40, event.width - 28))
        self._draw()

    def _draw(self):
        self.delete("frame")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 2 or h <= 2:
            return
        _rounded_rect(self, 1, 1, w - 1, h - 1, self.radius, fill=THEME["input_bg"],
                      outline=THEME["accent"] if self._focused else THEME["border"],
                      width=2 if self._focused else 1.5, tags="frame")
        self.tag_lower("frame")

    def _apply_placeholder(self):
        if self.placeholder and not self.entry.get():
            self._has_placeholder = True
            self.entry.configure(fg=THEME["placeholder"], show="")
            self.entry.insert(0, self.placeholder)

    def _clear_placeholder(self):
        if self._has_placeholder:
            self._has_placeholder = False
            self.entry.delete(0, "end")
            self.entry.configure(fg=THEME["text"], show=self.show_char)

    def _set_focus(self, focused):
        self._focused = focused
        if focused:
            self._clear_placeholder()
        else:
            self._apply_placeholder()
        self._draw()

    def get(self):
        return "" if self._has_placeholder else self.entry.get()

    def set(self, value):
        self._clear_placeholder()
        self.entry.delete(0, "end")
        if value:
            self.entry.insert(0, value)
            self.entry.configure(fg=THEME["text"], show=self.show_char)
        elif not self._focused:
            self._apply_placeholder()


class TogglePill(tk.Canvas):
    """Toggle chip for Video / Thumbnail / Transcript / Dub."""

    def __init__(self, parent, text, variable, parent_bg=None):
        self.text, self.var, self.font = text, variable, (UI_FONT, 9, "bold")
        super().__init__(parent, width=max(84, len(text) * 8 + 34), height=32,
                         bg=parent_bg or THEME["bg"], highlightthickness=0, bd=0,
                         cursor="hand2")
        self._hover = False
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Button-1>", lambda e: self.var.set(not self.var.get()))
        self.var.trace_add("write", lambda *_: self._draw())
        self._draw()

    def _set_hover(self, hover):
        self._hover = hover
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = int(self["width"]), int(self["height"])
        if self.var.get():
            fill = "#FFD8C4" if self._hover else THEME["accent_soft"]
            outline, fg, text = THEME["accent"], THEME["accent_text"], f"✓  {self.text}"
        else:
            fill = THEME["card"] if self._hover else THEME["white"]
            outline, fg, text = THEME["border"], THEME["muted"], self.text
        _rounded_rect(self, 1, 1, w - 1, h - 1, 15, fill=fill, outline=outline, width=1.2)
        self.create_text(w / 2, h / 2, text=text, fill=fg, font=self.font)


def _mix(c1, c2, k):
    """Blend two '#RRGGBB' colours (k=0 gives c1, k=1 gives c2)."""
    k = max(0.0, min(1.0, k))
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02X%02X%02X" % tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))


class PeachProgressBar(tk.Canvas):
    """
    Rounded progress bar that glides to each new value instead of jumping, and shows a
    moving sheen (plus a tiny creep) while work is running, so a long wait never looks frozen.
    """

    def __init__(self, parent, height=10, parent_bg=None):
        super().__init__(parent, height=height, bg=parent_bg or THEME["bg"],
                         highlightthickness=0, bd=0)
        self._target = self._shown = 0.0   # real value / value currently drawn (0-100)
        self._busy = False
        self._creep = 0.0                  # extra points shown while waiting (max ~4)
        self._phase = 0.0                  # position of the sheen
        self.bind("<Configure>", lambda e: self._draw())
        self.after(16, self._loop)

    def set(self, value):
        value = max(0.0, min(100.0, float(value)))
        if value > self._target:
            self._creep *= 0.4             # real progress arrived: the creep gives way to it
        self._target = value

    def reset(self):
        self._target = self._shown = self._creep = 0.0
        self._draw()

    def set_busy(self, busy):
        self._busy = bool(busy)
        if not busy:
            self._creep = 0.0

    def _loop(self):
        try:
            goal = self._target
            if self._busy and goal < 100:
                self._creep += (4.0 - self._creep) * 0.002
                goal = min(99.0, goal + self._creep)
            diff = goal - self._shown
            moved = abs(diff) > 0.005
            if moved:
                step = diff * 0.07
                if abs(step) < 0.05:
                    step = diff if abs(diff) < 0.05 else math.copysign(0.05, diff)
                self._shown += step
            if self._busy:
                self._phase = (self._phase + 0.011) % 1.0
            if moved or self._busy:
                self._draw()
        except tk.TclError:
            return
        self.after(16, self._loop)

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 4 or h <= 2:
            return
        _rounded_rect(self, 0, 0, w, h, h / 2, fill=THEME["card_alt"], outline="")
        if self._shown <= 0.05:
            return
        fw = min(w, max(h, w * self._shown / 100.0))
        _rounded_rect(self, 0, 0, fw, h, h / 2, fill=THEME["accent"], outline="")
        inner = fw - h  # keep the sheen away from the rounded ends
        if self._busy and inner > h * 2:
            sw = max(h * 3, inner * 0.22)
            x0 = h / 2 + (inner - sw) * (0.5 - 0.5 * math.cos(self._phase * 2 * math.pi))
            _rounded_rect(self, x0, 1, x0 + sw, h - 1, (h - 2) / 2,
                          fill=_mix(THEME["accent"], "#FFFFFF", 0.38), outline="")


# --------------------------------------------------------------------------- #
# Main application
# --------------------------------------------------------------------------- #
class SummitApp(tk.Tk):
    def __init__(self):
        super().__init__()
        try:  # start invisible, then fade in (see _fade_in)
            self.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        self.title("Summit")
        self.configure(bg=THEME["bg"])
        self._set_app_icon()
        self.q = queue.Queue()

        self._win_w, self._win_h = 860, 540
        sx = max(40, (self.winfo_screenwidth() - self._win_w) // 2)
        sy = max(40, (self.winfo_screenheight() - self._win_h) // 2)
        self.geometry(f"{self._win_w}x{self._win_h}+{sx}+{sy}")
        self.minsize(740, 520)

        self._is_maximized = False
        self._normal_geom = self.geometry()
        self._drag_x = self._drag_y = 0
        self._settings_open = False

        # preview + progress state
        self._info, self._info_url = None, ""   # video shown in the preview card
        self._fetched_url = self._seen_url = ""
        self._fetching = self._busy = self._preview_visible = False
        self._thumb_img = None
        self._stages, self._frac, self._overall, self._last_stage = [], {}, 0.0, ""
        self._issues = []

        self._setup_custom_window()
        self._configure_ttk_styles()
        self._build_ui()
        self._bind_global_shortcuts()
        self.after(100, self.poll)
        self.after(300, self._watch_url)
        self.after(60, self._fit_height)
        self.after(320, self._fade_in)  # after the taskbar fix has re-shown the window
        self.after(1500, self._show_fully)  # safety net: never stay see-through

    def _fade_in(self, a=0.0):
        a = min(1.0, a + 0.08)
        try:
            self.attributes("-alpha", a)
        except tk.TclError:
            return
        if a < 1.0:
            self.after(16, self._fade_in, a)

    def _show_fully(self):
        try:
            self.attributes("-alpha", 1.0)
        except tk.TclError:
            pass

    # ---- window chrome ---------------------------------------------------------
    def _set_app_icon(self):
        """Use summit.ico / summit.png as the window and taskbar icon."""
        try:
            if os.path.exists(ICON_PNG):
                from PIL import ImageTk
                big = Image.open(ICON_PNG).convert("RGBA").resize((256, 256), Image.LANCZOS)
                self._icon_img = ImageTk.PhotoImage(big)
                self.iconphoto(True, self._icon_img)
        except Exception:
            pass
        try:
            if IS_WIN and os.path.exists(ICON_ICO):
                self.iconbitmap(default=ICON_ICO)
        except Exception:
            pass

    def _setup_custom_window(self):
        """Frameless window that still has a Windows taskbar entry."""
        try:
            self.overrideredirect(True)
            if IS_WIN:
                self._taskbar_tries = 0
                # wait until the window is really on screen, then add the taskbar flag
                self.after(200, self._apply_win32_taskbar)
        except Exception:
            pass

    def _win32_hwnd(self):
        user32 = ctypes.windll.user32
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetParent.argtypes = [ctypes.c_void_p]
        self.update_idletasks()
        return user32.GetParent(self.winfo_id()) or self.winfo_id()

    def _apply_win32_taskbar(self):
        """Give the frameless window a taskbar button and an Alt+Tab entry."""
        try:
            user32 = ctypes.windll.user32
            user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
            user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_uint]
            hwnd = self._win32_hwnd()
            style = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            style = (style & ~0x00000080) | 0x00040000  # drop TOOLWINDOW, add APPWINDOW
            user32.SetWindowLongW(hwnd, -20, style)
            # NOSIZE | NOMOVE | NOZORDER | NOACTIVATE | FRAMECHANGED
            user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)
            self.withdraw()  # Windows only re-reads the flag when the window is shown again
            self.after(40, self._after_taskbar_apply)
        except Exception:
            try:
                self.deiconify()
            except Exception:
                pass

    def _icon_file(self):
        """summit.ico if present, otherwise one made from summit.png."""
        if os.path.exists(ICON_ICO):
            return ICON_ICO
        if os.path.exists(ICON_PNG) and Image is not None:
            try:
                import tempfile
                path = os.path.join(tempfile.gettempdir(), "summit_icon.ico")
                Image.open(ICON_PNG).convert("RGBA").save(
                    path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
                return path
            except Exception:
                pass
        return None

    def _apply_win32_icon(self):
        """Putting the icon on the window itself; Tk loses it when the window is rebuilt."""
        try:
            path = self._icon_file()
            if not path:
                return
            user32 = ctypes.windll.user32
            user32.LoadImageW.restype = ctypes.c_void_p
            user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                                          ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                            ctypes.c_void_p, ctypes.c_void_p]
            hwnd = self._win32_hwnd()
            # IMAGE_ICON = 1, LR_LOADFROMFILE = 0x10; WM_SETICON = 0x80 (1 = big, 0 = small)
            big = user32.LoadImageW(None, path, 1, 32, 32, 0x10)
            small = user32.LoadImageW(None, path, 1, 16, 16, 0x10)
            if big:
                user32.SendMessageW(hwnd, 0x80, 1, big)
            if small:
                user32.SendMessageW(hwnd, 0x80, 0, small)
        except Exception:
            pass

    def _after_taskbar_apply(self):
        try:
            self.deiconify()
            self._apply_win32_icon()
            self.after(400, self._check_win32_taskbar)
        except Exception:
            pass

    def _check_win32_taskbar(self):
        """Tk can reset the flag when it re-creates the window, so check and retry."""
        try:
            user32 = ctypes.windll.user32
            hwnd = self._win32_hwnd()
            if not (user32.GetWindowLongW(hwnd, -20) & 0x00040000) and self._taskbar_tries < 3:
                self._taskbar_tries += 1
                self._apply_win32_taskbar()
        except Exception:
            pass

    def _minimize_win(self):
        try:
            self.overrideredirect(False)
            self.iconify()
            self.bind("<Map>", self._restore_from_iconify)
        except Exception:
            self.iconify()

    def _restore_from_iconify(self, event=None):
        if event is not None and event.widget is not self:
            return  # a child widget was mapped, not the window itself
        self.unbind("<Map>")
        self.overrideredirect(True)
        if IS_WIN:
            self._taskbar_tries = 0
            self.after(30, self._apply_win32_taskbar)

    def _toggle_maximize(self):
        if not self._is_maximized:
            self._normal_geom = self.geometry()
            self.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight() - 40}+0+0")
        else:
            self.geometry(self._normal_geom)
        self._is_maximized = not self._is_maximized

    def _start_drag(self, event):
        self._drag_x, self._drag_y = event.x_root - self.winfo_x(), event.y_root - self.winfo_y()

    def _on_drag(self, event):
        if not self._is_maximized:
            self.geometry(f"+{event.x_root - self._drag_x}+{event.y_root - self._drag_y}")

    def _configure_ttk_styles(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Summit.TCombobox", fieldbackground=THEME["white"],
                        background=THEME["card"], foreground=THEME["text"],
                        arrowcolor=THEME["accent"], bordercolor=THEME["border"],
                        lightcolor=THEME["border"], darkcolor=THEME["border"], padding=(8, 4))
        style.map("Summit.TCombobox",
                  fieldbackground=[("readonly", THEME["white"])],
                  selectbackground=[("readonly", THEME["white"])],
                  selectforeground=[("readonly", THEME["text"])])
        style.configure("Summit.TCheckbutton", background=THEME["card"],
                        foreground=THEME["text"], font=(UI_FONT, 9))
        style.map("Summit.TCheckbutton", background=[("active", THEME["card"])])

    # ---- UI --------------------------------------------------------------------
    def _build_ui(self):
        self.shell = tk.Frame(self, bg=THEME["bg"], highlightthickness=1,
                              highlightbackground=THEME["border"])
        self.shell.pack(fill="both", expand=True)
        self._build_titlebar()

        # credits, bottom-right corner (packed before the body so it keeps its space)
        label(self.shell, "Made by: Nat 2026 ©", fg=THEME["placeholder"],
              font=(UI_FONT, 8)).pack(side="bottom", anchor="e", padx=18, pady=(0, 10))

        self.body = tk.Frame(self.shell, bg=THEME["bg"])
        self.body.pack(fill="both", expand=True, padx=56, pady=(34, 8))
        bg = THEME["bg"]

        # header: title + subtitle + settings button
        hdr = tk.Frame(self.body, bg=bg)
        hdr.pack(fill="x")
        title_col = tk.Frame(hdr, bg=bg)
        title_col.pack(side="left", fill="x", expand=True)
        label(title_col, "Summit", font=(UI_FONT, 26, "bold"), anchor="w").pack(anchor="w")
        label(title_col, "Paste a YouTube link — anywhere in the window. "
                         "Translate, subtitle, and dub.",
              fg=THEME["muted"], font=(UI_FONT, 10), anchor="w").pack(anchor="w", pady=(3, 0))
        self.settings_btn = RoundedButton(
            hdr, text="⚙", command=self.toggle_settings, width=40, height=40, radius=10,
            bg_color=THEME["card"], hover_color=THEME["card_alt"],
            press_color=THEME["accent_soft"], fg_color=THEME["accent_text"],
            border_color=THEME["border"], font=(UI_FONT, 13), parent_bg=bg)
        self.settings_btn.pack(side="right", anchor="n", pady=(4, 0))

        # URL input + Start
        self.url_row = url_row = tk.Frame(self.body, bg=bg)
        url_row.pack(fill="x", pady=(26, 0))
        self.url_box = RoundedEntry(url_row, placeholder="https://www.youtube.com/watch?v=...",
                                    height=46, radius=12, font=(UI_FONT, 11), parent_bg=bg,
                                    on_submit=self._on_enter)
        self.url_box.pack(side="left", fill="x", expand=True)
        self._url_menu = tk.Menu(self, tearoff=0, bg=THEME["white"], fg=THEME["text"])
        self._url_menu.add_command(label="Paste", command=self._paste_clipboard_to_url)
        self._url_menu.add_command(label="Clear", command=lambda: self.url_box.set(""))
        self.url_box.entry.bind("<Button-3>",
                                lambda e: self._url_menu.tk_popup(e.x_root, e.y_root))
        self.start_btn = RoundedButton(url_row, text="Start", command=self.start, width=112,
                                       height=46, radius=12, parent_bg=bg)
        self.start_btn.pack(side="left", padx=(12, 0))

        self._build_preview_card()

        # "Saving to <folder> Change"
        save_row = tk.Frame(self.body, bg=bg)
        save_row.pack(fill="x", pady=(14, 0))
        icon = tk.Canvas(save_row, width=18, height=16, bg=bg, highlightthickness=0)
        icon.pack(side="left", pady=(1, 0))
        icon.create_polygon(2, 3, 7, 3, 9, 5, 16, 5, 16, 14, 2, 14,
                            fill="", outline=THEME["muted"], width=1.4)
        label(save_row, "Saving to", fg=THEME["muted"]).pack(side="left", padx=(5, 6))
        self.folder_var = tk.StringVar(value=os.path.join(APP_DIR, "downloads"))
        label(save_row, fg=THEME["text"], font=(MONO_FONT, 9), anchor="w",
              textvariable=self.folder_var).pack(side="left")
        link(save_row, "Change", self.browse).pack(side="left", padx=(10, 0))

        # language + task chips
        pills_row = tk.Frame(self.body, bg=bg)
        pills_row.pack(fill="x", pady=(20, 0))
        lang_wrap = tk.Frame(pills_row, bg=bg)
        lang_wrap.pack(side="left")
        label(lang_wrap, "Language", fg=THEME["muted"],
              font=(UI_FONT, 8, "bold")).pack(anchor="w", pady=(0, 3))
        self.lang = ttk.Combobox(lang_wrap, values=list(LANGUAGES), state="readonly", width=11,
                                 style="Summit.TCombobox", font=(UI_FONT, 9))
        self.lang.set("Czech")
        self.lang.pack()

        tasks_wrap = tk.Frame(pills_row, bg=bg)
        tasks_wrap.pack(side="left", fill="x", expand=True, padx=(18, 0))
        label(tasks_wrap, "Include", fg=THEME["muted"],
              font=(UI_FONT, 8, "bold")).pack(anchor="w", pady=(0, 3))
        chips = tk.Frame(tasks_wrap, bg=bg)
        chips.pack(anchor="w")
        self.do_video, self.do_thumb, self.do_trans = (tk.BooleanVar(value=True) for _ in range(3))
        self.do_dub = tk.BooleanVar(value=False)
        for i, (text, var) in enumerate((("Video", self.do_video), ("Thumbnail", self.do_thumb),
                                         ("Transcript", self.do_trans),
                                         ("Dub Video", self.do_dub))):
            TogglePill(chips, text, var, bg).pack(side="left", padx=(0, 6 if i < 3 else 0))

        # collapsible settings card (⚙)
        self.settings_card = tk.Frame(self.body, bg=THEME["card"], highlightthickness=1,
                                      highlightbackground=THEME["border"])
        self._build_settings_card()

        # progress bar + status
        prog = tk.Frame(self.body, bg=bg)
        prog.pack(fill="x", pady=(22, 0))
        self.bar = PeachProgressBar(prog, height=8, parent_bg=bg)
        self.bar.pack(fill="x")
        status_row = tk.Frame(prog, bg=bg)
        status_row.pack(fill="x", pady=(8, 0))
        self.status = label(status_row, "Ready.", fg=THEME["muted"],
                            font=(UI_FONT, 9, "bold"), anchor="w")
        self.status.pack(side="left", fill="x", expand=True)

    def _build_preview_card(self):
        card = THEME["card"]
        self.preview_card = tk.Frame(self.body, bg=card, highlightthickness=1,
                                     highlightbackground=THEME["border"])
        pv_in = tk.Frame(self.preview_card, bg=card)
        pv_in.pack(fill="x", padx=12, pady=12)

        thumb_wrap = tk.Frame(pv_in, bg=THEME["card_alt"], width=PREVIEW_W, height=PREVIEW_H)
        thumb_wrap.pack(side="left")
        thumb_wrap.pack_propagate(False)
        self.thumb_lbl = tk.Label(thumb_wrap, bg=THEME["card_alt"], bd=0)
        self.thumb_lbl.pack(fill="both", expand=True)
        self.dur_badge = tk.Label(thumb_wrap, bg="#2B211E", fg="#FFFFFF",
                                  font=(MONO_FONT, 8, "bold"), padx=6, pady=1)

        pv_info = tk.Frame(pv_in, bg=card)
        pv_info.pack(side="left", fill="both", expand=True, padx=(16, 0))
        self.pv_title = label(pv_info, font=(UI_FONT, 12, "bold"), justify="left",
                              anchor="w", wraplength=340)
        self.pv_title.pack(anchor="w")
        self.pv_meta = label(pv_info, fg=THEME["muted"], justify="left", anchor="w",
                             wraplength=340)
        self.pv_meta.pack(anchor="w", pady=(6, 0))
        self.pv_links = tk.Frame(pv_info, bg=card)
        self.copy_lbl = link(self.pv_links, "Copy title", self._copy_title)
        self.copy_lbl.pack(side="left")
        link(self.pv_links, "Open on YouTube", self._open_youtube).pack(side="left", padx=(14, 0))
        pv_info.bind("<Configure>", lambda e: (
            self.pv_title.config(wraplength=max(180, e.width - 6)),
            self.pv_meta.config(wraplength=max(180, e.width - 6))))

    def _build_titlebar(self):
        tbc = THEME["titlebar"]
        tb = tk.Frame(self.shell, bg=tbc, height=38)
        tb.pack(fill="x")
        tb.pack_propagate(False)

        brand = tk.Frame(tb, bg=tbc)
        brand.pack(side="left", padx=(14, 0))
        try:
            from PIL import ImageTk
            em = Image.open(ICON_PNG).convert("RGBA").resize((22, 22), Image.LANCZOS)
            self._emblem_img = ImageTk.PhotoImage(em)
            emblem = tk.Label(brand, image=self._emblem_img, bg=tbc, bd=0)
        except Exception:  # icon file missing: fall back to the drawn badge
            emblem = tk.Canvas(brand, width=20, height=20, bg=tbc, highlightthickness=0)
            _rounded_rect(emblem, 1, 1, 19, 19, 5, fill=THEME["accent"], outline="")
            emblem.create_polygon(4, 15, 9, 6, 13, 12, 15, 9, 17, 15,
                                  fill=THEME["white"], outline="")
        emblem.pack(side="left")
        self.tb_title = label(brand, "Summit", font=(UI_FONT, 9, "bold"))
        self.tb_title.pack(side="left", padx=(8, 0))

        btns = tk.Frame(tb, bg=tbc)
        btns.pack(side="right", fill="y")
        for symbol, cmd, hover_bg, hover_fg in (
                ("—", self._minimize_win, THEME["card_alt"], THEME["text"]),
                ("□", self._toggle_maximize, THEME["card_alt"], THEME["text"]),
                ("✕", self.destroy, THEME["accent"], THEME["white"])):
            lbl = label(btns, symbol, fg=THEME["muted"], font=(UI_FONT, 10), width=4,
                        cursor="hand2")
            lbl.pack(side="left", fill="y")
            lbl.bind("<Enter>", lambda e, l=lbl, b=hover_bg, f=hover_fg: l.config(bg=b, fg=f))
            lbl.bind("<Leave>", lambda e, l=lbl: l.config(bg=tbc, fg=THEME["muted"]))
            lbl.bind("<Button-1>", lambda e, c=cmd: c())

        tk.Frame(self.shell, bg=THEME["border_subtle"], height=1).pack(fill="x")
        for widget in (tb, brand, emblem, self.tb_title):  # drag the window by the title bar
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._on_drag)
            widget.bind("<Double-Button-1>", lambda e: self._toggle_maximize())

    def _build_settings_card(self):
        card = THEME["card"]
        inner = tk.Frame(self.settings_card, bg=card)
        inner.pack(fill="x", padx=16, pady=12)

        # Row 0: Gemini API key + model + remember
        r0 = tk.Frame(inner, bg=card)
        r0.pack(fill="x", pady=(0, 8))
        label(r0, "Gemini API Key", font=(UI_FONT, 9, "bold")).pack(side="left")
        self.gkey = field(r0, (UI_FONT, 9), width=26, show="*")
        self.gkey.insert(0, load_saved_key())
        self.gkey.pack(side="left", padx=(8, 12), ipady=3)
        label(r0, "Model", fg=THEME["muted"]).pack(side="left")
        self.gmodel = ttk.Combobox(r0, width=18, values=GEMINI_MODELS, state="readonly",
                                   style="Summit.TCombobox", font=(UI_FONT, 9))
        self.gmodel.set(GEMINI_MODELS[0])
        self.gmodel.pack(side="left", padx=(6, 10))
        self.remember = tk.BooleanVar(value=False)
        ttk.Checkbutton(r0, text="Remember", variable=self.remember,
                        style="Summit.TCheckbutton").pack(side="left")

        # Row 1: voice status
        r1 = tk.Frame(inner, bg=card)
        r1.pack(fill="x", pady=(0, 8))
        found = os.path.isfile(VOICE_FILE)
        label(r1, "Voice: tomas_Nikl.mp3 (XTTS ready)" if found
              else "Voice: tomas_Nikl.mp3 not found in app folder",
              fg=THEME["success"] if found else THEME["danger"],
              font=(UI_FONT, 9, "bold")).pack(side="left")

        # Row 2: XTTS Python path
        r2 = tk.Frame(inner, bg=card)
        r2.pack(fill="x")
        label(r2, "XTTS Python", font=(UI_FONT, 9, "bold")).pack(side="left")
        self.xpython = field(r2, (MONO_FONT, 9))
        self.xpython.insert(0, load_settings().get("xtts_python") or default_xtts_python())
        self.xpython.pack(side="left", fill="x", expand=True, padx=(18, 8), ipady=3)
        link(r2, "Browse", self.pick_python).pack(side="left")

    def toggle_settings(self):
        self._settings_open = not self._settings_open
        if self._settings_open:
            self.settings_card.pack(fill="x", pady=(14, 0), before=self.bar.master)
        else:
            self.settings_card.pack_forget()
        self._adjust_window_height(135 if self._settings_open else -135)

    def _adjust_window_height(self, delta):
        if self._is_maximized:
            return
        self.update_idletasks()
        h = max(500, min(self.winfo_screenheight() - 80, self.winfo_height() + delta))
        self.geometry(f"{self.winfo_width()}x{h}")

    def _fit_height(self):
        """Grow the window if the content (e.g. the preview card) does not fit."""
        if self._is_maximized:
            return
        self.update_idletasks()
        need = self.shell.winfo_reqheight() + 2
        if need > self.winfo_height():
            self.geometry(f"{self.winfo_width()}x{min(self.winfo_screenheight() - 80, need)}")

    # ---- input helpers -----------------------------------------------------------
    def _bind_global_shortcuts(self):
        """Support 'Paste a YouTube link — anywhere in the window'."""
        for seq in ("<Control-v>", "<Control-V>") + (("<Command-v>",) if sys.platform == "darwin" else ()):
            self.bind_all(seq, self._on_global_paste)

    def _on_global_paste(self, event):
        focused = self.focus_get()
        if focused in (self.gkey, self.xpython):  # secondary inputs paste on their own
            return
        if focused is self.url_box.entry:
            self.url_box._clear_placeholder()  # so the native Entry paste inserts cleanly
            return
        self._paste_clipboard_to_url()
        return "break"

    def _paste_clipboard_to_url(self):
        try:
            clip = self.clipboard_get().strip()
        except tk.TclError:
            return
        if clip:
            self.url_box.set(clip)
            self.url_box.entry.focus_set()
            self.url_box.entry.icursor("end")

    def pick_python(self):
        p = filedialog.askopenfilename(filetypes=[("python.exe", "python.exe"), ("All files", "*.*")])
        if p:
            self.xpython.delete(0, "end")
            self.xpython.insert(0, p)

    def browse(self):
        d = filedialog.askdirectory()
        if d:
            self.folder_var.set(d)

    def log(self, msg):
        """Details go to last_run_log.txt (there is no log window)."""
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(str(msg) + "\n")
        except OSError:
            pass

    # ---- one overall progress bar for the whole job --------------------------------
    def _set_overall(self, v):
        v = max(self._overall, min(100.0, float(v)))  # the bar never goes backwards
        self._overall = v
        self.bar.set(v)
        label_text = STAGE_LABELS.get(self._last_stage, "Working")
        self.status.config(text=f"{label_text}...  {int(v)}%", fg=THEME["accent_text"])

    def progress_for(self, name):
        """A progress callback (0-100) for one step of the job."""
        return lambda v: self.q.put(("bar", (name, v)))

    def stage_done(self, name):
        self.q.put(("bar", (name, 100)))

    # ---- video preview ---------------------------------------------------------------
    def _watch_url(self):
        """Notice a pasted/typed YouTube link (any way of pasting) and show its preview."""
        try:
            if not self._busy:
                cur = self.url_box.get().strip()
                new = cur != self._fetched_url
                if cur != self._seen_url:
                    self._seen_url = cur  # just changed: wait one tick so a paste can finish
                elif cur and new and not self._fetching and looks_like_youtube(cur):
                    self._fetch_preview(cur)
                elif cur and new and self._preview_visible and not looks_like_youtube(cur):
                    self._hide_preview()
                elif not cur and (self._preview_visible or self._fetched_url):
                    self._hide_preview()
                    self._fetched_url = ""
        finally:
            self.after(300, self._watch_url)

    def _fetch_preview(self, url):
        self._fetching = True
        self._fetched_url = url
        self._info, self._info_url = None, ""
        self.status.config(text="Ready.", fg=THEME["muted"])
        self._show_preview(None, None)  # "Loading..." state
        threading.Thread(target=self._preview_worker, args=(url,), daemon=True).start()

    def _preview_worker(self, url):
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True}) as ydl:
                info = ydl.extract_info(url, download=False)
            pil = None
            for u in (info.get("thumbnail"), f"https://i.ytimg.com/vi/{info.get('id')}/hqdefault.jpg"):
                if not u:
                    continue
                try:
                    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=20) as r:
                        data = r.read()
                    pil = ImageOps.fit(Image.open(io.BytesIO(data)).convert("RGB"),
                                       (PREVIEW_W, PREVIEW_H), Image.LANCZOS)
                    break
                except Exception:
                    continue
            self.q.put(("preview", (url, info, pil)))
        except Exception as e:
            self.q.put(("preview_error", (url, str(e))))

    def _show_preview(self, info, pil):
        if not self._preview_visible:
            self.preview_card.pack(fill="x", pady=(18, 0), after=self.url_row)
            self._preview_visible = True
        if info is None:
            self.pv_title.config(text="Loading preview...")
            self.pv_meta.config(text="")
            self.thumb_lbl.config(image="")
            self._thumb_img = None
            self.dur_badge.place_forget()
            self.pv_links.pack_forget()
        else:
            self.pv_title.config(text=info.get("title") or "Untitled")
            parts = []
            channel = info.get("channel") or info.get("uploader")
            if channel:
                parts.append(channel)
            if info.get("view_count") is not None:
                parts.append(f"{int(info['view_count']):,} views")
            d = info.get("upload_date") or ""
            if len(d) == 8:
                parts.append(f"{d[:4]}-{d[4:6]}-{d[6:]}")
            self.pv_meta.config(text="  ·  ".join(parts))
            if pil is not None:
                try:
                    from PIL import ImageTk
                    self._thumb_img = ImageTk.PhotoImage(pil)
                    self.thumb_lbl.config(image=self._thumb_img)
                except Exception:
                    pass
            dur = fmt_duration(info.get("duration"))
            if dur:
                self.dur_badge.config(text=dur)
                self.dur_badge.place(relx=1.0, rely=1.0, x=-6, y=-6, anchor="se")
            else:
                self.dur_badge.place_forget()
            self.pv_links.pack(anchor="w", pady=(10, 0))
        self._fit_height()

    def _hide_preview(self):
        if self._preview_visible:
            self.preview_card.pack_forget()
            self._preview_visible = False
        self._info, self._info_url = None, ""

    def _copy_title(self):
        t = (self._info or {}).get("title")
        if t:
            self.clipboard_clear()
            self.clipboard_append(t)
            self.copy_lbl.config(text="Copied ✓")
            self.after(1500, lambda: self.copy_lbl.config(text="Copy title"))

    def _open_youtube(self):
        url = (self._info or {}).get("webpage_url") or self.url_box.get().strip()
        if url:
            webbrowser.open(url)

    def _on_enter(self):
        """Enter in the link box: show the preview first, then Enter again starts."""
        if self._busy:
            return
        cur = self.url_box.get().strip()
        if self._info is not None and self._info_url == cur:
            self.start()
        elif self._fetching:
            return
        elif looks_like_youtube(cur) and cur != self._fetched_url:
            self._fetch_preview(cur)
        else:
            self.start()

    # ---- messages from the worker threads ------------------------------------------------
    def poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "plan":
                    self._stages = list(val)
                    self._frac = {n: 0.0 for n in val}
                    self._overall, self._last_stage = 0.0, ""
                    self.bar.reset()
                elif kind == "bar":
                    name, v = val
                    if name in self._frac:
                        self._last_stage = name
                        self._frac[name] = max(self._frac[name],
                                               max(0.0, min(100.0, float(v))) / 100)
                        total = sum(STAGE_WEIGHTS[n] for n in self._stages) or 1
                        got = sum(STAGE_WEIGHTS[n] * f for n, f in self._frac.items())
                        self._set_overall(got / total * 100)
                elif kind == "overall":
                    self._set_overall(val)
                elif kind == "preview":
                    url, info, pil = val
                    self._fetching = False
                    if self.url_box.get().strip() != url:
                        continue  # link changed meanwhile; the watcher fetches the new one
                    self._info, self._info_url = info, url
                    self._show_preview(info, pil)
                elif kind == "preview_error":
                    url, _msg = val
                    self._fetching = False
                    if self.url_box.get().strip() == url:
                        self._hide_preview()
                        self.status.config(text="Couldn't load that link. Check the URL and try again.",
                                           fg=THEME["danger"])
                elif kind == "popup":
                    ok = val.get("ok", True)
                    head = "Summit — Finished" if ok else "Summit — Stopped"
                    self.status.config(text="Finished." if ok else "Stopped with an error.",
                                       fg=THEME["success"] if ok else THEME["danger"])
                    self.title(head)
                    self.tb_title.config(text=head)
                    if not windows_toast(head, val.get("line1", ""), val.get("line2", ""),
                                         val.get("folder")):  # no toast: old message box
                        self.bell()
                        messagebox.showinfo(head, val.get("text", ""))
                elif kind == "done":
                    self._busy = False
                    self.bar.set_busy(False)
                    self.start_btn.set_disabled(False)
                    self.start_btn.set_text("Start")
        except queue.Empty:
            pass
        self.after(100, self.poll)

    # ---- running a job -------------------------------------------------------------------
    def start(self):
        if self._busy:
            return
        url = self.url_box.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Paste a YouTube URL first.")
            return
        if self.remember.get() and self.gkey.get().strip():
            try:
                with open(KEY_FILE, "w", encoding="utf-8") as f:
                    f.write(self.gkey.get().strip())
            except OSError:
                pass
        save_settings({"xtts_python": self.xpython.get().strip()})

        self._busy = True
        self._issues = []
        self._stages, self._frac, self._overall, self._last_stage = [], {}, 0.0, ""
        try:  # fresh details file for this run
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write(time.strftime("Summit run %Y-%m-%d %H:%M:%S\n"))
        except OSError:
            pass
        self.log(f"URL: {url}")
        self.start_btn.set_disabled(True)
        self.start_btn.set_text("Working...")
        self.bar.reset()
        self.bar.set_busy(True)
        self.title("Summit — Working...")
        self.tb_title.config(text="Summit — Working...")
        self.status.config(text="Working...  0%", fg=THEME["accent_text"])
        threading.Thread(target=self.run_job, args=(url,), daemon=True).start()

    def pick_engine(self, code):
        """Settings for dubbing with the cloned voice, or None (then NO dub is made)."""
        py = self.xpython.get().strip()
        if code.lower() not in XTTS_LANGS:
            self.log(f"Dub skipped: the cloned voice can't speak this language ('{code}'; "
                     f"XTTS doesn't support it). The text files are still made.")
        elif not os.path.isfile(VOICE_FILE):
            self.log("Dub skipped: tomas_Nikl.mp3 was not found. Copy it into the same "
                     "folder as app.py.")
        elif not os.path.isfile(py):
            self.log("Dub skipped: the XTTS Python was not found - set the 'XTTS Python' "
                     "box to D:\\xtts-env\\Scripts\\python.exe")
        elif not os.path.isfile(XTTS_WORKER):
            self.log("Dub skipped: xtts_worker.py must be in the same folder as app.py.")
        else:
            return {"sample": VOICE_FILE, "python": py, "lang": code.lower()}
        return None

    def run_job(self, url):
        try:
            code = LANGUAGES[self.lang.get()]
            do_dub = self.do_dub.get()
            need_video = self.do_video.get() or do_dub
            need_trans = self.do_trans.get() or do_dub
            need_thumb = self.do_thumb.get()
            stages = [s for s, needed in (("video", need_video), ("thumb", need_thumb),
                                          ("trans", need_trans), ("dub", do_dub)) if needed]
            self.q.put(("plan", stages))

            if self._info is not None and self._info_url == url:
                info = self._info  # already loaded for the preview
            else:
                self.log("Fetching video info...")
                with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
                    info = ydl.extract_info(url, download=False)
            title = yt_dlp.utils.sanitize_filename(info["title"])
            folder = os.path.join(self.folder_var.get(), title)
            os.makedirs(folder, exist_ok=True)
            self.log(f"Output folder: {folder}")

            chosen = self.gmodel.get()  # read the settings here; helper threads use plain values
            gemini = {"key": self.gkey.get().strip(),
                      "models": [chosen] + [m for m in GEMINI_DEFAULT_MODELS if m != chosen],
                      "title": info.get("title", "")}
            res = {}

            def video_job():
                try:
                    res["video"] = download_video(url, VIDEO_HEIGHT, folder, self.log,
                                                  self.progress_for("video"))
                except Exception as e:
                    res["video_err"] = e
                    self.log(f"Video download failed: {e}")
                finally:
                    self.stage_done("video")

            def thumb_job():
                orig = None
                try:
                    orig = download_thumbnail(info, folder, self.log)
                    self.progress_for("thumb")(15)
                    try:
                        translate_thumbnail_ai(orig, info.get("title", ""), code, gemini,
                                               folder, self.log, self.progress_for("thumb"))
                    except Exception as e:
                        # the AI image step isn't available (e.g. free key): draw it with OCR
                        self.log(f"AI thumbnail not available ({e})")
                        self.log("Making the translated thumbnail by erasing and redrawing the text instead...")
                        redraw_thumbnail(orig, info.get("title", ""), code, gemini,
                                         thumbnail_path(folder, code), self.log)
                        self.progress_for("thumb")(100)
                    try:  # the translated thumbnail is done: the original is not kept
                        os.remove(orig)
                    except OSError:
                        pass
                except Exception as e:
                    self.log(f"Thumbnail step failed: {e}")
                    self._issues.append("thumbnail")
                    if orig and os.path.isfile(orig):  # keep the original so there is a thumbnail
                        try:
                            os.replace(orig, os.path.join(folder, "thumbnail_original.jpg"))
                            self.log("Kept the original thumbnail as thumbnail_original.jpg.")
                        except OSError:
                            pass
                finally:
                    self.stage_done("thumb")

            def trans_job():
                try:
                    res["groups"] = save_transcript(info["id"], code, folder, self.log,
                                                    info.get("language"), gemini,
                                                    progress=self.progress_for("trans"))
                except Exception as e:
                    self.log(f"Transcript step failed: {e}")
                    self._issues.append("transcript")
                finally:
                    self.stage_done("trans")

            # video, thumbnail and transcript do not depend on each other: run them together
            self.log("Downloading and translating at the same time...")
            threads = [threading.Thread(target=fn, daemon=True)
                       for needed, fn in ((need_video, video_job), (need_thumb, thumb_job),
                                          (need_trans, trans_job)) if needed]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            if "video_err" in res:
                raise res["video_err"]
            video_path, groups = res.get("video"), res.get("groups")

            dubbed = False
            if do_dub:
                if not (video_path and groups):
                    self.log("Dubbing skipped: it needs both the video and the transcript.")
                    self._issues.append("dub")
                else:
                    engine = self.pick_engine(code)
                    if not engine:
                        self._issues.append("dub")
                    else:
                        out_mp4 = os.path.join(folder, f"{title}_{code}_dubbed.mp4")
                        try:
                            make_dub(video_path, groups, info.get("duration") or 0, out_mp4,
                                     os.path.join(folder, "_dub_tmp"), self.log,
                                     self.progress_for("dub"), engine)
                            dubbed = os.path.isfile(out_mp4)
                            if dubbed and os.path.isfile(video_path):
                                try:  # dub worked: the original video is not needed
                                    os.remove(video_path)
                                    self.log("Original video removed - only the dubbed video is kept.")
                                except OSError:
                                    pass
                        except Exception as e:
                            self.log(f"Dubbing failed: {e}")
                            self._issues.append("dub")
                            self.log("No dub was made (the app never uses any other voice).")
                self.stage_done("dub")

            self.log("")
            self.log("*** FINISHED - everything is done ***")
            self.log(f"Files are in: {folder}")
            problems = sorted(set(self._issues))
            if problems:
                line2 = f"Finished with problems ({', '.join(problems)}). Select to open the folder."
                note = "\n\nSome steps had problems - see last_run_log.txt in the app folder."
            else:
                line2 = ("Dubbed video is ready. " if dubbed else "Your files are ready. ") \
                    + "Select to open the folder."
                note = ""
            self.q.put(("overall", 100))
            self.q.put(("popup", {
                "ok": True, "folder": folder,
                "line1": title if len(title) <= 60 else title[:57] + "...",
                "line2": line2, "text": f"Finished!\n\nYour files are in:\n{folder}" + note}))
        except Exception as e:
            self.log(f"Error: {e}")
            self.q.put(("popup", {"ok": False, "folder": None, "line1": "Something went wrong.",
                                  "line2": str(e)[:140],
                                  "text": f"Stopped with an error:\n{e}"}))
        finally:
            self.q.put(("done", None))


# --------------------------------------------------------------------------- #
# Start-up: splash screen while everything is preloaded
# --------------------------------------------------------------------------- #
def _warm_connection(host):
    """Resolve the name and set up TLS early so the first real request is quicker."""
    try:
        req = urllib.request.Request("https://" + host, method="HEAD",
                                     headers={"User-Agent": "Mozilla/5.0"})
        urllib.request.urlopen(req, timeout=5).close()
    except Exception:
        pass  # only a warm-up; errors don't matter


def preload(progress):
    """Import the heavy libraries and warm things up. Runs on a background thread."""
    global yt_dlp, GoogleTranslator, YouTubeTranscriptApi
    global Image, ImageDraw, ImageFilter, ImageFont, ImageOps

    progress(0.04, "Warming up connections...")
    for host in ("www.youtube.com", "generativelanguage.googleapis.com",
                 "translate.googleapis.com"):
        threading.Thread(target=_warm_connection, args=(host,), daemon=True).start()

    progress(0.10, "Loading the download engine...")
    import yt_dlp as _yt
    yt_dlp = _yt
    try:  # the biggest YouTube part: loaded now, not when the first video starts
        from yt_dlp.extractor.youtube import YoutubeIE  # noqa: F401
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}):
            pass
    except Exception:
        pass

    progress(0.55, "Loading translators...")
    from deep_translator import GoogleTranslator as _gt
    from youtube_transcript_api import YouTubeTranscriptApi as _yta
    GoogleTranslator, YouTubeTranscriptApi = _gt, _yta

    progress(0.80, "Loading image tools...")
    from PIL import Image as _i, ImageDraw as _id, ImageFilter as _if
    from PIL import ImageFont as _ifo, ImageOps as _io
    Image, ImageDraw, ImageFilter, ImageFont, ImageOps = _i, _id, _if, _ifo, _io
    try:
        from PIL import ImageTk  # noqa: F401  (used for the thumbnail preview)
    except Exception:
        pass

    progress(1.0, "Ready")


class Splash(tk.Tk):
    """Small animated start-up window: logo pops in, bar glides while libraries load."""
    W, H, MIN_SECONDS = 440, 280, 1.6

    def __init__(self):
        super().__init__()
        self.overrideredirect(True)
        self.configure(bg=THEME["border"])
        x = (self.winfo_screenwidth() - self.W) // 2
        y = (self.winfo_screenheight() - self.H) // 2 - 30
        self.geometry(f"{self.W}x{self.H}+{x}+{y}")
        try:
            self.attributes("-alpha", 0.0)
            self.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.cv = tk.Canvas(self, width=self.W - 2, height=self.H - 2, bg=THEME["bg"],
                            highlightthickness=0, bd=0)
        self.cv.place(x=1, y=1)
        self._t0 = time.time()
        self._target = self._shown = 0.0
        self._text = "Starting..."
        self._loaded = self._closing = False
        self._close_t = 0.0
        self.after(10, self._tick)

    # called from the loader thread (only plain assignments, no Tk calls)
    def on_progress(self, fraction, text):
        self._target, self._text = fraction, text

    def loaded(self):
        self._loaded = True

    @staticmethod
    def _pop(k):
        """Ease-out with a small overshoot (0 -> 1)."""
        k = max(0.0, min(1.0, k)) - 1
        return 1 + 2.70158 * k ** 3 + 1.70158 * k ** 2

    def _tick(self):
        t = time.time() - self._t0
        # glide the bar towards the real loading progress
        diff = self._target - self._shown
        self._shown += diff * 0.09 if abs(diff) > 0.002 else diff

        if self._loaded and not self._closing and self._shown > 0.985 and t >= self.MIN_SECONDS:
            self._closing, self._close_t = True, t
        alpha = min(1.0, t / 0.30)
        if self._closing:
            alpha = max(0.0, 1.0 - (t - self._close_t) / 0.22)
        try:
            self.attributes("-alpha", alpha)
        except tk.TclError:
            pass
        self._draw(t)
        if self._closing and alpha <= 0.0:
            self.destroy()
            return
        self.after(16, self._tick)

    def _draw(self, t):
        cv, W, H = self.cv, self.W - 2, self.H - 2
        cx = W / 2
        cv.delete("all")
        # soft peach glow behind the logo
        for i in range(7):
            r = 150 - i * 18
            cv.create_oval(cx - r, 82 - r * 0.8, cx + r, 82 + r * 0.8, outline="",
                           fill=_mix("#FFFFFF", "#FFE7DA", (i + 1) / 7))
        # logo badge: pops in, then breathes slowly
        s = self._pop(t / 0.6) * (1 + 0.015 * math.sin(t * 3)) if t > 0.6 else self._pop(t / 0.6)
        half = 34 * max(0.01, s)
        _rounded_rect(cv, cx - half, 82 - half, cx + half, 82 + half, 16 * max(0.2, s),
                      fill=THEME["accent"], outline="")
        peak = [(4, 15), (9, 6), (13, 12), (15, 9), (17, 15)]
        cv.create_polygon([c for px, py in peak
                           for c in (cx + (px - 10.5) * 4.2 * s, 82 + (py - 10.5) * 4.2 * s)],
                          fill=THEME["white"], outline="")
        # title and tagline fade up
        k1 = max(0.0, min(1.0, (t - 0.35) / 0.4))
        cv.create_text(cx, 150 + (1 - k1) * 10, text="Summit", font=(UI_FONT, 24, "bold"),
                       fill=_mix(THEME["bg"], THEME["text"], k1))
        k2 = max(0.0, min(1.0, (t - 0.6) / 0.4))
        cv.create_text(cx, 180, text="Translate, subtitle, and dub.", font=(UI_FONT, 10),
                       fill=_mix(THEME["bg"], THEME["muted"], k2))
        # progress bar with a moving sheen
        bw, bh, bx, by = 260, 6, cx - 130, 214
        _rounded_rect(cv, bx, by, bx + bw, by + bh, bh / 2, fill=THEME["card_alt"], outline="")
        fw = max(bh, bw * min(1.0, self._shown))
        _rounded_rect(cv, bx, by, bx + fw, by + bh, bh / 2, fill=THEME["accent"], outline="")
        if fw > 40 and not self._closing:
            sw = 34
            sx = bx + 3 + (fw - sw - 6) * (0.5 - 0.5 * math.cos((t * 0.9 % 1.0) * 2 * math.pi))
            _rounded_rect(cv, sx, by + 1, sx + sw, by + bh - 1, 2,
                          fill=_mix(THEME["accent"], "#FFFFFF", 0.4), outline="")
        cv.create_text(cx, 238, text=self._text, font=(UI_FONT, 8),
                       fill=_mix(THEME["bg"], THEME["muted"], k2))
        cv.create_text(W - 14, H - 12, text="Made by: Nat 2026 ©", font=(UI_FONT, 8),
                       anchor="se", fill=_mix(THEME["bg"], THEME["placeholder"], k2))


def start_app():
    splash = Splash()
    failure = []

    def work():
        try:
            preload(splash.on_progress)
        except BaseException as e:  # e.g. a package is not installed
            failure.append(e)
            splash.on_progress(1.0, "Problem while loading")
        finally:
            splash.loaded()

    threading.Thread(target=work, daemon=True).start()
    splash.mainloop()  # returns once the splash has faded out

    if failure:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Summit", "Summit could not load a required package:\n\n"
                      f"{failure[0]}\n\nOpen a terminal in the app folder and run:\n"
                      "pip install -r requirements.txt")
        return
    SummitApp().mainloop()


if __name__ == "__main__":
    start_app()