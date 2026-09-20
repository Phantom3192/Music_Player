import asyncio
import os
import re
import time
from urllib.parse import unquote

import httpx
import yt_dlp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

import jiosaavn_search
import lavalink_source
import soundcloud_search

load_dotenv()

FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:8000")

app = FastAPI(title="Ghost Cave")

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
        # Prefer a plain HTTP(S) file. SoundCloud's best audio is often an HLS
        # (.m3u8) playlist, which a browser <audio> tag can't play through a proxy.
        "format": "bestaudio[protocol^=http]/bestaudio",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(source_url, download=False)

    if "m3u8" in (info.get("protocol") or "") or ".m3u8" in info["url"]:
        raise RuntimeError("Only an HLS stream is available for this track")

    ext = info.get("ext", "webm")
    content_type = {
        "mp3": "audio/mpeg",
        "m4a": "audio/mp4",
        "opus": "audio/ogg",
        "ogg": "audio/ogg",
    }.get(ext, f"audio/{ext}")

    return {
        "url": info["url"],
        "content_type": content_type,
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


@app.get("/api/lavalink/status")
async def api_lavalink_status():
    """Used by the header badge to show whether the Lavalink node is reachable."""
    return await lavalink_source.status()


@app.get("/api/search")
async def api_search(q: str, source: str = "sc"):
    if not q.strip():
        raise HTTPException(400, "Query param 'q' is required")
    try:
        if source == "jiosaavn":
            results = await jiosaavn_search.search(q)
        elif source == "youtube":
            results = await lavalink_source.search(q)
        else:
            results = await soundcloud_search.search(q)
    except (
        soundcloud_search.SearchError,
        jiosaavn_search.JioSaavnError,
        lavalink_source.LavalinkError,
    ) as e:
        raise HTTPException(502, f"Search failed: {e}")
    # Most popular first (play count). Sort is stable, so tracks without a
    # play count keep their original order at the bottom.
    results.sort(key=lambda r: r.get("popularity") or 0, reverse=True)
    return {"results": results}


def _range_response(data: bytes, content_type: str, range_header: str | None) -> Response:
    """Serve in-memory audio with HTTP Range support so the seek bar works."""
    total = len(data)
    headers = {"accept-ranges": "bytes"}

    if range_header:
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
        if m and (m.group(1) or m.group(2)):
            start_s, end_s = m.groups()
            if not start_s:  # suffix range: last N bytes
                start = max(total - int(end_s), 0)
                end = total - 1
            else:
                start = int(start_s)
                end = int(end_s) if end_s else total - 1
            end = min(end, total - 1)
            if start >= total or start > end:
                return Response(status_code=416, headers={"content-range": f"bytes */{total}"})
            body = data[start : end + 1]
            headers["content-range"] = f"bytes {start}-{end}/{total}"
            headers["content-length"] = str(len(body))
            return Response(body, status_code=206, media_type=content_type, headers=headers)

    headers["content-length"] = str(total)
    return Response(data, media_type=content_type, headers=headers)


async def _serve_youtube(request: Request, video_id: str) -> Response:
    # The id goes into a URL path on your Lavalink server (with its password),
    # so only accept a real 11-character YouTube id.
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise HTTPException(400, "Invalid YouTube video id")
    try:
        data, content_type = await lavalink_source.get_audio(video_id)
    except lavalink_source.LavalinkError as e:
        raise HTTPException(502, f"Could not resolve stream: {e}")
    return _range_response(data, content_type, request.headers.get("range"))


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

    if source == "youtube":
        return await _serve_youtube(request, source_url)

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