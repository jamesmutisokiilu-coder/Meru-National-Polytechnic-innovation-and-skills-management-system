from flask import Flask, render_template, request, redirect, url_for, session, flash
import sqlite3, os
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps
from datetime import datetime

app = Flask(__name__)

# ---------------- SECURITY ----------------
app.secret_key = os.environ.get("SECRET_KEY", "fallbacksecret")

# ---------------- CONFIG ----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}

DB_NAME = os.path.join(BASE_DIR, "database.db")

# ---------------- DB ----------------
def get_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    # USERS
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        email TEXT UNIQUE,
        password TEXT,
        role TEXT
    )
    """)

    # PROJECTS
    cur.execute("""
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        description TEXT,
        file TEXT,
        uploader TEXT,
        email TEXT,
        phone TEXT,
        whatsapp TEXT
    )
    """)

    # SKILLS
    cur.execute("""
    CREATE TABLE IF NOT EXISTS skills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        class TEXT,
        department TEXT,
        area TEXT,
        supervisor TEXT
    )
    """)

    # DISCUSSIONS
    cur.execute("""
    CREATE TABLE IF NOT EXISTS discussions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic TEXT,
        user_name TEXT,
        message TEXT,
        created_at TEXT
    )
    """)

    # REPLIES (FIXED)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS replies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        discussion_id INTEGER,
        user_name TEXT,
        message TEXT,
        created_at TEXT
    )
    """)

    conn.commit()
    conn.close()


init_db()

# ---------------- HELPERS ----------------
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user' not in session:
            flash("Please login first!")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


# ---------------- INDEX ----------------
@app.route('/')
def index():
    return render_template('index.html')


# ---------------- AUTH ----------------
@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        password = request.form['password']
        confirm = request.form['confirm_password']

        if password != confirm:
            flash("Passwords do not match!")
            return redirect(url_for('register'))

        hashed = generate_password_hash(password)

        conn = get_connection()
        cur = conn.cursor()

        cur.execute("SELECT * FROM users WHERE email=?", (email,))
        if cur.fetchone():
            flash("Email already exists!")
            return redirect(url_for('login'))

        cur.execute("INSERT INTO users(name,email,password,role) VALUES (?,?,?,?)",
                    (name, email, hashed, "user"))

        conn.commit()
        conn.close()

        return redirect(url_for('login'))

    return render_template('register.html')


@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']

        conn = get_connection()
        cur = conn.cursor()

        cur.execute("SELECT * FROM users WHERE email=?", (email,))
        user = cur.fetchone()
        conn.close()

        if user and check_password_hash(user['password'], password):
            session['user'] = user['name']
            session['role'] = user['role']
            return redirect(url_for('dashboard'))
        else:
            flash("Invalid credentials!")
            return redirect(url_for('login'))

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    session.clear()
    return redirect(url_for('index'))


# ---------------- DASHBOARD ----------------
@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html')


# ---------------- PROJECTS ----------------
@app.route('/projects', methods=['GET','POST'])
@login_required
def projects():
    if request.method == 'POST':
        title = request.form['title']
        description = request.form['description']
        email = request.form['email']
        phone = request.form['phone']
        whatsapp = request.form['whatsapp']

        files = request.files.getlist('files')
        filenames = []

        for file in files:
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                filenames.append(filename)

        conn = get_connection()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO projects(title,description,file,uploader,email,phone,whatsapp)
            VALUES (?,?,?,?,?,?,?)
        """, (title, description, ",".join(filenames), session['user'], email, phone, whatsapp))

        conn.commit()
        conn.close()

        return redirect(url_for('uploads'))

    return render_template('projects.html')  # FIXED LOWERCASE


# ---------------- SKILLS ----------------
@app.route('/skills', methods=['POST'])
@login_required
def skills():
    name = request.form['name']
    student_class = request.form['class']
    department = request.form['department']
    area = request.form['area']
    supervisor = request.form['supervisor']

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO skills(name,class,department,area,supervisor)
        VALUES (?,?,?,?,?)
    """, (name, student_class, department, area, supervisor))

    conn.commit()
    conn.close()

    return redirect(url_for('uploads'))


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

    return render_template('uploads.html', projects=projects, skills=skills)


# ---------------- DELETE ----------------
@app.route('/delete_project/<int:project_id>', methods=['POST'])
@login_required
def delete_project(project_id):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT file FROM projects WHERE id=?", (project_id,))
    project = cur.fetchone()

    if project:
        files = project['file'].split(',')
        for f in files:
            path = os.path.join(app.config['UPLOAD_FOLDER'], f)
            if os.path.exists(path):
                os.remove(path)

        cur.execute("DELETE FROM projects WHERE id=?", (project_id,))
        conn.commit()

    conn.close()
    return redirect(url_for('uploads'))


# ---------------- STATIC PAGES ----------------
@app.route('/leadership')
@login_required
def leadership():
    return render_template('leadership.html')


@app.route('/about')
@login_required
def about():
    return render_template('about.html')


@app.route('/activities')
@login_required
def activities():
    return render_template('activities.html')


@app.route('/contact')
@login_required
def contact():
    return render_template('contact.html')


@app.route('/admin')
def admin():
    return render_template('admin.html')


# ---------------- DISCUSSION ----------------
@app.route('/discussion', methods=['GET', 'POST'])
@login_required
def discussion():
    conn = get_connection()
    cur = conn.cursor()

    if request.method == 'POST':
        user = session['user']
        discussion_id = request.form.get('discussion_id')
        message = request.form.get('message')

        if not message or message.strip() == "":
            flash("Message cannot be empty!")
            return redirect(url_for('discussion'))

        if discussion_id:
            cur.execute("""
                INSERT INTO replies(discussion_id, user_name, message, created_at)
                VALUES (?, ?, ?, ?)
            """, (
                discussion_id,
                user,
                message,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ))
        else:
            topic = request.form.get('topic', 'General')

            cur.execute("""
                INSERT INTO discussions(topic, user_name, message, created_at)
                VALUES (?, ?, ?, ?)
            """, (
                topic,
                user,
                message,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ))

        conn.commit()

    cur.execute("SELECT * FROM discussions ORDER BY created_at DESC")
    discussions = cur.fetchall()

    cur.execute("SELECT * FROM replies ORDER BY created_at ASC")
    replies = cur.fetchall()

    conn.close()

    replies_dict = {}
    for r in replies:
        replies_dict.setdefault(r['discussion_id'], []).append(r)

    return render_template(
        'discussion.html',
        discussions=discussions,
        replies_dict=replies_dict
    )


# ---------------- RUN ----------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)