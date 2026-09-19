"""
JioSaavn source, via the community-run saavn.dev API (wraps JioSaavn's own
undocumented endpoints — see https://github.com/sumitkolhe/jiosaavn-api).

Unlike YouTube/SoundCloud, JioSaavn's songs are already plain HTTPS audio
files, so there's no yt-dlp-style extraction step: search gives us a direct,
playable URL right away.

Note: saavn.dev's public instance is a *demo* with rate limiting. For
anything beyond light personal use, self-host your own instance (the repo
deploys to Vercel in a couple of commands) and point SAAVN_API_BASE at it.
"""

import os
import httpx

SAAVN_API_BASE = os.getenv("SAAVN_API_BASE", "https://saavn.dev/api")


class JioSaavnError(Exception):
    pass


def _pick_url(arr) -> str | None:
    """downloadUrl/image entries are lists of {quality, url|link} — take the
    last (highest quality) and accept either key name, since this varies
    across API versions."""
    if not arr:
        return None
    last = arr[-1]
    return last.get("url") or last.get("link")


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _artist_names(song) -> str | None:
    """Newer API versions return artists as {"primary": [{"name": ...}]};
    older ones used a plain `primaryArtists` string."""
    primary = (song.get("artists") or {}).get("primary") or []
    names = [a.get("name") for a in primary if a.get("name")]
    return ", ".join(names) or song.get("primaryArtists") or song.get("artist")


async def search(query: str, limit: int = 20) -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                f"{SAAVN_API_BASE}/search/songs",
                params={"query": query, "limit": limit},
            )
        except httpx.HTTPError as e:
            raise JioSaavnError(f"Could not reach JioSaavn API: {e}")

    if resp.status_code != 200:
        raise JioSaavnError(f"JioSaavn API returned {resp.status_code}")

    payload = resp.json()
    if payload.get("success") is False:
        raise JioSaavnError(payload.get("message", "Unknown JioSaavn API error"))

    songs = (payload.get("data") or {}).get("results") or []

    results = []
    for song in songs:
        duration_s = song.get("duration")
        results.append({
            "title": song.get("name") or song.get("title"),
            "author": _artist_names(song),
            "duration_ms": int(float(duration_s) * 1000) if duration_s else None,
            "artwork": _pick_url(song.get("image")),
            "uri": _pick_url(song.get("downloadUrl")),  # already a direct audio URL
            "popularity": _to_int(song.get("playCount") or song.get("play_count")),
            "source": "jiosaavn",
            "is_stream": False,
        })

    # Drop any result the API returned without a usable audio URL.
    return [r for r in results if r["uri"]]