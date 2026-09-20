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
import json
import os
import time
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
def _sniff(data: bytes) -> str | None:
    """Identify the audio container from its first bytes (don't trust headers)."""
    if len(data) < 8:
        return None
    if data[4:8] in (b"ftyp", b"styp", b"moov", b"moof"):
        return "audio/mp4"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "audio/webm"
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[:3] == b"ID3" or (data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "audio/mpeg"
    return None


# Formats to try, in order: AAC/m4a plays everywhere, opus/webm in nearly all
# modern browsers, then whatever Lavalink considers best.
_ITAGS = (140, 251, None)

# Lavalink's stream route takes an optional `clientIdentifier`. If its default
# client choice fails (HTTP 500), try these one by one and remember the winner.
_CLIENTS = ("ANDROID_VR", "TV", "WEB", "MWEB", "ANDROID_MUSIC", "IOS", "TVHTML5_SIMPLY")
_preferred_client: str | None = None


def _error_message(body: bytes) -> str:
    """Pull Lavalink's own error text out of a non-200 response body."""
    text = body[:2000].decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return str(data.get("message") or data.get("error") or text)[:300]
    except ValueError:
        pass
    return text.replace("\n", " ")[:300]


async def _attempt(client: httpx.AsyncClient, url: str, itag, lava_client, headers: dict, limit: int | None) -> dict:
    """One request to Lavalink's stream route. Never raises for HTTP problems."""
    att = {"itag": itag, "client": lava_client, "status": None, "content_type": None, "bytes": 0,
           "seconds": 0.0, "kind": None, "head_hex": None, "note": None, "data": None}
    params = {}
    if itag:
        params["itag"] = itag
    if lava_client:
        params["clientIdentifier"] = lava_client

    started = time.monotonic()
    try:
        async with client.stream("GET", url, params=params or None, headers=headers) as resp:
            att["status"] = resp.status_code
            att["content_type"] = (resp.headers.get("content-type") or "").split(";")[0].strip() or None
            if resp.status_code == 401:
                raise LavalinkError("Lavalink rejected the password (HTTP 401)")
            if resp.status_code != 200:
                att["note"] = _error_message(await resp.aread()) or None
                return att

            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > _MAX_TRACK_BYTES:
                    att["note"] = "track too large"
                    return att
                if limit and len(buf) >= limit:
                    break
            att["bytes"] = len(buf)
            att["head_hex"] = bytes(buf[:16]).hex()
            att["kind"] = _sniff(bytes(buf))
            if not buf:
                att["note"] = "empty response"
            elif not att["kind"]:
                att["note"] = "not audio: " + bytes(buf[:100]).decode("utf-8", "replace").replace("\n", " ")
            att["data"] = bytes(buf)
    except httpx.HTTPError as e:
        att["note"] = f"{type(e).__name__}: {e}"
    finally:
        att["seconds"] = round(time.monotonic() - started, 2)
    return att


def _combos() -> list[tuple]:
    """(itag, clientIdentifier) pairs in the order we try them."""
    combos = []
    if _preferred_client:
        combos.append((140, _preferred_client))
    combos += [(itag, None) for itag in _ITAGS]
    for c in _CLIENTS:
        if c != _preferred_client:
            combos += [(140, c), (None, c)]
    return combos


async def _try_formats(video_id: str, limit: int | None) -> tuple[list[dict], dict | None]:
    global _preferred_client
    base, headers = _config()
    routes = [f"{base}/youtube/stream/{video_id}", f"{base}/v4/youtube/stream/{video_id}"]
    attempts: list[dict] = []
    deadline = time.monotonic() + 30  # don't keep the listener waiting forever

    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as client:
        for itag, lava_client in _combos():
            if attempts and time.monotonic() > deadline:
                break
            for route in routes:
                att = await _attempt(client, route, itag, lava_client, headers, limit)
                att["route"] = route[len(base):]
                attempts.append(att)
                if att["status"] == 200 and att["kind"]:
                    if lava_client:
                        _preferred_client = lava_client
                    return attempts, att
                if att["status"] != 404:
                    break  # route exists (or failed otherwise); try the next combo
    return attempts, None


def _reasons(attempts: list[dict]) -> str:
    """Short, de-duplicated summary of why every attempt failed."""
    seen: list[str] = []
    for a in attempts:
        if a["status"] == 200:
            why = a["note"] or "no audio"
        else:
            why = f"HTTP {a['status']}" + (f": {a['note']}" if a["note"] else "")
        if why not in seen:
            seen.append(why)
    return "; ".join(seen[:3])


async def _download(video_id: str) -> tuple[bytes, str]:
    attempts, good = await _try_formats(video_id, limit=None)
    for a in attempts:
        print(f"[lavalink] {video_id} {a['route']} itag={a['itag']} client={a['client']} -> HTTP {a['status']} "
              f"{a['content_type']} {a['bytes']} bytes in {a['seconds']}s kind={a['kind']} {a['note'] or ''}",
              flush=True)
    if not good:
        raise LavalinkError(f"Lavalink could not stream this video ({_reasons(attempts)})")
    return good["data"], good["kind"]


async def debug(video_id: str) -> dict:
    """Probe the stream route without caching: shows what Lavalink really returns."""
    attempts, good = await _try_formats(video_id, limit=512 * 1024)
    out = []
    for a in attempts:
        kbps = round(a["bytes"] / 1024 / a["seconds"]) if a["seconds"] and a["bytes"] else None
        out.append({k: a[k] for k in ("route", "itag", "client", "status", "content_type", "kind",
                                      "bytes", "seconds", "head_hex", "note")}
                   | {"speed_kb_per_s": kbps})
    return {"works": bool(good), "used_client": good["client"] if good else None, "attempts": out}


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


# ---- Status ----------------------------------------------------------------
async def status() -> dict:
    """Health check for the UI badge. Never raises; always returns a dict."""
    try:
        base, headers = _config()
    except LavalinkError as e:
        return {"connected": False, "reason": str(e)}

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{base}/v4/info", headers=headers)
    except httpx.HTTPError as e:
        return {"connected": False, "reason": f"Cannot reach the Lavalink server ({type(e).__name__})"}
    latency_ms = round((time.monotonic() - started) * 1000)

    if resp.status_code == 401:
        return {"connected": False, "reason": "Lavalink rejected the password"}
    if resp.status_code != 200:
        return {"connected": False, "reason": f"Lavalink returned HTTP {resp.status_code}"}

    try:
        info = resp.json()
    except ValueError:
        info = {}
    plugins = [
        f"{p.get('name')} {p.get('version')}".strip()
        for p in (info.get("plugins") or [])
        if isinstance(p, dict) and p.get("name")
    ]
    return {
        "connected": True,
        "version": (info.get("version") or {}).get("semver"),
        "latency_ms": latency_ms,
        "plugins": plugins,
    }