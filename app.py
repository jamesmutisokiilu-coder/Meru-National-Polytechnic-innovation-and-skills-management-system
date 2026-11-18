
import os
import sqlite3
import secrets
import json
from functools import wraps
from datetime import datetime

from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify
)


# =========================================================
# APPLICATION CONFIGURATION
# =========================================================

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

# Respect HTTPS at the hosting provider while keeping local HTTP development usable.
IS_PRODUCTION = (
    os.environ.get("RENDER", "").lower() == "true"
    or os.environ.get("FLASK_ENV", "").lower() == "production"
)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# Render persistent disks can be mounted at /var/data.
# Locally, the database remains beside this file unless DATABASE_PATH is set.
DATABASE = os.environ.get(
    "DATABASE_PATH",
    os.path.join(BASE_DIR, "innovation_hub.db")
)
DATABASE = os.path.abspath(DATABASE)
os.makedirs(os.path.dirname(DATABASE), exist_ok=True)

UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "innovator")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "extravaganza")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "innovator@local")


# =========================================================
# DATABASE FUNCTIONS
# =========================================================

def get_db():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def current_time():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def allowed_file(filename):
    return (
        "." in filename and
        filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


def form_value(*names, default=""):
    for name in names:
        value = request.form.get(name)

        if value is not None:
            return value.strip()

    return default


def get_table_columns(cursor, table_name):
    return {
        row["name"]
        for row in cursor.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }


def migrate_columns(cursor, table_name, migrations):
    existing_columns = get_table_columns(cursor, table_name)

    for column_name, definition in migrations.items():

        if column_name not in existing_columns:

            cursor.execute(
                f"ALTER TABLE {table_name} "
                f"ADD COLUMN {column_name} {definition}"
            )

            existing_columns.add(column_name)


# =========================================================
# NOTIFICATION SYSTEM
# =========================================================

def create_notification(
    title,
    message,
    category="general",
    url=None
):
    db = get_db()

    try:
        db.execute("""
            INSERT INTO notifications
            (title, message, category, url, is_read, created_at)
            VALUES (?, ?, ?, ?, 0, ?)
        """, (
            title,
            message,
            category or "general",
            url or "/admin_dashboard",
            current_time()
        ))

        db.commit()

    finally:
        db.close()


def get_unread_notification_count():

    db = get_db()

    try:
        result = db.execute("""
            SELECT COUNT(*) AS total
            FROM notifications
            WHERE is_read = 0
        """).fetchone()

        return result["total"]

    finally:
        db.close()


# =========================================================
# DATABASE INITIALIZATION AND MIGRATIONS
# =========================================================

def init_db():

    db = get_db()
    cursor = db.cursor()

    try:

        # ADMIN USERS
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                username TEXT UNIQUE,
                password TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        migrate_columns(cursor, "admins", {
            "username": "TEXT"
        })

        # ASSISTANCE REQUESTS
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS assistance_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                phone TEXT,
                category TEXT,
                description TEXT NOT NULL,
                date TEXT NOT NULL,
                status TEXT DEFAULT 'Pending'
            )
        """)

        # RESPONDERS
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS responders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                phone TEXT,
                expertise TEXT,
                description TEXT,
                date TEXT NOT NULL
            )
        """)

        # SPONSOR / CONTACT MESSAGES
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sponsor_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                organization TEXT,
                email TEXT NOT NULL,
                subject TEXT,
                message TEXT NOT NULL,
                date TEXT NOT NULL,
                status TEXT DEFAULT 'Unread'
            )
        """)

        # INNOVATION SUPPORT REQUESTS
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS support_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sponsor TEXT NOT NULL,
                organization TEXT,
                email TEXT NOT NULL,
                phone TEXT,
                innovation TEXT NOT NULL,
                support_type TEXT,
                message TEXT,
                date TEXT NOT NULL,
                status TEXT DEFAULT 'Pending'
            )
        """)

        # SPONSOR ACCOUNTS
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sponsor_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                organization TEXT,
                email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                phone TEXT,
                sponsor_type TEXT,
                password TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        # SPONSORSHIP MIGRATIONS
        migrate_columns(cursor, "support_requests", {
            "sponsor_id": "INTEGER",
            "contribution": "TEXT"
        })

        migrate_columns(cursor, "sponsor_messages", {
            "sponsor_id": "INTEGER",
            "message_type": "TEXT"
        })

        # INNOVATIONS
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS innovations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                department TEXT,
                problem TEXT,
                solution TEXT,
                description TEXT,
                image TEXT,
                status TEXT DEFAULT 'Active',
                date TEXT NOT NULL
            )
        """)

        # GALLERY
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS gallery (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                image TEXT NOT NULL,
                date TEXT NOT NULL
            )
        """)

        # BLOG POSTS
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS blog_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                author TEXT,
                date TEXT NOT NULL
            )
        """)

        # DISCUSSIONS
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS discussions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT,
                topic TEXT NOT NULL,
                message TEXT NOT NULL,
                date TEXT NOT NULL,
                category TEXT DEFAULT 'General'
            )
        """)

        # DISCUSSION REPLIES
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS discussion_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discussion_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                email TEXT,
                reply TEXT NOT NULL,
                date TEXT,
                FOREIGN KEY (discussion_id)
                    REFERENCES discussions(id)
                    ON DELETE CASCADE
            )
        """)

        # RESEARCH PROJECTS
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS research_projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                researcher TEXT NOT NULL,
                department TEXT,
                description TEXT NOT NULL,
                email TEXT,
                date TEXT NOT NULL,
                status TEXT DEFAULT 'Submitted'
            )
        """)

        # NOTIFICATIONS
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'general',
                url TEXT NOT NULL DEFAULT '/admin_dashboard',
                is_read INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT ''
            )
        """)

        # INNOVATOR REGISTRATIONS
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS innovators (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                phone TEXT NOT NULL,
                county TEXT,
                institution TEXT,
                field TEXT NOT NULL,
                innovation_name TEXT,
                description TEXT,
                stage TEXT,
                status TEXT NOT NULL DEFAULT 'Pending',
                registration_date TEXT NOT NULL
            )
        """)

        # LEARNING MATERIALS
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS learning_materials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                category TEXT,
                file_url TEXT,
                date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Active'
            )
        """)

        # ONLINE MEETING SESSIONS
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS meet_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                meet_link TEXT NOT NULL,
                session_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Active',
                created_at TEXT NOT NULL
            )
        """)

        # EXISTING DATABASE MIGRATIONS
        migrate_columns(cursor, "notifications", {
            "category": "TEXT NOT NULL DEFAULT 'general'",
            "url": "TEXT NOT NULL DEFAULT '/admin_dashboard'",
            "is_read": "INTEGER NOT NULL DEFAULT 0",
            "created_at": "TEXT NOT NULL DEFAULT ''"
        })

        migrate_columns(cursor, "discussions", {
            "category": "TEXT DEFAULT 'General'"
        })

        migrate_columns(cursor, "innovations", {
            "image": "TEXT",
            "uploader": "TEXT",
            "category": "TEXT",
            "email": "TEXT",
            "phone": "TEXT",
            "whatsapp": "TEXT",
            "images": "TEXT"
        })

        # UPDATE EXISTING NOTIFICATIONS
        cursor.execute("""
            UPDATE notifications
            SET category = 'general'
            WHERE category IS NULL OR category = ''
        """)

        cursor.execute("""
            UPDATE notifications
            SET url = '/admin_dashboard'
            WHERE url IS NULL OR url = ''
        """)

        cursor.execute("""
            UPDATE notifications
            SET is_read = 0
            WHERE is_read IS NULL
        """)

        cursor.execute("""
            UPDATE notifications
            SET created_at = ?
            WHERE created_at IS NULL OR created_at = ''
        """, (current_time(),))

        cursor.execute("""
            UPDATE discussions
            SET category = 'General'
            WHERE category IS NULL OR category = ''
        """)

        # CREATE OR UPDATE ADMIN ACCOUNT
        admin = cursor.execute("""
            SELECT id FROM admins
            WHERE username = ? OR email = ?
            ORDER BY id
            LIMIT 1
        """, (
            ADMIN_USERNAME,
            ADMIN_EMAIL
        )).fetchone()

        if not admin:

            admin = cursor.execute("""
                SELECT id FROM admins
                ORDER BY id
                LIMIT 1
            """).fetchone()

        password_hash = generate_password_hash(ADMIN_PASSWORD)

        if admin:

            cursor.execute("""
                UPDATE admins
                SET username = ?, email = ?, password = ?
                WHERE id = ?
            """, (
                ADMIN_USERNAME,
                ADMIN_EMAIL,
                password_hash,
                admin["id"]
            ))

        else:

            cursor.execute("""
                INSERT INTO admins
                (email, username, password, created_at)
                VALUES (?, ?, ?, ?)
            """, (
                ADMIN_EMAIL,
                ADMIN_USERNAME,
                password_hash,
                current_time()
            ))

        db.commit()

    except Exception:

        db.rollback()
        raise

    finally:
        db.close()


init_db()


# =========================================================
# AUTHENTICATION DECORATORS
# =========================================================

def admin_required(function):

    @wraps(function)
    def decorated_function(*args, **kwargs):

        if "admin_id" not in session:

            if request.path.startswith("/api/"):

                return jsonify({
                    "success": False,
                    "error": "Authentication required"
                }), 401

            return redirect(url_for("admin_login"))

        return function(*args, **kwargs)

    return decorated_function


def sponsor_required(function):

    @wraps(function)
    def decorated_function(*args, **kwargs):

        if "sponsor_id" not in session:

            return jsonify({
                "success": False,
                "message": "Please log in to your sponsor account."
            }), 401

        return function(*args, **kwargs)

    return decorated_function


# =========================================================
# HOME PAGE
# =========================================================

@app.route("/")
@app.route("/index")
def index():

    db = get_db()

    try:

        innovations = db.execute("""
            SELECT * FROM innovations
            WHERE status = 'Active'
            ORDER BY id DESC
            LIMIT 6
        """).fetchall()

        gallery = db.execute("""
            SELECT * FROM gallery
            ORDER BY id DESC
            LIMIT 6
        """).fetchall()

    finally:
        db.close()

    return render_template(
        "index.html",
        innovations=innovations,
        gallery=gallery,
        unread_notifications=(
            get_unread_notification_count()
            if "admin_id" in session else 0
        )
    )


# =========================================================
# ABOUT
# =========================================================

@app.route("/about")
def about():
    return render_template("about.html")


# =========================================================
# CONTACT
# =========================================================

@app.route("/contact", methods=["GET", "POST"])
def contact():

    if request.method == "POST":

        name = form_value("name", "full_name")
        email = form_value("email")
        organization = form_value("organization")
        subject = form_value("subject", default="Website Contact")
        message = form_value("message")

        if not name or not email or not message:

            flash(
                "Please fill in your name, email and message.",
                "error"
            )

            return redirect(url_for("contact"))

        db = get_db()

        try:

            db.execute("""
                INSERT INTO sponsor_messages
                (name, organization, email, subject, message, date, status)
                VALUES (?, ?, ?, ?, ?, ?, 'Unread')
            """, (
                name,
                organization,
                email,
                subject,
                message,
                current_time()
            ))

            db.commit()

        finally:
            db.close()

        create_notification(
            "New Contact Message",
            "A visitor has submitted a new contact message.",
            "contact",
            url_for("contact")
        )

        flash(
            "Your message has been sent successfully.",
            "success"
        )

        return redirect(url_for("contact"))

    return render_template("contact.html")


# =========================================================
# ASSISTANCE
# =========================================================

@app.route("/assistance", methods=["GET", "POST"])
@app.route("/need-assistance", methods=["GET", "POST"])
def assistant():

    if request.method == "POST":

        name = form_value("name", "full_name")
        email = form_value("email")
        phone = form_value("phone", "telephone")
        category = form_value("category", "assistance_type")
        description = form_value("description", "message")

        if not name or not email or not description:

            flash(
                "Please complete your name, email and description.",
                "error"
            )

            return redirect(url_for("assistant"))

        db = get_db()

        try:

            submission_date = current_time()

            cursor = db.execute("""
                INSERT INTO assistance_requests (
                    name,
                    email,
                    phone,
                    category,
                    description,
                    date,
                    status
                )
                VALUES (?, ?, ?, ?, ?, ?, 'Pending')
            """, (
                name,
                email,
                phone,
                category,
                description,
                submission_date
            ))

            request_id = cursor.lastrowid

            db.execute("""
                INSERT INTO notifications (
                    title,
                    message,
                    category,
                    url,
                    is_read,
                    created_at
                )
                VALUES (?, ?, ?, ?, 0, ?)
            """, (
                "New Assistance Request",
                f"{name} has submitted a new assistance request.",
                "assistance",
                "/admin_dashboard",
                submission_date
            ))

            db.commit()

            app.logger.info(
                "Assistance request #%s submitted successfully.",
                request_id
            )

            flash(
                "Your assistance request has been submitted successfully. "
                "Our team will contact you.",
                "success"
            )

        except Exception:

            db.rollback()

            app.logger.exception(
                "Failed to submit assistance request."
            )

            flash(
                "Something went wrong while submitting your request. "
                "Please try again.",
                "error"
            )

        finally:
            db.close()

        return redirect(url_for("assistant"))

    return render_template("assistance.html")


# =========================================================
# RESPONDER REGISTRATION
# =========================================================

@app.route("/register-responder", methods=["GET", "POST"])
def register_responder():

    if request.method == "POST":

        name = form_value("name", "full_name")
        email = form_value("email")
        phone = form_value("phone")
        expertise = form_value("expertise", "area_of_expertise")
        description = form_value("description", "message")

        if not name or not email or not expertise:

            flash(
                "Please provide your name, email and expertise.",
                "error"
            )

            return redirect(url_for("assistant"))

        db = get_db()

        try:

            db.execute("""
                INSERT INTO responders
                (name, email, phone, expertise, description, date)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                name,
                email,
                phone,
                expertise,
                description,
                current_time()
            ))

            db.commit()

        finally:
            db.close()

        flash(
            "Your responder registration was successful.",
            "success"
        )

        return redirect(url_for("assistant"))

    return redirect(url_for("assistant"))


# =========================================================
# BLOG
# =========================================================

@app.route("/blog", methods=["GET", "POST"])
def blog():

    if request.method == "POST":

        if "admin_id" not in session:
            return redirect(url_for("admin_login"))

        title = form_value("title")
        content = form_value("content", "description")
        author = form_value("author", default="MNP Innovation Hub")

        if not title or not content:

            flash("Title and content are required.", "error")
            return redirect(url_for("blog"))

        db = get_db()

        try:

            db.execute("""
                INSERT INTO blog_posts (title, content, author, date)
                VALUES (?, ?, ?, ?)
            """, (
                title,
                content,
                author,
                current_time()
            ))

            db.commit()

        finally:
            db.close()

        create_notification(
            "New Blog Post Published",
            "A new blog post has been published.",
            "blog",
            url_for("blog")
        )

        flash(
            "Blog post published successfully.",
            "success"
        )

        return redirect(url_for("blog"))

    db = get_db()

    try:

        posts = db.execute("""
            SELECT * FROM blog_posts
            ORDER BY id DESC
        """).fetchall()

    finally:
        db.close()

    return render_template("blog.html", posts=posts)


# =========================================================
# DISCUSSION FORUM
# =========================================================

@app.route("/discussion", methods=["GET", "POST"])
def discussion():

    if request.method == "POST":

        name = form_value("name", "full_name")
        email = form_value("email")
        topic = form_value("topic", "title")
        message = form_value("message", "description")
        category = form_value("category", default="General")

        if not name or not topic or not message:

            flash(
                "Please fill in your name, topic and message.",
                "error"
            )

            return redirect(url_for("discussion"))

        db = get_db()

        try:

            cursor = db.execute("""
                INSERT INTO discussions
                (name, email, topic, message, date, category)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                name,
                email,
                topic,
                message,
                current_time(),
                category
            ))

            discussion_id = cursor.lastrowid
            db.commit()

        finally:
            db.close()

        create_notification(
            "New Forum Discussion",
            "A new discussion has been published in the forum.",
            "discussion",
            url_for(
                "view_discussion",
                discussion_id=discussion_id
            )
        )

        flash(
            "Your discussion has been published successfully!",
            "success"
        )

        return redirect(url_for("discussion"))

    area = request.args.get("area", "all")
    db = get_db()

    try:

        query = """
            SELECT d.*,
            (
                SELECT COUNT(*)
                FROM discussion_replies r
                WHERE r.discussion_id = d.id
            ) AS reply_count
            FROM discussions d
        """

        if area != "all":

            discussions = db.execute(
                query + " WHERE d.category = ? ORDER BY d.id DESC",
                (area,)
            ).fetchall()

        else:

            discussions = db.execute(
                query + " ORDER BY d.id DESC"
            ).fetchall()

    finally:
        db.close()

    return render_template(
        "discussion.html",
        discussions=discussions,
        active_discussion=None,
        replies=[]
    )


@app.route("/discussion/<int:discussion_id>")
def view_discussion(discussion_id):

    db = get_db()

    try:

        active_discussion = db.execute("""
            SELECT d.*,
            (
                SELECT COUNT(*)
                FROM discussion_replies r
                WHERE r.discussion_id = d.id
            ) AS reply_count
            FROM discussions d
            WHERE d.id = ?
        """, (discussion_id,)).fetchone()

        if active_discussion:

            replies = db.execute("""
                SELECT * FROM discussion_replies
                WHERE discussion_id = ?
                ORDER BY id ASC
            """, (discussion_id,)).fetchall()

        else:
            replies = []

    finally:
        db.close()

    if not active_discussion:

        flash("Discussion not found.", "error")
        return redirect(url_for("discussion"))

    return render_template(
        "discussion.html",
        discussions=[],
        active_discussion=active_discussion,
        replies=replies
    )


@app.route(
    "/discussion/<int:discussion_id>/reply",
    methods=["POST"]
)
def post_discussion_reply(discussion_id):

    name = form_value("name", "full_name")
    email = form_value("email")
    message = form_value("message", "reply")

    if not name or not message:

        flash(
            "Please provide your name and reply.",
            "error"
        )

        return redirect(url_for(
            "view_discussion",
            discussion_id=discussion_id
        ))

    db = get_db()

    try:

        discussion_record = db.execute("""
            SELECT id FROM discussions WHERE id = ?
        """, (discussion_id,)).fetchone()

        if discussion_record:

            db.execute("""
                INSERT INTO discussion_replies
                (discussion_id, name, email, reply, date)
                VALUES (?, ?, ?, ?, ?)
            """, (
                discussion_id,
                name,
                email,
                message,
                current_time()
            ))

            db.commit()

    finally:
        db.close()

    if not discussion_record:

        flash("Discussion not found.", "error")
        return redirect(url_for("discussion"))

    create_notification(
        "New Forum Reply",
        "Someone has replied to a forum discussion.",
        "discussion",
        url_for(
            "view_discussion",
            discussion_id=discussion_id
        )
    )

    flash(
        "Your reply has been posted successfully!",
        "success"
    )

    return redirect(url_for(
        "view_discussion",
        discussion_id=discussion_id
    ))


# =========================================================
# GALLERY
# =========================================================

@app.route("/gallery", methods=["GET", "POST"])
def gallery():

    if request.method == "POST":

        if "admin_id" not in session:
            return redirect(url_for("admin_login"))

        title = form_value("title")
        description = form_value("description")
        image_file = request.files.get("image")

        if not title or not image_file or not image_file.filename:

            flash(
                "Please provide a title and select an image.",
                "error"
            )

            return redirect(url_for("gallery"))

        if not allowed_file(image_file.filename):

            flash("Unsupported image format.", "error")
            return redirect(url_for("gallery"))

        filename = secure_filename(image_file.filename)

        unique_filename = (
            f"{secrets.token_hex(8)}_{filename}"
        )

        image_file.save(
            os.path.join(UPLOAD_FOLDER, unique_filename)
        )

        db = get_db()

        try:

            db.execute("""
                INSERT INTO gallery
                (title, description, image, date)
                VALUES (?, ?, ?, ?)
            """, (
                title,
                description,
                unique_filename,
                current_time()
            ))

            db.commit()

        finally:
            db.close()

        flash(
            "Gallery image uploaded successfully.",
            "success"
        )

        return redirect(url_for("gallery"))

    db = get_db()

    try:

        images = db.execute("""
            SELECT * FROM gallery
            ORDER BY id DESC
        """).fetchall()

    finally:
        db.close()

    return render_template("gallery.html", images=images)


# =========================================================
# INNOVATIONS
# =========================================================

@app.route("/innovations", methods=["GET", "POST"])
def innovations():

    if request.method == "POST":

        is_ajax = (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or "application/json" in request.headers.get("Accept", "")
        )

        def response_error(message, status=400):

            if is_ajax:

                return jsonify({
                    "success": False,
                    "message": message
                }), status

            flash(message, "error")
            return redirect(url_for("innovations"))

        name = (
            request.form.get("title", "").strip()
            or request.form.get("name", "").strip()
        )

        uploader = request.form.get("name", "").strip()
        department = request.form.get("department", "").strip()
        category = request.form.get("category", "").strip()
        description = request.form.get("description", "").strip()

        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        whatsapp = request.form.get("whatsapp", "").strip()

        problem = request.form.get("problem", "").strip()
        solution = request.form.get("solution", "").strip()

        if not name:
            return response_error("Project title is required.")

        if not uploader:
            return response_error("Student or staff name is required.")

        if not department:
            return response_error("Department is required.")

        if not category:
            return response_error("Please select a project category.")

        if not description:
            return response_error("Project description is required.")

        if not email or not phone or not whatsapp:

            return response_error(
                "Email, phone number and WhatsApp number are required."
            )

        image_files = request.files.getlist("images")

        image_files = [
            image for image in image_files
            if image and image.filename
        ]

        if not image_files:

            return response_error(
                "Please upload at least one project image."
            )

        if len(image_files) > 8:

            return response_error(
                "You can upload a maximum of 8 images."
            )

        allowed_extensions = {
            "jpg", "jpeg", "png", "gif", "webp"
        }

        for image in image_files:

            filename = image.filename

            if "." not in filename:

                return response_error(
                    "An uploaded image has an invalid filename."
                )

            extension = filename.rsplit(".", 1)[1].lower()

            if extension not in allowed_extensions:

                return response_error(
                    "Only JPG, JPEG, PNG, GIF and WebP images are allowed."
                )

            image_data = image.stream.read(
                5 * 1024 * 1024 + 1
            )

            if len(image_data) > 5 * 1024 * 1024:

                return response_error(
                    f"Image {filename} exceeds the 5 MB limit."
                )

            image.stream.seek(0)

        upload_folder = os.path.join(
            app.root_path,
            "static",
            "uploads"
        )

        os.makedirs(upload_folder, exist_ok=True)

        saved_images = []
        db = get_db()

        try:

            # SAVE IMAGE FILES
            for image in image_files:

                original_name = secure_filename(image.filename)

                extension = original_name.rsplit(".", 1)[1].lower()

                unique_filename = (
                    secrets.token_hex(16) + "." + extension
                )

                image_path = os.path.join(
                    upload_folder,
                    unique_filename
                )

                image.save(image_path)
                saved_images.append(unique_filename)

            images_json = json.dumps(saved_images)

            cursor = db.execute("""
                INSERT INTO innovations
                (
                    name,
                    department,
                    problem,
                    solution,
                    description,
                    date,
                    status,
                    uploader,
                    category,
                    email,
                    phone,
                    whatsapp,
                    images
                )
                VALUES (?, ?, ?, ?, ?, ?, 'Pending', ?, ?, ?, ?, ?, ?)
            """, (
                name,
                department,
                problem,
                solution,
                description,
                current_time(),
                uploader,
                category,
                email,
                phone,
                whatsapp,
                images_json
            ))

            innovation_id = cursor.lastrowid

            db.execute("""
                INSERT INTO notifications
                (
                    title,
                    message,
                    category,
                    url,
                    is_read,
                    created_at
                )
                VALUES (?, ?, ?, ?, 0, ?)
            """, (
                "New Innovation Submitted",
                (
                    f"A new innovation '{name}' was submitted "
                    f"by {uploader} from {department} "
                    "and requires review."
                ),
                "innovation",
                url_for("admin_dashboard"),
                current_time()
            ))

            db.commit()

            app.logger.info(
                "Innovation %s submitted successfully.",
                innovation_id
            )

        except Exception:

            db.rollback()

            for filename in saved_images:

                image_path = os.path.join(
                    upload_folder,
                    filename
                )

                if os.path.exists(image_path):
                    os.remove(image_path)

            app.logger.exception(
                "Innovation submission failed."
            )

            return response_error(
                "An error occurred while submitting your innovation. "
                "Please try again.",
                500
            )

        finally:
            db.close()

        if is_ajax:

            return jsonify({
                "success": True,
                "message": (
                    "Your innovation has been submitted successfully. "
                    "The administrator has been notified. "
                    "Your project will appear in the gallery after approval."
                ),
                "innovation_id": innovation_id,
                "status": "Pending"
            }), 201

        flash(
            "Innovation submitted successfully for approval.",
            "success"
        )

        return redirect(url_for("innovations"))

    db = get_db()

    try:

        records = db.execute("""
            SELECT *
            FROM innovations
            ORDER BY id DESC
        """).fetchall()

    finally:
        db.close()

    return render_template(
        "innovations.html",
        innovations=records
    )



# =========================================================
# EXPLORE PAGE
# =========================================================

@app.route("/explore")
def explore():
    db = get_db()

    try:
        total_innovators = db.execute("""
            SELECT COUNT(*) AS total FROM innovators
        """).fetchone()["total"]

        approved_innovators = db.execute("""
            SELECT COUNT(*) AS total
            FROM innovators
            WHERE status = 'Approved'
        """).fetchone()["total"]

        recent_innovators = db.execute("""
            SELECT * FROM innovators
            ORDER BY id DESC
            LIMIT 6
        """).fetchall()

        materials = db.execute("""
            SELECT * FROM learning_materials
            WHERE status = 'Active'
            ORDER BY id DESC
        """).fetchall()

        meet_sessions = db.execute("""
            SELECT * FROM meet_sessions
            WHERE status = 'Active'
            ORDER BY session_date ASC
        """).fetchall()

        return render_template(
            "explore.html",
            total_innovators=total_innovators,
            approved_innovators=approved_innovators,
            recent_innovators=recent_innovators,
            materials=materials,
            total_materials=len(materials),
            meet_sessions=meet_sessions
        )

    except sqlite3.Error:
        app.logger.exception("Explore page database error.")
        flash("Unable to load some Explore page information.", "error")
        return render_template(
            "explore.html",
            total_innovators=0,
            approved_innovators=0,
            recent_innovators=[],
            materials=[],
            total_materials=0,
            meet_sessions=[]
        )

    finally:
        db.close()


@app.route("/innovator_register", methods=["POST"])
def innovator_register():
    full_name = form_value("full_name", "name")
    email = form_value("email").lower()
    phone = form_value("phone")
    county = form_value("county")
    institution = form_value("institution")
    field = form_value("field", "area_of_expertise")
    innovation_name = form_value("innovation_name", "innovation")
    description = form_value("description")
    stage = form_value("stage")

    if not full_name or not email or not phone or not field:
        flash(
            "Please provide your name, email, phone number and innovation field.",
            "error"
        )
        return redirect(url_for("explore") + "#register")

    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        flash("Please enter a valid email address.", "error")
        return redirect(url_for("explore") + "#register")

    db = get_db()

    try:
        submission_date = current_time()
        cursor = db.execute("""
            INSERT INTO innovators (
                full_name, email, phone, county, institution, field,
                innovation_name, description, stage, status, registration_date
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pending', ?)
        """, (
            full_name, email, phone, county, institution, field,
            innovation_name, description, stage, submission_date
        ))
        innovator_id = cursor.lastrowid

        db.execute("""
            INSERT INTO notifications (
                title, message, category, url, is_read, created_at
            )
            VALUES (?, ?, ?, ?, 0, ?)
        """, (
            "New Innovator Registration",
            f"{full_name} has registered as an innovator.",
            "innovator",
            "/admin_dashboard",
            submission_date
        ))
        db.commit()

        app.logger.info("Innovator registration #%s submitted.", innovator_id)
        flash(
            "Your innovator registration has been submitted successfully. "
            "Your application is awaiting approval.",
            "success"
        )

    except sqlite3.IntegrityError:
        db.rollback()
        flash(
            "This email address is already registered. Please use another email address.",
            "error"
        )

    except Exception:
        db.rollback()
        app.logger.exception("Innovator registration failed.")
        flash("Unable to submit your registration. Please try again.", "error")

    finally:
        db.close()

    return redirect(url_for("explore") + "#register")


# =========================================================
# PUBLIC INNOVATIONS GALLERY API
# =========================================================

@app.route("/api/innovations", methods=["GET"])
def public_innovations_api():

    db = get_db()

    try:

        rows = db.execute("""
            SELECT *
            FROM innovations
            WHERE LOWER(TRIM(COALESCE(status, '')))
                IN ('active', 'approved')
            ORDER BY id DESC
        """).fetchall()

        projects = []

        for row in rows:

            project = dict(row)

            raw_images = project.get("images") or "[]"

            try:

                image_list = (
                    json.loads(raw_images)
                    if isinstance(raw_images, str)
                    else raw_images
                )

            except (TypeError, ValueError, json.JSONDecodeError):

                image_list = []

            if not isinstance(image_list, list):
                image_list = []

            image_list = [
                str(item)
                for item in image_list
                if item
            ]

            legacy_image = project.get("image") or ""

            if not image_list and legacy_image:
                image_list = [legacy_image]

            project["images"] = image_list

            project["image"] = (
                legacy_image
                or (image_list[0] if image_list else "")
            )

            project["title"] = (
                project.get("name")
                or "Untitled innovation"
            )

            project["uploader"] = (
                project.get("uploader")
                or project.get("student_name")
                or ""
            )

            project["student_name"] = project["uploader"]

            project["date"] = (
                project.get("date")
                or project.get("created_at")
                or ""
            )

            project["category"] = (
                project.get("category")
                or "General"
            )

            project["department"] = (
                project.get("department") or ""
            )

            project["description"] = (
                project.get("description") or ""
            )

            project["email"] = project.get("email") or ""
            project["phone"] = project.get("phone") or ""
            project["whatsapp"] = project.get("whatsapp") or ""

            projects.append(project)

        return jsonify({
            "success": True,
            "innovations": projects,
            "count": len(projects)
        }), 200

    except Exception:

        app.logger.exception(
            "Public innovations gallery API failed."
        )

        return jsonify({
            "success": False,
            "message": "Unable to load the innovations gallery.",
            "innovations": []
        }), 500

    finally:
        db.close()


# =========================================================
# LEADERSHIP
# =========================================================

@app.route("/leadership")
def leadership():
    return render_template("leadership.html")


# =========================================================
# RESEARCH
# =========================================================

@app.route("/research", methods=["GET", "POST"])
def research():

    if request.method == "POST":

        title = form_value("title", "project_title")
        researcher = form_value("researcher", "name", "full_name")
        department = form_value("department")
        description = form_value("description", "abstract")
        email = form_value("email")

        if not title or not researcher or not description:

            flash(
                "Please complete all required research fields.",
                "error"
            )

            return redirect(url_for("research"))

        db = get_db()

        try:

            db.execute("""
                INSERT INTO research_projects
                (
                    title,
                    researcher,
                    department,
                    description,
                    email,
                    date,
                    status
                )
                VALUES (?, ?, ?, ?, ?, ?, 'Submitted')
            """, (
                title,
                researcher,
                department,
                description,
                email,
                current_time()
            ))

            db.commit()

        finally:
            db.close()

        create_notification(
            "New Research Submission",
            "A new research project has been submitted.",
            "research",
            url_for("research")
        )

        flash(
            "Your research project has been submitted.",
            "success"
        )

        return redirect(url_for("research"))

    db = get_db()

    try:

        projects = db.execute("""
            SELECT * FROM research_projects
            ORDER BY id DESC
        """).fetchall()

    finally:
        db.close()

    return render_template(
        "research.html",
        projects=projects
    )


# =========================================================
# SPONSORSHIP PAGE
# =========================================================

@app.route("/sponsorship", methods=["GET", "POST"])
def sponsorship():

    if request.method == "POST":

        sponsor = form_value(
            "sponsor",
            "name",
            "full_name"
        )

        organization = form_value("organization")
        email = form_value("email").lower()
        phone = form_value("phone")

        innovation = form_value(
            "innovation",
            "innovation_name"
        )

        support_type = form_value(
            "support_type",
            "support"
        )

        message = form_value(
            "message",
            "description"
        )

        contribution = form_value(
            "contribution",
            "amount"
        )

        sponsor_id = session.get("sponsor_id")

        db = get_db()

        try:

            if sponsor_id:

                account = db.execute("""
                    SELECT id, name, organization, email, phone
                    FROM sponsor_accounts
                    WHERE id = ?
                """, (sponsor_id,)).fetchone()

                if not account:

                    session.pop("sponsor_id", None)

                    flash(
                        "Your sponsor session has expired. "
                        "Please log in again.",
                        "error"
                    )

                    return redirect(url_for("sponsorship"))

                sponsor = account["name"]
                organization = account["organization"] or ""
                email = account["email"]
                phone = account["phone"] or ""

            if not sponsor or not email:

                flash(
                    "Please provide your name and email.",
                    "error"
                )

                return redirect(url_for("sponsorship"))

            if not innovation and not message:

                flash(
                    "Please enter a message or select an innovation to support.",
                    "error"
                )

                return redirect(url_for("sponsorship"))

            submission_date = current_time()

            if innovation:

                db.execute("""
                    INSERT INTO support_requests (
                        sponsor,
                        organization,
                        email,
                        phone,
                        innovation,
                        support_type,
                        message,
                        date,
                        status,
                        sponsor_id,
                        contribution
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Pending', ?, ?)
                """, (
                    sponsor,
                    organization,
                    email,
                    phone,
                    innovation,
                    support_type or "General Support",
                    message,
                    submission_date,
                    sponsor_id,
                    contribution
                ))

                db.execute("""
                    INSERT INTO notifications (
                        title,
                        message,
                        category,
                        url,
                        is_read,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, 0, ?)
                """, (
                    "New Innovation Support Request",
                    f"{sponsor} has requested support for {innovation}.",
                    "sponsorship",
                    "/admin_dashboard",
                    submission_date
                ))

                flash(
                    "Your innovation support request has been submitted successfully.",
                    "success"
                )

            else:

                db.execute("""
                    INSERT INTO sponsor_messages (
                        name,
                        organization,
                        email,
                        subject,
                        message,
                        date,
                        status,
                        sponsor_id,
                        message_type
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'Unread', ?, ?)
                """, (
                    sponsor,
                    organization,
                    email,
                    support_type or "Sponsorship Enquiry",
                    message,
                    submission_date,
                    sponsor_id,
                    support_type or "Sponsorship Enquiry"
                ))

                db.execute("""
                    INSERT INTO notifications (
                        title,
                        message,
                        category,
                        url,
                        is_read,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, 0, ?)
                """, (
                    "New Sponsorship Enquiry",
                    f"{sponsor} has submitted a sponsorship message.",
                    "sponsorship",
                    "/admin_dashboard",
                    submission_date
                ))

                flash(
                    "Your sponsorship message has been sent successfully.",
                    "success"
                )

            db.commit()

        except Exception:

            db.rollback()

            app.logger.exception(
                "Sponsorship submission failed."
            )

            flash(
                "Unable to submit your request. Please try again.",
                "error"
            )

        finally:
            db.close()

        return redirect(url_for("sponsorship"))

    db = get_db()

    try:

        innovation_records = db.execute("""
            SELECT *
            FROM innovations
            WHERE status = 'Active'
            ORDER BY id DESC
        """).fetchall()

    finally:
        db.close()

    return render_template(
        "sponsorship.html",
        innovations=innovation_records
    )


# =========================================================
# SPONSOR REGISTRATION API
# =========================================================

@app.route("/api/sponsor/register", methods=["POST"])
def sponsor_register():

    data = request.get_json(silent=True) or request.form

    name = (
        data.get("name")
        or data.get("sponsor")
        or data.get("full_name")
        or ""
    ).strip()

    organization = data.get("organization", "").strip()
    email = data.get("email", "").strip().lower()
    phone = data.get("phone", "").strip()

    sponsor_type = (
        data.get("sponsor_type")
        or data.get("type")
        or ""
    ).strip()

    password = data.get("password", "")

    confirm_password = (
        data.get("confirm_password")
        or data.get("confirmPassword")
        or ""
    )

    if not name or not email or not password:

        return jsonify({
            "success": False,
            "message": "Name, email and password are required."
        }), 400

    if len(password) < 8:

        return jsonify({
            "success": False,
            "message": "Your password must contain at least 8 characters."
        }), 400

    if confirm_password and password != confirm_password:

        return jsonify({
            "success": False,
            "message": "Your passwords do not match."
        }), 400

    db = get_db()

    try:

        existing = db.execute("""
            SELECT id FROM sponsor_accounts
            WHERE email = ?
        """, (email,)).fetchone()

        if existing:

            return jsonify({
                "success": False,
                "message": "An account with this email already exists."
            }), 409

        password_hash = generate_password_hash(password)

        cursor = db.execute("""
            INSERT INTO sponsor_accounts (
                name,
                organization,
                email,
                phone,
                sponsor_type,
                password,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            name,
            organization,
            email,
            phone,
            sponsor_type,
            password_hash,
            current_time()
        ))

        sponsor_id = cursor.lastrowid

        db.execute("""
            INSERT INTO notifications (
                title,
                message,
                category,
                url,
                is_read,
                created_at
            )
            VALUES (?, ?, ?, ?, 0, ?)
        """, (
            "New Sponsor Registration",
            f"{name} has registered as a sponsor.",
            "sponsorship",
            "/admin_dashboard",
            current_time()
        ))

        db.commit()

        return jsonify({
            "success": True,
            "message": (
                "Your sponsor account has been created successfully. "
                "You can now log in."
            ),
            "sponsor_id": sponsor_id
        }), 201

    except sqlite3.IntegrityError:

        db.rollback()

        return jsonify({
            "success": False,
            "message": "This email address is already registered."
        }), 409

    except Exception:

        db.rollback()

        app.logger.exception(
            "Sponsor registration failed."
        )

        return jsonify({
            "success": False,
            "message": "Unable to create your account. Please try again."
        }), 500

    finally:
        db.close()


# =========================================================
# SPONSOR LOGIN API
# =========================================================

@app.route("/api/sponsor/login", methods=["POST"])
def sponsor_login():

    data = request.get_json(silent=True) or request.form

    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    if not email or not password:

        return jsonify({
            "success": False,
            "message": "Email and password are required."
        }), 400

    db = get_db()

    try:

        sponsor = db.execute("""
            SELECT *
            FROM sponsor_accounts
            WHERE email = ?
            LIMIT 1
        """, (email,)).fetchone()

        if not sponsor or not check_password_hash(
            sponsor["password"],
            password
        ):

            return jsonify({
                "success": False,
                "message": "Invalid email or password."
            }), 401

        # Clear any previous login identity.
        session.clear()

        session["sponsor_id"] = sponsor["id"]

        return jsonify({
            "success": True,
            "message": "You have logged in successfully.",
            "sponsor": {
                "id": sponsor["id"],
                "name": sponsor["name"],
                "organization": sponsor["organization"] or "",
                "email": sponsor["email"],
                "phone": sponsor["phone"] or "",
                "sponsor_type": sponsor["sponsor_type"] or ""
            }
        }), 200

    except Exception:

        app.logger.exception(
            "Sponsor login failed."
        )

        return jsonify({
            "success": False,
            "message": "Unable to log in. Please try again."
        }), 500

    finally:
        db.close()


# =========================================================
# SPONSOR PROFILE API
# =========================================================

@app.route("/api/sponsor/me", methods=["GET"])
@sponsor_required
def sponsor_me():

    db = get_db()

    try:

        sponsor = db.execute("""
            SELECT id, name, organization, email,
                   phone, sponsor_type, created_at
            FROM sponsor_accounts
            WHERE id = ?
        """, (session["sponsor_id"],)).fetchone()

        if not sponsor:

            session.pop("sponsor_id", None)

            return jsonify({
                "success": False,
                "message": "Sponsor account not found. Please log in again."
            }), 401

        return jsonify({
            "success": True,
            "sponsor": dict(sponsor)
        }), 200

    except Exception:

        app.logger.exception(
            "Loading sponsor profile failed."
        )

        return jsonify({
            "success": False,
            "message": "Unable to load your sponsor profile."
        }), 500

    finally:
        db.close()


# =========================================================
# SPONSOR LOGOUT API
# =========================================================

@app.route("/api/sponsor/logout", methods=["POST"])
def sponsor_logout():

    session.pop("sponsor_id", None)

    return jsonify({
        "success": True,
        "message": "You have logged out successfully."
    }), 200


# =========================================================
# SPONSOR DASHBOARD API
# =========================================================

@app.route("/api/sponsor/dashboard", methods=["GET"])
@sponsor_required
def sponsor_dashboard():

    db = get_db()

    try:

        sponsor_id = session["sponsor_id"]

        sponsor = db.execute("""
            SELECT id, name, organization, email,
                   phone, sponsor_type
            FROM sponsor_accounts
            WHERE id = ?
        """, (sponsor_id,)).fetchone()

        if not sponsor:

            session.pop("sponsor_id", None)

            return jsonify({
                "success": False,
                "message": "Sponsor account not found."
            }), 401

        support_requests = db.execute("""
            SELECT id, innovation, support_type,
                   contribution, message, date, status
            FROM support_requests
            WHERE sponsor_id = ?
            ORDER BY id DESC
        """, (sponsor_id,)).fetchall()

        messages = db.execute("""
            SELECT id, subject, message, message_type,
                   date, status
            FROM sponsor_messages
            WHERE sponsor_id = ?
            ORDER BY id DESC
        """, (sponsor_id,)).fetchall()

        return jsonify({
            "success": True,
            "sponsor": dict(sponsor),
            "support_requests": [
                dict(row) for row in support_requests
            ],
            "messages": [
                dict(row) for row in messages
            ]
        }), 200

    except Exception:

        app.logger.exception(
            "Sponsor dashboard failed."
        )

        return jsonify({
            "success": False,
            "message": "Unable to load your dashboard."
        }), 500

    finally:
        db.close()


# =========================================================
# SPONSOR INNOVATION SUPPORT API
# =========================================================

@app.route("/api/sponsor/support", methods=["POST"])
@sponsor_required
def sponsor_support():

    data = request.get_json(silent=True) or request.form

    innovation = (
        data.get("innovation")
        or data.get("innovation_name")
        or ""
    ).strip()

    support_type = (
        data.get("support_type")
        or data.get("support")
        or "General Support"
    ).strip()

    message = (
        data.get("message")
        or data.get("description")
        or ""
    ).strip()

    contribution = (
        data.get("contribution")
        or data.get("amount")
        or ""
    ).strip()

    if not innovation:

        return jsonify({
            "success": False,
            "message": "Please select an innovation to support."
        }), 400

    db = get_db()

    try:

        sponsor = db.execute("""
            SELECT *
            FROM sponsor_accounts
            WHERE id = ?
        """, (session["sponsor_id"],)).fetchone()

        if not sponsor:

            session.pop("sponsor_id", None)

            return jsonify({
                "success": False,
                "message": "Please log in again."
            }), 401

        submission_date = current_time()

        cursor = db.execute("""
            INSERT INTO support_requests (
                sponsor,
                organization,
                email,
                phone,
                innovation,
                support_type,
                message,
                date,
                status,
                sponsor_id,
                contribution
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Pending', ?, ?)
        """, (
            sponsor["name"],
            sponsor["organization"] or "",
            sponsor["email"],
            sponsor["phone"] or "",
            innovation,
            support_type,
            message,
            submission_date,
            sponsor["id"],
            contribution
        ))

        request_id = cursor.lastrowid

        db.execute("""
            INSERT INTO notifications (
                title,
                message,
                category,
                url,
                is_read,
                created_at
            )
            VALUES (?, ?, ?, ?, 0, ?)
        """, (
            "New Innovation Support Request",
            f"{sponsor['name']} has requested support for {innovation}.",
            "sponsorship",
            "/admin_dashboard",
            submission_date
        ))

        db.commit()

        return jsonify({
            "success": True,
            "message": (
                "Your innovation support request has been submitted successfully."
            ),
            "request_id": request_id
        }), 201

    except Exception:

        db.rollback()

        app.logger.exception(
            "Sponsor support request failed."
        )

        return jsonify({
            "success": False,
            "message": "Unable to submit your support request."
        }), 500

    finally:
        db.close()


# =========================================================
# SPONSOR GENERAL MESSAGE API
# =========================================================

@app.route("/api/sponsor/message", methods=["POST"])
@sponsor_required
def sponsor_message():

    data = request.get_json(silent=True) or request.form

    subject = (
        data.get("subject")
        or data.get("support_type")
        or "Sponsorship Enquiry"
    ).strip()

    message = (
        data.get("message")
        or data.get("description")
        or ""
    ).strip()

    if not message:

        return jsonify({
            "success": False,
            "message": "Please enter your message."
        }), 400

    db = get_db()

    try:

        sponsor = db.execute("""
            SELECT *
            FROM sponsor_accounts
            WHERE id = ?
        """, (session["sponsor_id"],)).fetchone()

        if not sponsor:

            session.pop("sponsor_id", None)

            return jsonify({
                "success": False,
                "message": "Please log in again."
            }), 401

        submission_date = current_time()

        cursor = db.execute("""
            INSERT INTO sponsor_messages (
                name,
                organization,
                email,
                subject,
                message,
                date,
                status,
                sponsor_id,
                message_type
            )
            VALUES (?, ?, ?, ?, ?, ?, 'Unread', ?, ?)
        """, (
            sponsor["name"],
            sponsor["organization"] or "",
            sponsor["email"],
            subject,
            message,
            submission_date,
            sponsor["id"],
            subject
        ))

        message_id = cursor.lastrowid

        db.execute("""
            INSERT INTO notifications (
                title,
                message,
                category,
                url,
                is_read,
                created_at
            )
            VALUES (?, ?, ?, ?, 0, ?)
        """, (
            "New Sponsorship Enquiry",
            f"{sponsor['name']} has submitted a sponsorship message.",
            "sponsorship",
            "/admin_dashboard",
            submission_date
        ))

        db.commit()

        return jsonify({
            "success": True,
            "message": "Your message has been sent successfully.",
            "message_id": message_id
        }), 201

    except Exception:

        db.rollback()

        app.logger.exception(
            "Sponsor message submission failed."
        )

        return jsonify({
            "success": False,
            "message": "Unable to send your message."
        }), 500

    finally:
        db.close()


# =========================================================
# ADMIN LOGIN
# =========================================================

@app.route("/admin_login", methods=["GET", "POST"])
def admin_login():

    if "admin_id" in session:
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":

        username = form_value("username", "email")
        password = request.form.get("password", "")

        db = get_db()

        try:

            admin = db.execute("""
                SELECT *
                FROM admins
                WHERE username = ? OR email = ?
                LIMIT 1
            """, (username, username)).fetchone()

        finally:
            db.close()

        if admin and check_password_hash(
            admin["password"],
            password
        ):

            session.clear()

            session["admin_id"] = admin["id"]
            session["admin_username"] = admin["username"]
            session["admin_email"] = admin["email"]

            flash(
                "Welcome to the Innovation Hub Admin Dashboard.",
                "success"
            )

            return redirect(url_for("admin_dashboard"))

        flash(
            "Invalid username or password.",
            "error"
        )

        return redirect(url_for("admin_login"))

    return render_template("admin_login.html")


@app.route("/admin-login", methods=["GET", "POST"])
def admin_login_alt():
    return redirect(url_for("admin_login"))


# =========================================================
# ADMIN DASHBOARD
# =========================================================

@app.route("/admin_dashboard")
@admin_required
def admin_dashboard():

    unread_count = get_unread_notification_count()

    return render_template(
        "admin_dashboard.html",
        admin_email=session.get("admin_email"),
        admin_username=session.get("admin_username"),
        unread_notifications=unread_count
    )


@app.route("/admin")
@admin_required
def admin_home():
    return redirect(url_for("admin_dashboard"))


# =========================================================
# ADMIN LOGOUT
# =========================================================

@app.route("/admin_logout")
def admin_logout():

    session.clear()

    flash(
        "You have logged out successfully.",
        "success"
    )

    return redirect(url_for("admin_login"))


# =========================================================
# NOTIFICATION API
# =========================================================

@app.route("/api/notifications", methods=["GET"])
@admin_required
def api_notifications():

    db = get_db()

    try:

        notifications = db.execute("""
            SELECT id, title, message, category, url,
                   is_read, created_at
            FROM notifications
            ORDER BY id DESC
            LIMIT 30
        """).fetchall()

        unread_count = db.execute("""
            SELECT COUNT(*) AS total
            FROM notifications
            WHERE is_read = 0
        """).fetchone()["total"]

        return jsonify({
            "success": True,
            "unread_count": unread_count,
            "notifications": [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "message": row["message"],
                    "category": row["category"],
                    "url": row["url"],
                    "is_read": bool(row["is_read"]),
                    "created_at": row["created_at"]
                }
                for row in notifications
            ]
        })

    finally:
        db.close()


# =========================================================
# MARK ONE NOTIFICATION AS READ
# =========================================================

@app.route(
    "/api/notifications/<int:notification_id>/read",
    methods=["POST"]
)
@admin_required
def mark_notification_read(notification_id):

    db = get_db()

    try:

        cursor = db.execute("""
            UPDATE notifications
            SET is_read = 1
            WHERE id = ?
        """, (notification_id,))

        db.commit()

        if cursor.rowcount == 0:

            return jsonify({
                "success": False,
                "error": "Notification not found"
            }), 404

    finally:
        db.close()

    return jsonify({
        "success": True,
        "unread_count": get_unread_notification_count()
    })


# =========================================================
# MARK ALL NOTIFICATIONS AS READ
# =========================================================

@app.route("/api/notifications/read-all", methods=["POST"])
@admin_required
def mark_all_notifications_read():

    db = get_db()

    try:

        db.execute("""
            UPDATE notifications
            SET is_read = 1
            WHERE is_read = 0
        """)

        db.commit()

    finally:
        db.close()

    return jsonify({
        "success": True,
        "unread_count": 0
    })


# =========================================================
# ADMIN API: ASSISTANCE REQUESTS
# =========================================================

@app.route("/api/admin/assistance")
@admin_required
def api_assistance():

    db = get_db()

    try:

        records = db.execute("""
            SELECT *
            FROM assistance_requests
            ORDER BY id DESC
        """).fetchall()

        return jsonify([
            {
                "id": row["id"],
                "name": row["name"],
                "email": row["email"],
                "phone": row["phone"] or "",
                "category": row["category"] or "",
                "description": row["description"],
                "date": row["date"],
                "status": row["status"]
            }
            for row in records
        ])

    finally:
        db.close()


# =========================================================
# ADMIN API: RESPONDERS
# =========================================================

@app.route("/api/admin/responders")
@admin_required
def api_responders():

    db = get_db()

    try:

        records = db.execute("""
            SELECT *
            FROM responders
            ORDER BY id DESC
        """).fetchall()

        return jsonify([
            {
                "id": row["id"],
                "name": row["name"],
                "email": row["email"],
                "phone": row["phone"] or "",
                "expertise": row["expertise"] or "",
                "description": row["description"] or "",
                "date": row["date"]
            }
            for row in records
        ])

    finally:
        db.close()


# =========================================================
# ADMIN API: SPONSOR MESSAGES
# =========================================================

@app.route("/api/admin/sponsor-messages")
@admin_required
def api_sponsor_messages():

    db = get_db()

    try:

        records = db.execute("""
            SELECT *
            FROM sponsor_messages
            ORDER BY id DESC
        """).fetchall()

        return jsonify([
            {
                "id": row["id"],
                "name": row["name"],
                "organization": row["organization"] or "",
                "email": row["email"],
                "subject": row["subject"] or "",
                "message": row["message"],
                "date": row["date"],
                "status": row["status"],
                "message_type": row["message_type"] or ""
            }
            for row in records
        ])

    finally:
        db.close()


# =========================================================
# ADMIN API: SUPPORT REQUESTS
# =========================================================

@app.route("/api/admin/support-requests")
@admin_required
def api_support_requests():

    db = get_db()

    try:

        records = db.execute("""
            SELECT *
            FROM support_requests
            ORDER BY id DESC
        """).fetchall()

        return jsonify([
            {
                "id": row["id"],
                "sponsor": row["sponsor"],
                "organization": row["organization"] or "",
                "email": row["email"],
                "phone": row["phone"] or "",
                "innovation": row["innovation"],
                "type": row["support_type"] or "",
                "contribution": row["contribution"] or "",
                "message": row["message"] or "",
                "date": row["date"],
                "status": row["status"],
                "sponsor_id": row["sponsor_id"]
            }
            for row in records
        ])

    finally:
        db.close()


# =========================================================
# ADMIN API: INNOVATIONS
# =========================================================

@app.route("/api/admin/innovations")
@admin_required
def api_innovations():

    db = get_db()

    try:

        records = db.execute("""
            SELECT *
            FROM innovations
            ORDER BY id DESC
        """).fetchall()

        return jsonify([
            {
                "id": row["id"],
                "name": row["name"],
                "department": row["department"] or "",
                "problem": row["problem"] or "",
                "solution": row["solution"] or "",
                "description": row["description"] or "",
                "image": row["image"] or "",
                "status": row["status"]
            }
            for row in records
        ])

    finally:
        db.close()


# =========================================================
# UPDATE ASSISTANCE STATUS
# =========================================================

@app.route(
    "/api/admin/assistance/<int:record_id>/status",
    methods=["POST"]
)
@admin_required
def update_assistance_status(record_id):

    data = request.get_json(silent=True) or {}
    status = data.get("status", "")

    if status not in ["Pending", "Approved", "Rejected"]:

        return jsonify({
            "success": False,
            "error": "Invalid status"
        }), 400

    db = get_db()

    try:

        cursor = db.execute("""
            UPDATE assistance_requests
            SET status = ?
            WHERE id = ?
        """, (status, record_id))

        db.commit()

        if cursor.rowcount == 0:

            return jsonify({
                "success": False,
                "error": "Request not found"
            }), 404

    finally:
        db.close()

    return jsonify({
        "success": True,
        "status": status
    })


# =========================================================
# UPDATE SUPPORT REQUEST STATUS
# =========================================================

@app.route(
    "/api/admin/support-requests/<int:record_id>/status",
    methods=["POST"]
)
@admin_required
def update_support_status(record_id):

    data = request.get_json(silent=True) or {}
    status = data.get("status", "")

    if status not in ["Pending", "Approved", "Rejected"]:

        return jsonify({
            "success": False,
            "error": "Invalid status"
        }), 400

    db = get_db()

    try:

        cursor = db.execute("""
            UPDATE support_requests
            SET status = ?
            WHERE id = ?
        """, (status, record_id))

        db.commit()

        if cursor.rowcount == 0:

            return jsonify({
                "success": False,
                "error": "Request not found"
            }), 404

    finally:
        db.close()

    return jsonify({
        "success": True,
        "status": status
    })


# =========================================================
# UPDATE INNOVATION STATUS
# =========================================================

@app.route(
    "/api/admin/innovations/<int:record_id>/status",
    methods=["POST"]
)
@admin_required
def update_innovation_status(record_id):

    data = request.get_json(silent=True) or {}
    status = data.get("status", "")

    if status not in ["Pending", "Active", "Rejected"]:

        return jsonify({
            "success": False,
            "error": "Invalid status"
        }), 400

    db = get_db()

    try:

        cursor = db.execute("""
            UPDATE innovations
            SET status = ?
            WHERE id = ?
        """, (status, record_id))

        db.commit()

        if cursor.rowcount == 0:

            return jsonify({
                "success": False,
                "error": "Innovation not found"
            }), 404

    finally:
        db.close()

    return jsonify({
        "success": True,
        "status": status,
        "message": "Innovation status updated successfully."
    })


# =========================================================
# MARK SPONSOR MESSAGE AS READ
# =========================================================

@app.route(
    "/api/admin/sponsor-messages/<int:record_id>/read",
    methods=["POST"]
)
@admin_required
def mark_sponsor_message_read(record_id):

    db = get_db()

    try:

        cursor = db.execute("""
            UPDATE sponsor_messages
            SET status = 'Read'
            WHERE id = ?
        """, (record_id,))

        db.commit()

        if cursor.rowcount == 0:

            return jsonify({
                "success": False,
                "error": "Message not found"
            }), 404

    finally:
        db.close()

    return jsonify({
        "success": True
    })


# =========================================================
# DELETE GALLERY IMAGE
# =========================================================

@app.route(
    "/admin/gallery/delete/<int:image_id>",
    methods=["POST"]
)
@admin_required
def delete_gallery_image(image_id):

    db = get_db()

    try:

        image = db.execute("""
            SELECT image
            FROM gallery
            WHERE id = ?
        """, (image_id,)).fetchone()

        if image:

            db.execute("""
                DELETE FROM gallery
                WHERE id = ?
            """, (image_id,))

            db.commit()

    finally:
        db.close()

    if not image:

        flash("Image not found.", "error")
        return redirect(url_for("gallery"))

    image_path = os.path.join(
        UPLOAD_FOLDER,
        image["image"]
    )

    if os.path.isfile(image_path):
        os.remove(image_path)

    flash(
        "Gallery image deleted.",
        "success"
    )

    return redirect(url_for("gallery"))


# =========================================================
# ERROR HANDLERS
# =========================================================

@app.errorhandler(404)
def page_not_found(error):

    if request.path.startswith("/api/"):

        return jsonify({
            "success": False,
            "message": "API endpoint not found."
        }), 404

    return """
        <h1>404 - Page Not Found</h1>
        <p>The page you requested does not exist.</p>
        <a href="/">Return Home</a>
    """, 404


@app.errorhandler(413)
def file_too_large(error):

    if request.path.startswith("/api/"):

        return jsonify({
            "success": False,
            "message": "The uploaded file is too large. Maximum size is 10 MB."
        }), 413

    flash(
        "The uploaded file is too large. Maximum size is 10 MB.",
        "error"
    )

    return redirect(
        request.referrer or url_for("index")
    )


@app.errorhandler(500)
def internal_server_error(error):

    app.logger.exception(
        "Internal server error."
    )

    if request.path.startswith("/api/"):

        return jsonify({
            "success": False,
            "message": "An internal server error occurred."
        }), 500

    return """
        <h1>500 - Server Error</h1>
        <p>Something went wrong. Please try again later.</p>
        <a href="/">Return Home</a>
    """, 500


# =========================================================
# RUN APPLICATION
# =========================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=(os.environ.get("FLASK_DEBUG", "false").lower() == "true" and not IS_PRODUCTION)
    )
