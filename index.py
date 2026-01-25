import os
import datetime
from flask import Flask, request
from pymongo import MongoClient
from telegram import Update, Bot
from telegram.ext import Dispatcher, CommandHandler, MessageHandler, Filters

# --- CONFIGURATION ---
app = Flask(__name__)

# Vercel Environment Variables မှ ယူမည်
TOKEN = os.environ.get("TELEGRAM_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")

# Database ချိတ်ဆက်ခြင်း
client = MongoClient(MONGO_URI)
db = client.get_database("recap_maker_db")
users_collection = db.users

# Bot Setting (Global)
bot = Bot(token=TOKEN)

# --- BOT FUNCTIONS ---

def start(update, context):
    try:
        user = update.effective_user
        chat_id = str(user.id)
        
        # User အသစ်လား စစ်ဆေးခြင်း
        if not users_collection.find_one({"telegram_id": chat_id}):
            new_user = {
                "telegram_id": chat_id,
                "username": user.username or "Unknown",
                "first_name": user.first_name,
                "coins": 0,
                "joined_at": datetime.datetime.now()
            }
            users_collection.insert_one(new_user)
            update.message.reply_text(f"👋 မင်္ဂလာပါ! Recap Bot မှ ကြိုဆိုပါတယ်။\n🆔 ID: {chat_id}")
        else:
            # User ဟောင်းဆိုရင် Coin လက်ကျန်ပြမယ်
            u_data = users_collection.find_one({"telegram_id": chat_id})
            coins = u_data.get("coins", 0)
            update.message.reply_text(f"👋 ကြိုဆိုပါတယ်။\n💎 လက်ကျန် Coins: {coins}")
            
    except Exception as e:
        print(f"Error in start: {e}")

def echo(update, context):
    # စာပြန်စမ်းသပ်ခြင်း
    update.message.reply_text("Bot is working! You said: " + update.message.text)

# --- SERVER ROUTES ---

@app.route('/')
def home():
    return "🤖 Telegram Bot is Running on Vercel!", 200

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.method == "POST":
        # Telegram က ပို့လိုက်တဲ့ Data ကို ယူမယ်
        update = Update.de_json(request.get_json(force=True), bot)
        
        # Dispatcher တည်ဆောက်မယ် (workers=0 ထားရမယ် Vercel အတွက်)
        dispatcher = Dispatcher(bot, None, workers=0)
        
        # Command တွေကို ထည့်မယ်
        dispatcher.add_handler(CommandHandler("start", start))
        dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, echo))
        
        # အလုပ်လုပ်ခိုင်းမယ်
        dispatcher.process_update(update)
        return "OK"
    return "OK"

# Vercel entry point
if __name__ == "__main__":
    app.run()
                                
