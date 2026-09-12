from flask import Flask, request, send_file, jsonify, render_template
from flask_cors import CORS
import yt_dlp
import uuid
import os
import glob
import time
import threading
import webbrowser
import zipfile
from collections import defaultdict

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

BASE_OPTS = {
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"]
        }
    },
    "noplaylist": True,
    "quiet": True,
}

RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = 5
request_log = defaultdict(list)

# ---------- In-memory job store for progress tracking ----------
# job_id -> { status: 'downloading'|'finished'|'error', percent: float,
#             file_path: str|None, download_name: str|None, error: str|None }
# Fine for a single-instance small app; not meant to survive server restarts
# or scale across multiple server processes.
jobs = {}
jobs_lock = threading.Lock()


def is_rate_limited(ip: str) -> bool:
    now = time.time()
    request_log[ip] = [t for t in request_log[ip] if now - t < RATE_LIMIT_WINDOW]
    if len(request_log[ip]) >= RATE_LIMIT_MAX:
        return True
    request_log[ip].append(now)
    return False


def friendly_error_message(raw_error: str) -> str:
    """Translates common yt-dlp/network error text into a short,
    user-friendly English message instead of showing raw technical output."""
    err = (raw_error or "").lower()

    if "private video" in err or "private" in err:
        return "This video is private and can't be downloaded."
    if "video unavailable" in err or "this video is unavailable" in err:
        return "This video is unavailable — it may have been removed or the link is incorrect."
    if ("sign in to confirm your age" in err) or ("age" in err and "restrict" in err):
        return "This video is age-restricted and can't be downloaded without an account."
    if "copyright" in err:
        return "This video has been taken down due to a copyright claim."
    if "403" in err or "forbidden" in err:
        return "The platform blocked this download. Please try again in a moment."
    if "404" in err or "not found" in err:
        return "This link couldn't be found — please check the URL and try again."
    if "unsupported url" in err or "no video formats found" in err:
        return "This link isn't supported, or no downloadable video was found."
    if "ffmpeg" in err and ("not found" in err or "not installed" in err):
        return "A required tool is missing on the server. Please try again later."
    if "network" in err or "timed out" in err or "connection" in err:
        return "A network error occurred — please check your connection and try again."
    if "unable to download webpage" in err or "http error" in err:
        return "Couldn't reach this link. Please double-check the URL."
    if "live event" in err or "is live" in err:
        return "Live streams can't be downloaded while they're still airing."

    # Fallback: don't show raw stack-trace-style text to the user.
    return "Something went wrong while processing this link. Please check the URL and try again."


def build_format(media_type: str, quality: str) -> str:
    if media_type == "mp3":
        return "bestaudio/best"
    if quality == "best" or not quality:
        return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    height = quality.replace("p", "")
    return (
        f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"best[height<={height}][ext=mp4]/best[height<={height}]"
    )


def run_download_job(job_id: str, url: str, media_type: str, quality: str):
    """Detects whether the link is a playlist or a single video, then
    hands off to the right handler."""
    try:
        with yt_dlp.YoutubeDL({**BASE_OPTS, "extract_flat": True, "noplaylist": False}) as probe:
            probe_info = probe.extract_info(url, download=False)
    except Exception as e:
        with jobs_lock:
            jobs[job_id].update({"status": "error", "error": friendly_error_message(str(e))})
        return

    is_playlist = bool(probe_info and probe_info.get("_type") == "playlist" and probe_info.get("entries"))

    if is_playlist:
        run_playlist_download_job(job_id, url, media_type, quality)
    else:
        run_single_download_job(job_id, url, media_type, quality)


def run_single_download_job(job_id: str, url: str, media_type: str, quality: str):
    """Runs in a background thread. Updates the shared `jobs` dict as
    yt-dlp reports progress, so the frontend can poll and show a bar."""

    def progress_hook(d):
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                return
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes", 0)
                if total:
                    job["percent"] = round(downloaded / total * 100, 1)
                job["status"] = "downloading"
            elif d.get("status") == "finished":
                # yt-dlp finished writing the raw stream; postprocessing
                # (merging/mp3 conversion) may still be running after this.
                job["percent"] = 95
                job["status"] = "processing"

    file_id = str(uuid.uuid4())
    outtmpl = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")

    ydl_opts = {
        **BASE_OPTS,
        "outtmpl": outtmpl,
        "format": build_format(media_type, quality),
        "progress_hooks": [progress_hook],
    }

    if media_type == "mp3":
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        ydl_opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

        base = os.path.splitext(filename)[0]
        matches = glob.glob(base + ".*")
        final_path = matches[0] if matches else filename

        safe_title = "".join(
            c for c in (info.get("title") or "video") if c.isalnum() or c in " -_"
        ).strip() or "video"
        ext = os.path.splitext(final_path)[1]
        download_name = f"{safe_title}{ext}"

        with jobs_lock:
            jobs[job_id].update({
                "status": "finished",
                "percent": 100,
                "file_path": final_path,
                "download_name": download_name,
            })
    except Exception as e:
        with jobs_lock:
            jobs[job_id].update({"status": "error", "error": friendly_error_message(str(e))})


def run_playlist_download_job(job_id: str, url: str, media_type: str, quality: str):
    """Downloads every video in a playlist, then zips them into one file.
    Progress is tracked as (videos finished + current video's own progress)
    divided by the total video count, so the bar moves smoothly across
    the whole playlist rather than jumping per video."""

    def progress_hook(d):
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                return
            info = d.get("info_dict") or {}
            idx = info.get("playlist_index") or 1
            count = info.get("playlist_count") or 1

            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes", 0)
                item_fraction = (downloaded / total) if total else 0
                overall = ((idx - 1) + item_fraction) / count * 100
                job["percent"] = round(min(overall, 99), 1)
                job["status"] = "downloading"
                job["detail"] = f"Video {idx} of {count}"
            elif d.get("status") == "finished":
                overall = idx / count * 100
                job["percent"] = round(min(overall, 99), 1)
                job["status"] = "processing"
                job["detail"] = f"Finished video {idx} of {count} — processing"

    outtmpl = os.path.join(DOWNLOAD_DIR, f"{job_id}_%(playlist_index)s - %(title)s.%(ext)s")
    ydl_opts = {
        **BASE_OPTS,
        "noplaylist": False,
        "outtmpl": outtmpl,
        "format": build_format(media_type, quality),
        "progress_hooks": [progress_hook],
    }

    if media_type == "mp3":
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        ydl_opts["merge_output_format"] = "mp4"

    try:
        with jobs_lock:
            jobs[job_id]["status"] = "downloading"
            jobs[job_id]["detail"] = "Starting playlist download..."

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        playlist_title = info.get("title") or "playlist"
        safe_title = "".join(
            c for c in playlist_title if c.isalnum() or c in " -_"
        ).strip() or "playlist"

        with jobs_lock:
            jobs[job_id]["status"] = "processing"
            jobs[job_id]["detail"] = "Packing files into a ZIP..."

        files = sorted(glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}_*.*")))
        files = [f for f in files if not f.endswith(".part")]

        if not files:
            raise RuntimeError("No videos were downloaded from this playlist.")

        zip_path = os.path.join(DOWNLOAD_DIR, f"{job_id}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                # Strip the "{job_id}_" prefix so filenames inside the
                # zip look clean (e.g. "1 - Song Name.mp3").
                arcname = os.path.basename(f).split(f"{job_id}_", 1)[-1]
                zf.write(f, arcname=arcname)

        for f in files:
            try:
                os.remove(f)
            except OSError:
                pass

        with jobs_lock:
            jobs[job_id].update({
                "status": "finished",
                "percent": 100,
                "file_path": zip_path,
                "download_name": f"{safe_title}.zip",
            })
    except Exception as e:
        with jobs_lock:
            jobs[job_id].update({"status": "error", "error": friendly_error_message(str(e))})



@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    url = (request.json or {}).get("url")
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    try:
        # extract_flat + noplaylist=False lets us detect a playlist link
        # cheaply (without fully resolving every video in it) while still
        # returning full details for an ordinary single-video link.
        flat_opts = {**BASE_OPTS, "extract_flat": True, "noplaylist": False}
        with yt_dlp.YoutubeDL(flat_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if info.get("_type") == "playlist" and info.get("entries"):
            entries = list(info.get("entries") or [])
            first_thumb = None
            if entries:
                thumbs = entries[0].get("thumbnails") or []
                first_thumb = entries[0].get("thumbnail") or (thumbs[-1]["url"] if thumbs else None)
            return jsonify({
                "type": "playlist",
                "title": info.get("title") or "Playlist",
                "count": len(entries),
                "thumbnail": first_thumb,
            })

        return jsonify({
            "type": "video",
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "uploader": info.get("uploader"),
        })
    except Exception as e:
        return jsonify({"error": friendly_error_message(str(e))}), 500


# ---------- Progress-based flow (used by the website) ----------
@app.route("/api/start-download", methods=["POST"])
def start_download():
    if is_rate_limited(request.remote_addr):
        return jsonify({"error": "Too many requests. Please wait a minute and try again."}), 429

    data = request.json or {}
    url = data.get("url")
    media_type = data.get("type", "mp4")
    quality = data.get("quality", "best")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"status": "downloading", "percent": 0, "file_path": None,
                         "download_name": None, "error": None}

    thread = threading.Thread(target=run_download_job, args=(job_id, url, media_type, quality))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/progress/<job_id>", methods=["GET"])
def get_progress(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "Unknown job"}), 404
        return jsonify({
            "status": job["status"],
            "percent": job["percent"],
            "error": job.get("error"),
            "detail": job.get("detail"),
        })


@app.route("/api/fetch/<job_id>", methods=["GET"])
def fetch_download(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job or job["status"] != "finished":
            return jsonify({"error": "File not ready"}), 400
        final_path = job["file_path"]
        download_name = job["download_name"]

    response = send_file(final_path, as_attachment=True, download_name=download_name)

    @response.call_on_close
    def cleanup():
        try:
            os.remove(final_path)
        except OSError:
            pass
        with jobs_lock:
            jobs.pop(job_id, None)

    return response


# ---------- Simple direct flow (used by the browser extension) ----------
@app.route("/download", methods=["GET"])
def download_get():
    if is_rate_limited(request.remote_addr):
        return jsonify({"error": "Too many requests. Please wait a minute and try again."}), 429

    url = request.args.get("url")
    media_type = request.args.get("type", "mp4")
    quality = request.args.get("quality", "best")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    file_id = str(uuid.uuid4())
    outtmpl = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")
    ydl_opts = {**BASE_OPTS, "outtmpl": outtmpl, "format": build_format(media_type, quality)}
    if media_type == "mp3":
        ydl_opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    else:
        ydl_opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

        base = os.path.splitext(filename)[0]
        matches = glob.glob(base + ".*")
        final_path = matches[0] if matches else filename
        safe_title = "".join(c for c in (info.get("title") or "video") if c.isalnum() or c in " -_").strip() or "video"
        ext = os.path.splitext(final_path)[1]
        download_name = f"{safe_title}{ext}"

        response = send_file(final_path, as_attachment=True, download_name=download_name)

        @response.call_on_close
        def cleanup():
            try:
                os.remove(final_path)
            except OSError:
                pass

        return response
    except Exception as e:
        return jsonify({"error": friendly_error_message(str(e))}), 500


def open_browser():
    webbrowser.open_new("http://127.0.0.1:5000")


if __name__ == "__main__":
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        threading.Timer(1.0, open_browser).start()
    app.run(debug=True)
