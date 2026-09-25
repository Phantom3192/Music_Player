import os
from urllib.parse import unquote

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import lavalink_search

load_dotenv()

FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:8000")

app = FastAPI(title="Eclipse")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN, "*"],  # loosen for local dev; tighten for prod
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/search")
async def api_search(q: str):
    if not q.strip():
        raise HTTPException(400, "Query param 'q' is required")
    try:
        results = await lavalink_search.search(q)
    except lavalink_search.LavalinkError as e:
        raise HTTPException(502, f"Search failed: {e}")
    return {"results": results}


@app.get("/api/stream")
async def api_stream(url: str):
    """
    Proxies audio from the Lavalink node's browserstream plugin.
    `url` is a track's `uri` from /api/search (the identifier the plugin
    re-resolves and transcodes to Ogg Opus on the fly).

    Note: because this is a live transcode rather than a static file, the
    plugin doesn't support byte-range requests, so seeking works only via
    the <audio> element's own buffered playback, not server-side seeking.
    """
    identifier = unquote(url)
    if not identifier:
        raise HTTPException(400, "Query param 'url' is required")

    try:
        client, upstream_resp = await lavalink_search.open_stream(identifier)
    except lavalink_search.LavalinkError as e:
        raise HTTPException(502, str(e))

    async def body_iterator():
        try:
            async for chunk in upstream_resp.aiter_bytes():
                yield chunk
        finally:
            await upstream_resp.aclose()
            await client.aclose()

    return StreamingResponse(
        body_iterator(),
        media_type="audio/ogg",
        headers={"Cache-Control": "no-store"},
    )


_FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")


@app.get("/player")
async def player_page():
    """The music player. The homepage (index.html) is served at "/"."""
    return FileResponse(os.path.join(_FRONTEND_DIR, "player.html"))


# Serve the rest of the frontend (index.html, style.css, app.js) at "/"
app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
