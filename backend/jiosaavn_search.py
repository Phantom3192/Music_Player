"""
JioSaavn source, via a self-hosted instance of cyberboysumanjay's Flask-based
JioSaavnAPI (see https://github.com/cyberboysumanjay/JioSaavnAPI).

Unlike YouTube/SoundCloud, JioSaavn's songs are already plain HTTPS .mp3
files, so there's no yt-dlp-style extraction step: search gives us a direct,
playable URL right away.

This API is a small Flask app meant to be self-hosted (it isn't a maintained
public service), so SAAVN_API_BASE should normally point at your own
deployment, e.g. http://localhost:5000 or wherever you've deployed it.
"""

import os
import httpx

SAAVN_API_BASE = os.getenv("SAAVN_API_BASE", "http://127.0.0.1:5000")


class JioSaavnError(Exception):
    pass


def _to_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


async def search(query: str, limit: int = 20) -> list[dict]:
    async with httpx.AsyncClient(timeout=40) as client:
        try:
            # The "universal" /result/ endpoint accepts a plain search term
            # (as opposed to a jiosaavn.com URL) and returns a list of songs.
            resp = await client.get(
                f"{SAAVN_API_BASE}/result/",
                params={"query": query, "lyrics": "false", "limit": limit},
            )
        except httpx.HTTPError as e:
            raise JioSaavnError(f"Could not reach JioSaavn API: {e}")

    if resp.status_code != 200:
        raise JioSaavnError(f"JioSaavn API returned {resp.status_code}")

    payload = resp.json()

    # This API returns {"status": False, "error": "..."} on failure, and
    # either a bare list or {"songs": [...]} of song dicts on success.
    if isinstance(payload, dict) and payload.get("status") is False:
        raise JioSaavnError(payload.get("error", "Unknown JioSaavn API error"))

    if isinstance(payload, dict):
        songs = payload.get("songs") or payload.get("results") or []
    else:
        songs = payload or []

    # A single-song lookup (e.g. a direct song URL was passed) comes back as
    # one dict rather than a list — normalize it.
    if isinstance(songs, dict):
        songs = [songs]

    results = []
    for song in songs[:limit]:
        duration_s = song.get("duration")
        uri = song.get("media_url") or song.get("url")
        results.append({
            "title": song.get("song") or song.get("title"),
            "author": song.get("singers") or song.get("primary_artists"),
            "duration_ms": int(float(duration_s) * 1000) if duration_s else None,
            "artwork": song.get("image") or song.get("image_url"),
            "uri": uri,  # already a direct, playable .mp3 URL
            "playable": bool(uri),
            "available": song.get("available", True),
            "popularity": _to_int(song.get("play_count") or song.get("playCount")),
            "source": "jiosaavn",
            "is_stream": False,
        })

    # Keep results even when JioSaavn gave no audio link, so the UI can show
    # them as "not available" instead of them silently disappearing.
    return results