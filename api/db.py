import os
from pymongo import MongoClient

# MongoDB Connection String ကို Environment Variable ကနေ ယူမယ်
# (Code ထဲမှာ တိုက်ရိုက်မထည့်တာ လုံခြုံရေးအရ ပိုကောင်းပါတယ်)
MONGO_URI = os.environ.get("MONGO_URI")

client = MongoClient(MONGO_URI)
db = client.get_database("recap_maker_db") # Database နာမည်

# Collections (ဇယားများ)
users_collection = db.users
config_collection = db.system_config
packages_collection = db.packages

def init_db():
    """System စ run တာနဲ့ လိုအပ်တဲ့ Default Config တွေ မရှိရင် ဖြည့်ပေးမယ့် Function"""
    if not config_collection.find_one({"setting_name": "global_config"}):
        default_config = {
            "setting_name": "global_config",
            "video_cost": 1,
            "daily_free_limit": 1,
            "payment_info": {
                "method": "Telegram Contact",
                "account": "@your_username",
                "instruction": "Contact admin to buy coins."
            },
            "verification": {
                "message": "Please join our channels to verify.",
                "channels": [] 
            }
        }
        config_collection.insert_one(default_config)
        print("✅ Database Initialized with Default Config!")
      
