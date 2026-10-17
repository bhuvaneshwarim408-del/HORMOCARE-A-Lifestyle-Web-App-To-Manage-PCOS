import os
import io
import json
import random
import requests
from datetime import datetime, timedelta
from functools import wraps

from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, jsonify, session,
    redirect, url_for, send_file
)
from flask_cors import CORS
from flask_pymongo import PyMongo
from bson.objectid import ObjectId
from werkzeug.security import generate_password_hash, check_password_hash
from flask_dance.contrib.google import make_google_blueprint, google

from reportlab.lib.pagesizes import letter, landscape, A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics

# --- ENVIRONMENT SETUP ---
load_dotenv()
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'  # for local dev only!

app = Flask(__name__)
CORS(app)
# Fixed key so you are not logged out every time Flask restarts
app.secret_key = os.getenv('SECRET_KEY', 'hormocare-dev-secret-key')

# --- DATABASE ---
app.config['MONGO_URI'] = os.getenv('MONGO_URI', 'mongodb://localhost:27017/hormocare')
mongo = PyMongo(app)

# Collections
users_collection = mongo.db.users
weekly_diet_collection = mongo.db.weekly_diet
food_collection = mongo.db.food_nutrition            # fooddetails.csv  (Add Food search)
food_collection_diet = mongo.db.food_nutrition_diet  # pcos_meal_fuzzy_matched.csv (weekly plan)
exercises_collection = mongo.db.exercises

# --- GROQ AI ---
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

# --- GOOGLE OAUTH ---
google_bp = make_google_blueprint(
    client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
    scope=["profile", "email", "https://www.googleapis.com/auth/fitness.activity.read"]
)
app.register_blueprint(google_bp, url_prefix="/login")


# --- UTILITY ---
@app.context_processor
def utility_processor():
    def todatetime(strdate, fmt='%Y-%m-%d'):
        try:
            return datetime.strptime(strdate, fmt)
        except Exception:
            return datetime.utcnow()

    def now():
        return datetime.utcnow()
    return dict(todatetime=todatetime, now=now, timedelta=timedelta)


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


# --- HELPERS ---
def get_user_allergies(user_id):
    user = users_collection.find_one({'_id': ObjectId(user_id)})
    return user.get('allergies', []) if user else []


def get_active_period(user_id):
    # Latest cycle that is not ended for this user
    return mongo.db.cycles.find_one(
        {"user_id": user_id, "marked_ended": False},
        sort=[("start_date", -1)]
    )


def ask_groq(messages, temperature=0.7, timeout=40):
    """Call Groq. Returns (text, error_message)."""
    if not GROQ_API_KEY:
        return None, "GROQ_API_KEY missing in .env"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {"model": GROQ_MODEL, "messages": messages, "temperature": temperature}
    try:
        r = requests.post(GROQ_API_URL, headers=headers, json=data, timeout=timeout)
        if r.status_code != 200:
            print("Groq HTTP error:", r.status_code, r.text)
            return None, f"AI service error ({r.status_code}). Check the Flask terminal."
        choices = r.json().get('choices') or []
        text = (choices[0].get('message', {}).get('content') or '').strip() if choices else ''
        if not text:
            return None, "AI returned an empty response. Please try again."
        return text, None
    except requests.exceptions.Timeout:
        return None, "AI request timed out. Please try again."
    except Exception as e:
        print("Groq AI error:", e)
        return None, "Could not reach the AI service. Check your internet."


# --- AUTH ROUTES ---
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        data = request.get_json()
        if not data or 'email' not in data or not data['email']:
            return jsonify({'success': False, 'message': 'Email required'})
        if users_collection.find_one({'email': data['email']}):
            return jsonify({'success': False, 'message': 'Email already exists'})
        if 'password' not in data or not data['password']:
            return jsonify({'success': False, 'message': 'Password required'})

        data['password'] = generate_password_hash(data['password'])
        data['created_at'] = datetime.utcnow()
        data['dark_mode'] = False

        # Ensure allergies and exercise_type fields are lists
        for field in ["allergies", "exercise_type"]:
            if field in data and not isinstance(data[field], list):
                data[field] = [data[field]]

        user_id = users_collection.insert_one(data).inserted_id
        session['user_id'] = str(user_id)
        session['email'] = data['email']
        return jsonify({'success': True})
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.get_json()
        if not data or 'email' not in data or 'password' not in data:
            return jsonify({'success': False, 'message': 'Email and password required'})
        user = users_collection.find_one({'email': data['email']})
        if user and check_password_hash(user['password'], data['password']):
            session['user_id'] = str(user['_id'])
            session['email'] = user['email']
            return jsonify({'success': True})
        return jsonify({'success': False, 'message': 'Invalid credentials'})
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# --- DASHBOARD ---
@app.route('/dashboard')
@login_required
def dashboard():
    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})
    if not user:
        session.clear()
        return redirect(url_for('login'))
    today = str(datetime.utcnow().date())
    name = user.get('full_name', 'User')

    hour = datetime.now().hour
    greeting = "Good morning" if 5 <= hour < 12 else ("Good afternoon" if 12 <= hour < 18 else "Good evening")

    diet_data = mongo.db.diet.find_one({'user_id': session['user_id'], 'date': today})
    activity_data = mongo.db.activity.find_one({'user_id': session['user_id'], 'date': today})

    calorie_limit = user.get('target_calories', 2000)
    step_goal = user.get('step_goal', 6000)
    activity_goal = user.get('activity_goal', 1)

    active_period = get_active_period(str(session['user_id']))

    return render_template(
        'dashboard.html',
        user=user, name=name, greeting=greeting,
        calorie_limit=calorie_limit,
        step_goal=step_goal, activity_goal=activity_goal,
        diet=diet_data, activity=activity_data,
        active_period=active_period
    )


# --- DIET ---
@app.route('/diet', methods=['GET', 'POST'])
@login_required
def diet():
    user_id = session['user_id']
    today = str(datetime.utcnow().date())
    if request.method == 'POST':
        data = request.get_json()
        mongo.db.diet.update_one(
            {'user_id': user_id, 'date': today},
            {'$set': {
                'calories_consumed': data.get('calories_consumed', 0),
                'total_allowed': data.get('total_allowed', 0),
                'protein': data.get('protein', 0),
                'carbs': data.get('carbs', 0),
                'fats': data.get('fats', 0),
                'foods': data.get('foods', []),
                'updated_at': datetime.utcnow()
            }},
            upsert=True
        )
        return jsonify({'success': True})

    doc = weekly_diet_collection.find_one({'user_id': user_id, 'days.date': today})
    meals = {}
    if doc:
        for day in doc['days']:
            if day['date'] == today:
                for meal, ids in day['meals'].items():
                    foods = []
                    for fid in ids:
                        food = food_collection_diet.find_one({"_id": ObjectId(fid)})
                        if food:
                            if 'food_name' not in food and 'name' in food:
                                food['food_name'] = food['name']
                            food['_id'] = str(food['_id'])
                            foods.append(food)
                        else:
                            foods.append({'food_name': 'Unknown Food', 'energy_kcal': 0, 'protein_g': 0, 'carb_g': 0, 'fat_g': 0})
                    meals[meal] = foods
    diet_data = mongo.db.diet.find_one({'user_id': user_id, 'date': today})
    user = users_collection.find_one({'_id': ObjectId(user_id)})
    return render_template('diet.html', user=user, meals=meals, diet=diet_data)


@app.route('/diet/update', methods=['POST'])
@login_required
def update_diet():
    user_id = session['user_id']
    today = str(datetime.utcnow().date())
    data = request.get_json()
    mongo.db.diet.update_one(
        {'user_id': user_id, 'date': today},
        {'$set': {
            'foods': data.get('meals', {}),
            'calories_consumed': data.get('calories_consumed', 0),
            'protein': data.get('protein', 0),
            'carbs': data.get('carbs', 0),
            'fats': data.get('fats', 0),
            'updated_at': datetime.utcnow()
        }},
        upsert=True
    )
    return jsonify({'success': True})


@app.route('/search_food')
@login_required
def search_food():
    q = request.args.get('q', '')
    results = list(food_collection.find({'food_name': {'$regex': q, '$options': 'i'}}).limit(10))
    for r in results:
        r['_id'] = str(r['_id'])
    return jsonify(results)


@app.route('/food_details/<fid>')
@login_required
def food_details(fid):
    try:
        food = food_collection.find_one({'_id': ObjectId(fid)})
    except Exception:
        food = None
    if not food:
        return jsonify({'success': False, 'message': 'Food not found'})
    food['_id'] = str(food['_id'])
    return jsonify({'success': True, 'food': food})


@app.route('/create_weekly_diet', methods=['POST'])
@login_required
def create_weekly_diet():
    user_id = session['user_id']
    allergies = get_user_allergies(user_id)
    week_start = str(datetime.utcnow().date())

    safe_foods = list(food_collection_diet.find({
        "ingredients": {
            "$not": {"$elemMatch": {"$in": allergies}}
        }
    }))

    if not safe_foods:
        return jsonify({
            'success': False,
            'message': "No foods found. Run 'python import_food.py' first (or check your allergies)."
        })

    weekly_plan = []
    for i in range(7):
        day_plan = {
            "date": str((datetime.utcnow() + timedelta(days=i)).date()),
            "meals": {
                "breakfast": [random.choice(safe_foods)["_id"]],
                "lunch": [random.choice(safe_foods)["_id"]],
                "snacks": [random.choice(safe_foods)["_id"]],
                "dinner": [random.choice(safe_foods)["_id"]],
            }
        }
        weekly_plan.append(day_plan)

    weekly_diet_collection.update_one(
        {'user_id': user_id, 'week_start': week_start},
        {'$set': {'days': weekly_plan}},
        upsert=True
    )
    return redirect(url_for('diet'))


@app.route('/diet/today', methods=['GET'])
@login_required
def get_today_diet():
    user_id = session['user_id']
    today = str(datetime.utcnow().date())

    doc = weekly_diet_collection.find_one({'user_id': user_id, 'days.date': today})
    if not doc:
        return jsonify({'success': False, 'message': 'No weekly diet set'})

    for day in doc['days']:
        if day['date'] == today:
            details = {}
            for meal, ids in day['meals'].items():
                foods = []
                for fid in ids:
                    food = food_collection_diet.find_one({"_id": ObjectId(fid)})
                    if food:
                        if 'food_name' not in food and 'name' in food:
                            food['food_name'] = food['name']
                        food['_id'] = str(food['_id'])
                        foods.append(food)
                    else:
                        foods.append({'food_name': 'Unknown Food', 'energy_kcal': 0, 'protein_g': 0, 'carb_g': 0, 'fat_g': 0})
                details[meal] = foods
            return jsonify({'success': True, 'meals': details})

    return jsonify({'success': False, 'message': 'Not found'})


@app.route('/download_weekly_diet')
@login_required
def download_weekly_diet():
    user_id = session.get('user_id')
    week_doc = weekly_diet_collection.find_one({'user_id': str(user_id)})
    if not week_doc:
        return "No weekly diet found", 404

    font_path = os.path.abspath(os.path.join('static', 'fonts', 'NotoSans-Regular.ttf'))
    if not os.path.exists(font_path):
        return "Font not found. Place NotoSans-Regular.ttf in static/fonts.", 500
    pdfmetrics.registerFont(TTFont('NotoSans', font_path))

    def get_food_name(obj_id):
        food = food_collection_diet.find_one({"_id": obj_id}) or food_collection.find_one({"_id": obj_id})
        if not food:
            return str(obj_id)
        return food.get('food_name', '') or food.get('name', '') or str(obj_id)

    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=landscape(letter))
    width, height = landscape(letter)
    margin = 50

    logo_path = os.path.join('static', 'images', 'hormocare_logo.png')

    meal_types = ['breakfast', 'lunch', 'snacks', 'dinner']
    for page_num, meal_type in enumerate(meal_types):
        if page_num > 0:
            p.showPage()
        if os.path.exists(logo_path):
            p.drawImage(logo_path, margin / 4, height / 2 - 60, width=80, height=80, mask='auto')
        p.setFont('NotoSans', 26)
        p.drawString(margin / 3, height - margin / 2, "Hormocare+")

        p.setFont('NotoSans', 20)
        p.drawCentredString(width / 2, height - margin, f"Weekly {meal_type.capitalize()} Plan")

        p.setFont('NotoSans', 12)
        x_start = margin + 100
        y = height - margin * 1.7
        p.drawString(x_start, y, "Date")
        p.drawString(x_start + 250, y, meal_type.capitalize())

        p.setFont('NotoSans', 10)
        y -= 28
        for day in week_doc['days']:
            foods = []
            for m in day['meals'].get(meal_type, []):
                f_obj = m if isinstance(m, ObjectId) else ObjectId(str(m))
                foods.append(get_food_name(f_obj))
            line = ', '.join(foods)
            p.drawString(x_start, y, day['date'])
            p.drawString(x_start + 250, y, line[:90] if line else "-")
            y -= 22

    p.save()
    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name="weekly_diet.pdf",
        mimetype='application/pdf'
    )


# --- ACTIVITY / EXERCISES ---
@app.route('/get_weight')
@login_required
def get_weight():
    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})
    user_weight = user['weight'] if user and 'weight' in user else 60
    return jsonify({'weight': user_weight})


@app.route('/get_images')
def get_images():
    ex_type = request.args.get('type')

    if ex_type == 'hiit':
        query = {
            '$or': [
                {'equipment': {'$regex': 'body only|no equipment|no equipments', '$options': 'i'}},
                {'instructions': {'$elemMatch': {'$regex': 'null|body|no equipment|no equipments', '$options': 'i'}}}
            ]
        }
        exercises = []
        for ex in exercises_collection.find(query):
            exercises.append({
                "name": ex.get('name', ''),
                "img": f"/static/exercises/{ex['images'][0]}" if ex.get('images') else '',
                "targetMuscles": ", ".join(ex.get('primaryMuscles', [])),
                "instructions": " ".join(ex.get('instructions', []))
            })
        return jsonify(exercises)

    elif ex_type == 'aerobic':
        images = [
            'https://example.com/aerobic1.png',
            'https://example.com/aerobic2.png',
            'https://example.com/aerobic3.png'
        ]
        return jsonify(images)

    elif ex_type == 'yoga':
        try:
            url = "https://yoga-api-nzy4.onrender.com/v1/poses?level=beginner"
            data = requests.get(url, timeout=15).json()
        except Exception as e:
            print("Yoga API error:", e)
            return jsonify([])
        result = []
        for pose in data.get('poses', []):
            result.append({
                "name": pose.get('english_name', ''),
                "description": pose.get('pose_description', ''),
                "img": pose.get('url_png')
            })
        return jsonify(result)

    return jsonify([])


@app.route('/activity', methods=['GET', 'POST'])
@login_required
def activity():
    if request.method == 'POST':
        data = request.get_json()
        today = str(datetime.utcnow().date())

        mongo.db.activity.update_one(
            {'user_id': session['user_id'], 'date': today},
            {'$set': {
                'calories_burnt': data.get('calories_burnt', 0),
                'goal_calories': data.get('goal_calories', 0),
                'steps': data.get('steps', 0),
                'goal_steps': data.get('goal_steps', 0),
                'hours': data.get('hours', 0),
                'goal_hours': data.get('goal_hours', 0),
                'activities': data.get('activities', []),
                'updated_at': datetime.utcnow()
            }},
            upsert=True
        )
        return jsonify({'success': True})

    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})
    user_weight = user['weight'] if user and 'weight' in user else 60
    return render_template('activity.html', user_weight=user_weight)


def fetch_exercises_by_name(name, keywords="", limit=10):
    url = "https://exercisedb-api1.p.rapidapi.com/api/v1/exercises"
    querystring = {"name": name, "keywords": keywords, "limit": str(limit)}
    headers = {
        "x-rapidapi-key": os.getenv("RAPIDAPI_KEY", ""),
        "x-rapidapi-host": "exercisedb-api1.p.rapidapi.com"
    }
    try:
        response = requests.get(url, headers=headers, params=querystring, timeout=15)
        response.raise_for_status()
        data = response.json()
        return data.get("data", data) if isinstance(data, dict) else data
    except Exception as e:
        print("Error fetching exercises:", e)
        return []


@app.route('/exercises/search')
def exercises_search():
    name = request.args.get('name', '')
    keywords = request.args.get('keywords', '')
    limit = int(request.args.get('limit', 10))
    exercises = fetch_exercises_by_name(name, keywords, limit)
    simplified = []
    for ex in exercises:
        if isinstance(ex, dict):
            simplified.append({
                'id': ex.get('exerciseId') or ex.get('id', ''),
                'name': ex.get('name', ''),
                'gifUrl': ex.get('imageUrl', '') or ex.get('gifUrl', ''),
                'bodyPart': ', '.join(ex.get('bodyParts', [])) if 'bodyParts' in ex else ex.get('bodyPart', ''),
                'equipment': ', '.join(ex.get('equipments', [])) if 'equipments' in ex else ex.get('equipment', '')
            })
    return jsonify({'success': True, 'exercises': simplified})


@app.route('/save_workout', methods=['POST'])
@login_required
def save_workout():
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': 'No data received'}), 400

    workout = {
        'user_id': session['user_id'],
        'workout_type': data.get('workout_type'),
        'exercises': data.get('exercises'),
        'duration': data.get('duration'),
        'rest_period': data.get('rest_period', 0),
        'intensity': data.get('intensity', 0),
        'timestamp': datetime.utcnow()
    }
    mongo.db.workouts.insert_one(workout)
    return jsonify({'success': True, 'message': 'Workout saved successfully'})


# --- JOURNAL / PROFILE / CYCLE ---
@app.route('/api/journal', methods=['POST'])
@login_required
def add_journal_entry():
    data = request.get_json()
    entry = {
        'user_id': session.get('user_id'),
        'date': data.get('date'),
        'mood': data.get('mood'),
        'stress': data.get('stress'),
        'symptoms': data.get('symptoms'),
        'notes': data.get('notes'),
        'feelData': data.get('feelData'),
        'timestamp': datetime.utcnow()
    }
    mongo.db.journals.insert_one(entry)
    return jsonify({"status": "success"}), 201


@app.route('/journal', methods=['GET', 'POST'])
@login_required
def journal():
    if request.method == 'POST':
        data = request.get_json()
        today = str(datetime.utcnow().date())
        mongo.db.journal.update_one(
            {'user_id': session['user_id'], 'date': today},
            {'$set': {
                'mood': data.get('mood', ''),
                'sleep_quality': data.get('sleep_quality', 0),
                'behavioral_pattern': data.get('behavioral_pattern', ''),
                'notes': data.get('notes', ''),
                'updated_at': datetime.utcnow()
            }},
            upsert=True
        )
        return jsonify({'success': True})

    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})
    journal_entries = list(mongo.db.journal.find(
        {'user_id': session['user_id']},
        sort=[('date', -1)],
        limit=30))
    return render_template('journal.html', user=user, entries=journal_entries)


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        data = request.get_json()
        update_data = {}
        updatable_fields = [
            'cycle_length', 'last_period_date', 'daily_calorie_goal', 'weight', 'height', 'bmi',
            'blood_group', 'pulse_rate', 'cycle_months', 'marriage_status', 'hip', 'waist', 'whratio',
            'basic_history', 'dark_mode', 'age', 'pcos', 'pregnant', 'abortions', 'bloated',
            'facial_hair', 'chest_hair', 'obesity', 'mood_swings', 'stress', 'irregular_sleep',
            'weight_gain', 'hair_growth', 'skin_darkening', 'hair_loss', 'pimples', 'fast_food', 'reg_exercise'
        ]
        for field in updatable_fields:
            if field in data:
                update_data[field] = data[field]
        if update_data:
            users_collection.update_one({'_id': ObjectId(session['user_id'])}, {'$set': update_data})
        return jsonify({'success': True})

    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})
    return render_template('profile.html', user=user)


@app.route('/toggle_dark_mode', methods=['POST'])
@login_required
def toggle_dark_mode():
    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})
    new_mode = not user.get('dark_mode', False)
    users_collection.update_one({'_id': ObjectId(session['user_id'])}, {'$set': {'dark_mode': new_mode}})
    return jsonify({'success': True, 'dark_mode': new_mode})


@app.route('/add_cycle', methods=['POST'])
@login_required
def add_cycle():
    data = request.get_json()
    if not data or not data.get('last_period_date') or not data.get('flow_duration') or not data.get('normal_cycle_days'):
        return jsonify({'success': False, 'message': 'Missing required cycle data'}), 400

    try:
        start_date = datetime.strptime(data['last_period_date'], "%Y-%m-%d")
        duration = int(data['flow_duration'])
        cycle_length = int(data['normal_cycle_days'])
    except Exception:
        return jsonify({'success': False, 'message': 'Invalid date or numeric format'}), 400

    cycle_entry = {
        'start': data.get('last_period_date'),
        'duration': duration,
        'cycle_length': cycle_length,
        'flow_intensity': data.get('flow_intensity'),
        'symptoms': data.get('symptoms'),
        'end': (start_date + timedelta(days=duration)).strftime("%Y-%m-%d"),
        'created_at': datetime.utcnow()
    }

    users_collection.update_one(
        {'_id': ObjectId(session["user_id"])},
        {'$push': {'cycles': cycle_entry}}
    )
    return jsonify({'success': True})


@app.route('/record_period', methods=['POST'])
@login_required
def record_period():
    data = request.get_json()
    date = data.get('date')
    user_id = session.get('user_id')
    if not date or not user_id:
        return jsonify({"success": False, "message": "Date required"})
    mongo.db.cycles.insert_one({
        "user_id": str(user_id),
        "start_date": date,
        "marked_ended": False,
        "created_at": datetime.utcnow()
    })
    users_collection.update_one({'_id': ObjectId(user_id)}, {'$set': {'last_period_date': date}})
    return jsonify({"success": True})


@app.route('/end_period', methods=['POST'])
@login_required
def end_period():
    data = request.get_json()
    cycle_id = data.get('cycle_id')
    if not cycle_id:
        return jsonify({"success": False})
    mongo.db.cycles.update_one(
        {'_id': ObjectId(cycle_id)},
        {'$set': {'marked_ended': True, 'end_date': datetime.utcnow()}}
    )
    return jsonify({"success": True})


@app.route('/predictor', methods=['GET', 'POST'])
@login_required
def predictor():
    if request.method == 'POST':
        data = request.get_json()
        try:
            last_period = datetime.strptime(data['last_period_date'], '%Y-%m-%d')
            cycle_length = int(data.get('cycle_length', 28))
        except Exception:
            return jsonify({'success': False, 'message': 'Invalid date or cycle length'}), 400

        predicted_date = last_period + timedelta(days=cycle_length)
        cycle_day = (datetime.utcnow() - last_period).days % cycle_length

        mongo.db.cycle.insert_one({
            'user_id': session['user_id'],
            'last_period_date': str(last_period.date()),
            'cycle_length': cycle_length,
            'predicted_date': str(predicted_date.date()),
            'cycle_day': cycle_day,
            'created_at': datetime.utcnow()
        })
        return jsonify({'success': True, 'predicted_date': str(predicted_date.date()), 'cycle_day': cycle_day})

    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})
    cycle_data = mongo.db.cycle.find_one({'user_id': session['user_id']}, sort=[('_id', -1)])
    return render_template('predictor.html', user=user, cycle=cycle_data)


@app.route("/google_access_fitness")
def google_access_fitness():
    if not google.authorized:
        return redirect(url_for("google.login"))
    resp = google.get("/oauth2/v2/userinfo")
    if not resp.ok:
        return "Failed to fetch user info."
    userinfo = resp.json()
    return f"Hello, {userinfo['email']}! Google Fit access granted."


# --- ALAGI CHATBOT ---
@app.route('/alagi')
@login_required
def alagi():
    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})
    return render_template('alagi.html', user=user)


@app.route("/chat", methods=["POST"])
@login_required
def chat():
    payload = request.get_json(silent=True) or {}
    user_message = (payload.get("message") or "").strip()
    if not user_message:
        return jsonify({"reply": "Please type your message."}), 400

    messages = [
        {"role": "system", "content": "You are a helpful healthcare pcos lifestyle assistant named Alagi meaning beautiful. Provide accurate and empathetic responses to user queries about PCOS, diet, exercise, and lifestyle."},
        {"role": "user", "content": user_message}
    ]
    reply, error = ask_groq(messages, temperature=0.7)
    if error:
        return jsonify({"reply": error}), 502
    return jsonify({"reply": reply})


# --- WEEKLY REPORT PDF ---
@app.route('/download_weekly_report_pdf', methods=['GET'])
@login_required
def download_weekly_report_pdf():
    user_id = session['user_id']
    today = datetime.utcnow().date()
    start_date = today - timedelta(days=6)
    date_range = {'$gte': str(start_date), '$lte': str(today)}

    user = users_collection.find_one({'_id': ObjectId(user_id)}) or {}
    if user:
        user['_id'] = str(user['_id'])
        user.pop('password', None)

    def fetch(coll, query):
        rows = list(coll.find(query))
        for r in rows:
            r['_id'] = str(r['_id'])
        return rows

    summary = {
        'user_profile': user,
        'activity_logs': fetch(mongo.db.activity, {'user_id': user_id, 'date': date_range}),
        'diet_logs': fetch(mongo.db.diet, {'user_id': user_id, 'date': date_range}),
        'journal_entries': fetch(mongo.db.journal, {'user_id': user_id, 'date': date_range}),
        'cycle_details': fetch(mongo.db.cycles, {
            'user_id': user_id,
            '$or': [{'start_date': date_range}, {'end_date': date_range}]
        }),
    }

    report_prompt = """
    You are a helpful healthcare assistant. Given this JSON representing a user's previous 7 days, create a neat, positive, and structured weekly health report.
    Use this format exactly for each section, filling with bullet points, summary, or an observation if data is missing.
    Use plain text only (no markdown symbols like ** or #).

    1. Introduction
    Give a brief week overview or positive greeting (optional).

    2. Activity Summary
    Bullet points highlighting exercise frequency, type, duration, steps, or active minutes.
    Note improvements or consistency.
    Say "Activity data unavailable" if missing.

    3. Diet Summary
    Bullet points for meal types, timing, foods consumed, portions, notable intakes (e.g. fruits, hydration).
    Note healthy choices or patterns.
    Say "Diet data unavailable" if missing.

    4. Behavioral/Journal Insights
    Bullet points for mood, stress, energy, sleep, and journal entries.
    Show positive or mindful behaviors.
    Say "Journal data unavailable" if missing.

    5. Cycle Details (if applicable)
    Bullet points about menstruation/cycle: start/end dates, symptoms, flow, irregularities.
    Offer supportive notes.
    Say "Cycle data unavailable" if missing.

    6. Overall Positives and Suggestions
    Summary paragraph or bullets on positives, with gentle suggestions for next week.
    """
    messages = [
        {"role": "system", "content": "You are a helpful healthcare assistant."},
        {"role": "user", "content": report_prompt + "\n\n" + json.dumps(summary, default=str)}
    ]
    report_text, error = ask_groq(messages, temperature=0.6, timeout=60)
    if error:
        report_text = "Weekly report could not be generated: " + error

    # Generate PDF
    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    p.setFont("Helvetica-Bold", 30)
    p.drawCentredString(width / 2, height - 70, "HORMOCARE+")
    p.setLineWidth(2)
    p.line(width * 0.2, height - 85, width * 0.8, height - 85)

    def wrap(line, font, size, max_width):
        words, cur, out = line.split(' '), '', []
        for w in words:
            test = (cur + ' ' + w).strip()
            if pdfmetrics.stringWidth(test, font, size) <= max_width:
                cur = test
            else:
                out.append(cur)
                cur = w
        out.append(cur)
        return out

    y = height - 115
    max_w = width - 120
    for raw in report_text.splitlines():
        stripped = raw.strip()
        is_head = stripped.startswith(('1.', '2.', '3.', '4.', '5.', '6.'))
        font, size = ("Helvetica-Bold", 14) if is_head else ("Helvetica", 12)
        # strip characters Helvetica cannot draw
        safe = raw.encode('latin-1', 'ignore').decode('latin-1')
        for seg in wrap(safe, font, size, max_w) if safe.strip() else ['']:
            if y < 70:
                p.showPage()
                y = height - 70
            p.setFont(font, size)
            p.drawString(60, y, seg)
            y -= 17
    p.save()
    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name="weekly_report.pdf",
        mimetype='application/pdf'
    )


if __name__ == '__main__':
    # Run with debug=False in production
    app.run(debug=True)