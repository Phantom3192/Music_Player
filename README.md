# Eclipse — personal music player

Browser-based player that streams from JioSaavn (via your own
[Jio-Savan-API](https://github.com/Phantom3192/Jio-Savan-API) deployment).
The UI uses the same dark "system console" design as the Jarvis website.

## Architecture

```
Browser (index.html = homepage, player.html + app.js = player)
   |
   |-- GET /api/search?q=...   -> Jio-Savan-API  /result/?query=...
   |
   '-- GET /api/stream?url=... -> proxies the audio file from JioSaavn's CDN
                                  (saavncdn.com only), with Range support
```

## Setup

```bash
cd backend
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r ../requirements.txt
# Configuration goes in environment variables (or a .env file in backend/):
#   SAAVN_API_BASE   e.g. https://your-jio-savan-api.vercel.app
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000 for the homepage, or http://localhost:8000/player for the player.

## Notes

- **Region matters.** JioSaavn restricts a lot of its catalog (especially
  English / major-label tracks) to requests coming from India. Run
  Jio-Savan-API — and, if streams fail, this backend too — from an Indian
  region (e.g. Vercel `bom1`). Tracks JioSaavn flags as restricted show an
  "Unavailable" tag and are sorted below playable ones.
- `/api/stream` only proxies `saavncdn.com` URLs, so it can't be used as an
  open proxy.
- This is for personal use.