# HORMOCARE+
1. pip install -r requirements.txt
2. Make sure MongoDB is running, and .env has GROQ_API_KEY and MONGO_URI=mongodb://localhost:27017/hormocare
3. python import_food.py   (run once: loads food data into the hormocare database)
4. python test_groq.py     (optional: checks the AI key)
5. python app.py  ->  http://127.0.0.1:5000
