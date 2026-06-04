from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import cloudinary
import cloudinary.uploader
import os

app = Flask(__name__)

# ================= SECURITY =================
app.secret_key = "supersecretkey123"

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
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(200), unique=True, nullable=False)
    password = db.Column(db.String(500), nullable=False)
    role = db.Column(db.String(50), default="user")

class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(300))
    description = db.Column(db.Text)
    file = db.Column(db.Text)
    uploader = db.Column(db.String(200))

class Skill(db.Model):
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
        if not session.get("user"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper

# ================= BASIC PAGES =================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/home")
def home():
    return render_template("home.html")

@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", user=session.get("user"))

@app.route("/admin")
@login_required
def admin():
    return render_template("admin.html")

@app.route("/about")
def about():
    return render_template("about.html")

@app.route("/activities")
def activities():
    return render_template("activities.html")

@app.route("/assistant")
def assistant_page():
    return render_template("assistant.html")

@app.route("/blog")
def blog():
    return render_template("blog.html")

@app.route("/contact")
def contact():
    return render_template("contact.html")

@app.route("/gallery")
def gallery():
    return render_template("gallery.html")

@app.route("/leadership")
def leadership():
    return render_template("leadership.html")

@app.route("/sponsor")
def sponsor():
    return render_template("sponsor.html")

@app.route("/discussion")
@login_required
def discussion():
    return render_template("discussion.html")

@app.route("/projects")
@login_required
def projects():
    return render_template("projects.html")

@app.route("/uploads")
@login_required
def uploads():
    projects = Project.query.all()
    skills = Skill.query.all()
    return render_template("uploads.html", projects=projects, skills=skills)

# ================= AUTH =================
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":

        user = User(
            username=request.form.get("username"),
            email=request.form.get("email"),
            password=generate_password_hash(request.form.get("password"))
        )

        db.session.add(user)
        db.session.commit()

        return redirect(url_for("login"))

    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":

        user = User.query.filter_by(email=request.form.get("email")).first()

        if not user or not check_password_hash(user.password, request.form.get("password")):
            flash("Invalid login")
            return redirect(url_for("login"))

        session.clear()
        session["user"] = user.username
        session["email"] = user.email

        return redirect(url_for("dashboard"))

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

# ================= PROJECT UPLOAD =================
@app.route("/upload_project", methods=["POST"])
@login_required
def upload_project():

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
        uploader=session.get("user")
    )

    db.session.add(project)
    db.session.commit()

    return redirect(url_for("uploads"))

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

    return redirect(url_for("uploads"))

# ================= RUN =================
if __name__ == "__main__":
    app.run(debug=True)
