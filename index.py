import os
import datetime
import secrets
import logging
from flask import Flask, request
from pymongo import MongoClient
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode
from telegram.ext import Dispatcher, CommandHandler, CallbackQueryHandler, MessageHandler, Filters
from telegram.error import BadRequest, Unauthorized

# --- CONFIGURATION ---
app = Flask(__name__)

# Debugging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")

# Database Connect
client = MongoClient(MONGO_URI)
db = client.get_database("recap_maker_db")
users_collection = db.users
config_collection = db.system_config

bot = Bot(token=TOKEN)

# --- HELPER FUNCTIONS ---

def get_global_config():
    """Database ထဲက Admin ပြင်ထားတဲ့ Setting တွေကို ယူမယ်"""
    config = config_collection.find_one({"setting_name": "global_config"})
    if not config:
        # Default Config (Admin Panel မသုံးရသေးခင်)
        return {
            "verification_message": "Please join our channels to use this bot.",
            "verification_channels": []
        }
    return config

def check_subscription(user_id):
    """User က Channel တွေထဲ ဝင်ထားလား စစ်ဆေးသည်"""
    config = get_global_config()
    channels = config.get("verification_channels", [])
    not_joined = []
    
    for ch in channels:
        # Admin Panel မှာ Link နေရာမှာ @username သို့မဟုတ် ID ထည့်ထားမှ စစ်လို့ရမယ်
        # Link အရှည် (https://t.me/...) ထည့်ထားရင် စစ်လို့မရဘူး၊ ဒါပေမဲ့ Button တော့ပြမယ်
        channel_identifier = ch.get('link') 
        
        # URL ဖြစ်နေရင် စစ်မရလို့ ကျော်သွားမယ် (Button ပဲပြမယ်)
        if "t.me/" in channel_identifier or "https://" in channel_identifier:
             # ID အစစ်မရှိရင် မစစ်ဘဲ "ဝင်ပြီး" လို့ ယူဆလိုက်မယ် (Error မတက်အောင်)
             continue

        try:
            # Bot က Admin ဖြစ်မှ စစ်လို့ရမယ်
            member = bot.get_chat_member(chat_id=channel_identifier, user_id=user_id)
            if member.status in ['left', 'kicked']:
                not_joined.append(ch)
        except BadRequest:
            # Bot က Admin မဟုတ်ရင် သို့မဟုတ် ID မှားနေရင်
            logger.warning(f"⚠️ Cannot check member for {channel_identifier}. Make sure Bot is Admin!")
            # မသေချာရင် လွှတ်ပေးလိုက်မလား? သို့မဟုတ် တားမလား? 
            # လောလောဆယ် တားမယ် (Uncomment if you want to skip error channels)
            # not_joined.append(ch)
        except Exception as e:
            logger.error(f"Error checking channel {channel_identifier}: {e}")
            
    return not_joined, config # Config ပါ ပြန်ပို့ပေးမယ် (Message ယူဖို့)

def generate_credentials(chat_id, user_first_name):
    """Username နဲ့ Password ထုတ်ပေးခြင်း"""
    username = f"User_{chat_id}"
    password = secrets.token_urlsafe(8)
    
    existing_user = users_collection.find_one({"telegram_id": chat_id})
    
    if not existing_user:
        new_user = {
            "telegram_id": chat_id,
            "first_name": user_first_name,
            "login_username": username,
            "password": password,
            "coins": 0,
            "is_verified": True,
            "joined_at": datetime.datetime.now()
        }
        users_collection.insert_one(new_user)
        return username, password, True
    else:
        # User ဟောင်းဆိုရင် Update မလုပ်တော့ဘူး၊ ရှိတာပဲ ပြန်ပြမယ်
        current_user = existing_user.get('login_username', username)
        current_pass = existing_user.get('password', password)
        # Verify State Update
        users_collection.update_one({"telegram_id": chat_id}, {"$set": {"is_verified": True}})
        return current_user, current_pass, False

# --- BOT COMMANDS ---

def start(update, context):
    user = update.effective_user
    chat_id = str(user.id)
    
    # 1. Verification စစ်ဆေးခြင်း
    not_joined_channels, config = check_subscription(chat_id)
    
    if not_joined_channels:
        # Admin Panel က ပြင်ထားတဲ့ စာသားကို ယူမယ်
        custom_msg = config.get("verification_message", "⚠️ Bot ကို အသုံးပြုရန် အောက်ပါ Channel များကို Join ပေးပါ။")
        
        buttons = []
        # Join ရမယ့် Channel တွေကို Button လုပ်မယ်
        for ch in not_joined_channels:
            # Button စာသား (Admin Panel က Name)
            btn_text = f"👉 Join {ch.get('name', 'Channel')}"
            # Button Link
            btn_url = ch.get('link')
            
            # URL မဟုတ်ရင် (ID ဖြစ်နေရင်) Link ပြောင်းပေးရမယ်
            if "http" not in btn_url and "t.me" not in btn_url:
                if btn_url.startswith("@"):
                    btn_url = f"https://t.me/{btn_url.replace('@', '')}"
                # ID ဂဏန်းဖြစ်နေရင်တော့ Link လုပ်မရဘူး (Invite link သီးသန့်မရှိရင်)

            buttons.append([InlineKeyboardButton(btn_text, url=btn_url)])
        
        # Verify Button
        buttons.append([InlineKeyboardButton("✅ Verify / ဝင်ပြီးပါပြီ", callback_data="check_verify")])
        
        update.message.reply_text(custom_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))
        return

    # 2. အကုန်စုံရင် Login Info ထုတ်ပေးမယ်
    username, password, is_new = generate_credentials(chat_id, user.first_name)
    
    if is_new:
        msg = (
            f"✅ **Account Created Successfully!**\n\n"
            f"🌐 **Web Dashboard Login Info:**\n"
            f"👤 Username: `{username}`\n"
            f"🔐 Password: `{password}`\n\n"
            f"⚠️ **Login ဝင်ရန် သိမ်းထားပါ။**"
        )
    else:
        msg = (
            f"👋 **Welcome Back!**\n\n"
            f"👤 Username: `{username}`\n"
            f"🔐 Password: `{password}`\n\n"
            f"Website သို့ ဝင်ရောက်နိုင်ပါပြီ။"
        )
    
    update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

def verify_callback(update, context):
    """Verify ခလုတ်နှိပ်ရင် စစ်မယ့် Function"""
    query = update.callback_query
    user = query.from_user
    chat_id = str(user.id)
    
    not_joined_channels, config = check_subscription(chat_id)
    
    if not_joined_channels:
        query.answer("⚠️ Channel များကို မဝင်ရသေးပါ။ သေချာ Join ပေးပါ။", show_alert=True)
    else:
        query.answer("✅ Verification Success!")
        query.message.delete()
        
        # Login Info ထုတ်ပေးမယ်
        username, password, is_new = generate_credentials(chat_id, user.first_name)
        
        msg = (
            f"✅ **Verified!**\n\n"
            f"👤 Username: `{username}`\n"
            f"🔐 Password: `{password}`\n\n"
            f"Login ဝင်နိုင်ပါပြီ။"
        )
        context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

def forgot(update, context):
    chat_id = str(update.effective_user.id)
    
    # Re-check verification before giving password
    not_joined, _ = check_subscription(chat_id)
    
    if not_joined:
        users_collection.update_one({"telegram_id": chat_id}, {"$set": {"is_verified": False}})
        update.message.reply_text("⛔️ **Access Revoked**\nChannel ထဲမှ ထွက်သွားသည့်အတွက် ဝန်ဆောင်မှု ရပ်ဆိုင်းထားပါသည်။\n`/start` နှိပ်ပြီး ပြန် Join ပါ။", parse_mode=ParseMode.MARKDOWN)
        return

    user_data = users_collection.find_one({"telegram_id": chat_id})
    if user_data:
        msg = (
            f"🔐 **Password Recovery**\n\n"
            f"👤 Username: `{user_data.get('login_username')}`\n"
            f"🔑 Password: `{user_data.get('password')}`"
        )
        update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    else:
        update.message.reply_text("⚠️ Account မရှိသေးပါ။ `/start` ကို နှိပ်ပါ။")

# --- SERVER & WEBHOOK ---

@app.route('/')
def home():
    return "🤖 Bot is Running with DB Sync!", 200

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), bot)
        dispatcher = Dispatcher(bot, None, workers=0)
        
        dispatcher.add_handler(CommandHandler("start", start))
        dispatcher.add_handler(CommandHandler("verify", start))
        dispatcher.add_handler(CommandHandler("forgot", forgot))
        dispatcher.add_handler(CallbackQueryHandler(verify_callback, pattern="check_verify"))
        
        dispatcher.process_update(update)
        return "OK"
    return "OK"

if __name__ == "__main__":
    app.run()
    
