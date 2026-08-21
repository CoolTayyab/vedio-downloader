from flask import Flask, request, send_file, jsonify, render_template
from flask_cors import CORS
import yt_dlp
import uuid
import os
import glob
import time
from collections import defaultdict

app = Flask(__name__)
CORS(app)  # allows the browser extension (different origin) to call this API

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

# --- very basic per-IP rate limiting (in-memory, resets on restart) ---
# Good enough to stop a single user hammering the server; NOT a substitute
# for real infrastructure (flask-limiter + Redis) if this gets real traffic.
RATE_LIMIT_WINDOW = 60      # seconds
RATE_LIMIT_MAX = 5          # requests per window per IP
request_log = defaultdict(list)


def is_rate_limited(ip: str) -> bool:
    now = time.time()
    request_log[ip] = [t for t in request_log[ip] if now - t < RATE_LIMIT_WINDOW]
    if len(request_log[ip]) >= RATE_LIMIT_MAX:
        return True
    request_log[ip].append(now)
    return False


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


def run_download(url: str, media_type: str, quality: str):
    """Shared download logic used by both the JSON API and the GET endpoint."""
    file_id = str(uuid.uuid4())
    outtmpl = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")

    ydl_opts = {
        **BASE_OPTS,
        "outtmpl": outtmpl,
        "format": build_format(media_type, quality),
    }

    if media_type == "mp3":
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        ydl_opts["merge_output_format"] = "mp4"

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

    return final_path, download_name


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    url = (request.json or {}).get("url")
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    try:
        with yt_dlp.YoutubeDL({**BASE_OPTS}) as ydl:
            info = ydl.extract_info(url, download=False)
            return jsonify({
                "title": info.get("title"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "uploader": info.get("uploader"),
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- used by your own website (index.html) ---
@app.route("/api/download", methods=["POST"])
def download_post():
    if is_rate_limited(request.remote_addr):
        return jsonify({"error": "Too many requests. Please wait a minute and try again."}), 429

    data = request.json or {}
    url = data.get("url")
    media_type = data.get("type", "mp4")
    quality = data.get("quality", "best")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        final_path, download_name = run_download(url, media_type, quality)
        response = send_file(final_path, as_attachment=True, download_name=download_name)

        @response.call_on_close
        def cleanup():
            try:
                os.remove(final_path)
            except OSError:
                pass

        return response
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- used by the browser extension: chrome.downloads.download() only works
# with GET-able URLs, so the extension hits this endpoint directly ---
@app.route("/download", methods=["GET"])
def download_get():
    if is_rate_limited(request.remote_addr):
        return jsonify({"error": "Too many requests. Please wait a minute and try again."}), 429

    url = request.args.get("url")
    media_type = request.args.get("type", "mp4")
    quality = request.args.get("quality", "best")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        final_path, download_name = run_download(url, media_type, quality)
        response = send_file(final_path, as_attachment=True, download_name=download_name)

        @response.call_on_close
        def cleanup():
            try:
                os.remove(final_path)
            except OSError:
                pass

        return response
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True)
