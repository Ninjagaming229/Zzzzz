import os
import datetime
import secrets
from flask import Flask, request
from pymongo import MongoClient
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode
from telegram.ext import Dispatcher, CommandHandler, CallbackQueryHandler, MessageHandler, Filters

# --- 1. CONFIGURATION ---
app = Flask(__name__)

# Vercel Environment Variables မှ ရယူခြင်း
TOKEN = os.environ.get("TELEGRAM_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")

# Database ချိတ်ဆက်ခြင်း
client = MongoClient(MONGO_URI)
db = client.get_database("recap_maker_db")
users_collection = db.users
config_collection = db.system_config

# Bot Initialize
bot = Bot(token=TOKEN)

# --- 2. HELPER FUNCTIONS (အကူ Function များ) ---

def get_required_channels():
    """Database ထဲက Admin သတ်မှတ်ထားတဲ့ Channel တွေကို ဆွဲထုတ်သည်"""
    config = config_collection.find_one({"setting_name": "global_config"})
    if config and "verification_channels" in config:
        # Format: [{"id": "-100xxxx", "link": "https://t.me/...", "name": "Channel Name"}]
        return config["verification_channels"]
    return []

def check_subscription(user_id):
    """User က Channel တွေထဲ ဝင်ထားလား စစ်ဆေးသည်"""
    channels = get_required_channels()
    not_joined = []
    
    for ch in channels:
        try:
            # Bot က Channel မှာ Admin ဖြစ်မှ စစ်လို့ရပါမယ်
            member = bot.get_chat_member(chat_id=ch['id'], user_id=user_id)
            if member.status in ['left', 'kicked']:
                not_joined.append(ch)
        except Exception as e:
            # Bot က Admin မဟုတ်ရင် သို့မဟုတ် Error တက်ရင် လောလောဆယ် ကျော်သွားမယ်
            print(f"Error checking channel {ch.get('id')}: {e}")
            # လုံခြုံရေးအရ Error တက်ရင် မဝင်ရသေးဘူးလို့ သတ်မှတ်ချင်ရင် ဒီအောက်က line ကိုဖွင့်ပါ
            # not_joined.append(ch)
            
    return not_joined

def generate_credentials(chat_id, user_first_name):
    """Username နဲ့ Password အသစ် ထုတ်ပေးပြီး Database မှာ သိမ်းသည်"""
    username = f"User_{chat_id}"
    password = secrets.token_urlsafe(8)
    
    # Database မှာ ရှိပြီးသားလား စစ်မယ်
    existing_user = users_collection.find_one({"telegram_id": chat_id})
    
    if not existing_user:
        # User အသစ် ဆောက်မယ်
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
        return username, password, True # True means New User
    else:
        # User ဟောင်းဆိုရင် ရှိပြီးသားကို Update လုပ်မယ် (Password ကတော့ အဟောင်းအတိုင်းထားမယ်)
        users_collection.update_one({"telegram_id": chat_id}, {"$set": {"is_verified": True}})
        return existing_user['login_username'], existing_user['password'], False # False means Old User

# --- 3. BOT COMMAND HANDLERS ---

def start(update, context):
    user = update.effective_user
    chat_id = str(user.id)
    
    # A. Channel Verification စစ်ဆေးခြင်း
    not_joined_channels = check_subscription(chat_id)
    
    if not_joined_channels:
        # Channel မစုံသေးရင် Join ခိုင်းမယ် Button တွေပြမယ်
        buttons = []
        for ch in not_joined_channels:
            btn_text = f"👉 Join {ch.get('name', 'Channel')}"
            buttons.append([InlineKeyboardButton(btn_text, url=ch['link'])])
        
        # Verify ခလုတ်ထည့်မယ်
        buttons.append([InlineKeyboardButton("✅ Verify / ဝင်ပြီးပါပြီ", callback_data="check_verify")])
        
        msg = (
            "🛑 **Access Denied / ဝင်ရောက်ခွင့် မရှိသေးပါ**\n\n"
            "Bot ကို အသုံးပြုရန် အောက်ပါ Channel များကို Join ပေးပါ။\n"
            "Join ပြီးပါက **Verify** ခလုတ်ကို နှိပ်ပါ။"
        )
        update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))
        return

    # B. အကုန်စုံရင် Login Info ထုတ်ပေးမယ်
    username, password, is_new = generate_credentials(chat_id, user.first_name)
    
    if is_new:
        msg = (
            f"✅ **Account Created Successfully!**\n\n"
            f"🌐 **Web Dashboard Login Info:**\n"
            f"👤 Username: `{username}`\n"
            f"🔐 Password: `{password}`\n\n"
            f"⚠️ **အသိပေးချက်:** ဒီ Username နဲ့ Password ကို Website မှာ Login ဝင်ရန် သိမ်းထားပါ။"
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
    """Verify ခလုတ်နှိပ်ရင် လုပ်ဆောင်မည့် အပိုင်း"""
    query = update.callback_query
    user = query.from_user
    chat_id = str(user.id)
    
    # Channel ပြန်စစ်မယ်
    not_joined_channels = check_subscription(chat_id)
    
    if not_joined_channels:
        query.answer("⚠️ Channel များကို မဝင်ရသေးပါ။ သေချာ Join ပေးပါ။", show_alert=True)
    else:
        query.answer("✅ Verification Success!")
        query.message.delete() # ခလုတ်အဟောင်းဖျက်
        
        # Login Info ထုတ်ပေးမယ်
        username, password, is_new = generate_credentials(chat_id, user.first_name)
        
        msg = (
            f"✅ **Verified!**\n\n"
            f"👤 Username: `{username}`\n"
            f"🔐 Password: `{password}`\n\n"
            f"Website တွင် Login ဝင်နိုင်ပါပြီ။"
        )
        context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

def forgot(update, context):
    """Password မေ့သွားရင် ပြန်ကြည့်သည့် Command"""
    chat_id = str(update.effective_user.id)
    
    # 1. Channel ထဲ ရှိမရှိ ပြန်စစ် (Re-Check State)
    not_joined = check_subscription(chat_id)
    
    if not_joined:
        # Channel ထဲက ထွက်သွားရင် Block မယ်
        users_collection.update_one({"telegram_id": chat_id}, {"$set": {"is_verified": False}})
        update.message.reply_text(
            "⛔️ **Access Revoked**\n\n"
            "Channel ထဲမှ ထွက်သွားသည့်အတွက် ဝန်ဆောင်မှု ရပ်ဆိုင်းထားပါသည်။\n"
            "ကျေးဇူးပြု၍ `/start` နှိပ်ပြီး Channel ပြန် Join ပါ။",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # 2. ရှိရင် Password ပြန်ပြမယ်
    user_data = users_collection.find_one({"telegram_id": chat_id})
    
    if user_data:
        msg = (
            f"🔐 **Password Recovery**\n\n"
            f"👤 Username: `{user_data.get('login_username')}`\n"
            f"🔑 Password: `{user_data.get('password')}`"
        )
        update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    else:
        update.message.reply_text("⚠️ Account မရှိသေးပါ။ `/start` ကို နှိပ်ပါ။", parse_mode=ParseMode.MARKDOWN)

# --- 4. FLASK SERVER & WEBHOOK ---

@app.route('/')
def home():
    return "🤖 Telegram Bot is Running on Vercel!", 200

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.method == "POST":
        # Telegram Update ကို လက်ခံရယူခြင်း
        update = Update.de_json(request.get_json(force=True), bot)
        
        # Dispatcher တည်ဆောက်ခြင်း (Vercel အတွက် stateless ဖြစ်ရမည်)
        dispatcher = Dispatcher(bot, None, workers=0)
        
        # Commands များကို ချိတ်ဆက်ခြင်း
        dispatcher.add_handler(CommandHandler("start", start))
        dispatcher.add_handler(CommandHandler("verify", start)) # /verify လည်း start လိုပဲ စစ်မယ်
        dispatcher.add_handler(CommandHandler("forgot", forgot))
        dispatcher.add_handler(CallbackQueryHandler(verify_callback, pattern="check_verify"))
        
        # Process Update
        dispatcher.process_update(update)
        return "OK"
    return "OK"

# Local စက်မှာ Run ရင် အလုပ်လုပ်ဖို့
if __name__ == "__main__":
    app.run(debug=True)
    
