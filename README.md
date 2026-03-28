# Recap Maker API Gateway

Lightweight API gateway for the Recap Maker Android app.
Deployed on Vercel (serverless) + MongoDB Atlas (cloud).

## Endpoints

### Auth
- `POST /api/register` — username + password + email → JWT
- `POST /api/login` — username/email + password → JWT
- `POST /api/link-email` — add email to existing account
- `POST /api/forgot-password` — send 6-digit reset code to email
- `POST /api/reset-password` — verify code + set new password
- `POST /api/change-password` — change password (requires old password)

### User & Config
- `GET /api/user-info` — coin balance, checkin status, pricing
- `GET /api/config` — app config (pricing, maintenance mode)

### Coins
- `POST /api/daily-checkin` — earn silver coins
- `POST /api/deduct-coins` — deduct before video processing
- `POST /api/refund-coins` — refund on processing failure

### AI Proxy (protects API keys)
- `POST /api/ai/tts` — Gemini TTS (text → audio base64)
- `POST /api/ai/stt` — Groq Whisper (audio file → transcript)
- `POST /api/ai/analyze` — Gemini text analysis (script translation)

### Other
- `GET /api/vpn-check` — VPN/proxy detection
- `GET /api/health` — service + database health

## Deploy to Vercel

1. Push this folder to a GitHub repo
2. Go to vercel.com → New Project → Import repo
3. Add environment variables from .env.example
4. Deploy

## MongoDB Atlas Setup

1. Create free M0 cluster at mongodb.com/atlas
2. Get connection string
3. Set as MONGO_URI in Vercel env vars
4. Your existing recap_maker_db data works as-is

## Password Migration

Existing users with plaintext passwords are auto-migrated
to bcrypt on their first login. No manual action needed.
