"""
SoundCloud search via yt-dlp. YouTube has been dropped from this project —
it was the one source needing cookies/bot-detection workarounds, while
SoundCloud and JioSaavn both work cookie-free.

Uses yt-dlp's scsearchN: extractor with extract_flat so search stays fast
(metadata only) — full extraction only happens later, in main.py's
/api/stream, for whichever track actually gets played.
"""

import asyncio

import yt_dlp

_search_semaphore = asyncio.Semaphore(4)


class SearchError(Exception):
    pass


def _run_search(query: str, limit: int) -> list[dict]:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "default_search": "scsearch",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"scsearch{limit}:{query}", download=False)

    entries = info.get("entries") or []
    results = []
    for e in entries:
        if not e:
            continue
        results.append({
            "title": e.get("title"),
            "author": e.get("uploader") or e.get("channel"),
            "duration_ms": int(e["duration"] * 1000) if e.get("duration") else None,
            "artwork": e.get("thumbnail") or _best_thumbnail(e.get("thumbnails")),
            "uri": e.get("webpage_url") or e.get("url"),
            "popularity": e.get("view_count"),  # SoundCloud play count
            "source": "soundcloud",
            "is_stream": False,
        })
    return results


def _best_thumbnail(thumbnails):
    if not thumbnails:
        return None
    return thumbnails[-1].get("url")


async def search(query: str, limit: int = 20) -> list[dict]:
    async with _search_semaphore:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, _run_search, query, limit)
        except Exception as e:
            raise SearchError(str(e))