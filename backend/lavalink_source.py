"""YouTube source, powered by your own Lavalink server (youtube-source plugin).

Your Lavalink server already deals with YouTube's OAuth / PO-token checks, so
this app needs no cookies and no yt-dlp for YouTube. We only use two things
Lavalink exposes over HTTP:

    GET /v4/loadtracks?identifier=ytmsearch:<query>   -> search results
    GET /youtube/stream/<videoId>?itag=140            -> raw audio bytes

Environment variables:
    LAVALINK_URL       e.g. http://127.0.0.1:26135   (no trailing slash needed)
    LAVALINK_PASSWORD  the Lavalink server password

Lavalink's stream route is a plain pass-through with no Range support, which
would break the seek bar. So we download each track once (a few MB), keep it
in a small in-memory LRU cache, and serve it to the browser ourselves.
"""

import asyncio
import os
from collections import OrderedDict

import httpx


class LavalinkError(Exception):
    pass


_search_semaphore = asyncio.Semaphore(4)
_download_semaphore = asyncio.Semaphore(4)

# ---- Audio cache -----------------------------------------------------------
_MAX_TRACK_BYTES = 40 * 1024 * 1024  # refuse anything bigger (not a song)
_CACHE_MAX_TRACKS = 20               # ~4-10 MB each -> well under 256 MB
_audio_cache: "OrderedDict[str, tuple[bytes, str]]" = OrderedDict()
_download_locks: dict[str, asyncio.Lock] = {}


def _config() -> tuple[str, dict]:
    base = (os.getenv("LAVALINK_URL") or "").strip().rstrip("/")
    password = os.getenv("LAVALINK_PASSWORD") or ""
    if not base or not password:
        raise LavalinkError("LAVALINK_URL and LAVALINK_PASSWORD are not set")
    return base, {"Authorization": password}


def _check_status(resp: httpx.Response) -> None:
    if resp.status_code == 401:
        raise LavalinkError("Lavalink rejected the password (HTTP 401)")
    if resp.status_code != 200:
        raise LavalinkError(f"Lavalink returned HTTP {resp.status_code}")


# ---- Search ----------------------------------------------------------------
async def _load(client: httpx.AsyncClient, base: str, headers: dict, identifier: str) -> list[dict]:
    resp = await client.get(
        f"{base}/v4/loadtracks", params={"identifier": identifier}, headers=headers
    )
    _check_status(resp)
    body = resp.json()
    load_type = body.get("loadType")
    data = body.get("data")
    if load_type == "search":
        return data or []
    if load_type == "track" and isinstance(data, dict):
        return [data]
    if load_type == "playlist" and isinstance(data, dict):
        return data.get("tracks") or []
    if load_type == "error":
        raise LavalinkError((data or {}).get("message") or "Lavalink failed to load tracks")
    return []  # "empty"


async def search(query: str, limit: int = 20) -> list[dict]:
    base, headers = _config()

    async with _search_semaphore:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                # YouTube Music search gives cleaner, song-focused results
                # (official uploads first). Fall back to plain YouTube search.
                try:
                    tracks = await _load(client, base, headers, f"ytmsearch:{query}")
                except LavalinkError:
                    tracks = []
                if not tracks:
                    tracks = await _load(client, base, headers, f"ytsearch:{query}")
        except httpx.HTTPError as e:
            raise LavalinkError(f"Could not reach Lavalink: {e}") from e

    results = []
    for t in tracks:
        info = t.get("info") or {}
        video_id = info.get("identifier")
        if not video_id or info.get("isStream"):
            continue
        results.append({
            "title": info.get("title"),
            "author": info.get("author"),
            "duration_ms": info.get("length") or None,
            "artwork": info.get("artworkUrl") or f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
            "uri": video_id,          # just the video id; /api/stream resolves it
            "popularity": None,       # Lavalink doesn't expose view counts
            "playable": True,
            "source": "youtube",
            "is_stream": False,
        })
        if len(results) >= limit:
            break
    return results


# ---- Audio -----------------------------------------------------------------
async def _download(video_id: str) -> tuple[bytes, str]:
    base, headers = _config()

    # AAC/m4a (itag 140) plays in every browser. If Lavalink doesn't have it,
    # ask for whatever it considers best. The second route covers Lavalink
    # builds that prefix plugin routes with /v4.
    candidates = [
        (f"{base}/youtube/stream/{video_id}", {"itag": 140}),
        (f"{base}/v4/youtube/stream/{video_id}", {"itag": 140}),
        (f"{base}/youtube/stream/{video_id}", None),
    ]

    last_error = "no response"
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as client:
        for url, params in candidates:
            try:
                async with client.stream("GET", url, params=params, headers=headers) as resp:
                    if resp.status_code == 401:
                        raise LavalinkError("Lavalink rejected the password (HTTP 401)")
                    if resp.status_code != 200:
                        last_error = f"HTTP {resp.status_code}"
                        continue

                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > _MAX_TRACK_BYTES:
                            raise LavalinkError("Track is too large to play")

                    if not buf:
                        last_error = "empty response"
                        continue

                    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
                    if not ctype.startswith(("audio/", "video/")):
                        ctype = "audio/mp4"
                    return bytes(buf), ctype
            except httpx.HTTPError as e:
                raise LavalinkError(f"Could not reach Lavalink: {e}") from e

    raise LavalinkError(f"Lavalink could not stream this video ({last_error})")


async def get_audio(video_id: str) -> tuple[bytes, str]:
    """Return (audio_bytes, content_type) for a YouTube video id, cached."""
    if video_id in _audio_cache:
        _audio_cache.move_to_end(video_id)
        return _audio_cache[video_id]

    # One download per video even if the browser fires several requests at once.
    lock = _download_locks.setdefault(video_id, asyncio.Lock())
    try:
        async with lock:
            if video_id in _audio_cache:
                _audio_cache.move_to_end(video_id)
                return _audio_cache[video_id]
            async with _download_semaphore:
                item = await _download(video_id)
            _audio_cache[video_id] = item
            while len(_audio_cache) > _CACHE_MAX_TRACKS:
                _audio_cache.popitem(last=False)
            return item
    finally:
        _download_locks.pop(video_id, None)
