from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps
from sqlalchemy import text
import cloudinary
import cloudinary.uploader
import os

# =========================================================
# APP CONFIG
# =========================================================
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "supersecretkey")

# =========================================================
# DATABASE CONFIG
# =========================================================
database_url = os.environ.get("DATABASE_URL")

if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# =========================================================
# AUTO FIX DATABASE
# =========================================================
with app.app_context():
    try:
        db.session.execute(text("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS name VARCHAR(200)
        """))

        db.session.execute(text("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS role VARCHAR(50)
        """))

        db.session.commit()
        print("DB schema checked successfully.")

    except Exception as e:
        print("DB auto-fix skipped:", e)

# =========================================================
# CLOUDINARY CONFIG
# =========================================================
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
    secure=True
)

# =========================================================
# MODELS
# =========================================================
class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(200))  # CHANGE THIS BACK
    email = db.Column(db.String(200), unique=True)
    password = db.Column(db.String(500))
    role = db.Column(db.String(50))


class Project(db.Model):
    __tablename__ = "projects"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(300))
    description = db.Column(db.Text)
    file = db.Column(db.Text)
    email = db.Column(db.String(200))
    phone = db.Column(db.String(100))
    whatsapp = db.Column(db.String(100))
    uploader = db.Column(db.String(200))


class Skill(db.Model):
    __tablename__ = "skills"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200))
    class_name = db.Column(db.String(200))
    department = db.Column(db.String(200))
    area = db.Column(db.String(200))
    supervisor = db.Column(db.String(200))


class Assistant(db.Model):
    __tablename__ = "assistants"

    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(100))
    innovator_name = db.Column(db.String(200))
    phone_number = db.Column(db.String(100))
    project_name = db.Column(db.String(300))
    assistant_area = db.Column(db.String(300))
    category = db.Column(db.String(200))


class Discussion(db.Model):
    __tablename__ = "discussions"

    id = db.Column(db.Integer, primary_key=True)
    user_name = db.Column(db.String(200))
    topic = db.Column(db.String(300))
    message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, server_default=db.func.now())


class Reply(db.Model):
    __tablename__ = "replies"

    id = db.Column(db.Integer, primary_key=True)
    discussion_id = db.Column(db.Integer)
    user_name = db.Column(db.String(200))
    message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, server_default=db.func.now())


# Create tables
with app.app_context():
    db.create_all()

# =========================================================
# LOGIN REQUIRED DECORATOR
# =========================================================
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


# =========================================================
# ROUTES
# =========================================================
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/dashboard')
@login_required
def dashboard():
    return render_template("dashboard.html")


# ---------------- LOGIN ----------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(email=request.form['email']).first()

        if user and check_password_hash(user.password, request.form['password']):
            session['user'] = user.name
            session['email'] = user.email
            return redirect(url_for('dashboard'))

        flash("Invalid login")
        return redirect(url_for('login'))

    return render_template('login.html')


# ---------------- REGISTER ----------------
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        existing_user = User.query.filter_by(email=request.form['email']).first()

        if existing_user:
            flash("Email already exists")
            return redirect(url_for('register'))

    new_user = User(
    username=request.form['name'],
    email=request.form['email'],
    password=generate_password_hash(request.form['password']),
    role="user"
)

        db.session.add(new_user)
        db.session.commit()

        flash("Registration successful")
        return redirect(url_for('login'))

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
    if request.method == 'POST':
        files = request.files.getlist('files')
        uploaded_files = []

        for file in files:
            if file and file.filename:
                filename = secure_filename(file.filename)

                result = cloudinary.uploader.upload(
                    file,
                    resource_type="auto",
                    folder="innovation_projects",
                    public_id=filename
                )

                uploaded_files.append(result['secure_url'])

        project = Project(
            title=request.form.get('title', ''),
            description=request.form.get('description', ''),
            file=",".join(uploaded_files),
            email=request.form.get('email', ''),
            phone=request.form.get('phone', ''),
            whatsapp=request.form.get('whatsapp', ''),
            uploader=session['user']
        )

        db.session.add(project)
        db.session.commit()

        flash("Project uploaded successfully")
        return redirect(url_for('uploads'))

    return render_template('projects.html')


# ---------------- UPLOADS ----------------
@app.route('/uploads')
@login_required
def uploads():
    projects = Project.query.order_by(Project.id.desc()).all()
    skills = Skill.query.order_by(Skill.id.desc()).all()
    return render_template("uploads.html", projects=projects, skills=skills)


# ---------------- ADD SKILL ----------------
@app.route('/add_skill', methods=['POST'])
@login_required
def add_skill():
    skill = Skill(
        name=request.form.get('name', ''),
        class_name=request.form.get('class', ''),
        department=request.form.get('department', ''),
        area=request.form.get('area', ''),
        supervisor=request.form.get('supervisor', '')
    )

    db.session.add(skill)
    db.session.commit()

    flash("Skill added successfully")
    return redirect(url_for('uploads'))


# ---------------- DELETE PROJECT ----------------
@app.route('/delete_project/<int:project_id>', methods=['POST'])
@login_required
def delete_project(project_id):
    project = Project.query.get_or_404(project_id)
    db.session.delete(project)
    db.session.commit()

    flash("Project deleted")
    return redirect(url_for('uploads'))


# ---------------- DELETE SKILL ----------------
@app.route('/delete_skill/<int:skill_id>', methods=['POST'])
@login_required
def delete_skill(skill_id):
    skill = Skill.query.get_or_404(skill_id)
    db.session.delete(skill)
    db.session.commit()

    flash("Skill deleted")
    return redirect(url_for('uploads'))


# ---------------- ASSISTANT ----------------
@app.route('/assistant', methods=['GET', 'POST'])
@login_required
def assistant():
    if request.method == 'POST':
        new_assistant = Assistant(
            type=request.form.get('type', ''),
            innovator_name=request.form.get('innovator_name', ''),
            phone_number=request.form.get('phone_number', ''),
            project_name=request.form.get('project_name', ''),
            assistant_area=request.form.get('assistant_area', ''),
            category=request.form.get('category', '')
        )

        db.session.add(new_assistant)
        db.session.commit()

        flash("Assistant request added")
        return redirect(url_for('assistant'))

    assistants = Assistant.query.order_by(Assistant.id.desc()).all()
    return render_template('assistant.html', assistants=assistants)


# ---------------- DISCUSSION ----------------
@app.route('/discussion', methods=['GET', 'POST'])
@login_required
def discussion():
    if request.method == 'POST':
        message = request.form.get('message')
        discussion_id = request.form.get('discussion_id')

        if discussion_id:
            reply = Reply(
                discussion_id=int(discussion_id),
                user_name=session['user'],
                message=message
            )
            db.session.add(reply)
        else:
            new_discussion = Discussion(
                user_name=session['user'],
                topic=request.form.get('topic'),
                message=message
            )
            db.session.add(new_discussion)

        db.session.commit()
        return redirect(url_for('discussion'))

    discussions = Discussion.query.order_by(Discussion.id.desc()).all()
    replies = Reply.query.order_by(Reply.id.asc()).all()

    replies_dict = {}
    for r in replies:
        replies_dict.setdefault(r.discussion_id, []).append(r)

    return render_template(
        'discussion.html',
        discussions=discussions,
        replies_dict=replies_dict
    )


# ---------------- STATIC PAGES ----------------
@app.route('/activities')
def activities():
    return render_template('activities.html')


@app.route('/contact')
def contact():
    return render_template('contact.html')


@app.route('/sponsor')
def sponsor():
    return render_template('sponsor.html')


@app.route('/about')
def about():
    return render_template('about.html')


@app.route('/leadership')
def leadership():
    return render_template('leadership.html')


@app.route('/blog')
def blog():
    return render_template('blog.html')


@app.route('/gallery')
def gallery():
    return render_template('gallery.html')


# ---------------- ERROR HANDLER ----------------
@app.errorhandler(500)
def server_error(e):
    print("SERVER ERROR:", e)
    return "Internal Server Error", 500


# =========================================================
# RUN APP
# =========================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
