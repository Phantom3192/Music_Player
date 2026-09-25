"""
Search/stream source backed by a self-hosted Lavalink server running the
"browserstream" plugin (com.necrozma:browserstream, standalone HTTP mode).

The plugin resolves a search term or a direct media URL/identifier through
Lavaplayer - exactly like a Discord music bot would - and exposes that over
plain HTTP instead of a voice connection:

  GET /browser/search?identifier=...            -> track metadata (JSON)
  GET /browser/stream?identifier=...&index=N     -> raw Ogg Opus audio bytes

Both endpoints require the Lavalink node's Authorization header, so this
module (like the plugin's own test-page proxy) holds the password
server-side and the browser never sees it.

Config comes from environment variables (see README):
  LAVALINK_HOST, LAVALINK_PORT, LAVALINK_PASSWORD, LAVALINK_SECURE
"""

import os

import httpx

LAVALINK_HOST = os.getenv("LAVALINK_HOST", "127.0.0.1")
LAVALINK_PORT = int(os.getenv("LAVALINK_PORT", "26135"))
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", "")
LAVALINK_SECURE = os.getenv("LAVALINK_SECURE", "false").lower() == "true"

# A bare search term is resolved against YouTube via the "ytsearch:" prefix,
# same convention Lavalink/Lavaplayer use everywhere else. A pasted URL is
# passed straight through untouched.
SEARCH_PREFIX = "ytsearch:"


class LavalinkError(Exception):
    pass


def _base_url() -> str:
    scheme = "https" if LAVALINK_SECURE else "http"
    return f"{scheme}://{LAVALINK_HOST}:{LAVALINK_PORT}"


def _require_password():
    if not LAVALINK_PASSWORD:
        raise LavalinkError("LAVALINK_PASSWORD is not configured on the server")


def _error_hint(status: int) -> str:
    return {
        401: "wrong Lavalink password",
        404: "no match / browserstream plugin not installed (need v1.1.0+)",
        502: "Lavalink couldn't load that source",
        503: "Lavalink still starting",
    }.get(status, "")


async def search(query: str, limit: int = 20) -> list[dict]:
    _require_password()
    identifier = query if "://" in query else SEARCH_PREFIX + query

    async with httpx.AsyncClient(timeout=45) as client:
        try:
            # Resolving a search term (rather than a direct URL) can take a
            # few seconds while Lavaplayer talks to YouTube, hence the long timeout.
            resp = await client.get(
                f"{_base_url()}/browser/search",
                params={"identifier": identifier},
                headers={"Authorization": LAVALINK_PASSWORD},
            )
        except httpx.HTTPError as e:
            raise LavalinkError(f"Could not reach Lavalink: {e}")

    if resp.status_code != 200:
        raise LavalinkError(
            f"Lavalink returned {resp.status_code}. {_error_hint(resp.status_code)} "
            f"{resp.text[:300]}".strip()
        )

    payload = resp.json()
    if isinstance(payload, dict) and payload.get("error"):
        raise LavalinkError(payload["error"])

    tracks = (payload.get("tracks") or [])[:limit]
    results = []
    for t in tracks:
        # Prefer the resolved "uri" (e.g. the youtube.com watch URL) as the
        # value we'll later send to /browser/stream - it re-resolves to this
        # exact track on its own, so no "index" bookkeeping is needed.
        stream_id = t.get("uri") or t.get("identifier")
        results.append({
            "title": t.get("title") or "Untitled",
            "author": t.get("author"),
            "duration_ms": t.get("length"),
            "artwork": t.get("artworkUrl"),
            "uri": stream_id,
            "playable": bool(stream_id),
            "available": True,
            "is_stream": bool(t.get("isStream")),
            "source": "lavalink",
        })
    return results


async def open_stream(identifier: str):
    """
    Opens a streaming GET to /browser/stream and returns (client, response).
    The caller must read response.aiter_bytes() and then close both.
    """
    _require_password()

    client = httpx.AsyncClient(timeout=None)
    req = client.build_request(
        "GET",
        f"{_base_url()}/browser/stream",
        params={"identifier": identifier},
        headers={"Authorization": LAVALINK_PASSWORD},
    )
    try:
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as e:
        await client.aclose()
        raise LavalinkError(f"Could not reach Lavalink: {e}")

    if resp.status_code != 200:
        body = await resp.aread()
        await resp.aclose()
        await client.aclose()
        raise LavalinkError(
            f"Lavalink returned {resp.status_code}. {_error_hint(resp.status_code)} "
            f"{body[:300].decode(errors='replace')}".strip()
        )

    return client, resp
