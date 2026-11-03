from flask import Flask, request, send_file, jsonify, render_template
from flask_cors import CORS
import yt_dlp
import uuid, os, glob, time, threading, webbrowser, zipfile, re, shutil, socket, ipaddress
from urllib.parse import urlparse
from collections import defaultdict
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # API requests are tiny; reject big bodies

# Behind Railway/Back4app/Render the real client IP is in X-Forwarded-For.
# Only trust it when TRUST_PROXY=1 (set in the Dockerfile), otherwise anyone could spoof it.
if os.environ.get("TRUST_PROXY") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# The web page uses same-origin requests (no CORS needed). Only the browser extension may call the API cross-origin.
CORS(app, resources={r"/api/*": {"origins": [re.compile(r"^chrome-extension://[a-p]{32}$")]}})

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

MAX_PLAYLIST_SIZE = 25  # max videos per batch

class QuietLogger:
    """Keep server logs short: no download-progress spam, one trimmed line per error."""
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg):
        print("[yt-dlp]", str(msg).replace("\n", " ")[:220], flush=True)

BASE_OPTS = {
    "logger": QuietLogger(),
    "noprogress": True,
    "socket_timeout": 30,
    "max_filesize": 500 * 1024 * 1024,  # 500 MB per file

    "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    "noplaylist": True,
    "quiet": True,
    # Do NOT use the "generic" extractor: it would download any direct file link
    # (.exe, .apk, .zip ...) and serve it from this domain -> Google "harmful downloads" flag.
    "allowed_extractors": ["default", "-generic"],
}

# Only these file types may ever be served to users
ALLOWED_EXTS = {".mp4", ".mp3", ".m4a", ".webm", ".mkv", ".srt", ".zip"}

# ---- optional YouTube cookies (free fix for "Sign in to confirm you're not a bot") ----
# Put the cookies.txt content, base64-encoded, in the environment variable YT_COOKIES_B64
# (never commit the cookies file to GitHub).
import base64
COOKIE_PATH = "/tmp/yt_cookies.txt" if os.name != "nt" else os.path.join(os.environ.get("TEMP", "."), "yt_cookies.txt")
_b64 = os.environ.get("YT_COOKIES_B64", "").strip()
if _b64:
    try:
        with open(COOKIE_PATH, "wb") as _f:
            _f.write(base64.b64decode(_b64))
        BASE_OPTS["cookiefile"] = COOKIE_PATH
        # the android client ignores cookies, so use yt-dlp's default clients when cookies are set
        BASE_OPTS.pop("extractor_args", None)
    except Exception as _e:
        print("Could not load YT_COOKIES_B64:", _e)

# ---- rate limits per IP and per kind of request ----
hits = defaultdict(list)
hits_lock = threading.Lock()
def is_rate_limited(ip, limit=5, window=60, bucket="download"):
    now = time.time()
    key = (bucket, ip)
    with hits_lock:
        hits[key] = [t for t in hits[key] if now - t < window]
        if len(hits[key]) >= limit:
            return True
        hits[key].append(now)
        return False

# ---- limits against overload ----
MAX_ACTIVE_JOBS = int(os.environ.get("MAX_ACTIVE_JOBS", "3"))
direct_slots = threading.BoundedSemaphore(MAX_ACTIVE_JOBS)  # for the extension's /download route
MIN_FREE_DISK = 1024 * 1024 * 1024  # refuse new jobs when less than 1 GB is free

ALLOWED_TYPES = {"mp4", "mp3", "srt"}
ALLOWED_QUALITIES = {"best", "1080p", "720p", "480p", "360p"}
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")

def server_busy():
    """True when too many downloads are running or the disk is almost full."""
    with jobs_lock:
        active = sum(1 for j in jobs.values() if j.get("status") in ("downloading", "processing"))
    if active >= MAX_ACTIVE_JOBS:
        return True
    try:
        return shutil.disk_usage(DOWNLOAD_DIR).free < MIN_FREE_DISK
    except OSError:
        return False

def check_url(url):
    """Return an error message if the URL is unsafe (not http/https, or points to a private/internal address)."""
    if not url or len(url) > 2048:
        return "Please paste a valid link."
    try:
        u = urlparse(url)
    except ValueError:
        return "Please paste a valid link."
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        return "Please paste a valid http or https link."
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return "This link's website could not be found. Please check the link."
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return "This link is not allowed."
    return None

jobs = {}
jobs_lock = threading.Lock()

def set_job(job_id, **kw):
    with jobs_lock:
        j = jobs.setdefault(job_id, {"created": time.time()})
        if j.get("cancel") and kw.get("status") in ("downloading", "processing"):
            kw.pop("status")  # never overwrite a cancel request
        j.update(kw)

def is_cancelled(job_id):
    with jobs_lock:
        return bool(jobs.get(job_id, {}).get("cancel"))

def check_cancel(job_id):
    if is_cancelled(job_id):
        raise yt_dlp.utils.DownloadCancelled("cancelled")

def remove_job_files(job_id):
    for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}*")):
        try: os.remove(f)
        except OSError: pass

def janitor():
    """Every 5 min: delete leftover files/jobs older than 60 min (abandoned downloads)."""
    while True:
        time.sleep(300)
        now = time.time()
        for f in glob.glob(os.path.join(DOWNLOAD_DIR, "*")):
            try:
                if now - os.path.getmtime(f) > 3600:
                    os.remove(f)
            except OSError:
                pass
        with jobs_lock:
            for jid in [j for j, v in jobs.items() if now - v.get("created", now) > 3600]:
                jobs.pop(jid, None)
        with hits_lock:
            for k in [k for k, v in hits.items() if not v or now - v[-1] > 3600]:
                hits.pop(k, None)

threading.Thread(target=janitor, daemon=True).start()

def friendly_error_message(raw):
    r = (raw or "").lower()
    if "not a bot" in r or "confirm you" in r:
        return "YouTube is temporarily blocking this server, so this link cannot be downloaded right now. Please try again later or try a link from another site."
    if "no subtitles" in r:
        return "No subtitles are available for this video."
    if "private" in r:
        return "This video is private and cannot be downloaded."
    if "unavailable" in r or "removed" in r or "deleted" in r:
        return "This video is unavailable. It may have been removed."
    if "sign in" in r or "age" in r and "restrict" in r:
        return "This video is age-restricted or requires login, so it cannot be downloaded."
    if "copyright" in r:
        return "This video is blocked due to copyright."
    if "403" in r:
        return "The site refused the request (403). Please try again in a moment."
    if "404" in r:
        return "Video not found. Please check the link."
    if "unsupported url" in r:
        return "This link is not supported. Please paste a direct video link."
    if "ffmpeg" in r:
        return "Server is missing a required tool (ffmpeg) to process this video."
    if "network" in r or "timed out" in r or "connection" in r:
        return "Network problem while contacting the site. Please try again."
    if "live" in r:
        return "Live streams cannot be downloaded."
    return "Something went wrong while processing this link. Please check the URL and try again."

def build_format(media_type, quality):
    if media_type == "mp3":
        return "bestaudio/best"
    if quality == "best":
        return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    h = quality.replace("p", "")
    return f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/best[height<={h}][ext=mp4]/best[height<={h}]"

SUB_LANGS = {"en", "ur", "hi", "ar", "es", "fr", "de", "pt", "ru", "tr", "id", "bn"}

def apply_media_opts(opts, media_type, subs=""):
    """Format options for mp4 / mp3. Subtitles are fetched in a separate pass (download_subs)."""
    if media_type == "mp3":
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    else:
        opts["merge_output_format"] = "mp4"
    return opts

SUB_CLIENT_ATTEMPTS = [["tv"], ["web_embedded"], ["tv_simply"], ["ios"], ["web", "mweb"]]

def download_subs(url, outtmpl, subs, job_id, playlist_items=None):
    """Separate pass for subtitles. web/mweb need a PO token for subtitles, so try other
    clients first and stop as soon as an .srt file exists."""
    last_err = None
    for clients in SUB_CLIENT_ATTEMPTS:
        opts = dict(BASE_OPTS,
                    extractor_args={"youtube": {"player_client": clients}},
                    skip_download=True, ignore_no_formats_error=True,
                    writesubtitles=True, writeautomaticsub=True,
                    subtitleslangs=[subs], subtitlesformat="srt/vtt/best", outtmpl=outtmpl,
                    postprocessors=[{"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"}])
        if playlist_items:
            opts.update(noplaylist=False, playlist_items=playlist_items, ignoreerrors=True)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
            last_err = None
        except yt_dlp.utils.DownloadCancelled:
            raise
        except Exception as ex:
            last_err = ex
            continue
        if glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}*.srt")):
            return info
    if last_err:
        raise last_err
    return info

def safe_name(s):
    return re.sub(r'[\\/:*?"<>|]', "_", s or "download").strip()[:80] or "download"

def resolve_range(total, start, end):
    """Return (start, end) validated, or raise ValueError with a friendly message."""
    try:
        start = int(start) if start not in (None, "") else 1
        end = int(end) if end not in (None, "") else min(total, start + MAX_PLAYLIST_SIZE - 1)
    except (TypeError, ValueError):
        raise ValueError("Start and end must be numbers.")
    if start < 1 or end < start:
        raise ValueError("Invalid range: start must be 1 or more and end must not be less than start.")
    if start > total:
        raise ValueError(f"This playlist only has {total} videos.")
    end = min(end, total)
    if end - start + 1 > MAX_PLAYLIST_SIZE:
        raise ValueError(f"You can download at most {MAX_PLAYLIST_SIZE} videos at a time. Please choose a smaller range.")
    return start, end

def run_download_job(job_id, url, media_type, quality, start, end, subs=""):
    try:
        set_job(job_id, status="downloading", percent=0, detail="Checking link...")
        probe_opts = dict(BASE_OPTS, noplaylist=False, extract_flat=True)
        with yt_dlp.YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        is_playlist = info.get("_type") == "playlist" and info.get("entries")
        if is_playlist:
            entries = [e for e in info["entries"] if e]
            total = info.get("playlist_count") or len(entries)
            s, e = resolve_range(total, start, end)
            run_playlist_download_job(job_id, url, media_type, quality, info.get("title"), s, e, subs)
        else:
            run_single_download_job(job_id, url, media_type, quality, subs)
    except yt_dlp.utils.DownloadCancelled:
        remove_job_files(job_id)
        set_job(job_id, status="cancelled", detail="Cancelled")
    except ValueError as ve:
        set_job(job_id, status="error", error=str(ve))
    except Exception as ex:
        remove_job_files(job_id)
        if is_cancelled(job_id):
            set_job(job_id, status="cancelled", detail="Cancelled")
        else:
            set_job(job_id, status="error", error=friendly_error_message(str(ex)))

def run_single_download_job(job_id, url, media_type, quality, subs=""):
    def hook(d):
        check_cancel(job_id)
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            pct = (d.get("downloaded_bytes", 0) / total * 100) if total else 0
            set_job(job_id, status="downloading", percent=round(pct, 1), detail="")
        elif d["status"] == "finished":
            set_job(job_id, status="processing", percent=100, detail="Processing...")
    opts = dict(BASE_OPTS,
                format=build_format(media_type, quality),
                outtmpl=os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s"),
                progress_hooks=[hook])
    apply_media_opts(opts, media_type, subs)
    title = "download"
    if media_type != "srt":
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = safe_name(info.get("title"))
    if subs or media_type == "srt":
        lang = subs or "en"
        set_job(job_id, status="processing", percent=100, detail="Fetching subtitles...")
        try:
            sinfo = download_subs(url, os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s"), lang, job_id)
            if media_type == "srt":
                title = safe_name(sinfo.get("title"))
        except Exception:
            if media_type == "srt":
                raise
    files = [f for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*")) if not f.endswith(".zip")]
    sub_files = [f for f in files if f.endswith(".srt")]
    media_files = [f for f in files if f not in sub_files and not f.endswith((".part", ".ytdl", ".vtt"))
                   and os.path.splitext(f)[1].lower() in ALLOWED_EXTS]
    note = ""
    if (subs or media_type == "srt") and not sub_files:
        note = "No subtitles are available for this video."
    if media_type == "srt":
        if not sub_files:
            raise Exception("No subtitles are available for this video.")
        path = sub_files[0]
        set_job(job_id, status="finished", percent=100, file_path=path,
                download_name=f"{title}{path[len(os.path.join(DOWNLOAD_DIR, job_id)):]}", detail="", note="")
        return
    if not media_files:
        raise Exception("download failed")
    media = media_files[0]
    ext = os.path.splitext(media)[1]
    if sub_files:
        zip_path = os.path.join(DOWNLOAD_DIR, f"{job_id}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
            z.write(media, arcname=f"{title}{ext}")
            for sf in sub_files:
                z.write(sf, arcname=f"{title}{sf[len(os.path.join(DOWNLOAD_DIR, job_id)):]}")
        for f in files:
            try: os.remove(f)
            except OSError: pass
        set_job(job_id, status="finished", percent=100, file_path=zip_path,
                download_name=f"{title}.zip", detail="", note="")
    else:
        set_job(job_id, status="finished", percent=100, file_path=media,
                download_name=f"{title}{ext}", detail="", note=note)

def run_playlist_download_job(job_id, url, media_type, quality, playlist_title, start, end, subs=""):
    batch = end - start + 1
    def hook(d):
        check_cancel(job_id)
        idx = d.get("info_dict", {}).get("playlist_index") or start
        pos = max(1, min(batch, idx - start + 1))
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            frac = (d.get("downloaded_bytes", 0) / total) if total else 0
            pct = ((pos - 1) + frac) / batch * 100
            set_job(job_id, status="downloading", percent=round(pct, 1), detail=f"Video {pos} of {batch}")
        elif d["status"] == "finished":
            set_job(job_id, detail=f"Video {pos} of {batch} - processing")
    opts = dict(BASE_OPTS,
                noplaylist=False,
                playlist_items=f"{start}-{end}",
                format=build_format(media_type, quality),
                outtmpl=os.path.join(DOWNLOAD_DIR, f"{job_id}_%(playlist_index)s - %(title)s.%(ext)s"),
                progress_hooks=[hook],
                ignoreerrors=True)
    apply_media_opts(opts, media_type, subs)
    stopped = False
    try:
        if media_type != "srt":
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadCancelled:
        stopped = True
    if (subs or media_type == "srt") and not stopped:
        set_job(job_id, detail="Fetching subtitles...")
        try:
            download_subs(url, os.path.join(DOWNLOAD_DIR, f"{job_id}_%(playlist_index)s - %(title)s.%(ext)s"),
                          subs or "en", job_id, f"{start}-{end}")
        except Exception:
            pass
    with jobs_lock:
        discard = bool(jobs.get(job_id, {}).get("discard"))
    if stopped and discard:
        raise yt_dlp.utils.DownloadCancelled("cancelled")
    # keep only fully finished files (skip .part, fragments like .f137.mp4, temp files)
    bad = re.compile(r"(\.part|\.ytdl|\.temp|\.vtt|\.f\d+(-\w+)?\.\w+|\.zip)$", re.I)
    files = [f for f in sorted(glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}_*"))) if not bad.search(f) and os.path.splitext(f)[1].lower() in ALLOWED_EXTS]
    if not files:
        if stopped:
            raise yt_dlp.utils.DownloadCancelled("cancelled")
        raise Exception("download failed")
    set_job(job_id, status="processing", percent=100, detail="Creating ZIP...")
    zip_path = os.path.join(DOWNLOAD_DIR, f"{job_id}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
        for f in files:
            z.write(f, arcname=os.path.basename(f)[len(job_id) + 1:])

    for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}_*")):
        try: os.remove(f)
        except OSError: pass
    name = f"{safe_name(playlist_title)} ({start}-{end}){' - partial' if stopped else ''}.zip"
    set_job(job_id, status="finished", percent=100, file_path=zip_path, download_name=name, detail="")

@app.route("/")
def index():
    return render_template("index.html")

# ---- simple info pages (About / Privacy / Terms / Contact) ----
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "your-email@example.com")  # set CONTACT_EMAIL on the host, or edit here

PAGES = {
    "about": ("About", """
<p>This is a free online tool that lets you save videos and audio from public links as MP4, MP3 or subtitle (SRT) files.</p>
<p>You paste a public link, choose a format and quality, and the file is prepared and sent to your browser's Downloads folder.</p>
<p><strong>What this site does not do:</strong> it does not ask for passwords or payment details, and it does not install any software on your device. You can optionally create a simple profile (a name and an optional email); it is saved only in your own browser and is not sent to our server.</p>
<p>Only download content you own or have permission to download.</p>"""),
    "privacy": ("Privacy Policy", """
<p>You do not need an account to use the tool. On your first visit you may create an optional profile (a name and an optional email) and the tool keeps a list of your recent downloads. <strong>Both are stored only in your own browser (local storage) on your device and are never sent to our server.</strong> You can delete them at any time from the profile window in the top-right corner, or by clearing your browser data.</p>
<p><strong>Links you paste</strong> are sent to our server only to prepare your file. Prepared files are deleted automatically shortly after your download finishes (and in any case within about an hour).</p>
<p><strong>Logs:</strong> like most servers, our hosting may keep technical logs such as IP address and request time for security and abuse prevention.</p>
<p><strong>Cookies and advertising:</strong> this site does not currently set tracking cookies. If advertising is added in the future, this page will be updated to describe it.</p>
<p>If you have questions, contact us at the email on the Contact page.</p>"""),
    "terms": ("Terms of Use", """
<p>By using this tool you agree to the following:</p>
<ul>
<li>Use it only for content you own, content that is in the public domain, or content you have permission to download.</li>
<li>You are responsible for following the terms of the website you download from and the copyright laws of your country.</li>
<li>Do not use the tool to harm, overload or abuse the service. Requests may be rate-limited.</li>
<li>The service is provided "as is" without any guarantee that it will always be available or work for every link.</li>
</ul>
<p>We may block links or users at any time. We respect copyright; if you believe content is being misused, please contact us.</p>"""),
    "contact": ("Contact", """
<p>For questions, copyright concerns or feedback, email us at:</p>
<p><strong>{email}</strong></p>"""),
}

PAGE_TEMPLATE = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title} - Video Downloader</title>
<style>body{{margin:0;min-height:100vh;font-family:'Segoe UI',Arial,sans-serif;color:#f1f2f6;background:#0f1224;padding:24px}}
.box{{max-width:720px;margin:0 auto;background:#171b2e;border-radius:16px;padding:28px;line-height:1.6}}
h1{{margin-top:0;background:linear-gradient(90deg,#7c5cff,#22d3ee);-webkit-background-clip:text;background-clip:text;color:transparent}}
a{{color:#22d3ee}}nav{{margin-top:22px;font-size:.9rem}}nav a{{margin-right:14px}}</style></head><body>
<div class="box"><h1>{title}</h1>{body}
<nav><a href="/">Home</a><a href="/about">About</a><a href="/privacy">Privacy</a><a href="/terms">Terms</a><a href="/contact">Contact</a></nav></div></body></html>"""

@app.route("/<page>")
def info_page(page):
    if page not in PAGES:
        return jsonify({"error": "Not found"}), 404
    title, body = PAGES[page]
    return PAGE_TEMPLATE.format(title=title, body=body.replace("{email}", CONTACT_EMAIL)), 200, {"Content-Type": "text/html; charset=utf-8"}

# Google Search Console ownership verification (must be served at the site root)
@app.route("/google7f332598abb08dc8.html")
def google_verification():
    return "google-site-verification: google7f332598abb08dc8.html", 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/api/info", methods=["POST"])
def api_info():
    if is_rate_limited(request.remote_addr, limit=20, window=60, bucket="info"):
        return jsonify({"error": "Too many requests. Please wait a minute."}), 429
    url = str((request.get_json(silent=True) or {}).get("url", "")).strip()
    if not url:
        return jsonify({"error": "No URL"}), 400
    bad = check_url(url)
    if bad:
        return jsonify({"error": bad}), 400
    try:
        opts = dict(BASE_OPTS, noplaylist=False, extract_flat=True)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if info.get("_type") == "playlist" and info.get("entries"):
            entries = [e for e in info["entries"] if e]
            count = info.get("playlist_count") or len(entries)
            thumb = info.get("thumbnail")
            if not thumb and entries:
                thumbs = entries[0].get("thumbnails") or []
                thumb = thumbs[-1]["url"] if thumbs else None
            return jsonify({"type": "playlist", "title": info.get("title"), "count": count,
                            "thumbnail": thumb, "too_large": count > MAX_PLAYLIST_SIZE,
                            "max_batch": MAX_PLAYLIST_SIZE})
        return jsonify({"type": "video", "title": info.get("title"), "thumbnail": info.get("thumbnail"),
                        "duration": info.get("duration"), "uploader": info.get("uploader")})
    except Exception as ex:
        return jsonify({"error": friendly_error_message(str(ex))}), 400

@app.route("/api/start-download", methods=["POST"])
def start_download():
    ip = request.remote_addr
    if is_rate_limited(ip, bucket="start"):
        return jsonify({"error": "Too many requests. Please wait a minute."}), 429
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    if not url:
        return jsonify({"error": "No URL"}), 400
    media_type = data.get("type", "mp4")
    quality = data.get("quality", "best")
    if media_type not in ALLOWED_TYPES or quality not in ALLOWED_QUALITIES:
        return jsonify({"error": "Invalid options."}), 400
    bad = check_url(url)
    if bad:
        return jsonify({"error": bad}), 400
    start, end = data.get("start"), data.get("end")
    subs = data.get("subs", "")
    if subs not in SUB_LANGS:
        subs = ""
    if server_busy():
        return jsonify({"error": "The server is busy right now. Please try again in a minute."}), 503
    job_id = uuid.uuid4().hex
    set_job(job_id, status="downloading", percent=0, detail="Starting...")
    threading.Thread(target=run_download_job, args=(job_id, url, media_type, quality, start, end, subs), daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/api/cancel/<job_id>", methods=["POST"])
def cancel(job_id):
    if not JOB_ID_RE.match(job_id):
        return jsonify({"ok": False}), 400
    discard = request.args.get("discard") == "1"
    with jobs_lock:
        j = jobs.get(job_id)
        if j and j.get("status") not in ("finished", "error", "cancelled"):
            j["cancel"] = True
            j["discard"] = discard
            j["detail"] = "Cancelling..."
    return jsonify({"ok": True})

@app.route("/api/progress/<job_id>")
def progress(job_id):
    if not JOB_ID_RE.match(job_id):
        return jsonify({"status": "error", "error": "Unknown job"}), 404
    with jobs_lock:
        j = jobs.get(job_id)
        if not j:
            return jsonify({"status": "error", "error": "Unknown job"}), 404
        if j.get("status") == "cancelled":
            return jsonify({"status": "cancelled", "percent": 0, "error": None, "detail": "Cancelled"})
        return jsonify({"status": j.get("status"), "percent": j.get("percent", 0),
                        "error": j.get("error"), "detail": j.get("detail", ""),
                        "note": j.get("note", "")})

@app.route("/api/fetch/<job_id>")
def fetch(job_id):
    if not JOB_ID_RE.match(job_id):
        return jsonify({"error": "Not ready"}), 400
    with jobs_lock:
        j = jobs.get(job_id)
    if not j or j.get("status") != "finished":
        return jsonify({"error": "Not ready"}), 400
    path = j["file_path"]
    resp = send_file(path, as_attachment=True, download_name=j["download_name"])
    def cleanup():
        time.sleep(30)
        try: os.remove(path)
        except OSError: pass
        with jobs_lock:
            jobs.pop(job_id, None)
    threading.Thread(target=cleanup, daemon=True).start()
    return resp

# direct flow used by the Chrome extension (single video, no progress)
@app.route("/download")
def download():
    ip = request.remote_addr
    if is_rate_limited(ip, bucket="direct"):
        return jsonify({"error": "Too many requests. Please wait a minute."}), 429
    url = request.args.get("url", "").strip()
    media_type = request.args.get("type", "mp4")
    quality = request.args.get("quality", "best")
    if not url:
        return jsonify({"error": "No URL"}), 400
    bad = check_url(url)
    if bad:
        return jsonify({"error": bad}), 400
    if media_type not in ("mp4", "mp3") or quality not in ALLOWED_QUALITIES:
        return jsonify({"error": "Invalid options."}), 400
    if not direct_slots.acquire(blocking=False) or server_busy():
        try: direct_slots.release()
        except ValueError: pass
        return jsonify({"error": "The server is busy right now. Please try again in a minute."}), 503
    job_id = uuid.uuid4().hex
    try:
        opts = dict(BASE_OPTS, format=build_format(media_type, quality),
                    outtmpl=os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s"))
        apply_media_opts(opts, media_type)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        title = safe_name(info.get("title"))
        files = [f for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
                 if os.path.splitext(f)[1].lower() in ALLOWED_EXTS]
        if not files:
            raise Exception("unsupported url")
        path = files[0]
        ext = os.path.splitext(path)[1]
        resp = send_file(path, as_attachment=True, download_name=f"{title}{ext}")
        def cleanup():
            time.sleep(30)
            remove_job_files(job_id)
        threading.Thread(target=cleanup, daemon=True).start()
        return resp
    except Exception as ex:
        remove_job_files(job_id)
        return jsonify({"error": friendly_error_message(str(ex))}), 400
    finally:
        try: direct_slots.release()
        except ValueError: pass

@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    resp.headers.setdefault("Content-Security-Policy",
        "default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'")
    if request.is_secure:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp

def open_browser():
    webbrowser.open("http://127.0.0.1:5000")

if __name__ == "__main__":
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        threading.Timer(1.0, open_browser).start()
    app.run(debug=True)
