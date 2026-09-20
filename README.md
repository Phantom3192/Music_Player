# Ghost Cave — personal music player

Browser-based player with three sources: SoundCloud (via `yt-dlp`),
JioSaavn (via a `jiosaavn-api` instance), and YouTube (via your own Lavalink
server, which handles YouTube's OAuth / PO-token checks so no cookies are
needed here).

## Architecture

```
Browser (index.html/app.js)
   |
   |-- GET /api/search?q=...&source=sc|jiosaavn|youtube
   |     |-- sc        -> yt-dlp search (extract_flat, fast metadata only)
   |     |-- jiosaavn  -> jiosaavn-api /api/search/songs
   |     '-- youtube   -> Lavalink /v4/loadtracks (ytmsearch, then ytsearch)
   |
   '-- GET /api/stream?url=...&source=...
         |-- sc        -> yt-dlp resolves direct audio -> proxied w/ Range
         |-- jiosaavn  -> already a direct audio URL -> proxied straight through
         '-- youtube   -> Lavalink /youtube/stream/<id> -> downloaded once,
                          cached in memory, served with Range support
```

## Setup

```bash
cd backend
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
# Configuration goes in environment variables (or a .env file in backend/):
#   SAAVN_API_BASE     e.g. https://your-jiosaavn-api.vercel.app/api
#   LAVALINK_URL       e.g. http://127.0.0.1:26135
#   LAVALINK_PASSWORD  your Lavalink server password
```

Run it:

```bash
cd backend
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000 — the backend also serves the frontend directly.

## Notes / known rough edges

- **YouTube source (Lavalink):** needs `LAVALINK_URL` and `LAVALINK_PASSWORD`.
  Lavalink's YouTube plugin must be running with its OAuth token / PO-token
  helper. If it breaks, only the YouTube source is affected. Each track is
  downloaded once into an in-memory cache (20 tracks, max 40 MB each) so the
  seek bar works; the video id is validated before it is used in a Lavalink URL.

- **Stream cache:** resolved SoundCloud CDN URLs are cached in-memory for 3
  hours (`_stream_cache` in `main.py`). Fine for a single-process personal
  deploy; swap for Redis if you ever run multiple backend workers.
- **Concurrency caps:** `main.py`'s `_extract_semaphore` limits simultaneous
  full audio extractions to 4, and `soundcloud_search.py`'s
  `_search_semaphore` does the same for search calls (also 4). Search is
  cheap (`extract_flat`, no full resolution), so it can handle more than
  streaming can — bump each independently based on what you see in practice.
- **JioSaavn source:** uses the public demo instance at `saavn.dev`, which
  is rate-limited (it's meant for demos, not production traffic). For
  regular use beyond light/personal, deploy your own instance of
  [sumitkolhe/jiosaavn-api](https://github.com/sumitkolhe/jiosaavn-api)
  (one-click Vercel deploy) and point `SAAVN_API_BASE` in `.env` at it.
  JioSaavn's own API is undocumented and could change shape without notice —
  if search suddenly returns empty results, check the saavn.dev repo's
  issues page first.
- **SoundCloud:** no cookies or bot-detection workarounds needed currently —
  `yt-dlp` resolves it the same way it did for YouTube, just without the
  friction.
- **This is for personal/portfolio use.** Scraping SoundCloud audio outside
  its official API isn't within its ToS — fine for a project you and a few
  friends use, not something to scale into a public product.

## Where to go next

- Persist the queue (localStorage first, then a real DB if you add accounts)
- Add a "liked songs" list
- Swap the in-memory stream cache for Redis if you deploy multiple workers
- Add basic auth if you expose this beyond your own network