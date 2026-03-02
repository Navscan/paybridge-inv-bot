import dns.resolver
dns.resolver.default_resolver = dns.resolver.Resolver(configure=False)
dns.resolver.default_resolver.nameservers = ['8.8.8.8']

import os
from pymongo import MongoClient
import bcrypt
from datetime import datetime

client = MongoClient(os.environ.get("MONGODB_URI"))
db = client["paybridge"]
orders_col = db["orders"]
pins_col = db["pins"]          # keep for old orders
shops_col = db["shops"]         # new collection for shops

# ----- Shop functions -----
def get_shop(user_id):
    return shops_col.find_one({"user_id": user_id})

def create_shop(user_id, name, phone, address, pin):
    salt = bcrypt.gensalt()
    pin_hash = bcrypt.hashpw(pin.encode('utf-8'), salt)
    shop = {
        "user_id": user_id,
        "name": name,
        "phone": phone,
        "address": address,
        "pin_hash": pin_hash,
        "created_at": datetime.utcnow()
    }
    shops_col.insert_one(shop)

def update_shop_pin(user_id, new_pin):
    salt = bcrypt.gensalt()
    pin_hash = bcrypt.hashpw(new_pin.encode('utf-8'), salt)
    shops_col.update_one(
        {"user_id": user_id},
        {"$set": {"pin_hash": pin_hash}}
    )

# ----- Order functions -----
def save_order_structured(user_id, items_list, total, customer_name="", customer_phone="", customer_address=""):
    order = {
        "user_id": user_id,
        "items": items_list,
        "total": total,
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "customer_address": customer_address,
        "status": "pending",
        "created_at": datetime.utcnow()
    }
    result = orders_col.insert_one(order)
    return str(result.inserted_id)

def get_order(order_id):
    from bson.objectid import ObjectId
    return orders_col.find_one({"_id": ObjectId(order_id)})

def get_most_recent_order(user_id):
    return orders_col.find_one({"user_id": user_id}, sort=[("created_at", -1)])

def get_pending_orders():
    return list(orders_col.find({"status": "pending"}).sort("created_at", -1).limit(10))

# ----- Old PIN functions (kept for old orders) -----
def save_pin(order_id, pin):
    salt = bcrypt.gensalt()
    pin_hash = bcrypt.hashpw(pin.encode('utf-8'), salt)
    from bson.objectid import ObjectId
    pin_doc = {
        "order_id": ObjectId(order_id),
        "pin_hash": pin_hash
    }
    pins_col.insert_one(pin_doc)

def get_pin_hash(order_id):
    from bson.objectid import ObjectId
    doc = pins_col.find_one({"order_id": ObjectId(order_id)})
    return doc["pin_hash"] if doc else None
