# Eclipse — personal music player

Browser-based player that streams through your own Lavalink node, using the
[browserstream plugin](https://github.com/Phantom3192/Jarvis_Lavalink) in its
standalone HTTP mode (no Discord voice connection needed). The UI uses the
same dark "system console" design as the Jarvis website.

## Architecture

```
Browser (index.html = homepage, player.html + app.js = player)
   |
   |-- GET /api/search?q=...   -> Lavalink  GET /browser/search?identifier=...
   |                              (bare terms are searched as "ytsearch:...";
   |                               a pasted URL is passed straight through)
   |
   '-- GET /api/stream?url=... -> Lavalink  GET /browser/stream?identifier=...
                                  live-transcoded Ogg Opus audio.
                                  `url` here is a track's `uri` from
                                  /api/search - opaque to the frontend.
```

The FastAPI backend holds the Lavalink node's Authorization password
server-side (`lavalink_search.py`) so the browser never sees it and can't be
used to hit the node directly.

## Setup

```bash
cd backend
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r ../requirements.txt
# Configuration goes in environment variables (or a .env file in backend/):
#   LAVALINK_HOST       e.g. 127.0.0.1 (or wherever Jarvis_Lavalink is deployed)
#   LAVALINK_PORT       e.g. 26135 (matches application.yml's server.port)
#   LAVALINK_PASSWORD   same value as application.yml's server.password
#   LAVALINK_SECURE     "true" if the node is behind TLS, otherwise omit
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000 for the homepage, or http://localhost:8000/player for the player.

## Notes

- **The Lavalink node must be reachable from this backend** and running the
  browserstream plugin (v1.1.0+, from `Jarvis_Lavalink/plugins/`), started
  outside a Discord voice session — see that repo's `start.sh`.
- **No seeking on the server side.** `/browser/stream` transcodes live and
  doesn't support byte-range requests, so the progress bar/seek control is
  driven only by what the `<audio>` element has already buffered.
- `/api/stream` only ever talks to the configured `LAVALINK_HOST`, so it
  can't be used as an open proxy to arbitrary URLs.
- This is for personal use.
