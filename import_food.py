# Run from the hormocare-main folder:  python import_food.py
import os
import pandas as pd
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
uri = os.getenv("MONGO_URI", "mongodb://localhost:27017/hormocare")
db = MongoClient(uri).get_default_database()   # -> "hormocare", same DB app.py uses

def load(csv_file, collection_name):
    df = pd.read_csv(csv_file, encoding="utf-8-sig").astype(object)
    df = df.where(df.notna(), None)
    db[collection_name].drop()                  # avoids duplicates on re-run
    db[collection_name].insert_many(df.to_dict("records"))
    print(f"{collection_name}: {db[collection_name].count_documents({})} docs")

load("pcos_meal_fuzzy_matched.csv", "food_nutrition_diet")  # weekly diet plan
load("fooddetails.csv", "food_nutrition")                   # Add Food search
print("Done. Database:", db.name)
