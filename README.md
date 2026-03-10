# Chat Site v2

Realtime chat with password auth, message history, and disappearing media.

## Features
- **Password login** — accounts persist across sessions, no re-entering username each time
- **Message history** — last 5 public room messages loaded on join; last 5 DMs per conversation loaded on demand
- **Disappearing media** — images and videos (up to 50 MB) are deleted from the server the moment the recipient views them
- **Private messaging** — click any online user to open a private thread
- **Typing indicators** — shows all users currently typing, not just one
- **Markdown** — messages support bold, italic, tables, etc.

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env and set a strong SECRET_KEY

# 3. Run
python app.py
```

Then open http://localhost:5000

## Media notes
- Supported types: JPEG, PNG, GIF, WebP, MP4, WebM, MOV
- Max size: 50 MB
- Files are stored in `static/uploads/` temporarily until viewed
- Once the recipient opens the media, the file is deleted from disk and the DB record removed
- The sender sees a "Viewed & deleted" confirmation in the chat bubble

## Deployment notes
- Set `SECRET_KEY` to a random 32+ byte hex string in your `.env`
- Set `FLASK_DEBUG=false` in production
- Consider setting `DATABASE_URL` to a Postgres connection string for production
- The `static/uploads/` directory needs to be writable by the server process
