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

# Logging
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
    config = config_collection.find_one({"setting_name": "global_config"})
    if not config:
        return {"verification_message": "Please join our channels.", "verification_channels": []}
    return config

def check_subscription(user_id):
    """User က Channel တွေထဲ ဝင်ထားလား စစ်ဆေးသည်"""
    config = get_global_config()
    channels = config.get("verification_channels", [])
    not_joined = []
    
    for ch in channels:
        identifier = ch.get('link') # Admin Panel က Link နေရာမှာ ID (-100...) ထည့်ထားနိုင်သည်
        
        # Link အရှည်ကြီး (https://...) ဆိုရင် Bot က Member စစ်လို့မရပါ (Admin Panel က ID ဖြုတ်လိုက်လို့)
        if "t.me/" in identifier or "https://" in identifier:
             # ID မသိရင် မစစ်တော့ဘူး (Error မတက်အောင်)
             continue

        try:
            # Bot က Admin ဖြစ်မှ Private ID ကို စစ်လို့ရမယ်
            member = bot.get_chat_member(chat_id=identifier, user_id=user_id)
            if member.status in ['left', 'kicked']:
                # မဝင်ရသေးရင် List ထဲထည့်မယ်
                # Button အတွက် Link လိုတဲ့အတွက်၊ ID ဖြစ်နေရင် Link ရှာထည့်ပေးရမယ်
                if str(identifier).startswith("-100"):
                    try:
                        # Private Chat ဆိုရင် Bot က Invite Link ကို လှမ်းတောင်းမယ်
                        chat_info = bot.get_chat(chat_id=identifier)
                        ch['invite_link'] = chat_info.invite_link
                    except:
                        ch['invite_link'] = None # Link ရှာမတွေ့ရင် None
                
                not_joined.append(ch)
                
        except BadRequest as e:
            logger.warning(f"⚠️ Cannot check member for {identifier}: {e}")
            # Error တက်ရင် Bot က Admin မဟုတ်လို့ ဖြစ်နိုင်တယ်၊ ဒါပေမဲ့ User ကို ဒုက္ခမပေးဘဲ ကျော်လိုက်မယ်
            # not_joined.append(ch) <--- တကယ်လို့ စစ်မရရင် မဝင်ရသေးဘူးလို့ သတ်မှတ်ချင်ရင် ဒါကိုဖွင့်ပါ
        except Exception as e:
            logger.error(f"Error checking channel {identifier}: {e}")
            
    return not_joined, config

def generate_credentials(chat_id, user_first_name):
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
        users_collection.update_one({"telegram_id": chat_id}, {"$set": {"is_verified": True}})
        return existing_user.get('login_username', username), existing_user.get('password', password), False

# --- BOT COMMANDS ---

def start(update, context):
    user = update.effective_user
    chat_id = str(user.id)
    
    # 1. Verification Check
    not_joined_channels, config = check_subscription(chat_id)
    
    if not_joined_channels:
        custom_msg = config.get("verification_message", "⚠️ Bot ကို အသုံးပြုရန် အောက်ပါ Channel များကို Join ပေးပါ။")
        buttons = []
        
        for ch in not_joined_channels:
            btn_text = f"👉 Join {ch.get('name', 'Channel')}"
            
            # Button နှိပ်ရင် သွားမယ့် Link ကို ဆုံးဖြတ်ခြင်း
            target_url = ""
            
            # Code က Auto ရှာပေးထားတဲ့ Invite Link ရှိလား?
            if ch.get('invite_link'):
                target_url = ch['invite_link']
            else:
                # မရှိရင် Admin ထည့်တဲ့ Link အတိုင်း သွားမယ်
                raw_link = ch.get('link')
                if str(raw_link).startswith("-100"):
                    # ID ဖြစ်နေပြီး Invite Link ရှာမရရင် Button အလုပ်မလုပ်နိုင်ဘူး
                    # ဒါကြောင့် Error မတက်အောင် ယာယီ Link ထည့်မယ် (သို့) Bot PM
                    target_url = f"https://t.me/{bot.username}" 
                elif raw_link.startswith("@"):
                    target_url = f"https://t.me/{raw_link.replace('@', '')}"
                else:
                    target_url = raw_link

            buttons.append([InlineKeyboardButton(btn_text, url=target_url)])
        
        buttons.append([InlineKeyboardButton("✅ Verify / ဝင်ပြီးပါပြီ", callback_data="check_verify")])
        
        update.message.reply_text(custom_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))
        return

    # 2. Login Generation
    username, password, is_new = generate_credentials(chat_id, user.first_name)
    if is_new:
        msg = f"✅ **Account Created!**\n\n👤 User: `{username}`\n🔐 Pass: `{password}`\n\n⚠️ **Save This!**"
    else:
        msg = f"👋 **Welcome Back!**\n\n👤 User: `{username}`\n🔐 Pass: `{password}`"
    
    update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

def verify_callback(update, context):
    query = update.callback_query
    user = query.from_user
    chat_id = str(user.id)
    
    not_joined_channels, _ = check_subscription(chat_id)
    
    if not_joined_channels:
        query.answer("⚠️ Channel များကို မဝင်ရသေးပါ။", show_alert=True)
    else:
        query.answer("✅ Success!")
        query.message.delete()
        username, password, _ = generate_credentials(chat_id, user.first_name)
        msg = f"✅ **Verified!**\n\n👤 User: `{username}`\n🔐 Pass: `{password}`"
        context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)

def forgot(update, context):
    chat_id = str(update.effective_user.id)
    not_joined, _ = check_subscription(chat_id)
    if not_joined:
        users_collection.update_one({"telegram_id": chat_id}, {"$set": {"is_verified": False}})
        update.message.reply_text("⛔️ **Access Denied**\nPlease join channel again.", parse_mode=ParseMode.MARKDOWN)
        return

    user_data = users_collection.find_one({"telegram_id": chat_id})
    if user_data:
        msg = f"🔐 **Recovery**\n\n👤 `{user_data.get('login_username')}`\n🔑 `{user_data.get('password')}`"
        update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    else:
        update.message.reply_text("⚠️ No Account. Press /start")

# --- WEBHOOK ---
@app.route('/', methods=['GET'])
def home(): return "Bot Running", 200

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

if __name__ == "__main__":
    app.run()
    
