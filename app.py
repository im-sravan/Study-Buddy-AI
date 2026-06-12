import os
import sqlite3
import json
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
#import google.generativeai as genai
import markdown as md
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ====== CONFIG =======
# Prefer reading the API key from the environment so it's easy to rotate/update
# without changing source. A fallback value can remain for local testing.
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY') 

app = Flask(__name__)
app.secret_key = "study_buddy_secret_key_change_this"
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024  #2MB
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

DB_PATH = os.path.join(os.path.dirname(__file__), 'studybuddy.db')

# NOTE: Do not configure `genai` here at import time. We configure it on demand
# inside `call_gemini()` using the environment variable so the running process
# can pick up key changes without a code edit and we can provide better error
# messages when keys are invalid.


@app.template_filter('markdown')
def markdown_filter(text):
    return md.markdown(text or "", extensions=['extra', 'sane_lists'])


# ======= DATABASE ========
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        full_name TEXT,
        age TEXT,
        education_level TEXT,
        college TEXT,
        branch TEXT,
        year TEXT,
        bio TEXT,
        created_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS chat_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        module TEXT NOT NULL,
        role TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TEXT,
        FOREIGN KEY (user_id) REFERENCES users (id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS study_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT,
        subjects TEXT,
        exam_date TEXT,
        plan_content TEXT,
        created_at TEXT,
        FOREIGN KEY (user_id) REFERENCES users (id)
    )''')
    conn.commit()
    conn.close()


init_db()


# ====== HELPERS ======
def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def get_current_user():
    if 'user_id' not in session:
        return None
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    conn.close()
    return user


def build_user_context(user):
    """Build a profile context string for the AI."""
    if not user:
        return ""
    parts = []
    if user['full_name']:
        parts.append(f"Name: {user['full_name']}")
    if user['age']:
        parts.append(f"Age: {user['age']}")
    if user['education_level']:
        parts.append(f"Education Level: {user['education_level']}")
    if user['college']:
        parts.append(f"College/School: {user['college']}")
    if user['branch']:
        parts.append(f"Branch/Stream: {user['branch']}")
    if user['year']:
        parts.append(f"Year/Grade: {user['year']}")
    if user['bio']:
        parts.append(f"About: {user['bio']}")
    if not parts:
        return ""
    return "Student Profile Info (use this to personalize your answer):\n" + "\n".join(parts) + "\n\n"


def call_gemini(prompt, file_path=None, mime_type=None):
    """Call Gemini API with optional file attachment."""
    # Prefer environment value, fallback to the configured variable.
    key = os.environ.get('GEMINI_API_KEY') or GEMINI_API_KEY
    if not key or key == "YOUR_GEMINI_API_KEY_HERE":
        return ("⚠️ Gemini API key is not configured. Set the GEMINI_API_KEY environment "
                "variable or update the GEMINI_API_KEY in app.py to enable AI responses.")

    try:
        # Configure the client with the active key before making requests.
        genai.configure(api_key=key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        if file_path:
            import base64
            with open(file_path, 'rb') as f:
                file_bytes = f.read()
            file_part = {
                "mime_type": mime_type or "application/octet-stream",
                "data": base64.b64encode(file_bytes).decode('utf-8')
            }
            response = model.generate_content([file_part, prompt])
        else:
            response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        # Provide clearer guidance for common API key errors.
        msg = str(e)
        if 'API key not valid' in msg or 'API_KEY_INVALID' in msg or 'invalid' in msg.lower():
            return ("⚠️ Gemini API key appears invalid. Please verify your GEMINI_API_KEY "
                    "environment variable or update the key in app.py. (Error: " + msg + ")")
        return f"⚠️ Error contacting Gemini AI: {msg}"


def save_chat(user_id, module, role, message):
    conn = get_db()
    conn.execute(
        'INSERT INTO chat_history (user_id, module, role, message, created_at) VALUES (?, ?, ?, ?, ?)',
        (user_id, module, role, message, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def get_chat_history(user_id, module, limit=50):
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM chat_history WHERE user_id = ? AND module = ? ORDER BY id ASC LIMIT ?',
        (user_id, module, limit)
    ).fetchall()
    conn.close()
    return rows


# ======= AUTH ROUTES =======
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('home'))
    return redirect(url_for('login'))


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')

        if not username or not password:
            flash('Username and password are required.', 'error')
            return redirect(url_for('signup'))

        if password != confirm:
            flash('Passwords do not match.', 'error')
            return redirect(url_for('signup'))

        conn = get_db()
        existing = conn.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
        if existing:
            conn.close()
            flash('Username already exists. Please choose another.', 'error')
            return redirect(url_for('signup'))

        hashed = generate_password_hash(password)
        conn.execute(
            'INSERT INTO users (username, password, created_at) VALUES (?, ?, ?)',
            (username, hashed, datetime.now().isoformat())
        )
        conn.commit()
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        conn.close()

        session['user_id'] = user['id']
        session['username'] = user['username']
        flash('Account created! Please complete your profile.', 'success')
        return redirect(url_for('profile'))

    return render_template('signup.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        conn.close()

        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('home'))
        else:
            flash('Invalid username or password.', 'error')
            return redirect(url_for('login'))

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ======= MAIN PAGES =======
@app.route('/home')
@login_required
def home():
    user = get_current_user()
    return render_template('home.html', user=user)


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    user = get_current_user()
    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        age = request.form.get('age', '').strip()
        education_level = request.form.get('education_level', '').strip()
        college = request.form.get('college', '').strip()
        branch = request.form.get('branch', '').strip()
        year = request.form.get('year', '').strip()
        bio = request.form.get('bio', '').strip()

        conn = get_db()
        conn.execute('''UPDATE users SET full_name=?, age=?, education_level=?, college=?,
                         branch=?, year=?, bio=? WHERE id=?''',
                      (full_name, age, education_level, college, branch, year, bio, user['id']))
        conn.commit()
        conn.close()
        flash('Profile updated successfully!', 'success')
        return redirect(url_for('profile'))

    return render_template('profile.html', user=user)


@app.route('/settings')
@login_required
def settings():
    user = get_current_user()
    return render_template('settings.html', user=user)


@app.route('/change_password', methods=['POST'])
@login_required
def change_password():
    user = get_current_user()
    current = request.form.get('current_password', '')
    new = request.form.get('new_password', '')
    confirm = request.form.get('confirm_new_password', '')

    if not check_password_hash(user['password'], current):
        flash('Current password is incorrect.', 'error')
        return redirect(url_for('settings'))

    if new != confirm:
        flash('New passwords do not match.', 'error')
        return redirect(url_for('settings'))

    if len(new) < 4:
        flash('New password is too short.', 'error')
        return redirect(url_for('settings'))

    conn = get_db()
    conn.execute('UPDATE users SET password=? WHERE id=?', (generate_password_hash(new), user['id']))
    conn.commit()
    conn.close()
    flash('Password changed successfully!', 'success')
    return redirect(url_for('settings'))


# ============ 1) CHAT ASSISTANT ============
@app.route('/chat-assistant')
@login_required
def chat_assistant():
    user = get_current_user()

    if not user:
        return redirect(url_for('login'))
    
    history = get_chat_history(user['id'], 'chat_assistant')
    return render_template('chat_assistant.html', user=user, history=history)


@app.route('/api/chat-assistant', methods=['POST'])
@login_required
def api_chat_assistant():
    user = get_current_user()
    data = request.get_json()
    message = data.get('message', '').strip()
    if not message:
        return jsonify({'error': 'Empty message'}), 400

    save_chat(user['id'], 'chat_assistant', 'user', message)

    context = build_user_context(user)
    history = get_chat_history(user['id'], 'chat_assistant', limit=10)
    convo = ""
    for h in history[:-1]:
        role = "Student" if h['role'] == 'user' else "Study Buddy"
        convo += f"{role}: {h['message']}\n"

    prompt = (f"You are 'Study Buddy', a friendly and supportive AI assistant for students. "
              f"{context}"
              f"Conversation so far:\n{convo}\n"
              f"Student: {message}\n"
              f"Respond helpfully, clearly, and in a friendly tone as Study Buddy. "
              f"Personalize your answer using the student's profile info where relevant.")

    reply = call_gemini(prompt)
    save_chat(user['id'], 'chat_assistant', 'assistant', reply)
    return jsonify({'reply': reply})


# ============ 2) ACADEMIC QUERY SUPPORT ============
@app.route('/academic-query')
@login_required
def academic_query():
    user = get_current_user()
    history = get_chat_history(user['id'], 'academic_query')
    return render_template('academic_query.html', user=user, history=history)


@app.route('/api/academic-query', methods=['POST'])
@login_required
def api_academic_query():
    user = get_current_user()
    message = request.form.get('message', '').strip()
    file = request.files.get('file')

    context = build_user_context(user)
    file_path = None
    mime_type = None
    display_message = message

    if file and file.filename:
        filename = secure_filename(file.filename)
        unique_name = f"{user['id']}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{filename}"
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_name)
        file.save(file_path)
        mime_type = file.mimetype
        display_message = (message + f" [Uploaded file: {filename}]") if message else f"[Uploaded file: {filename}]"

    if not display_message:
        return jsonify({'error': 'Please type a question or upload a file.'}), 400

    save_chat(user['id'], 'academic_query', 'user', display_message)

    if file_path:
        prompt = (f"You are 'Study Buddy', an AI academic assistant. {context}"
                  f"The student uploaded a file. "
                  + (f"Their instruction/question: {message}. " if message else
                     "They want you to summarize and explain the content clearly. ")
                  + "Provide a clear, well-structured summary/explanation of the file content, "
                    "and answer any specific question if asked.")
        reply = call_gemini(prompt, file_path=file_path, mime_type=mime_type)
    else:
        prompt = (f"You are 'Study Buddy', an AI academic assistant specializing in subject doubts, "
                  f"programming questions, and exam preparation tips. {context}"
                  f"Student's question: {message}\n"
                  f"Give a clear, accurate, well-explained answer suitable for a student.")
        reply = call_gemini(prompt)

    save_chat(user['id'], 'academic_query', 'assistant', reply)
    return jsonify({'reply': reply})


# ============ 3) STUDY PLANNER ============
@app.route('/study-planner')
@login_required
def study_planner():
    user = get_current_user()
    conn = get_db()
    plans = conn.execute('SELECT * FROM study_plans WHERE user_id=? ORDER BY id DESC', (user['id'],)).fetchall()
    conn.close()
    return render_template('study_planner.html', user=user, plans=plans)


@app.route('/study-planner/create', methods=['GET', 'POST'])
@login_required
def create_study_plan():
    user = get_current_user()
    if request.method == 'POST':
        subjects = request.form.get('subjects', '').strip()
        exam_date = request.form.get('exam_date', '').strip()
        title = request.form.get('title', '').strip() or f"Plan for {exam_date}"

        context = build_user_context(user)
        today = datetime.now().strftime('%Y-%m-%d')

        prompt = (f"You are 'Study Buddy', an AI study planner. {context}"
                  f"Today's date is {today}. The student needs a study plan for the following subjects: "
                  f"{subjects}. The exam date is {exam_date}.\n\n"
                  f"Create a detailed, day-by-day study schedule from today until the exam date, "
                  f"including:\n"
                  f"1. A Daily Study Schedule (topics to cover each day, balanced across subjects)\n"
                  f"2. A Revision Plan for the final days before the exam\n\n"
                  f"Format the output using clear Markdown with headings, bullet points, and a table if helpful. "
                  f"Be realistic and practical given the time available.")

        plan_content = call_gemini(prompt)

        conn = get_db()
        conn.execute('''INSERT INTO study_plans (user_id, title, subjects, exam_date, plan_content, created_at)
                         VALUES (?, ?, ?, ?, ?, ?)''',
                      (user['id'], title, subjects, exam_date, plan_content, datetime.now().isoformat()))
        conn.commit()
        new_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.close()

        return redirect(url_for('view_study_plan', plan_id=new_id))

    return render_template('create_study_plan.html', user=user)


@app.route('/study-planner/<int:plan_id>')
@login_required
def view_study_plan(plan_id):
    user = get_current_user()
    conn = get_db()
    plan = conn.execute('SELECT * FROM study_plans WHERE id=? AND user_id=?', (plan_id, user['id'])).fetchone()
    conn.close()
    if not plan:
        flash('Plan not found.', 'error')
        return redirect(url_for('study_planner'))
    return render_template('view_study_plan.html', user=user, plan=plan)


@app.route('/study-planner/<int:plan_id>/delete', methods=['POST'])
@login_required
def delete_study_plan(plan_id):
    user = get_current_user()
    conn = get_db()
    conn.execute('DELETE FROM study_plans WHERE id=? AND user_id=?', (plan_id, user['id']))
    conn.commit()
    conn.close()
    return redirect(url_for('study_planner'))


# ============ 4) CAREER GUIDANCE ============
@app.route('/career-guidance')
@login_required
def career_guidance():
    user = get_current_user()
    history = get_chat_history(user['id'], 'career_guidance')
    return render_template('career_guidance.html', user=user, history=history)


@app.route('/api/career-guidance', methods=['POST'])
@login_required
def api_career_guidance():
    user = get_current_user()
    data = request.get_json()
    message = data.get('message', '').strip()
    if not message:
        return jsonify({'error': 'Empty message'}), 400

    save_chat(user['id'], 'career_guidance', 'user', message)

    context = build_user_context(user)
    history = get_chat_history(user['id'], 'career_guidance', limit=10)
    convo = ""
    for h in history[:-1]:
        role = "Student" if h['role'] == 'user' else "Study Buddy"
        convo += f"{role}: {h['message']}\n"

    prompt = (f"You are 'Study Buddy', an AI career guidance counselor for students. {context}"
              f"Conversation so far:\n{convo}\n"
              f"Student: {message}\n\n"
              f"Help the student choose their career domain, create a clear career roadmap, "
              f"explain the tech stacks/skills they need to learn, and provide guidance on jobs, "
              f"certifications, or exams if relevant. Be encouraging, specific, and structured "
              f"(use Markdown with headings/bullets when giving a roadmap). "
              f"Personalize based on the student's profile info where relevant.")

    reply = call_gemini(prompt)
    save_chat(user['id'], 'career_guidance', 'assistant', reply)
    return jsonify({'reply': reply})


# ============ CLEAR CHAT ============
@app.route('/api/clear-chat/<module>', methods=['POST'])
@login_required
def clear_chat(module):
    user = get_current_user()
    conn = get_db()
    conn.execute('DELETE FROM chat_history WHERE user_id=? AND module=?', (user['id'], module))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


# Debug route to help diagnose API key problems. Only allow requests from localhost.
@app.route('/debug/gemini-status')
def debug_gemini_status():
    # Only respond to localhost to avoid exposing info publicly.
    if request.remote_addr not in ('127.0.0.1', '::1', 'localhost'):
        return jsonify({'error': 'Not allowed'}), 403

    key = os.environ.get('GEMINI_API_KEY') or GEMINI_API_KEY
    if not key or key == 'YOUR_GEMINI_API_KEY_HERE':
        return jsonify({'status': 'missing', 'message': 'No GEMINI_API_KEY set in environment or app.'})

    # Try a lightweight API call to validate the key without sending user data.
    try:
        genai.configure(api_key=key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        # small harmless test prompt
        resp = model.generate_content('Say OK')
        text = getattr(resp, 'text', None) or getattr(resp, 'result', None) or str(resp)
        return jsonify({'status': 'ok', 'message': 'API key accepted by Gemini (test prompt succeeded).'})
    except Exception as e:
        err = str(e)
        # sanitize common sensitive fragments
        if 'API key' in err or 'API_KEY_INVALID' in err or 'invalid' in err.lower():
            return jsonify({'status': 'invalid', 'message': 'API key rejected by Gemini. Check key permissions or value.', 'detail': err}), 400
        return jsonify({'status': 'error', 'message': 'Unexpected error when contacting Gemini.', 'detail': err}), 500


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)