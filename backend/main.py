import os
from urllib.parse import unquote, urlparse

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import jiosaavn_search

load_dotenv()

FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:8000")

app = FastAPI(title="Eclipse")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN, "*"],  # loosen for local dev; tighten for prod
    allow_methods=["*"],
    allow_headers=["*"],
)

# Only JioSaavn's audio CDN may be proxied, so /api/stream can't be used as an
# open proxy to arbitrary URLs.
_ALLOWED_AUDIO_HOSTS = ("saavncdn.com",)


def _is_allowed_audio_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme in ("http", "https") and any(
        host == h or host.endswith("." + h) for h in _ALLOWED_AUDIO_HOSTS
    )


def _content_type_for(url: str) -> str:
    path = urlparse(url).path.lower()
    if path.endswith(".mp3"):
        return "audio/mpeg"
    return "audio/mp4"  # JioSaavn serves AAC in an .mp4 container


@app.get("/api/search")
async def api_search(q: str):
    if not q.strip():
        raise HTTPException(400, "Query param 'q' is required")
    try:
        results = await jiosaavn_search.search(q)
    except jiosaavn_search.JioSaavnError as e:
        raise HTTPException(502, f"Search failed: {e}")
    # Available tracks first, then most popular (play count). Sort is stable,
    # so tracks without a play count keep their original order at the bottom.
    results.sort(
        key=lambda r: (r.get("available", True), r.get("popularity") or 0),
        reverse=True,
    )
    return {"results": results}


@app.get("/api/stream")
async def api_stream(request: Request, url: str):
    """
    Proxies a JioSaavn audio file (a track's `uri` from /api/search).
    Forwards the client's Range header so seeking works in the <audio> tag.
    """
    target_url = unquote(url)
    if not _is_allowed_audio_url(target_url):
        raise HTTPException(400, "Only JioSaavn audio URLs can be streamed")

    upstream_headers = {}
    range_header = request.headers.get("range")
    if range_header:
        upstream_headers["Range"] = range_header

    client = httpx.AsyncClient(timeout=None)
    try:
        upstream_req = client.build_request("GET", target_url, headers=upstream_headers)
        upstream_resp = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as e:
        await client.aclose()
        raise HTTPException(502, f"Could not reach JioSaavn's audio server: {e}")

    if upstream_resp.status_code >= 400:
        status = upstream_resp.status_code
        await upstream_resp.aclose()
        await client.aclose()
        raise HTTPException(502, f"JioSaavn refused this track (HTTP {status})")

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
        media_type=_content_type_for(target_url),
        headers=response_headers,
    )


_FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")


@app.get("/player")
async def player_page():
    """The music player. The homepage (index.html) is served at "/"."""
    return FileResponse(os.path.join(_FRONTEND_DIR, "player.html"))


# Serve the rest of the frontend (index.html, style.css, app.js) at "/"
app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")