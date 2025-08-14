# Video API (yt-dlp + Flask) — Production Ready for Render

This repository implements a production-ready Flask Video API using `yt-dlp` with Redis caching, API key auth, rate limiting, subtitles support, playlist & batch handling, and a minimal frontend UI. It is configured to deploy on Render (or any container platform).

## What is included
- `/api/info` — metadata & formats
- `/api/download` — get direct URL or proxy-stream
- `/api/audio` — audio direct URL
- `/api/batch` — batch metadata
- `/api/playlist` — expand playlist
- `/api/search` — ytsearch
- `/api/subtitles` — list/download subtitles
- Minimal frontend at `/`
- Redis caching
- API Key auth and rate limiting

## Quick start (local with docker-compose)
1. Copy `.env.example` to `.env` and set values.
2. Build & run with Docker (optional)

Or run locally (requires Python 3.11+):python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export FLASK_APP=app.api:create_app
export API_KEY=your_api_key_here
flask run --host=0.0.0.0 --port=5000

## Deploy to Render
1. Push this repo to GitHub.
2. Create a new Web Service on Render, connect the repo.
3. Set environment variables in Render (API_KEY, REDIS_URL, etc.).
4. Use the start command: `gunicorn app.api:create_app() -b 0.0.0.0:5000 --workers 3` (Render will handle process manager).

## Files
See the repo file list in the repository root.
