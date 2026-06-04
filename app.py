from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps
import cloudinary
import cloudinary.uploader
import os

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "supersecretkey")

# ================= DATABASE =================
database_url = os.environ.get("DATABASE_URL")

if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

if not database_url:
    database_url = "sqlite:///app.db"

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ================= CLOUDINARY =================
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
    secure=True
)

# ================= MODELS =================
class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    email = db.Column(db.String(200), unique=True, nullable=False)
    password = db.Column(db.String(500), nullable=False)
    role = db.Column(db.String(50), default="user")


class Project(db.Model):
    __tablename__ = "projects"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(300))
    description = db.Column(db.Text)
    file = db.Column(db.Text)
    uploader = db.Column(db.String(200))


class Skill(db.Model):
    __tablename__ = "skills"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200))
    class_name = db.Column(db.String(200))
    department = db.Column(db.String(200))
    area = db.Column(db.String(200))
    supervisor = db.Column(db.String(200))


with app.app_context():
    db.create_all()

# ================= LOGIN REQUIRED =================
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper

# ================= PAGES =================
@app.route("/")
def index():
    return render_template("index.html")

@app.route('/dashboard')
@login_required
def dashboard():
    try:
        return render_template('dashboard.html', user=session.get('user'))
    except Exception as e:
        print("DASHBOARD ERROR:", e)
        return "Dashboard error"

# ================= REGISTER =================
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":

        username = request.form.get("username")
        email = request.form.get("email")
        password = request.form.get("password")

        if not username or not email or not password:
            flash("All fields required")
            return redirect(url_for("register"))

        if User.query.filter_by(email=email).first():
            flash("Email already exists")
            return redirect(url_for("register"))

        user = User(
            username=username,
            email=email,
            password=generate_password_hash(password)
        )

        db.session.add(user)
        db.session.commit()

        flash("Registered successfully")
        return redirect(url_for("login"))

    return render_template("register.html")

# ================= LOGIN =================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":

        email = request.form.get("email")
        password = request.form.get("password")

        user = User.query.filter_by(email=email).first()

        if not user:
            flash("Not registered")
            return redirect(url_for("register"))

        if not check_password_hash(user.password, password):
            flash("Wrong password")
            return redirect(url_for("login"))

        session["user"] = user.username
        session["email"] = user.email

        return redirect(url_for("dashboard"))

    return render_template("login.html")

# ================= LOGOUT =================
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

# ================= PROJECTS =================
@app.route("/projects", methods=["GET", "POST"])
@login_required
def projects():

    if request.method == "POST":

        files = request.files.getlist("files")
        uploaded = []

        for f in files:
            if f and f.filename:
                result = cloudinary.uploader.upload(f)
                uploaded.append(result["secure_url"])

        project = Project(
            title=request.form.get("title"),
            description=request.form.get("description"),
            file=",".join(uploaded),
            uploader=session["user"]
        )

        db.session.add(project)
        db.session.commit()

        flash("Project uploaded")
        return redirect(url_for("uploads"))

    return render_template("projects.html")

# ================= UPLOADS (SAFE FIX) =================
@app.route("/uploads")
@login_required
def uploads():
    try:
        projects = Project.query.all()
        skills = Skill.query.all()
        return render_template("uploads.html", projects=projects, skills=skills)
    except Exception as e:
        print("UPLOAD ERROR:", e)
        return "Uploads page error"

# ================= SKILLS =================
@app.route("/skills", methods=["POST"])
@login_required
def skills():

    skill = Skill(
        name=request.form.get("name"),
        class_name=request.form.get("class"),
        department=request.form.get("department"),
        area=request.form.get("area"),
        supervisor=request.form.get("supervisor")
    )

    db.session.add(skill)
    db.session.commit()

    flash("Skill added")
    return redirect(url_for("uploads"))

# ================= RUN =================
if __name__ == "__main__":
    app.run(debug=True)
