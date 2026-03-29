
import os
import datetime
import secrets
import hashlib
import time
import httpx
import bcrypt
import jwt
import logging
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional, List
from pymongo import MongoClient
from bson.objectid import ObjectId

# --- CONFIG ---
app = FastAPI(title="Recap Maker API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MONGO_URI = os.environ.get("MONGO_URI")
JWT_SECRET = os.environ.get("JWT_SECRET", "change_this_in_production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = 30

# Gemini API Keys — GEMINI_API_KEY_1, GEMINI_API_KEY_2, ... (unlimited)
def _load_numbered_keys(prefix: str) -> list:
    """Load keys from env vars like PREFIX_1, PREFIX_2, ... until no more found."""
    keys = []
    i = 1
    while True:
        k = os.environ.get(f"{prefix}_{i}", "")
        if not k:
            break
        keys.append(k.strip())
        i += 1
    return keys

GEMINI_KEYS = _load_numbered_keys("GEMINI_API_KEY")
GROQ_KEYS = _load_numbered_keys("GROQ_API_KEY")

logging.info(f"Loaded {len(GEMINI_KEYS)} Gemini keys, {len(GROQ_KEYS)} Groq keys")

# Email (Resend)
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "noreply@recapmaker.app")

# VPN Detection
PROXYCHECK_KEYS_RAW = os.environ.get("PROXYCHECK_KEYS", "")
PROXYCHECK_KEYS = [k.strip() for k in PROXYCHECK_KEYS_RAW.split(",") if k.strip()]

# Admin Panel
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change_this_admin_password")

# --- DATABASE ---
client = MongoClient(MONGO_URI)
db = client.get_database("recap_maker_db")
users_col = db.users
config_col = db.system_config
transaction_col = db.transaction_logs

logging.basicConfig(level=logging.INFO)


# ═══════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════

def get_config():
    return config_col.find_one({"setting_name": "global_config"}) or {}

def get_user_coins(user):
    return {
        "gold": user.get("gold_coins", user.get("coins", 0)),
        "silver": user.get("silver_coins", 0),
    }

def log_transaction(user_id, trans_type, amount, reason):
    try:
        transaction_col.insert_one({
            "user_id": str(user_id),
            "type": trans_type,
            "amount": amount,
            "reason": reason,
            "timestamp": datetime.datetime.utcnow(),
        })
    except Exception:
        pass

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())

def create_jwt(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "exp": datetime.datetime.utcnow() + datetime.timedelta(days=JWT_EXPIRE_DAYS),
        "iat": datetime.datetime.utcnow(),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


# --- AUTH DEPENDENCY ---
async def get_current_user(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    token = auth.split(" ", 1)[1]
    payload = decode_jwt(token)
    user = users_col.find_one({"_id": ObjectId(payload["sub"])})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if user.get("is_banned", False):
        raise HTTPException(status_code=403, detail="Account banned")
    return user


# --- CLIENT IP ---
def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "0.0.0.0"


# ═══════════════════════════════════════════
# VPN DETECTION (simplified for serverless)
# ═══════════════════════════════════════════

async def check_vpn(ip: str) -> dict:
    result = {"is_vpn": False, "reason": "", "score": 0}
    if ip in ("127.0.0.1", "::1") or ip.startswith(("10.", "172.16.", "192.168.")):
        return result

    async with httpx.AsyncClient(timeout=5) as client:
        # Layer 1: proxycheck.io
        try:
            pc_key = PROXYCHECK_KEYS[0] if PROXYCHECK_KEYS else None
            pc_url = f"https://proxycheck.io/v2/{ip}?vpn=1&asn=1&risk=1"
            if pc_key:
                pc_url += f"&key={pc_key}"
            resp = await client.get(pc_url)
            if resp.status_code == 200:
                data = resp.json()
                ip_data = data.get(ip, {})
                risk = int(ip_data.get("risk", 0))
                if ip_data.get("proxy") == "yes" or risk >= 66:
                    result["is_vpn"] = True
                    result["score"] = risk
                    result["reason"] = f"proxycheck: risk {risk}"
        except Exception:
            pass

        # Layer 2: ip-api.com
        try:
            resp = await client.get(
                f"http://ip-api.com/json/{ip}?fields=status,proxy,hosting"
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "success":
                    if data.get("proxy") or data.get("hosting"):
                        result["is_vpn"] = True
                        result["score"] = max(result["score"], 80)
                        result["reason"] += " | ip-api: proxy/hosting"
        except Exception:
            pass

    return result

def is_vpn_enabled() -> bool:
    config = get_config()
    return config.get("vpn_detection", {}).get("enabled", False)


# ═══════════════════════════════════════════
# REQUEST / RESPONSE MODELS
# ═══════════════════════════════════════════

class RegisterReq(BaseModel):
    username: str
    password: str
    email: Optional[str] = None

class LoginReq(BaseModel):
    username: str  # can be username or email
    password: str

class LinkEmailReq(BaseModel):
    email: str

class ForgotPasswordReq(BaseModel):
    email: str

class ResetPasswordReq(BaseModel):
    email: str
    code: str
    new_password: str

class ChangePasswordReq(BaseModel):
    old_password: str
    new_password: str

class DeductCoinsReq(BaseModel):
    amount: int
    reason: str
    coin_type: str = "auto"  # auto, gold, silver

class RefundCoinsReq(BaseModel):
    amount: int
    reason: str
    coin_type: str = "gold"


# ═══════════════════════════════════════════
# AUTH ROUTES
# ═══════════════════════════════════════════

@app.post("/api/register")
async def register(req: RegisterReq):
    if len(req.username) < 3:
        raise HTTPException(400, "Username must be at least 3 characters")
    if len(req.password) < 4:
        raise HTTPException(400, "Password must be at least 4 characters")

    # Check username taken
    if users_col.find_one({"login_username": req.username}):
        raise HTTPException(409, "Username already taken")

    # Check email taken (if provided)
    if req.email:
        if users_col.find_one({"email": req.email}):
            raise HTTPException(409, "Email already in use")

    config = get_config()
    welcome_gold = config.get("welcome_gold", 0)
    welcome_silver = config.get("welcome_silver", 0)

    user_doc = {
        "login_username": req.username,
        "password": hash_password(req.password),
        "email": req.email,
        "gold_coins": welcome_gold,
        "silver_coins": welcome_silver,
        "is_banned": False,
        "created_at": datetime.datetime.utcnow(),
    }
    result = users_col.insert_one(user_doc)
    user_id = str(result.inserted_id)

    if welcome_gold > 0:
        log_transaction(user_id, "Welcome Bonus (Gold)", welcome_gold, "New registration")
    if welcome_silver > 0:
        log_transaction(user_id, "Welcome Bonus (Silver)", welcome_silver, "New registration")

    token = create_jwt(user_id)
    return {
        "status": "success",
        "token": token,
        "user_id": user_id,
        "gold": welcome_gold,
        "silver": welcome_silver,
    }


@app.post("/api/login")
async def login(req: LoginReq):
    # Trim whitespace from inputs
    username = req.username.strip()
    password = req.password

    # Find by username OR email
    user = users_col.find_one({
        "$or": [
            {"login_username": username},
            {"email": username},
        ]
    })

    if not user:
        raise HTTPException(401, "Invalid username or password")

    if user.get("is_banned", False):
        raise HTTPException(403, "Account banned")

    # Password check — support legacy plaintext migration
    stored_pw = user.get("password", "")
    password_ok = False

    if stored_pw.startswith("$2b$") or stored_pw.startswith("$2a$"):
        # Already bcrypt hashed
        password_ok = verify_password(password, stored_pw)
    else:
        # Legacy plaintext — compare and migrate
        if password == stored_pw:
            password_ok = True
            new_hash = hash_password(password)
            users_col.update_one(
                {"_id": user["_id"]},
                {"$set": {"password": new_hash}}
            )
            logging.info(f"Migrated password to bcrypt for user {user.get('login_username')}")

    if not password_ok:
        raise HTTPException(401, "Invalid username or password")

    user_id = str(user["_id"])
    token = create_jwt(user_id)
    coins = get_user_coins(user)

    return {
        "status": "success",
        "token": token,
        "user_id": user_id,
        "username": user.get("login_username", ""),
        "gold": coins["gold"],
        "silver": coins["silver"],
        "email_missing": not user.get("email"),
    }


@app.post("/api/link-email")
async def link_email(req: LinkEmailReq, user=Depends(get_current_user)):
    if users_col.find_one({"email": req.email, "_id": {"$ne": user["_id"]}}):
        raise HTTPException(409, "Email already in use by another account")

    users_col.update_one({"_id": user["_id"]}, {"$set": {"email": req.email}})
    return {"status": "success", "message": "Email linked"}


@app.post("/api/forgot-password")
async def forgot_password(req: ForgotPasswordReq):
    user = users_col.find_one({"email": req.email})
    if not user:
        # Don't reveal if email exists
        return {"status": "success", "message": "If this email is registered, a code has been sent."}

    code = f"{secrets.randbelow(1000000):06d}"
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    expires = datetime.datetime.utcnow() + datetime.timedelta(minutes=15)

    users_col.update_one(
        {"_id": user["_id"]},
        {"$set": {"reset_code": code_hash, "reset_expires": expires}}
    )

    # Send email via Resend
    if RESEND_API_KEY:
        try:
            async with httpx.AsyncClient() as http:
                await http.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                    json={
                        "from": EMAIL_FROM,
                        "to": [req.email],
                        "subject": "Recap Maker - Password Reset Code",
                        "html": f"<h2>Password Reset</h2><p>Your code: <b>{code}</b></p><p>Expires in 15 minutes.</p>",
                    },
                )
        except Exception as e:
            logging.error(f"Email send error: {e}")

    return {"status": "success", "message": "If this email is registered, a code has been sent."}


@app.post("/api/reset-password")
async def reset_password(req: ResetPasswordReq):
    user = users_col.find_one({"email": req.email})
    if not user:
        raise HTTPException(400, "Invalid request")

    stored_hash = user.get("reset_code", "")
    expires = user.get("reset_expires")

    if not stored_hash or not expires:
        raise HTTPException(400, "No reset code found. Request a new one.")

    if datetime.datetime.utcnow() > expires:
        raise HTTPException(400, "Code expired. Request a new one.")

    code_hash = hashlib.sha256(req.code.encode()).hexdigest()
    if code_hash != stored_hash:
        raise HTTPException(400, "Invalid code")

    if len(req.new_password) < 4:
        raise HTTPException(400, "Password must be at least 4 characters")

    users_col.update_one(
        {"_id": user["_id"]},
        {
            "$set": {"password": hash_password(req.new_password)},
            "$unset": {"reset_code": "", "reset_expires": ""},
        },
    )
    return {"status": "success", "message": "Password updated"}


@app.post("/api/change-password")
async def change_password(req: ChangePasswordReq, user=Depends(get_current_user)):
    stored_pw = user.get("password", "")

    if stored_pw.startswith("$2b$") or stored_pw.startswith("$2a$"):
        if not verify_password(req.old_password, stored_pw):
            raise HTTPException(400, "Current password is incorrect")
    else:
        if req.old_password != stored_pw:
            raise HTTPException(400, "Current password is incorrect")

    if len(req.new_password) < 4:
        raise HTTPException(400, "New password must be at least 4 characters")

    users_col.update_one(
        {"_id": user["_id"]},
        {"$set": {"password": hash_password(req.new_password)}}
    )
    return {"status": "success", "message": "Password changed"}


# ═══════════════════════════════════════════
# USER INFO & CONFIG
# ═══════════════════════════════════════════

@app.get("/api/user-info")
async def user_info(user=Depends(get_current_user)):
    config = get_config()
    coins = get_user_coins(user)
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    checkin_cfg = config.get("daily_task_config", {})

    return {
        "status": "success",
        "username": user.get("login_username", ""),
        "email": user.get("email"),
        "gold": coins["gold"],
        "silver": coins["silver"],
        "checked_in_today": user.get("last_checkin_date") == today,
        "checkin_silver": checkin_cfg.get("checkin_silver", 15),
        "pricing_tiers": config.get("pricing_tiers", []),
        "packages": config.get("packages", []),
        "payment_message": config.get("payment_message", ""),
    }


@app.get("/api/config")
async def get_app_config():
    config = get_config()
    return {
        "status": "success",
        "maintenance_mode": config.get("maintenance_mode", False),
        "pricing_tiers": config.get("pricing_tiers", []),
        "packages": config.get("packages", []),
        "payment_message": config.get("payment_message", ""),
    }


# ═══════════════════════════════════════════
# COIN SYSTEM
# ═══════════════════════════════════════════

@app.post("/api/daily-checkin")
async def daily_checkin(request: Request, user=Depends(get_current_user)):
    # VPN check
    if is_vpn_enabled():
        ip = get_client_ip(request)
        vpn = await check_vpn(ip)
        if vpn["is_vpn"]:
            raise HTTPException(403, "VPN/Proxy detected. Please disable and try again.")

    today = datetime.datetime.now().strftime("%Y-%m-%d")
    if user.get("last_checkin_date") == today:
        raise HTTPException(400, "Already checked in today. Come back tomorrow.")

    config = get_config()
    checkin_silver = config.get("daily_task_config", {}).get("checkin_silver", 15)

    users_col.update_one(
        {"_id": user["_id"]},
        {"$set": {"last_checkin_date": today}, "$inc": {"silver_coins": checkin_silver}},
    )
    log_transaction(str(user["_id"]), "Daily Check-in (Silver)", checkin_silver, "Daily check-in")

    updated = users_col.find_one({"_id": user["_id"]})
    coins = get_user_coins(updated)
    return {
        "status": "success",
        "coins_earned": checkin_silver,
        "coin_type": "silver",
        "gold": coins["gold"],
        "silver": coins["silver"],
    }


@app.post("/api/deduct-coins")
async def deduct_coins(req: DeductCoinsReq, request: Request, user=Depends(get_current_user)):
    # VPN check
    if is_vpn_enabled():
        ip = get_client_ip(request)
        vpn = await check_vpn(ip)
        if vpn["is_vpn"]:
            raise HTTPException(403, "VPN/Proxy detected.")

    config = get_config()
    if config.get("maintenance_mode", False):
        raise HTTPException(503, "System under maintenance")

    coins = get_user_coins(user)
    cost = req.amount

    if cost <= 0:
        return {"status": "success", "cost": 0, "coin_type": "none", "gold": coins["gold"], "silver": coins["silver"]}

    coin_used = "none"

    if req.coin_type == "gold":
        if coins["gold"] < cost:
            raise HTTPException(400, f"Not enough Gold coins. Need {cost}, have {coins['gold']}")
        users_col.update_one({"_id": user["_id"]}, {"$inc": {"gold_coins": -cost}})
        coin_used = "gold"
    elif req.coin_type == "silver":
        if coins["silver"] < cost:
            raise HTTPException(400, f"Not enough Silver coins. Need {cost}, have {coins['silver']}")
        users_col.update_one({"_id": user["_id"]}, {"$inc": {"silver_coins": -cost}})
        coin_used = "silver"
    else:
        # Auto: silver first, then gold
        if coins["silver"] >= cost:
            users_col.update_one({"_id": user["_id"]}, {"$inc": {"silver_coins": -cost}})
            coin_used = "silver"
        elif coins["gold"] >= cost:
            users_col.update_one({"_id": user["_id"]}, {"$inc": {"gold_coins": -cost}})
            coin_used = "gold"
        else:
            raise HTTPException(400, f"Not enough coins. Need {cost} (Silver: {coins['silver']}, Gold: {coins['gold']})")

    log_transaction(str(user["_id"]), f"Deduct ({coin_used})", -cost, req.reason)
    updated = users_col.find_one({"_id": user["_id"]})
    new_coins = get_user_coins(updated)

    return {
        "status": "success",
        "cost": cost,
        "coin_type": coin_used,
        "gold": new_coins["gold"],
        "silver": new_coins["silver"],
    }


@app.post("/api/refund-coins")
async def refund_coins(req: RefundCoinsReq, user=Depends(get_current_user)):
    if req.amount <= 0:
        return {"status": "success"}

    field = "gold_coins" if req.coin_type == "gold" else "silver_coins"
    users_col.update_one({"_id": user["_id"]}, {"$inc": {field: req.amount}})
    log_transaction(str(user["_id"]), f"Refund ({req.coin_type})", req.amount, req.reason)

    updated = users_col.find_one({"_id": user["_id"]})
    coins = get_user_coins(updated)
    return {"status": "success", "gold": coins["gold"], "silver": coins["silver"]}


# ═══════════════════════════════════════════
# AI KEY ROTATION (pick 1 random, retry others on 429)
# ═══════════════════════════════════════════

import random

def _rotation_order(keys: list) -> list:
    """
    Pick ONE random key first → if 429 → try remaining in random order.
    Each request gets a different starting key, spreading load evenly.
    """
    if not keys:
        return []
    shuffled = keys.copy()
    random.shuffle(shuffled)
    return shuffled


# ═══════════════════════════════════════════
# AI PROXY — Gemini TTS (rotation + retry)
# ═══════════════════════════════════════════

# TTS models to try per key: flash first (higher RPM), then pro
GEMINI_TTS_MODELS = [
    "gemini-2.5-flash-preview-tts",
    "gemini-2.5-pro-preview-tts",
]

class GeminiTTSReq(BaseModel):
    text: str
    voice: str = "Puck"

@app.post("/api/ai/tts")
async def gemini_tts_proxy(req: GeminiTTSReq, user=Depends(get_current_user)):
    if not GEMINI_KEYS:
        raise HTTPException(503, "TTS service not configured")

    payload = {
        "contents": [{"parts": [{"text": req.text}]}],
        "generationConfig": {
            "response_modalities": ["AUDIO"],
            "speech_config": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voice_name": req.voice}
                }
            },
        },
    }

    last_error = ""
    shuffled_keys = _rotation_order(GEMINI_KEYS)

    async with httpx.AsyncClient(timeout=30) as http:
        # Try each key × each model
        for key in shuffled_keys:
            for model in GEMINI_TTS_MODELS:
                try:
                    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
                    resp = await http.post(api_url, json=payload)

                    if resp.status_code == 200:
                        data = resp.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            for part in parts:
                                if "inlineData" in part:
                                    return {
                                        "status": "success",
                                        "audio_data": part["inlineData"]["data"],
                                        "mime_type": part["inlineData"].get("mimeType", "audio/mp3"),
                                    }

                    elif resp.status_code == 429:
                        # Rate limited — try next key/model
                        logging.warning(f"Gemini TTS 429 on key ...{key[-6:]}/{model}")
                        last_error = "Rate limited, retrying..."
                        continue
                    else:
                        last_error = resp.text[:200]
                        logging.warning(f"Gemini TTS {resp.status_code} on key ...{key[-6:]}/{model}: {last_error}")
                        continue

                except httpx.TimeoutException:
                    last_error = "Timeout"
                    continue
                except Exception as e:
                    last_error = str(e)
                    continue

    raise HTTPException(503, f"TTS failed after trying all keys. Last error: {last_error}")


# ═══════════════════════════════════════════
# AI PROXY — Groq STT (rotation + retry)
# ═══════════════════════════════════════════

@app.post("/api/ai/stt")
async def groq_stt_proxy(request: Request, user=Depends(get_current_user)):
    if not GROQ_KEYS:
        raise HTTPException(503, "STT service not configured")

    form = await request.form()
    audio_file = form.get("audio")
    if not audio_file:
        raise HTTPException(400, "No audio file provided")

    language = form.get("language", "my")
    model = form.get("model", "whisper-large-v3")
    audio_bytes = await audio_file.read()
    filename = audio_file.filename or "audio.mp3"

    last_error = ""
    shuffled_keys = _rotation_order(GROQ_KEYS)

    async with httpx.AsyncClient(timeout=60) as http:
        for key in shuffled_keys:
            try:
                resp = await http.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {key}"},
                    files={"file": (filename, audio_bytes, "audio/mpeg")},
                    data={
                        "model": model,
                        "language": language,
                        "response_format": "verbose_json",
                        "timestamp_granularities[]": "segment",
                    },
                )
                if resp.status_code == 200:
                    return {"status": "success", "result": resp.json()}
                elif resp.status_code == 429:
                    logging.warning(f"Groq STT 429 on key ...{key[-6:]}")
                    last_error = "Rate limited"
                    continue
                else:
                    last_error = resp.text[:200]
                    continue
            except httpx.TimeoutException:
                last_error = "Timeout"
                continue
            except Exception as e:
                last_error = str(e)
                continue

    raise HTTPException(503, f"STT failed after trying all keys. Last error: {last_error}")


# ═══════════════════════════════════════════
# AI PROXY — Gemini Text (rotation + retry)
# ═══════════════════════════════════════════

GEMINI_TEXT_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.5-pro",
    "gemini-3-flash-preview",
]

class AnalyzeReq(BaseModel):
    text: str = ""
    system_instruction: str = ""
    audio_data: str = ""  # Base64 MP3 for Gemini multimodal transcribe+translate

@app.post("/api/ai/analyze")
async def gemini_analyze_proxy(req: AnalyzeReq, user=Depends(get_current_user)):
    if not GEMINI_KEYS:
        raise HTTPException(503, "AI service not configured")

    # ── Build payload: text-only OR audio multimodal ──
    if req.audio_data:
        # MULTIMODAL: audio + system instruction → Gemini transcribes + translates
        import base64 as _b64
        try:
            audio_bytes = _b64.b64decode(req.audio_data)
            logging.info(f"Audio analyze: {len(audio_bytes)} bytes received")
        except Exception:
            raise HTTPException(400, "Invalid audio base64 data")

        # Detect mime type from header bytes
        mime_type = "audio/mpeg"
        if audio_bytes[:4] == b'RIFF':
            mime_type = "audio/wav"
        elif audio_bytes[:3] == b'fLa':
            mime_type = "audio/flac"

        instruction_text = req.system_instruction or "Transcribe this audio and translate to Burmese."

        # Gemini multimodal format: audio as inlineData + instruction as text
        # systemInstruction must be SEPARATE from contents
        payload = {
            "systemInstruction": {"parts": [{"text": instruction_text}]},
            "contents": [{
                "parts": [
                    {"inlineData": {"mimeType": mime_type, "data": req.audio_data}},
                ]
            }],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 8192,
            }
        }
    else:
        # TEXT-ONLY: existing behavior
        if not req.text:
            raise HTTPException(400, "No text or audio provided")
        payload = {
            "contents": [{"parts": [{"text": req.text}]}],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 8192,
            }
        }
        if req.system_instruction:
            payload["systemInstruction"] = {"parts": [{"text": req.system_instruction}]}

    last_error = ""
    shuffled_keys = _rotation_order(GEMINI_KEYS)

    async with httpx.AsyncClient(timeout=120) as http:
        for key in shuffled_keys:
            for model in GEMINI_TEXT_MODELS:
                try:
                    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
                    resp = await http.post(api_url, json=payload)

                    if resp.status_code == 200:
                        data = resp.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            text = ""
                            for p in parts:
                                if "text" in p:
                                    text += p["text"]
                            if text:
                                return {"status": "success", "text": text.strip()}
                        # No text in response
                        last_error = f"Empty response from {model}"
                        logging.warning(f"Gemini analyze: empty response on {model}")
                        continue

                    elif resp.status_code == 429:
                        logging.warning(f"Gemini analyze 429 on key ...{key[-6:]}/{model}")
                        last_error = "Rate limited"
                        continue
                    else:
                        # Log the full error for debugging
                        error_body = resp.text[:500]
                        logging.error(f"Gemini analyze {resp.status_code} on {model}: {error_body}")
                        last_error = f"{resp.status_code}: {error_body[:100]}"
                        continue
                except httpx.TimeoutException:
                    last_error = "Timeout (audio too long?)"
                    logging.warning(f"Gemini analyze timeout on {model}")
                    continue
                except Exception as e:
                    last_error = str(e)[:100]
                    logging.error(f"Gemini analyze error: {e}")
                    continue

    raise HTTPException(503, f"AI analysis failed after trying all keys. Last error: {last_error}")


# ═══════════════════════════════════════════
# VPN CHECK ENDPOINT
# ═══════════════════════════════════════════

@app.get("/api/vpn-check")
async def vpn_check(request: Request, user=Depends(get_current_user)):
    if not is_vpn_enabled():
        return {"status": "success", "vpn_enabled": False, "is_vpn": False}

    ip = get_client_ip(request)
    result = await check_vpn(ip)
    return {
        "status": "success",
        "vpn_enabled": True,
        "is_vpn": result["is_vpn"],
        "score": result["score"],
    }


# ═══════════════════════════════════════════
# ADMIN AUTH
# ═══════════════════════════════════════════

def create_admin_jwt() -> str:
    payload = {
        "sub": "admin",
        "role": "admin",
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=12),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

async def require_admin(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Admin auth required")
    token = auth.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("role") != "admin":
            raise HTTPException(403, "Not admin")
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Session expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")

class AdminLoginReq(BaseModel):
    username: str
    password: str

@app.post("/admin/login")
async def admin_login(req: AdminLoginReq):
    if req.username == ADMIN_USERNAME and req.password == ADMIN_PASSWORD:
        return {"status": "success", "token": create_admin_jwt()}
    raise HTTPException(401, "Invalid admin credentials")


# ═══════════════════════════════════════════
# ADMIN API — USER MANAGEMENT
# ═══════════════════════════════════════════

@app.get("/admin/api/users")
async def admin_list_users(
    search: str = "",
    page: int = 1,
    limit: int = 20,
    _=Depends(require_admin),
):
    query = {}
    if search:
        query = {
            "$or": [
                {"login_username": {"$regex": search, "$options": "i"}},
                {"email": {"$regex": search, "$options": "i"}},
            ]
        }
    skip = (page - 1) * limit
    total = users_col.count_documents(query)
    users = list(users_col.find(query).sort("created_at", -1).skip(skip).limit(limit))

    result = []
    for u in users:
        result.append({
            "id": str(u["_id"]),
            "username": u.get("login_username", ""),
            "email": u.get("email", ""),
            "gold": u.get("gold_coins", u.get("coins", 0)),
            "silver": u.get("silver_coins", 0),
            "is_banned": u.get("is_banned", False),
            "created_at": u.get("created_at", "").isoformat() if isinstance(u.get("created_at"), datetime.datetime) else "",
            "last_checkin": u.get("last_checkin_date", ""),
        })
    return {"users": result, "total": total, "page": page, "pages": (total + limit - 1) // limit}


@app.get("/admin/api/users/{user_id}")
async def admin_get_user(user_id: str, _=Depends(require_admin)):
    user = users_col.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(404, "User not found")
    coins = get_user_coins(user)
    return {
        "id": str(user["_id"]),
        "username": user.get("login_username", ""),
        "email": user.get("email", ""),
        "gold": coins["gold"],
        "silver": coins["silver"],
        "is_banned": user.get("is_banned", False),
        "created_at": user.get("created_at", "").isoformat() if isinstance(user.get("created_at"), datetime.datetime) else "",
    }


class AdminCoinReq(BaseModel):
    user_id: str
    coin_type: str  # gold or silver
    amount: int
    reason: str = "Admin adjustment"

@app.post("/admin/api/add-coins")
async def admin_add_coins(req: AdminCoinReq, _=Depends(require_admin)):
    user = users_col.find_one({"_id": ObjectId(req.user_id)})
    if not user:
        raise HTTPException(404, "User not found")

    field = "gold_coins" if req.coin_type == "gold" else "silver_coins"
    users_col.update_one({"_id": user["_id"]}, {"$inc": {field: req.amount}})
    log_transaction(req.user_id, f"Admin Add ({req.coin_type})", req.amount, req.reason)

    updated = users_col.find_one({"_id": user["_id"]})
    coins = get_user_coins(updated)
    return {"status": "success", "gold": coins["gold"], "silver": coins["silver"]}


class AdminBanReq(BaseModel):
    user_id: str
    banned: bool

@app.post("/admin/api/ban-user")
async def admin_ban_user(req: AdminBanReq, _=Depends(require_admin)):
    users_col.update_one({"_id": ObjectId(req.user_id)}, {"$set": {"is_banned": req.banned}})
    return {"status": "success", "is_banned": req.banned}


class AdminResetPwReq(BaseModel):
    user_id: str
    new_password: str

@app.post("/admin/api/reset-user-password")
async def admin_reset_user_password(req: AdminResetPwReq, _=Depends(require_admin)):
    users_col.update_one(
        {"_id": ObjectId(req.user_id)},
        {"$set": {"password": hash_password(req.new_password)}}
    )
    return {"status": "success", "message": "Password reset"}


# ═══════════════════════════════════════════
# ADMIN API — CONFIG / PACKAGES
# ═══════════════════════════════════════════

@app.get("/admin/api/config")
async def admin_get_config(_=Depends(require_admin)):
    config = get_config()
    return {
        "maintenance_mode": config.get("maintenance_mode", False),
        "pricing_tiers": config.get("pricing_tiers", []),
        "packages": config.get("packages", []),
        "payment_message": config.get("payment_message", ""),
        "daily_task_config": config.get("daily_task_config", {}),
        "vpn_detection": config.get("vpn_detection", {}),
        "welcome_gold": config.get("welcome_gold", 0),
        "welcome_silver": config.get("welcome_silver", 0),
    }


class AdminConfigUpdate(BaseModel):
    maintenance_mode: Optional[bool] = None
    pricing_tiers: Optional[list] = None
    packages: Optional[list] = None
    payment_message: Optional[str] = None
    daily_task_config: Optional[dict] = None
    vpn_detection: Optional[dict] = None
    welcome_gold: Optional[int] = None
    welcome_silver: Optional[int] = None

@app.post("/admin/api/config")
async def admin_update_config(req: AdminConfigUpdate, _=Depends(require_admin)):
    update = {}
    for field, value in req.dict(exclude_none=True).items():
        update[field] = value

    if update:
        config_col.update_one(
            {"setting_name": "global_config"},
            {"$set": update},
            upsert=True,
        )
    return {"status": "success", "updated_fields": list(update.keys())}


# ═══════════════════════════════════════════
# ADMIN API — STATS
# ═══════════════════════════════════════════

@app.get("/admin/api/stats")
async def admin_stats(_=Depends(require_admin)):
    total_users = users_col.count_documents({})
    banned_users = users_col.count_documents({"is_banned": True})
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    checkins_today = users_col.count_documents({"last_checkin_date": today})

    # Users registered in last 7 days
    week_ago = datetime.datetime.utcnow() - datetime.timedelta(days=7)
    new_users_week = users_col.count_documents({"created_at": {"$gte": week_ago}})

    # Recent transactions
    recent_tx = list(transaction_col.find().sort("timestamp", -1).limit(20))
    for tx in recent_tx:
        tx["_id"] = str(tx["_id"])
        if isinstance(tx.get("timestamp"), datetime.datetime):
            tx["timestamp"] = tx["timestamp"].strftime("%Y-%m-%d %H:%M")

    return {
        "total_users": total_users,
        "banned_users": banned_users,
        "checkins_today": checkins_today,
        "new_users_week": new_users_week,
        "recent_transactions": recent_tx,
    }


# --- TEMPORARY DEBUG: Check user password format ---
@app.get("/admin/api/debug-user")
async def debug_user(username: str = "", _=Depends(require_admin)):
    if not username:
        return {"error": "Add ?username=xxx"}
    user = users_col.find_one({"login_username": username})
    if not user:
        return {"found": False, "searched": username}
    stored_pw = user.get("password", "")
    return {
        "found": True,
        "username": user.get("login_username"),
        "has_password_field": "password" in user,
        "password_length": len(stored_pw),
        "password_starts_with": stored_pw[:10] if stored_pw else "(empty)",
        "is_bcrypt": stored_pw.startswith("$2b$") or stored_pw.startswith("$2a$"),
        "is_plaintext": not (stored_pw.startswith("$2b$") or stored_pw.startswith("$2a$")) and len(stored_pw) > 0,
        "all_fields": [k for k in user.keys() if k != "password"],
    }


# ═══════════════════════════════════════════
# ADMIN HTML PANEL
# ═══════════════════════════════════════════

ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Recap Maker Admin</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,system-ui,sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}
.login-wrap{display:flex;align-items:center;justify-content:center;min-height:100vh}
.login-box{background:#1e293b;padding:32px;border-radius:12px;width:320px}
.login-box h2{margin-bottom:16px;color:#7c6aff}
input,select,textarea{width:100%;padding:10px 12px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;border-radius:8px;margin-bottom:12px;font-size:14px}
input:focus,select:focus,textarea:focus{outline:none;border-color:#7c6aff}
button,.btn{padding:10px 16px;border:none;border-radius:8px;cursor:pointer;font-size:14px;font-weight:500}
.btn-primary{background:#7c6aff;color:#fff}.btn-primary:hover{background:#6c5ce7}
.btn-danger{background:#ef4444;color:#fff}.btn-danger:hover{background:#dc2626}
.btn-success{background:#10b981;color:#fff}.btn-success:hover{background:#059669}
.btn-sm{padding:6px 12px;font-size:12px}
.app{display:none}
.topbar{background:#1e293b;padding:12px 24px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #334155}
.topbar h1{font-size:18px;color:#7c6aff}
.tabs{display:flex;gap:4px;padding:12px 24px;background:#1e293b}
.tab{padding:8px 16px;border-radius:8px;cursor:pointer;color:#94a3b8;font-size:14px}
.tab.active{background:#7c6aff;color:#fff}
.content{padding:24px;max-width:900px;margin:0 auto}
.card{background:#1e293b;border-radius:12px;padding:20px;margin-bottom:16px;border:1px solid #334155}
.card h3{color:#7c6aff;margin-bottom:12px;font-size:16px}
.stat{display:inline-block;background:#0f172a;padding:12px 20px;border-radius:8px;margin:4px;text-align:center}
.stat .num{font-size:24px;font-weight:700;color:#7c6aff}
.stat .label{font-size:11px;color:#94a3b8;margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:8px 12px;color:#94a3b8;border-bottom:1px solid #334155;font-weight:500}
td{padding:8px 12px;border-bottom:1px solid #1e293b}
.badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:500}
.badge-red{background:#7f1d1d;color:#fca5a5}
.badge-green{background:#064e3b;color:#6ee7b7}
.row{display:flex;gap:12px;flex-wrap:wrap}
.row>*{flex:1;min-width:200px}
.msg{padding:10px;border-radius:8px;margin-bottom:12px;font-size:13px;display:none}
.msg-ok{background:#064e3b;color:#6ee7b7;display:block}
.msg-err{background:#7f1d1d;color:#fca5a5;display:block}
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;z-index:100;display:none}
.modal{background:#1e293b;padding:24px;border-radius:12px;width:400px;max-width:90vw}
.modal h3{margin-bottom:16px}
.mt8{margin-top:8px}.mb8{margin-bottom:8px}
</style>
</head>
<body>

<!-- LOGIN -->
<div class="login-wrap" id="loginWrap">
<div class="login-box">
<h2>Admin Login</h2>
<input id="aUser" placeholder="Username">
<input id="aPass" type="password" placeholder="Password">
<button class="btn btn-primary" style="width:100%" onclick="doLogin()">Login</button>
<div id="loginMsg" class="msg" style="margin-top:12px"></div>
</div>
</div>

<!-- APP -->
<div class="app" id="app">
<div class="topbar">
<h1>Recap Maker Admin</h1>
<button class="btn btn-sm btn-danger" onclick="logout()">Logout</button>
</div>
<div class="tabs">
<div class="tab active" onclick="showTab('dashboard')">Dashboard</div>
<div class="tab" onclick="showTab('users')">Users</div>
<div class="tab" onclick="showTab('config')">Config</div>
<div class="tab" onclick="showTab('packages')">Packages</div>
</div>

<!-- DASHBOARD TAB -->
<div class="content" id="tab-dashboard">
<div id="statsArea"></div>
<div class="card">
<h3>Recent Transactions</h3>
<div style="overflow-x:auto"><table><thead><tr><th>User</th><th>Type</th><th>Amount</th><th>Reason</th><th>Time</th></tr></thead><tbody id="txBody"></tbody></table></div>
</div>
</div>

<!-- USERS TAB -->
<div class="content" id="tab-users" style="display:none">
<div class="card">
<div class="row mb8">
<input id="userSearch" placeholder="Search username or email..." style="margin:0" onkeyup="if(event.key==='Enter')searchUsers()">
<button class="btn btn-primary" onclick="searchUsers()">Search</button>
</div>
<div style="overflow-x:auto"><table><thead><tr><th>Username</th><th>Email</th><th>Gold</th><th>Silver</th><th>Status</th><th>Actions</th></tr></thead><tbody id="usersBody"></tbody></table></div>
<div id="userPages" style="margin-top:12px;text-align:center"></div>
</div>
</div>

<!-- CONFIG TAB -->
<div class="content" id="tab-config" style="display:none">
<div id="configMsg" class="msg"></div>
<div class="card">
<h3>General Settings</h3>
<label style="color:#94a3b8;font-size:13px">Maintenance Mode</label>
<select id="cfgMaint"><option value="false">OFF</option><option value="true">ON</option></select>
<div class="row">
<div><label style="color:#94a3b8;font-size:13px">Welcome Gold</label><input id="cfgWelcomeGold" type="number"></div>
<div><label style="color:#94a3b8;font-size:13px">Welcome Silver</label><input id="cfgWelcomeSilver" type="number"></div>
</div>
<label style="color:#94a3b8;font-size:13px">Daily Check-in Silver Reward</label>
<input id="cfgCheckinSilver" type="number">
<label style="color:#94a3b8;font-size:13px">Payment Message</label>
<textarea id="cfgPayMsg" rows="3"></textarea>
<button class="btn btn-primary mt8" onclick="saveConfig()">Save Config</button>
</div>
<div class="card">
<h3>Pricing Tiers</h3>
<div id="tiersArea"></div>
<button class="btn btn-success btn-sm mt8" onclick="addTier()">+ Add Tier</button>
<button class="btn btn-primary btn-sm mt8" onclick="saveTiers()">Save Tiers</button>
</div>
<div class="card">
<h3>VPN Detection</h3>
<select id="cfgVpnEnabled"><option value="false">Disabled</option><option value="true">Enabled</option></select>
<button class="btn btn-primary btn-sm mt8" onclick="saveVpn()">Save</button>
</div>
</div>

<!-- PACKAGES TAB -->
<div class="content" id="tab-packages" style="display:none">
<div id="pkgMsg" class="msg"></div>
<div class="card">
<h3>Coin Packages</h3>
<div id="pkgArea"></div>
<button class="btn btn-success btn-sm mt8" onclick="addPackage()">+ Add Package</button>
<button class="btn btn-primary mt8" onclick="savePackages()">Save Packages</button>
</div>
</div>
</div>

<!-- USER DETAIL MODAL -->
<div class="modal-bg" id="userModal">
<div class="modal">
<h3 id="modalTitle">User Detail</h3>
<div id="modalBody"></div>
<div style="text-align:right;margin-top:16px">
<button class="btn btn-sm" style="background:#334155;color:#e2e8f0" onclick="closeModal()">Close</button>
</div>
</div>
</div>

<script>
let TOKEN='';
const API='';
const h=()=>({Authorization:'Bearer '+TOKEN,'Content-Type':'application/json'});

async function doLogin(){
 const u=document.getElementById('aUser').value,p=document.getElementById('aPass').value;
 try{
  const r=await fetch(API+'/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})});
  const d=await r.json();
  if(d.token){TOKEN=d.token;localStorage.setItem('at',TOKEN);showApp()}
  else{document.getElementById('loginMsg').className='msg msg-err';document.getElementById('loginMsg').textContent=d.detail||'Login failed'}
 }catch(e){document.getElementById('loginMsg').className='msg msg-err';document.getElementById('loginMsg').textContent='Connection error'}
}
function showApp(){document.getElementById('loginWrap').style.display='none';document.getElementById('app').style.display='block';loadDashboard();loadConfig()}
function logout(){TOKEN='';localStorage.removeItem('at');location.reload()}
window.onload=()=>{const t=localStorage.getItem('at');if(t){TOKEN=t;showApp()}}

function showTab(name){
 document.querySelectorAll('.tab').forEach((t,i)=>t.classList.toggle('active',t.textContent.toLowerCase()===name));
 ['dashboard','users','config','packages'].forEach(n=>document.getElementById('tab-'+n).style.display=n===name?'block':'none');
 if(name==='users')searchUsers();
 if(name==='dashboard')loadDashboard();
}

// DASHBOARD
async function loadDashboard(){
 const r=await fetch(API+'/admin/api/stats',{headers:h()});
 const d=await r.json();
 document.getElementById('statsArea').innerHTML=
  `<div class="card"><div class="stat"><div class="num">${d.total_users}</div><div class="label">Total Users</div></div>`+
  `<div class="stat"><div class="num">${d.new_users_week}</div><div class="label">New (7d)</div></div>`+
  `<div class="stat"><div class="num">${d.checkins_today}</div><div class="label">Check-ins Today</div></div>`+
  `<div class="stat"><div class="num">${d.banned_users}</div><div class="label">Banned</div></div></div>`;
 const tb=document.getElementById('txBody');
 tb.innerHTML=d.recent_transactions.map(t=>`<tr><td>${t.user_id||''}</td><td>${t.type||''}</td><td>${t.amount||0}</td><td>${t.reason||''}</td><td>${t.timestamp||''}</td></tr>`).join('');
}

// USERS
let currentPage=1;
async function searchUsers(page=1){
 currentPage=page;
 const s=document.getElementById('userSearch').value;
 const r=await fetch(API+`/admin/api/users?search=${encodeURIComponent(s)}&page=${page}&limit=15`,{headers:h()});
 const d=await r.json();
 const tb=document.getElementById('usersBody');
 tb.innerHTML=d.users.map(u=>`<tr>
  <td>${u.username}</td><td>${u.email||'-'}</td><td>${u.gold}</td><td>${u.silver}</td>
  <td>${u.is_banned?'<span class="badge badge-red">Banned</span>':'<span class="badge badge-green">Active</span>'}</td>
  <td><button class="btn btn-sm btn-primary" onclick="openUser('${u.id}')">Manage</button></td>
 </tr>`).join('');
 let pg='';
 for(let i=1;i<=d.pages;i++)pg+=`<button class="btn btn-sm ${i===page?'btn-primary':''}" style="margin:2px;${i===page?'':'background:#334155;color:#e2e8f0'}" onclick="searchUsers(${i})">${i}</button>`;
 document.getElementById('userPages').innerHTML=pg;
}

async function openUser(id){
 const r=await fetch(API+`/admin/api/users/${id}`,{headers:h()});
 const u=await r.json();
 document.getElementById('modalTitle').textContent=u.username;
 document.getElementById('modalBody').innerHTML=`
  <p style="color:#94a3b8;font-size:13px;margin-bottom:12px">Email: ${u.email||'none'} | Created: ${u.created_at||'?'}</p>
  <div class="row mb8">
   <div><label style="color:#94a3b8;font-size:12px">Gold: ${u.gold}</label></div>
   <div><label style="color:#94a3b8;font-size:12px">Silver: ${u.silver}</label></div>
  </div>
  <h3 style="font-size:14px;margin:12px 0 8px">Add/Remove Coins</h3>
  <div class="row">
   <select id="mCoinType"><option value="gold">Gold</option><option value="silver">Silver</option></select>
   <input id="mCoinAmt" type="number" placeholder="Amount (negative to remove)" style="margin:0">
  </div>
  <input id="mCoinReason" placeholder="Reason" style="margin-top:8px">
  <button class="btn btn-success btn-sm mt8" onclick="doAddCoins('${u.id}')">Apply</button>
  <hr style="border-color:#334155;margin:16px 0">
  <h3 style="font-size:14px;margin-bottom:8px">${u.is_banned?'Unban':'Ban'} User</h3>
  <button class="btn btn-sm ${u.is_banned?'btn-success':'btn-danger'}" onclick="doBan('${u.id}',${!u.is_banned})">${u.is_banned?'Unban User':'Ban User'}</button>
  <hr style="border-color:#334155;margin:16px 0">
  <h3 style="font-size:14px;margin-bottom:8px">Reset Password</h3>
  <input id="mNewPw" type="text" placeholder="New password">
  <button class="btn btn-sm btn-danger" onclick="doResetPw('${u.id}')">Reset Password</button>
 `;
 document.getElementById('userModal').style.display='flex';
}
function closeModal(){document.getElementById('userModal').style.display='none'}
async function doAddCoins(id){
 const ct=document.getElementById('mCoinType').value,amt=parseInt(document.getElementById('mCoinAmt').value),reason=document.getElementById('mCoinReason').value||'Admin';
 if(!amt){alert('Enter amount');return}
 await fetch(API+'/admin/api/add-coins',{method:'POST',headers:h(),body:JSON.stringify({user_id:id,coin_type:ct,amount:amt,reason:reason})});
 openUser(id);searchUsers(currentPage);
}
async function doBan(id,banned){
 if(!confirm(banned?'Ban this user?':'Unban this user?'))return;
 await fetch(API+'/admin/api/ban-user',{method:'POST',headers:h(),body:JSON.stringify({user_id:id,banned:banned})});
 closeModal();searchUsers(currentPage);
}
async function doResetPw(id){
 const pw=document.getElementById('mNewPw').value;
 if(!pw||pw.length<4){alert('Password must be at least 4 chars');return}
 if(!confirm('Reset password?'))return;
 await fetch(API+'/admin/api/reset-user-password',{method:'POST',headers:h(),body:JSON.stringify({user_id:id,new_password:pw})});
 alert('Password reset!');closeModal();
}

// CONFIG
let configData={};
async function loadConfig(){
 const r=await fetch(API+'/admin/api/config',{headers:h()});
 configData=await r.json();
 document.getElementById('cfgMaint').value=String(configData.maintenance_mode||false);
 document.getElementById('cfgWelcomeGold').value=configData.welcome_gold||0;
 document.getElementById('cfgWelcomeSilver').value=configData.welcome_silver||0;
 document.getElementById('cfgCheckinSilver').value=(configData.daily_task_config||{}).checkin_silver||15;
 document.getElementById('cfgPayMsg').value=configData.payment_message||'';
 document.getElementById('cfgVpnEnabled').value=String((configData.vpn_detection||{}).enabled||false);
 renderTiers(configData.pricing_tiers||[]);
 renderPackages(configData.packages||[]);
}
async function saveConfig(){
 const body={
  maintenance_mode:document.getElementById('cfgMaint').value==='true',
  welcome_gold:parseInt(document.getElementById('cfgWelcomeGold').value)||0,
  welcome_silver:parseInt(document.getElementById('cfgWelcomeSilver').value)||0,
  daily_task_config:{checkin_silver:parseInt(document.getElementById('cfgCheckinSilver').value)||15},
  payment_message:document.getElementById('cfgPayMsg').value,
 };
 await fetch(API+'/admin/api/config',{method:'POST',headers:h(),body:JSON.stringify(body)});
 showMsg('configMsg','Saved!',false);
}
async function saveVpn(){
 await fetch(API+'/admin/api/config',{method:'POST',headers:h(),body:JSON.stringify({vpn_detection:{enabled:document.getElementById('cfgVpnEnabled').value==='true'}})});
 showMsg('configMsg','VPN config saved!',false);
}

// PRICING TIERS
let tiers=[];
function renderTiers(t){
 tiers=t||[];
 document.getElementById('tiersArea').innerHTML=tiers.map((t,i)=>`<div class="row mb8">
  <input placeholder="Max seconds" type="number" value="${t.max_seconds||0}" onchange="tiers[${i}].max_seconds=parseInt(this.value)">
  <input placeholder="Cost (coins)" type="number" value="${t.cost||0}" onchange="tiers[${i}].cost=parseInt(this.value)">
  <button class="btn btn-sm btn-danger" onclick="tiers.splice(${i},1);renderTiers(tiers)">X</button>
 </div>`).join('')||'<p style="color:#94a3b8;font-size:13px">No tiers configured</p>';
}
function addTier(){tiers.push({max_seconds:0,cost:0});renderTiers(tiers)}
async function saveTiers(){
 await fetch(API+'/admin/api/config',{method:'POST',headers:h(),body:JSON.stringify({pricing_tiers:tiers})});
 showMsg('configMsg','Tiers saved!',false);
}

// PACKAGES
let packages=[];
function renderPackages(p){
 packages=p||[];
 document.getElementById('pkgArea').innerHTML=packages.map((p,i)=>`<div class="card" style="padding:12px">
  <div class="row mb8">
   <input placeholder="Package name" value="${p.name||''}" onchange="packages[${i}].name=this.value">
   <input placeholder="Price (MMK)" type="number" value="${p.price||0}" onchange="packages[${i}].price=parseInt(this.value)">
  </div>
  <div class="row mb8">
   <input placeholder="Gold coins" type="number" value="${p.gold||0}" onchange="packages[${i}].gold=parseInt(this.value)">
   <input placeholder="Silver coins" type="number" value="${p.silver||0}" onchange="packages[${i}].silver=parseInt(this.value)">
  </div>
  <input placeholder="Description" value="${p.description||''}" onchange="packages[${i}].description=this.value">
  <button class="btn btn-sm btn-danger mt8" onclick="packages.splice(${i},1);renderPackages(packages)">Remove Package</button>
 </div>`).join('')||'<p style="color:#94a3b8;font-size:13px">No packages</p>';
}
function addPackage(){packages.push({name:'',price:0,gold:0,silver:0,description:''});renderPackages(packages)}
async function savePackages(){
 await fetch(API+'/admin/api/config',{method:'POST',headers:h(),body:JSON.stringify({packages:packages})});
 showMsg('pkgMsg','Packages saved!',false);
}
function showMsg(id,text,isErr){const el=document.getElementById(id);el.textContent=text;el.className='msg '+(isErr?'msg-err':'msg-ok');setTimeout(()=>el.className='msg',3000)}
</script>
</body>
</html>"""

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel():
    return ADMIN_HTML


# ═══════════════════════════════════════════
# HEALTH CHECK
# ═══════════════════════════════════════════

@app.get("/")
async def root():
    return {"status": "ok", "service": "Recap Maker API", "version": "2.0"}

@app.get("/api/health")
async def health():
    try:
        client.admin.command("ping")
        db_ok = True
    except Exception:
        db_ok = False
    return {"status": "ok", "database": db_ok}
