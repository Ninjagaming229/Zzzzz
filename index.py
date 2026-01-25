import os
import datetime
import secrets
from flask import Flask, request
from pymongo import MongoClient
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode
from telegram.ext import Dispatcher, CommandHandler, CallbackQueryHandler, MessageHandler, Filters

# --- CONFIGURATION ---
app = Flask(__name__)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")

# Database Connect
client = MongoClient(MONGO_URI)
db = client.get_database("recap_maker_db")
users_collection = db.users
config_collection = db.system_config

bot = Bot(token=TOKEN)

# --- HELPER FUNCTIONS ---

def get_required_channels():
    # Database ထဲက Admin သတ်မှတ်ထားတဲ့ Channel တွေကို ဆွဲထုတ်တာ
    # Admin Panel မရသေးခင် လောလောဆယ် Channel မရှိရင် အလွတ် [] ပြန်ပေးမယ်
    config = config_collection.find_one({"setting_name": "global_config"})
    if config and "verification_channels" in config:
        return config["verification_channels"] # Format: [{"id": "-100xxxx", "link": "https://t.me/..."}]
    return [] 

def check_subscription(user_id):
    channels = get_required_channels()
    not_joined = []
    
    for ch in channels:
        try:
            # Bot က Channel ထဲမှာ Admin ဖြစ်မှ စစ်လို့ရမယ်
            member = bot.get_chat_member(chat_id=ch['id'], user_id=user_id)
            if member.status in ['left', 'kicked']:
                not_joined.append(ch)
        except Exception as e:
            # Bot က Admin မဟုတ်ရင် Error တက်မယ်၊ အဲ့ကျရင် လောလောဆယ် ကျော်လိုက်မယ်
            print(f"Error checking channel {ch['id']}: {e}")
            
    return not_joined

# --- BOT COMMANDS ---

def start(update, context):
    user = update.effective_user
    chat_id = str(user.id)
    
    # 1. Verification စစ်ဆေးခြင်း
    not_joined_channels = check_subscription(chat_id)
    
    if not_joined_channels:
        # Channel မစုံသေးရင် Join ခိုင်းမယ်
        buttons = []
        for ch in not_joined_channels:
            buttons.append([InlineKeyboardButton(f"👉 Join {ch.get('name', 'Channel')}", url=ch['link'])])
        
        buttons.append([InlineKeyboardButton("✅ Verify", callback_data="check_verify")])
        
        msg = "🛑 **Access Denied**\n\nBot ကို အသုံးပြုရန် အောက်ပါ Channel များကို Join ပေးပါ။"
        update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))
        return

    # 2. Verification အောင်မြင်ရင် Login Info ထုတ်ပေးခြင်း
    process_login_generation(update, user)

def verify_callback(update, context):
    query = update.callback_query
    user = query.from_user
    chat_id = str(user.id)
    
    not_joined_channels = check_subscription(chat_id)
    
    if not_joined_channels:
        query.answer("⚠️ Channel များကို မဝင်ရသေးပါ။", show_alert=True)
    else:
        query.answer("✅ Verification Success!")
        query.message.delete() # ခလုတ်အဟောင်းဖျက်
        # Login Info ထုတ်ပေးမယ့် Function ကို လှမ်းခေါ်
        # (Callback ဖြစ်လို့ context.bot.send_message သုံးရမယ်)
        process_login_generation_callback(context.bot, chat_id, user)

def process_login_generation(update, user):
    chat_id = str(user.id)
    existing_user = users_collection.find_one({"telegram_id": chat_id})
    
    if not existing_user:
        # User အသစ်
        username = f"User_{chat_id}"
        password = secrets.token_urlsafe(8)
        
        new_user = {
            "telegram_id": chat_id,
            "first_name": user.first_name,
            "login_username": username,
            "password": password,
            "coins": 0,
            "daily_usage": 0,
            "is_verified": True,
            "joined_at": datetime.datetime.now()
        }
        users_collection.insert_one(new_user)
        
        msg = (
            f"✅ **Verified & Registered!**\n\n"
            f"🌐 **Web Login Info:**\n"
            f"👤 Username: `{username}`\n"
            f"🔐 Password: `{password}`\n\n"
            f"⚠️ ဒီ Password ကိုသုံးပြီး Website မှာ Login ဝင်ပါ။"
        )
        update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    else:
        # User ဟောင်း
        users_collection.update_one({"telegram_id": chat_id}, {"$set": {"is_verified": True}})
        msg = (
            f"👋 Welcome Back!\n\n"
            f"👤 Username: `{existing_user['login_username']}`\n"
            f"🔐 Password: `{existing_user['password']}`"
        )
        update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

def process_login_generation_callback(bot, chat_id, user):
    # Callback အတွက် သီးသန့် Function
    existing_user = users_collection.find_one({"telegram_id": chat_id})
    if not existing_user:
        username = f"User_{chat_id}"
        password = secrets.token_urlsafe(8)
        new_user = {
            "telegram_id": chat_id,
            "first_name": user.first_name,
            "login_username": username,
            "password": password,
            "coins": 0,
            "daily_usage": 0,
            "is_verified": True,
            "joined_at": datetime.datetime.now()
        }
        users_collection.insert_one(new_user)
        msg = f"✅ **Verified!**\n👤 User: `{username}`\n🔐 Pass: `{password}`"
        bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)
    else:
        users_collection.update_one({"telegram_id": chat_id}, {"$set": {"is_verified": True}})
        msg = f"👋 Welcome Back!\n👤 User: `{existing_user['login_username']}`\n🔐 Pass: `{existing_user['password']}`"
        bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

def forgot(update, context):
    chat_id = str(update.effective_user.id)
    
    # Channel ပြန်စစ်မယ်
    not_joined = check_subscription(chat_id)
    if not_joined:
        update.message.reply_text("⛔️ Channel ထဲက ထွက်သွားတဲ့အတွက် Password ပြန်ကြည့်ခွင့် ပိတ်ထားပါတယ်။\nကျေးဇူးပြု၍ /start နှိပ်ပြီး ပြန် Join ပါ။")
        # DB မှာ Verified False လုပ်
        users_collection.update_one({"telegram_id": chat_id}, {"$set": {"is_verified": False}})
        return

    user_data = users_collection.find_one({"telegram_id": chat_id})
    if user_data:
        msg = (
            f"🔐 **Password Recovery**\n\n"
            f"👤 Username: `{user_data['login_username']}`\n"
            f"🔑 Password: `{user_data['password']}`"
        )
        update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    else:
        update.message.reply_text("⚠️ Account မရှိသေးပါ။ /start ကို နှိပ်ပါ။")

# --- SERVER & WEBHOOK ---

@app.route('/')
def home():
    return "🤖 Telegram Bot is Running Separate!", 200

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), bot)
        dispatcher = Dispatcher(bot, None, workers=0)
        
        # Handlers
        dispatcher.add_handler(CommandHandler("start", start))
        dispatcher.add_handler(CommandHandler("forgot", forgot))
        dispatcher.add_handler(CommandHandler("verify", start)) # /verify is same as start check
        dispatcher.add_handler(CallbackQueryHandler(verify_callback, pattern="check_verify"))
        
        dispatcher.process_update(update)
        return "OK"
    return "OK"

if __name__ == "__main__":
    app.run()
    
