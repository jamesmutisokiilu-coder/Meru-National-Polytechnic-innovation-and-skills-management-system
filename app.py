from flask import Flask, render_template, request, redirect, url_for, session, flash
import sqlite3, os
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps

app = Flask(__name__)

# ---------------- SECRET KEY ----------------
app.secret_key = os.environ.get("SECRET_KEY", "supersecretkey")

# ---------------- BASE DIR ----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------- RENDER-SAFE STORAGE ----------------
DB_NAME = os.path.join("/tmp", "database.db")
UPLOAD_FOLDER = os.path.join("/tmp", "uploads")

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER


# ---------------- DATABASE CONNECTION ----------------
def get_connection():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------- INIT DATABASE ----------------
def init_db():
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            email TEXT UNIQUE,
            password TEXT,
            role TEXT
        );

        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            description TEXT,
            file TEXT,
            email TEXT,
            phone TEXT,
            whatsapp TEXT,
            uploader TEXT
        );

        CREATE TABLE IF NOT EXISTS skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            class_name TEXT,
            department TEXT,
            area TEXT,
            supervisor TEXT
        );

        CREATE TABLE IF NOT EXISTS assistants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT,
            innovator_name TEXT,
            phone_number TEXT,
            project_name TEXT,
            assistant_area TEXT,
            category TEXT
        );

        CREATE TABLE IF NOT EXISTS discussions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_name TEXT,
            topic TEXT,
            message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discussion_id INTEGER,
            user_name TEXT,
            message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        conn.commit()
        conn.close()

    except Exception as e:
        print("DB INIT ERROR:", e)


init_db()


# ---------------- LOGIN REQUIRED ----------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


# ---------------- HOME ----------------
@app.route('/')
def index():
    return render_template('index.html')


# ---------------- DASHBOARD ----------------
@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM projects")
    projects_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM skills")
    skills_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM assistants")
    assistants_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM discussions")
    discussions_count = cur.fetchone()[0]

    conn.close()

    return render_template(
        "dashboard.html",
        projects_count=projects_count,
        skills_count=skills_count,
        assistants_count=assistants_count,
        discussions_count=discussions_count
    )


# ---------------- LOGIN ----------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("SELECT * FROM users WHERE email=?", (request.form['email'],))
        user = cur.fetchone()
        conn.close()

        if user and check_password_hash(user['password'], request.form['password']):
            session['user'] = user['name']
            return redirect(url_for('dashboard'))

        flash("Invalid login")
        return redirect(url_for('login'))

    return render_template('login.html')


# ---------------- REGISTER ----------------
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        try:
            conn = get_connection()
            cur = conn.cursor()

            cur.execute("""
                INSERT INTO users(name,email,password,role)
                VALUES (?,?,?,?)
            """, (
                request.form['name'],
                request.form['email'],
                generate_password_hash(request.form['password']),
                "user"
            ))

            conn.commit()
            conn.close()

            return redirect(url_for('login'))

        except Exception as e:
            print("REGISTER ERROR:", e)
            flash("Registration failed")
            return redirect(url_for('register'))

    return render_template('register.html')


# ---------------- LOGOUT ----------------
@app.route('/logout')
@login_required
def logout():
    session.clear()
    return redirect(url_for('index'))


# ---------------- PROJECTS ----------------
@app.route('/projects', methods=['GET', 'POST'])
@login_required
def projects():
    conn = get_connection()
    cur = conn.cursor()

    if request.method == 'POST':
        try:
            files = request.files.getlist('files')
            filenames = []

            for file in files:
                if file and file.filename.strip():
                    filename = secure_filename(file.filename)
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                    filenames.append(filename)

            cur.execute("""
                INSERT INTO projects(
                    title, description, file, email, phone, whatsapp, uploader
                ) VALUES (?,?,?,?,?,?,?)
            """, (
                request.form.get('title', ''),
                request.form.get('description', ''),
                ",".join(filenames),
                request.form.get('email', ''),
                request.form.get('phone', ''),
                request.form.get('whatsapp', ''),
                session['user']
            ))

            conn.commit()

        except Exception as e:
            print("PROJECT ERROR:", e)

        finally:
            conn.close()

        return redirect(url_for('uploads'))

    conn.close()
    return render_template('projects.html')


# ---------------- UPLOADS ----------------
@app.route('/uploads')
@login_required
def uploads():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM projects ORDER BY id DESC")
    projects = cur.fetchall()

    cur.execute("SELECT * FROM skills ORDER BY id DESC")
    skills = cur.fetchall()

    conn.close()

    return render_template("uploads.html", projects=projects, skills=skills)


# ---------------- SKILLS FIX ROUTE (IMPORTANT FIX) ----------------
@app.route('/skills', methods=['POST'])
@login_required
def add_skill():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO skills(name,class_name,department,area,supervisor)
        VALUES (?,?,?,?,?)
    """, (
        request.form.get('name', ''),
        request.form.get('class', ''),
        request.form.get('department', ''),
        request.form.get('area', ''),
        request.form.get('supervisor', '')
    ))

    conn.commit()
    conn.close()

    return redirect(url_for('uploads'))


# ---------------- ASSISTANT ----------------
@app.route('/assistant', methods=['GET', 'POST'])
@login_required
def assistant():
    conn = get_connection()
    cur = conn.cursor()

    if request.method == 'POST':
        cur.execute("""
            INSERT INTO assistants(
                type, innovator_name, phone_number,
                project_name, assistant_area, category
            ) VALUES (?,?,?,?,?,?)
        """, (
            request.form.get('type', ''),
            request.form.get('innovator_name', ''),
            request.form.get('phone_number', ''),
            request.form.get('project_name', ''),
            request.form.get('assistant_area', ''),
            request.form.get('category', '')
        ))

        conn.commit()
        return redirect(url_for('assistant'))

    cur.execute("SELECT * FROM assistants ORDER BY id DESC")
    assistants = cur.fetchall()

    conn.close()

    return render_template('assistant.html', assistants=assistants)


# ---------------- DISCUSSION ----------------
@app.route('/discussion', methods=['GET', 'POST'])
@login_required
def discussion():
    conn = get_connection()
    cur = conn.cursor()

    if request.method == 'POST':
        message = request.form.get('message')
        discussion_id = request.form.get('discussion_id')

        if discussion_id:
            cur.execute("""
                INSERT INTO replies(discussion_id,user_name,message)
                VALUES (?,?,?)
            """, (discussion_id, session['user'], message))
        else:
            cur.execute("""
                INSERT INTO discussions(user_name,topic,message)
                VALUES (?,?,?)
            """, (
                session['user'],
                request.form.get('topic'),
                message
            ))

        conn.commit()
        return redirect(url_for('discussion'))

    cur.execute("SELECT * FROM discussions ORDER BY id DESC")
    discussions = cur.fetchall()

    cur.execute("SELECT * FROM replies ORDER BY id ASC")
    replies = cur.fetchall()

    replies_dict = {}
    for r in replies:
        replies_dict.setdefault(r['discussion_id'], []).append(r)

    conn.close()

    return render_template(
        'discussion.html',
        discussions=discussions,
        replies_dict=replies_dict
    )


# ---------------- OTHER PAGES ----------------
@app.route('/activities')
def activities():
    return render_template('activities.html')

@app.route('/contact')
def contact():
    return render_template('contact.html')

@app.route('/about')
def about():
    return render_template('about.html')

@app.route('/leadership')
def leadership():
    return render_template('leadership.html')


# ---------------- ERROR DEBUG ----------------
@app.errorhandler(500)
def server_error(e):
    print("SERVER ERROR:", e)
    return str(e), 500


# ---------------- RUN ----------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
