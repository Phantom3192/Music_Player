import asyncio
import os
import time
from urllib.parse import unquote

import httpx
import yt_dlp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

import jiosaavn_search
import soundcloud_search

load_dotenv()

FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:8000")

app = FastAPI(title="Personal Music Player")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN, "*"],  # loosen for local dev; tighten for prod
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Resolved-stream cache -------------------------------------------------
# yt-dlp extraction is the expensive step. CDN URLs it returns are time-limited
# (a few hours), so we cache per source URL and re-extract once expired.
_CACHE_TTL = 3 * 60 * 60  # 3 hours
_stream_cache: dict[str, dict] = {}

# Cap concurrent yt-dlp extractions so a burst of plays doesn't spawn unbounded
# subprocesses/threads (relevant even at 10-20 concurrent users).
_extract_semaphore = asyncio.Semaphore(4)


def _extract_direct_url(source_url: str) -> dict:
    ydl_opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(source_url, download=False)

    return {
        "url": info["url"],
        "content_type": f"audio/{info.get('ext', 'webm')}",
        "title": info.get("title"),
    }


async def _get_direct_stream_info(source_url: str) -> dict:
    cached = _stream_cache.get(source_url)
    if cached and cached["expires_at"] > time.time():
        return cached

    async with _extract_semaphore:
        # yt-dlp is sync/blocking — run it off the event loop.
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, _extract_direct_url, source_url)

    info["expires_at"] = time.time() + _CACHE_TTL
    _stream_cache[source_url] = info
    return info


@app.get("/api/search")
async def api_search(q: str, source: str = "sc"):
    if not q.strip():
        raise HTTPException(400, "Query param 'q' is required")
    try:
        if source == "jiosaavn":
            results = await jiosaavn_search.search(q)
        else:
            results = await soundcloud_search.search(q)
    except (soundcloud_search.SearchError, jiosaavn_search.JioSaavnError) as e:
        raise HTTPException(502, f"Search failed: {e}")
    return {"results": results}


@app.get("/api/stream")
async def api_stream(request: Request, url: str, source: str = "sc"):
    """
    Proxies audio for a given source URL (a track's `uri` from /api/search).
    Forwards the client's Range header so seeking works in the <audio> tag.

    JioSaavn URLs are already direct, playable audio files, so we skip the
    yt-dlp extraction step entirely for `source=jiosaavn` and just proxy the
    URL straight through. SoundCloud still needs yt-dlp to resolve a direct
    audio URL first.
    """
    source_url = unquote(url)

    if source == "jiosaavn":
        target_url = source_url
        content_type = "audio/mp4"  # JioSaavn typically serves .m4a/mp4 audio
    else:
        try:
            info = await _get_direct_stream_info(source_url)
        except Exception as e:
            raise HTTPException(502, f"Could not resolve stream: {e}")
        target_url = info["url"]
        content_type = info["content_type"]

    upstream_headers = {}
    range_header = request.headers.get("range")
    if range_header:
        upstream_headers["Range"] = range_header

    client = httpx.AsyncClient(timeout=None)
    upstream_req = client.build_request("GET", target_url, headers=upstream_headers)
    upstream_resp = await client.send(upstream_req, stream=True)

    response_headers = {}
    for h in ("content-range", "content-length", "accept-ranges"):
        if h in upstream_resp.headers:
            response_headers[h] = upstream_resp.headers[h]
    response_headers.setdefault("accept-ranges", "bytes")

    async def body_iterator():
        try:
            async for chunk in upstream_resp.aiter_bytes():
                yield chunk
        finally:
            await upstream_resp.aclose()
            await client.aclose()

    return StreamingResponse(
        body_iterator(),
        status_code=upstream_resp.status_code,
        media_type=content_type,
        headers=response_headers,
    )


# Serve the frontend (index.html, style.css, app.js) at "/"
app.mount("/", StaticFiles(directory="../frontend", html=True), name="frontend")
