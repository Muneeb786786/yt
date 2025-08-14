#!/usr/bin/env python3
"""
video_api_singlefile.py
Single-file production-ready Flask app for video metadata & streaming using yt-dlp.

How to run (local dev):
    pip install -r requirements.txt
    export API_KEY="your_api_key_here"
    python video_api_singlefile.py           # runs Flask dev server (not for heavy production)

For production (Gunicorn):
    export API_KEY="your_api_key_here"
    gunicorn video_api_singlefile:app -b 0.0.0.0:5000 --workers 3 --threads 4

Environment variables:
    API_KEY         (optional but recommended) - expected API key for requests
    REDIS_URL       (optional) - redis://host:port/db
    RATE_LIMIT      (optional) - e.g. "100/hour" (string parsed into requests/hour)
    CACHE_TTL       (optional) - cache TTL in seconds (default 3600)
    YTDLP_QUIET     (optional) - '1'/'true' to silence yt-dlp console output
"""
import os
import time
import json
import tempfile
import threading
import shutil
from functools import wraps
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

from flask import Flask, request, jsonify, Response, stream_with_context, send_file, make_response

# External libs - ensure installed:
# pip install Flask yt-dlp pafy redis requests gunicorn
try:
    import yt_dlp
except Exception as e:
    raise RuntimeError("yt-dlp is required. Install with `pip install yt-dlp`") from e

# pafy is optional fallback
try:
    import pafy
    os.environ.setdefault("PAFY_BACKEND", "internal")
except Exception:
    pafy = None

# optional redis support for cache and rate-limiter
try:
    import redis
except Exception:
    redis = None

import requests

# -------------------------
# Configuration
# -------------------------
API_KEY = os.environ.get("mm")  # set this in Render environment
REDIS_URL = os.environ.get("REDIS_URL")
RATE_LIMIT = os.environ.get("RATE_LIMIT", "300/hour")  # default 300 requests per hour per IP
CACHE_TTL = int(os.environ.get("CACHE_TTL", "3600"))  # seconds
YTDLP_QUIET = os.environ.get("YTDLP_QUIET", "true").lower() in ("1", "true", "yes")
MAX_PROXY_SIZE = int(os.environ.get("MAX_PROXY_SIZE", str(250 * 1024 * 1024)))  # 250MB default
SERVER_NAME = os.environ.get("SERVER_NAME", None)  # optional Flask SERVER_NAME

# Interpret RATE_LIMIT like "<num>/hour" or "<num>/minute"
def parse_rate_limit(rl: str):
    try:
        n, per = rl.split("/")
        n = int(n)
        per = per.strip().lower()
        if per.startswith("hour"):
            return n, 3600
        if per.startswith("minute"):
            return n, 60
        if per.startswith("day"):
            return n, 86400
    except Exception:
        pass
    # fallback
    return 300, 3600

RATE_LIMIT_COUNT, RATE_LIMIT_PERIOD = parse_rate_limit(RATE_LIMIT)

# -------------------------
# Redis helpers (optional)
# -------------------------
_redis_client = None
if redis and REDIS_URL:
    try:
        _redis_client = redis.from_url(REDIS_URL, decode_responses=False)
    except Exception:
        _redis_client = None

# -------------------------
# Simple TTL cache (Redis if available, else in-memory)
# -------------------------
class SimpleCache:
    def __init__(self):
        self._mem = {}
        self._lock = threading.Lock()
        # start cleanup thread
        t = threading.Thread(target=self._cleanup_loop, daemon=True)
        t.start()

    def _cleanup_loop(self):
        while True:
            time.sleep(60)
            now = time.time()
            with self._lock:
                keys = list(self._mem.keys())
                for k in keys:
                    v, exp = self._mem.get(k, (None, 0))
                    if exp and exp < now:
                        self._mem.pop(k, None)

    def get(self, key):
        if _redis_client:
            try:
                raw = _redis_client.get(key)
                if raw is None:
                    return None
                return json.loads(raw)
            except Exception:
                pass
        # in-memory
        with self._lock:
            val = self._mem.get(key)
            if not val:
                return None
            data, exp = val
            if exp and exp < time.time():
                self._mem.pop(key, None)
                return None
            return data

    def set(self, key, value, ex=CACHE_TTL):
        if _redis_client:
            try:
                _redis_client.set(key, json.dumps(value), ex=ex)
                return
            except Exception:
                pass
        with self._lock:
            self._mem[key] = (value, time.time() + ex if ex else None)

cache = SimpleCache()

# -------------------------
# Simple rate limiter (Redis-backed preferred)
# Sliding window counter per key (IP or API key)
# -------------------------
class RateLimiter:
    def __init__(self, count: int, period: int):
        self.count = count
        self.period = period

    def _key(self, identity: str):
        return f"rl:{identity}"

    def allow(self, identity: str) -> (bool, int):
        """
        Returns (allowed, remaining_seconds) - remaining_seconds is reset timer when blocked
        """
        now = int(time.time())
        if _redis_client:
            key = self._key(identity)
            pipe = _redis_client.pipeline()
            # use sorted set with timestamps to allow sliding window
            min_ts = now - self.period
            # remove old
            pipe.zremrangebyscore(key, 0, min_ts)
            pipe.zcard(key)
            pipe.zadd(key, {str(now): now})
            pipe.expire(key, self.period + 10)
            res = pipe.execute()
            current_count = res[1]
            if current_count > self.count:
                # blocked; compute TTL
                ttl = _redis_client.ttl(key)
                return False, ttl if ttl >= 0 else self.period
            return True, 0
        else:
            # naive in-memory sliding window
            key = f"memrl:{identity}"
            nowf = time.time()
            if not hasattr(self, "_mem"):
                self._mem = {}
            arr = self._mem.get(key, [])
            # drop old
            cutoff = nowf - self.period
            arr = [ts for ts in arr if ts >= cutoff]
            arr.append(nowf)
            self._mem[key] = arr
            if len(arr) > self.count:
                # blocked: time until earliest timestamp + period
                ttl = int(self.period - (nowf - arr[0])) if arr else self.period
                return False, ttl
            return True, 0

rate_limiter = RateLimiter(RATE_LIMIT_COUNT, RATE_LIMIT_PERIOD)

# -------------------------
# Auth decorator
# -------------------------
def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        expected = API_KEY
        if not expected:
            return f(*args, **kwargs)  # open mode if not set
        api_key = request.headers.get("X-API-KEY") or request.args.get("api_key")
        if not api_key or api_key != expected:
            return jsonify({"error": "Unauthorized - invalid API key"}), 401
        return f(*args, **kwargs)
    return decorated

# -------------------------
# Simple request rate-limiting decorator
# -------------------------
def rate_limit(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # identify by API key if present, else by IP
        identity = request.headers.get("X-API-KEY") or request.remote_addr or "anonymous"
        allowed, retry_after = rate_limiter.allow(identity)
        if not allowed:
            return jsonify({"error": "Rate limit exceeded", "retry_after": retry_after}), 429
        return f(*args, **kwargs)
    return decorated

# -------------------------
# yt-dlp helpers
# -------------------------
def yt_dlp_opts(extra=None):
    opts = {
        "quiet": YTDLP_QUIET,
        "no_warnings": True,
        "skip_download": True,
        "nocheckcertificate": True,
    }
    if extra:
        opts.update(extra)
    return opts

def extract_info(url_or_query: str, extra_opts: Optional[dict] = None) -> dict:
    """
    Use yt-dlp to extract info. Raise exceptions on failure.
    """
    opts = yt_dlp_opts(extra_opts)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url_or_query, download=False)
    return info

def formats_from_info(info: dict) -> List[Dict[str, Any]]:
    fmts = []
    for f in info.get("formats", []):
        fmts.append({
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "resolution": f.get("resolution") or (f.get("height") and f"{f.get('height')}p"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "url": f.get("url"),
            "vcodec": f.get("vcodec"),
            "acodec": f.get("acodec"),
            "protocol": f.get("protocol"),
            "fps": f.get("fps"),
            "format_note": f.get("format_note"),
        })
    return fmts

# -------------------------
# Fallback to pafy if needed
# -------------------------
def pafy_fallback(url):
    if not pafy:
        raise RuntimeError("pafy not installed; cannot fallback")
    video = pafy.new(url)
    best = video.getbest()
    info = {
        "id": getattr(video, "videoid", None),
        "title": video.title,
        "duration": video.duration,
        "thumbnail": video.thumb,
        "formats": [{
            "format_id": "best",
            "ext": best.extension,
            "resolution": f"{best.quality}",
            "url": best.url
        }]
    }
    return info

# -------------------------
# Flask app
# -------------------------
app = Flask(__name__)
if SERVER_NAME:
    app.config['SERVER_NAME'] = SERVER_NAME

@app.after_request
def add_security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp

# -------------------------
# Routes
# -------------------------
@app.route("/", methods=["GET"])
def home():
    # Minimal frontend embedded in the single file
    html = """
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8"/>
      <meta name="viewport" content="width=device-width,initial-scale=1"/>
      <title>Video API UI</title>
      <style>
        body{font-family:Arial,Helvetica,sans-serif;max-width:900px;margin:20px auto;padding:10px}
        input,button,select{padding:8px;margin:6px 0;width:100%}
        pre{background:#f4f4f4;padding:8px;overflow:auto}
        .row{display:flex;gap:8px}
      </style>
    </head>
    <body>
      <h2>Video API - UI</h2>
      <p>Provide API key (X-API-KEY) and a video/playlist URL</p>
      <input id="apiKey" placeholder="API Key (X-API-KEY)"/>
      <input id="url" placeholder="Video / playlist URL or search query"/>
      <div class="row">
        <button onclick="getInfo()">Get Info</button>
        <button onclick="getDirect()">Get Direct URL</button>
        <button onclick="getAudio()">Get Audio URL</button>
        <button onclick="getSubs()">List Subtitles</button>
      </div>
      <h3>Response</h3>
      <pre id="out">—</pre>
    <script>
    function headers(){ const k=document.getElementById('apiKey').value; return k? {'X-API-KEY':k}:{}; }
    async function api(path, opts){ opts=opts||{}; opts.headers={...(opts.headers||{}), ...headers()}; const r=await fetch(path, opts); try{return await r.json();}catch(e){return {error:'non-json response'} } }
    async function getInfo(){ const u=document.getElementById('url').value; if(!u) return alert('enter url'); const r=await api('/api/info?url='+encodeURIComponent(u)); document.getElementById('out').textContent=JSON.stringify(r,null,2); }
    async function getDirect(){ const u=document.getElementById('url').value; if(!u) return alert('enter url'); const info=await api('/api/info?url='+encodeURIComponent(u)); if(info.error) return document.getElementById('out').textContent=JSON.stringify(info,null,2); const f=info.info.formats && info.info.formats[0]; const r=await api('/api/download?url='+encodeURIComponent(u)+'&format_id='+encodeURIComponent(f.format_id)+'&redir=true'); document.getElementById('out').textContent=JSON.stringify(r,null,2); }
    async function getAudio(){ const u=document.getElementById('url').value; if(!u) return alert('enter url'); const r=await api('/api/audio?url='+encodeURIComponent(u)); document.getElementById('out').textContent=JSON.stringify(r,null,2); }
    async function getSubs(){ const u=document.getElementById('url').value; if(!u) return alert('enter url'); const r=await api('/api/subtitles?url='+encodeURIComponent(u)); document.getElementById('out').textContent=JSON.stringify(r,null,2); }
    </script>
    </body>
    </html>
    """
    resp = make_response(html)
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    return resp

@app.route("/api/info", methods=["GET","POST"])
@require_api_key
@rate_limit
def api_info():
    """
    Returns metadata and formats for a single URL.
    Accepts:
      - GET ?url=...
      - POST JSON {"url": "..."}
    Caches results (Redis if configured, else in-memory).
    """
    payload = request.get_json(silent=True) or {}
    url = request.args.get("url") or payload.get("url")
    if not url:
        return jsonify({"error": "No url provided"}), 400

    cache_key = f"info:{url}"
    cached = cache.get(cache_key)
    if cached:
        return jsonify({"cached": True, "info": cached})

    try:
        info = extract_info(url)
    except Exception as e:
        # fallback to pafy if available
        try:
            info = pafy_fallback(url)
        except Exception:
            return jsonify({"error": "yt-dlp failed: " + str(e)}), 500

    data = {
        "id": info.get("id"),
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("uploader_id"),
        "upload_date": info.get("upload_date"),
        "duration": info.get("duration"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "description": info.get("description"),
        "webpage_url": info.get("webpage_url"),
        "thumbnail": info.get("thumbnail"),
        "formats": formats_from_info(info),
        "subtitles": info.get("subtitles") or info.get("automatic_captions") or {},
        "is_live": info.get("is_live")
    }
    cache.set(cache_key, data, ex=CACHE_TTL)
    return jsonify({"cached": False, "info": data})

@app.route("/api/download", methods=["GET","POST"])
@require_api_key
@rate_limit
def api_download():
    """
    Return direct URL to stream or proxy-stream via server.
    Params:
      url (required)
      format_id (optional)
      redir (true/false) default true -> returns JSON {"direct_url": "..."}
      as_attachment (true/false) default true when proxying
    """
    payload = request.get_json(silent=True) or {}
    url = request.args.get("url") or payload.get("url")
    format_id = request.args.get("format_id") or payload.get("format_id")
    redir = (request.args.get("redir") or payload.get("redir") or "true").lower() in ("1","true","yes")
    as_attachment = (request.args.get("as_attachment") or payload.get("as_attachment") or "true").lower() in ("1","true","yes")
    if not url:
        return jsonify({"error": "No url provided"}), 400

    try:
        ydl_opts = {"quiet": YTDLP_QUIET, "no_warnings": True, "skip_download": True}
        if format_id:
            ydl_opts["format"] = format_id
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            # if info contains a direct url (rare for merged)
            if info.get("url"):
                direct = info["url"]
                ext = info.get("ext") or "mp4"
            else:
                chosen = None
                # prefer explicit format_id
                if format_id:
                    for f in info.get("formats", []):
                        if f.get("format_id") == format_id:
                            chosen = f
                            break
                if not chosen:
                    # pick highest quality with a direct url
                    for f in reversed(info.get("formats", [])):
                        if f.get("url"):
                            chosen = f
                            break
                if not chosen:
                    return jsonify({"error": "No downloadable format found"}), 404
                direct = chosen.get("url")
                ext = chosen.get("ext") or "mp4"
    except Exception as e:
        # fallback to pafy if available
        try:
            info = pafy_fallback(url)
            fmts = info.get("formats", [])
            chosen = fmts[0] if fmts else None
            if not chosen:
                return jsonify({"error": "No format found via fallback"}), 404
            direct = chosen.get("url")
            ext = chosen.get("ext") or "mp4"
        except Exception:
            return jsonify({"error": "Failed to extract direct URL: " + str(e)}), 500

    if redir:
        return jsonify({"direct_url": direct, "ext": ext})

    # Proxy streaming mode (streams through server)
    def generate():
        with requests.get(direct, stream=True, timeout=15) as r:
            r.raise_for_status()
            total = 0
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    total += len(chunk)
                    if MAX_PROXY_SIZE and total > MAX_PROXY_SIZE:
                        # stop if size exceeds proxy limit
                        break
                    yield chunk

    disp = f'attachment; filename="video.{ext}"' if as_attachment else 'inline'
    headers = {"Content-Disposition": disp}
    return Response(stream_with_context(generate()), headers=headers, mimetype="application/octet-stream")

@app.route("/api/audio", methods=["GET","POST"])
@require_api_key
@rate_limit
def api_audio():
    """
    Return best audio direct URL (no server-side mp3 conversion here).
    """
    payload = request.get_json(silent=True) or {}
    url = request.args.get("url") or payload.get("url")
    redir = (request.args.get("redir") or payload.get("redir") or "true").lower() in ("1","true","yes")
    if not url:
        return jsonify({"error": "No url provided"}), 400
    try:
        info = extract_info(url)
    except Exception as e:
        try:
            info = pafy_fallback(url)
        except Exception:
            return jsonify({"error": "yt-dlp failed: " + str(e)}), 500

    best_audio = None
    for f in reversed(info.get("formats", [])):
        if f.get("acodec") and f.get("url"):
            if best_audio is None or (f.get("abr') if f.get('abr') else 0) > (best_audio.get('abr') if best_audio.get('abr') else 0):
                best_audio = f
    # Simple loop to choose some audio if above miss (fallback)
    if not best_audio:
        for f in reversed(info.get("formats", [])):
            if f.get("url") and f.get("ext") in ("m4a", "webm", "mp3", "ogg"):
                best_audio = f
                break
    if not best_audio:
        return jsonify({"error": "No audio format found"}), 404
    if redir:
        return jsonify({"direct_url": best_audio.get("url"), "ext": best_audio.get("ext"), "abr": best_audio.get("abr")})
    # proxy stream audio
    def generate():
        with requests.get(best_audio.get("url"), stream=True, timeout=15) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
    headers = {"Content-Disposition": f'attachment; filename="audio.{best_audio.get("ext") or "m4a"}"'}
    return Response(stream_with_context(generate()), headers=headers, mimetype="application/octet-stream")

@app.route("/api/batch", methods=["POST"])
@require_api_key
@rate_limit
def api_batch():
    """
    Accept JSON body: {"urls": ["url1","url2", ...]}
    Returns metadata for each (cached).
    """
    data = request.get_json(silent=True)
    if not data or not isinstance(data.get("urls"), list):
        return jsonify({"error": "JSON body must contain 'urls': [ ... ]"}), 400
    urls = data["urls"]
    results = {}
    for u in urls:
        try:
            cache_key = f"info:{u}"
            cached = cache.get(cache_key)
            if cached:
                results[u] = {"cached": True, "info": cached}
                continue
            try:
                info = extract_info(u)
            except Exception:
                info = pafy_fallback(u) if pafy else {}
            d = {
                "title": info.get("title"),
                "duration": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
                "formats": formats_from_info(info),
            }
            cache.set(cache_key, d, ex=CACHE_TTL)
            results[u] = {"cached": False, "info": d}
        except Exception as e:
            results[u] = {"error": str(e)}
    return jsonify(results)

@app.route("/api/playlist", methods=["GET","POST"])
@require_api_key
@rate_limit
def api_playlist():
    """
    Expand playlist (flat) and return entries list (id, title, url).
    """
    payload = request.get_json(silent=True) or {}
    url = request.args.get("url") or payload.get("url")
    if not url:
        return jsonify({"error": "No url provided"}), 400
    try:
        info = extract_info(url, extra_opts={"extract_flat": True})
        entries = info.get("entries") or []
        videos = []
        for e in entries:
            videos.append({
                "id": e.get("id"),
                "title": e.get("title"),
                "url": e.get("url") or e.get("webpage_url")
            })
        return jsonify({"title": info.get("title"), "entries": videos, "playlist_id": info.get("id")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/search", methods=["GET"])
@require_api_key
@rate_limit
def api_search():
    """
    Search using yt-dlp's ytsearch protocol.
    ?q=...&max=5
    """
    q = request.args.get("q")
    if not q:
        return jsonify({"error": "query param q required"}), 400
    max_results = int(request.args.get("max", 5))
    try:
        query = f"ytsearch{max_results}:{q}"
        info = extract_info(query)
        results = []
        for e in (info.get("entries") or []):
            results.append({
                "id": e.get("id"),
                "title": e.get("title"),
                "webpage_url": e.get("webpage_url"),
                "duration": e.get("duration"),
                "thumbnail": e.get("thumbnail")
            })
        return jsonify({"query": q, "results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/subtitles", methods=["GET","POST"])
@require_api_key
@rate_limit
def api_subtitles():
    """
    List available subtitle languages or download a given language.
    ?url=...            -> lists languages
    ?url=...&lang=en    -> returns subtitle file attachment
    """
    payload = request.get_json(silent=True) or {}
    url = request.args.get("url") or payload.get("url")
    lang = request.args.get("lang") or payload.get("lang")
    if not url:
        return jsonify({"error": "No url provided"}), 400
    try:
        info = extract_info(url)
    except Exception as e:
        try:
            info = pafy_fallback(url)
        except Exception:
            return jsonify({"error": "yt-dlp failed: " + str(e)}), 500

    subs = info.get("subtitles", {}) or {}
    auto = info.get("automatic_captions", {}) or {}
    combined = {}
    combined.update(subs)
    combined.update(auto)  # auto may overwrite same keys

    if not lang:
        return jsonify({"available": list(combined.keys())})

    # Use yt-dlp to download specified subtitle
    tmpdir = tempfile.mkdtemp(prefix="vidapi_subs_")
    try:
        ydl_opts = {
            "writesubtitles": True,
            "writeautomaticsub": True,
            "skip_download": True,
            "subtitlesformat": "srt",
            "subtitleslangs": [lang],
            "outtmpl": tmpdir + "/%(id)s.%(ext)s",
            "quiet": YTDLP_QUIET,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
        # find srt/vtt file
        found = None
        for fn in os.listdir(tmpdir):
            if fn.endswith(".srt") or fn.endswith(".vtt"):
                found = os.path.join(tmpdir, fn)
                break
        if not found:
            return jsonify({"error": "Subtitle not found after download"}), 404
        return send_file(found, as_attachment=True)
    finally:
        # schedule cleanup
        def cleanup(path):
            try:
                shutil.rmtree(path)
            except Exception:
                pass
        threading.Timer(30.0, cleanup, args=(tmpdir,)).start()

# -------------------------
# Error handlers
# -------------------------
@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Server error", "detail": str(e)}), 500

# -------------------------
# Run (development)
# -------------------------
if __name__ == "__main__":
    # Warn if API_KEY not set
    if not API_KEY:
        print("WARNING: API_KEY is not set. Running in open mode (not recommended for production).")
    print(f"Starting Flask dev server. Rate limit: {RATE_LIMIT_COUNT}/{RATE_LIMIT_PERIOD}s, Cache TTL: {CACHE_TTL}s")
    app.run(host="0.0.0.0", port=5000, debug=False)
