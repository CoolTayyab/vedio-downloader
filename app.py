from flask import Flask, request, send_file, jsonify, render_template
from flask_cors import CORS
import yt_dlp
import uuid, os, glob, time, threading, webbrowser, zipfile, re
from collections import defaultdict

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

MAX_PLAYLIST_SIZE = 25  # max videos per batch

BASE_OPTS = {
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

# ---- simple rate limit: 5 requests / 60 s per IP ----
hits = defaultdict(list)
def is_rate_limited(ip, limit=5, window=60):
    now = time.time()
    hits[ip] = [t for t in hits[ip] if now - t < window]
    if len(hits[ip]) >= limit:
        return True
    hits[ip].append(now)
    return False

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

# Google Search Console ownership verification (must be served at the site root)
@app.route("/google7f332598abb08dc8.html")
def google_verification():
    return "google-site-verification: google7f332598abb08dc8.html", 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/api/info", methods=["POST"])
def api_info():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL"}), 400
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
    if is_rate_limited(ip):
        return jsonify({"error": "Too many requests. Please wait a minute."}), 429
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL"}), 400
    media_type = data.get("type", "mp4")
    quality = data.get("quality", "best")
    start, end = data.get("start"), data.get("end")
    subs = data.get("subs", "")
    if subs not in SUB_LANGS:
        subs = ""
    job_id = uuid.uuid4().hex
    set_job(job_id, status="downloading", percent=0, detail="Starting...")
    threading.Thread(target=run_download_job, args=(job_id, url, media_type, quality, start, end, subs), daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/api/cancel/<job_id>", methods=["POST"])
def cancel(job_id):
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
    if is_rate_limited(ip):
        return jsonify({"error": "Too many requests. Please wait a minute."}), 429
    url = request.args.get("url", "").strip()
    media_type = request.args.get("type", "mp4")
    quality = request.args.get("quality", "best")
    if not url:
        return jsonify({"error": "No URL"}), 400
    job_id = uuid.uuid4().hex
    try:
        opts = dict(BASE_OPTS, format=build_format(media_type, quality),
                    outtmpl=os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s"))
        if media_type == "mp3":
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
        else:
            opts["merge_output_format"] = "mp4"
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        title = safe_name(info.get("title"))
        path = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))[0]
        ext = os.path.splitext(path)[1]
        if ext.lower() not in ALLOWED_EXTS:
            remove_job_files(job_id)
            raise Exception("unsupported url")
        resp = send_file(path, as_attachment=True, download_name=f"{title}{ext}")
        def cleanup():
            time.sleep(30)
            try: os.remove(path)
            except OSError: pass
        threading.Thread(target=cleanup, daemon=True).start()
        return resp
    except Exception as ex:
        return jsonify({"error": friendly_error_message(str(ex))}), 400

def open_browser():
    webbrowser.open("http://127.0.0.1:5000")

if __name__ == "__main__":
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        threading.Timer(1.0, open_browser).start()
    app.run(debug=True)
