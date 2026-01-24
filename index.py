from flask import Flask, request, jsonify, render_template, session, redirect, url_for
import os
import requests
import secrets
import datetime
# ပြင်လိုက်သည့်အချက် (2): template_folder="." (အစက်လေး ထည့်ပေးပါ)
app = Flask(__name__, template_folder=".", static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY", "admin-secret-key")

# ... ကျန်တဲ့ Code တွေက အတူတူပါပဲ ...
# ... (Admin Login, Routes, Webhook Code တွေကို အောက်မှာ ဆက်ထည့်ပါ) ...

# --- CONFIG ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
BOT_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Initialize DB defaults on first run
try:
    init_db()
except:
    pass

# --- HELPER: TELEGRAM SEND ---
def send_msg(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(f"{BOT_URL}/sendMessage", json=payload)

def check_member(chat_id, user_id):
    """Check if user is member of channel"""
    try:
        url = f"{BOT_URL}/getChatMember?chat_id={chat_id}&user_id={user_id}"
        res = requests.get(url).json()
        if res.get("ok"):
            status = res["result"]["status"]
            return status in ["member", "administrator", "creator"]
    except:
        return False
    return False

# --- ROUTES: ADMIN PANEL ---

@app.route('/')
def home():
    if not session.get('is_admin'):
        return render_template('admin.html', view='login')
    return render_template('admin.html', view='dashboard')

@app.route('/api/login', methods=['POST'])
def login():
    pw = request.json.get('password')
    if pw == ADMIN_PASSWORD:
        session['is_admin'] = True
        return jsonify({"status": "success"})
    return jsonify({"status": "fail"}), 401

@app.route('/api/stats')
def get_stats():
    if not session.get('is_admin'): return jsonify({}), 403
    
    total_users = users_collection.count_documents({})
    # Active today logic (Reset logic handled in Main App, here just count)
    today_str = str(datetime.date.today())
    active_today = users_collection.count_documents({"daily_usage.date": today_str})
    
    # Calculate Total Revenue (Just a sum of manual topups log - Simplified for now)
    # For now, we just return basic stats
    return jsonify({
        "total_users": total_users,
        "active_today": active_today
    })

@app.route('/api/users', methods=['GET'])
def search_users():
    if not session.get('is_admin'): return jsonify({}), 403
    query = request.args.get('q', '')
    
    # Search by username or telegram_id
    filter_q = {"$or": [{"username": {"$regex": query}}, {"telegram_id": {"$regex": query}}]} if query else {}
    users = list(users_collection.find(filter_q, {"_id": 0}).limit(20))
    return jsonify(users)

@app.route('/api/topup', methods=['POST'])
def topup_user():
    if not session.get('is_admin'): return jsonify({}), 403
    data = request.json
    telegram_id = data.get('telegram_id')
    amount = int(data.get('amount'))
    
    # Update DB
    users_collection.update_one({"telegram_id": telegram_id}, {"$inc": {"coins": amount}})
    user = users_collection.find_one({"telegram_id": telegram_id})
    
    # Notify User
    if user:
        msg = f"💎 <b>Top-up Successful!</b>\n\nReceived: {amount} Coins\nNew Balance: {user['coins']} Coins"
        send_msg(telegram_id, msg)
    
    return jsonify({"status": "success", "new_balance": user['coins']})

@app.route('/api/config', methods=['GET', 'POST'])
def manage_config():
    if not session.get('is_admin'): return jsonify({}), 403
    if request.method == 'GET':
        conf = config_collection.find_one({"setting_name": "global_config"}, {"_id": 0})
        return jsonify(conf)
    else:
        # Update Config
        new_data = request.json
        config_collection.update_one({"setting_name": "global_config"}, {"$set": new_data})
        return jsonify({"status": "success"})

@app.route('/api/packages', methods=['GET', 'POST', 'DELETE'])
def manage_packages():
    if not session.get('is_admin'): return jsonify({}), 403
    if request.method == 'GET':
        pkgs = list(packages_collection.find({}, {"_id": 0}))
        return jsonify(pkgs)
    elif request.method == 'POST':
        pkg = request.json
        if not pkg.get('id'): pkg['id'] = secrets.token_hex(4)
        packages_collection.update_one({"id": pkg['id']}, {"$set": pkg}, upsert=True)
        return jsonify({"status": "success"})
    elif request.method == 'DELETE':
        pid = request.args.get('id')
        packages_collection.delete_one({"id": pid})
        return jsonify({"status": "success"})

# --- ROUTE: TELEGRAM WEBHOOK ---

@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    update = request.json
    
    # 1. Handle Chat Member Updates (Leave/Kick)
    if "my_chat_member" in update:
        mcm = update["my_chat_member"]
        new_status = mcm.get("new_chat_member", {}).get("status")
        user_id = str(mcm.get("from", {}).get("id"))
        
        if new_status in ["left", "kicked"]:
            # User left a channel -> Unverify them
            users_collection.update_one({"telegram_id": user_id}, {"$set": {"is_verified": False}})
            print(f"🚫 User {user_id} left channel. Unverified.")
        return "ok"

    if "message" in update:
        msg = update["message"]
        chat_id = str(msg["chat"]["id"])
        text = msg.get("text", "")
        
        # /start command
        if text.startswith("/start"):
            user = users_collection.find_one({"telegram_id": chat_id})
            if not user:
                # Create Account
                username = f"User_{secrets.token_hex(2).upper()}"
                password = secrets.token_hex(4)
                new_user = {
                    "telegram_id": chat_id,
                    "username": username,
                    "password": password,
                    "coins": 0,
                    "is_verified": False,
                    "daily_usage": {"date": "", "count": 0},
                    "created_at": datetime.datetime.now()
                }
                users_collection.insert_one(new_user)
                reply = f"👋 <b>Welcome to Recap Maker!</b>\n\n🆔 Username: <code>{username}</code>\n🔑 Password: <code>{password}</code>\n\n⚠️ <i>Please verify your account to start using.</i>\nType /verify"
                send_msg(chat_id, reply)
            else:
                send_msg(chat_id, f"👋 Welcome back!\n\n🆔 Username: <code>{user['username']}</code>\nType /verify to check status.")
        
        # /verify command
        elif text.startswith("/verify"):
            conf = config_collection.find_one({"setting_name": "global_config"})
            v_data = conf.get('verification', {})
            channels = v_data.get('channels', [])
            
            if not channels:
                # No verification needed
                users_collection.update_one({"telegram_id": chat_id}, {"$set": {"is_verified": True}})
                send_msg(chat_id, "✅ No channels configured. You are verified automatically!")
                return "ok"

            # Check if already joined
            all_joined = True
            for ch in channels:
                if not check_member(ch['id'], chat_id):
                    all_joined = False
                    break
            
            if all_joined:
                users_collection.update_one({"telegram_id": chat_id}, {"$set": {"is_verified": True}})
                send_msg(chat_id, "✅ <b>Verification Success!</b>\nYou can now login to the website.")
            else:
                # Send Join Links
                buttons = {"inline_keyboard": []}
                for ch in channels:
                    buttons["inline_keyboard"].append([{"text": f"Join {ch['name']}", "url": ch['link']}])
                # Add Check Button (Callback)
                buttons["inline_keyboard"].append([{"text": "✅ Click here after Joining", "callback_data": "check_verify"}])
                
                send_msg(chat_id, v_data.get("message", "Join these channels:"), buttons)

        # /forgot command
        elif text.startswith("/forgot"):
            user = users_collection.find_one({"telegram_id": chat_id})
            if user:
                # Check verify first
                if not user.get('is_verified'):
                    send_msg(chat_id, "❌ Account not verified. Type /verify first.")
                else:
                    send_msg(chat_id, f"🔐 <b>Recovery Info</b>\n\nUser: <code>{user['username']}</code>\nPass: <code>{user['password']}</code>")
            else:
                send_msg(chat_id, "❌ No account found. Type /start")

    # Handle Button Click (Callback Query)
    if "callback_query" in update:
        cb = update["callback_query"]
        chat_id = str(cb["message"]["chat"]["id"])
        data = cb["data"]
        
        if data == "check_verify":
            conf = config_collection.find_one({"setting_name": "global_config"})
            channels = conf.get('verification', {}).get('channels', [])
            
            all_joined = True
            for ch in channels:
                if not check_member(ch['id'], chat_id):
                    all_joined = False
                    break
            
            if all_joined:
                users_collection.update_one({"telegram_id": chat_id}, {"$set": {"is_verified": True}})
                send_msg(chat_id, "✅ <b>Verified!</b> You can now login.")
            else:
                send_msg(chat_id, "❌ You haven't joined all channels yet. Please try again.")

    return "ok"

# Vercel Entry Point
# (Vercel needs `app` to be exposed)
