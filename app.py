import os
import re
import uuid
import glob
import json
import subprocess
import threading
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", os.path.join(os.path.dirname(__file__), "downloads"))
COOKIES_FILE  = os.environ.get("COOKIES_FILE",  os.path.join(os.path.dirname(__file__), "data", "cookies.txt"))
CACHE_DIR     = os.environ.get("YTDLP_CACHE_DIR", os.path.join(os.path.dirname(__file__), "data", "yt-dlp-cache"))

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

jobs = {}

COOKIES_BROWSER = os.environ.get("COOKIES_BROWSER", "").strip()

# OAuth2 device-flow state (in-memory, one flow at a time)
_oauth = {"status": "idle", "code": None, "url": None, "error": None}
_oauth_lock = threading.Lock()


def base_ytdlp_args():
    """Common yt-dlp args added to every command."""
    return ["--cache-dir", CACHE_DIR]


def anti_bot_args():
    """Args that bypass YouTube bot-detection.

    After OAuth2 auth the cache contains a valid token — yt-dlp uses it
    automatically.  cookies.txt / COOKIES_BROWSER are added on top if present.
    """
    args = ["--extractor-args", "youtube:player_client=web,tv_embedded,ios,mweb"]
    if COOKIES_BROWSER:
        args += ["--cookies-from-browser", COOKIES_BROWSER]
    elif os.path.exists(COOKIES_FILE):
        args += ["--cookies", COOKIES_FILE]
    return args


# ── download job ──────────────────────────────────────────────────────────────

def run_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    cmd = ["yt-dlp", "--no-playlist", "-o", out_template] + base_ytdlp_args() + anti_bot_args()

    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            job["status"] = "error"
            job["error"] = result.stderr.strip().split("\n")[-1]
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        job["status"] = "done"
        job["file"] = chosen
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:20].strip()
            job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            job["filename"] = os.path.basename(chosen)
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "Download timed out (5 min limit)"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


# ── OAuth2 device flow ────────────────────────────────────────────────────────

def _run_oauth():
    """Background thread: runs yt-dlp OAuth2 and captures the device code."""
    with _oauth_lock:
        _oauth.update({"status": "pending", "code": None, "url": None, "error": None})

    # Use a throwaway video just to trigger the auth flow.
    # Requires the yt-dlp-youtube-oauth2 plugin (in requirements.txt).
    cmd = [
        "yt-dlp",
        "--username", "oauth2", "--password", "",
        "--cache-dir", CACHE_DIR,
        "--no-playlist", "-j",
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",  # "Me at the zoo" — first YT video
    ]

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1
        )

        stderr_lines = []
        for line in proc.stderr:
            line = line.strip()
            if line:
                stderr_lines.append(line)
            # yt-dlp prints something like:
            # "Please open https://www.google.com/device in your browser and enter: XXXX-XXXX"
            code_m = re.search(r'\b([A-Z0-9]{4}-[A-Z0-9]{4})\b', line)
            if code_m:
                with _oauth_lock:
                    _oauth["code"] = code_m.group(1)
                    _oauth["url"]  = "https://www.google.com/device"
                    _oauth["status"] = "waiting_user"
            if "Token has been written" in line or "Saving token" in line or "oauth2_token" in line:
                with _oauth_lock:
                    _oauth["status"] = "done"

        proc.wait(timeout=300)

        with _oauth_lock:
            if proc.returncode == 0 or _oauth["status"] == "done":
                _oauth["status"] = "done"
            elif _oauth["status"] not in ("done",):
                _oauth["status"] = "error"
                # Surface the actual yt-dlp error instead of a generic message
                last_error = next(
                    (l for l in reversed(stderr_lines) if "ERROR" in l or "error" in l.lower()),
                    stderr_lines[-1] if stderr_lines else "Authentication failed or timed out"
                )
                _oauth["error"] = last_error
    except Exception as e:
        with _oauth_lock:
            _oauth["status"] = "error"
            _oauth["error"] = str(e)


def _oauth_is_cached():
    """True if a valid OAuth2 token is already saved in the cache dir."""
    token_dir = os.path.join(CACHE_DIR, "youtube-oauth2.token")
    # yt-dlp stores the token as a JSON file
    for root, _, files in os.walk(CACHE_DIR):
        for f in files:
            if "oauth" in f.lower() or "token" in f.lower():
                return True
    return False


# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cmd = ["yt-dlp", "--no-playlist", "-j"] + base_ytdlp_args() + anti_bot_args() + [url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = json.loads(result.stdout)

        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                tbr = f.get("tbr") or 0
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            formats.append({"id": f["format_id"], "label": f"{height}p", "height": height})
        formats.sort(key=lambda x: x["height"], reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "downloading", "url": url, "title": title}

    t = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id))
    t.daemon = True
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({"status": job["status"], "error": job.get("error"), "filename": job.get("filename")})


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


# ── cookies ───────────────────────────────────────────────────────────────────

@app.route("/api/cookies-status")
def cookies_status():
    has_file = os.path.exists(COOKIES_FILE)
    return jsonify({
        "has_cookies": has_file or bool(COOKIES_BROWSER),
        "source": COOKIES_BROWSER if COOKIES_BROWSER else ("file" if has_file else "none"),
    })


@app.route("/api/upload-cookies", methods=["POST"])
def upload_cookies():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400
    content = f.read().decode("utf-8", errors="ignore")
    os.makedirs(os.path.dirname(COOKIES_FILE), exist_ok=True)
    with open(COOKIES_FILE, "w", encoding="utf-8") as fh:
        fh.write(content)
    return jsonify({"ok": True})


@app.route("/api/delete-cookies", methods=["POST"])
def delete_cookies():
    if os.path.exists(COOKIES_FILE):
        os.remove(COOKIES_FILE)
    return jsonify({"ok": True})


# ── OAuth2 ────────────────────────────────────────────────────────────────────

@app.route("/api/oauth/status")
def oauth_status():
    with _oauth_lock:
        state = dict(_oauth)
    state["cached"] = _oauth_is_cached()
    return jsonify(state)


@app.route("/api/oauth/start", methods=["POST"])
def oauth_start():
    with _oauth_lock:
        if _oauth["status"] in ("pending", "waiting_user"):
            return jsonify({"error": "OAuth flow already in progress"}), 400
    t = threading.Thread(target=_run_oauth, daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/api/oauth/revoke", methods=["POST"])
def oauth_revoke():
    """Delete cached OAuth token."""
    deleted = False
    for root, dirs, files in os.walk(CACHE_DIR):
        for f in files:
            if "oauth" in f.lower() or "token" in f.lower():
                os.remove(os.path.join(root, f))
                deleted = True
    with _oauth_lock:
        _oauth.update({"status": "idle", "code": None, "url": None, "error": None})
    return jsonify({"ok": True, "deleted": deleted})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0")
    app.run(host=host, port=port)
