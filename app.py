
import os
import re
import sqlite3
import urllib.request
import urllib.error
import mimetypes
from urllib.parse import quote, urlparse
import secrets
import json
from functools import wraps
from datetime import datetime
from html import escape as html_escape

from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify, abort, render_template_string
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

# Supabase PostgreSQL connection. Keep SQLite available for local development
# only when DATABASE_URL is not configured.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
SUPABASE_STORAGE_BUCKET = os.environ.get("SUPABASE_STORAGE_BUCKET", "innovation-uploads").strip()

UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 45 * 1024 * 1024
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "innovator")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "extravaganza")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "innovator@local")


class HybridRow(dict):
    """A row supporting both row['column'] and row[0] access."""
    def __init__(self, columns, values):
        super().__init__(zip(columns, values))
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


def _hybrid_row_factory(cursor):
    # Psycopg calls the factory before executing the query, so resolve column
    # names lazily when each row is actually constructed.
    return lambda values: HybridRow(
        [column.name for column in cursor.description],
        values
    )


class PostgreSQLCursor:
    def __init__(self, cursor):
        self.raw = cursor
        self._lastrowid = None

    @property
    def lastrowid(self):
        return self._lastrowid

    @property
    def rowcount(self):
        return self.raw.rowcount

    def execute(self, sql, params=None):
        statement = sql.strip()
        # SQLite compatibility used by the existing routes.
        ignore_conflicts = bool(re.search(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", statement, flags=re.I))
        statement = statement.replace("?", "%s")
        statement = re.sub(r"\s+COLLATE\s+NOCASE\b", "", statement, flags=re.I)
        statement = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
                           "BIGSERIAL PRIMARY KEY", statement, flags=re.I)
        statement = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b",
                           "INSERT INTO", statement, flags=re.I)
        if statement.upper().startswith("PRAGMA FOREIGN_KEYS"):
            return self

        is_insert = statement.upper().startswith("INSERT ")
        if is_insert and ignore_conflicts and "ON CONFLICT" not in statement.upper():
            statement += " ON CONFLICT DO NOTHING"
        # Capture generated ids for the handful of routes that use cursor.lastrowid.
        if is_insert and not re.search(r"\bRETURNING\b", statement, flags=re.I):
            statement += " RETURNING id"

        self.raw.execute(statement, params or ())
        self._lastrowid = None
        if is_insert and re.search(r"\bRETURNING\s+id\b", statement, flags=re.I):
            returned = self.raw.fetchone()
            if returned:
                self._lastrowid = returned["id"] if isinstance(returned, dict) else returned[0]
        return self

    def fetchone(self):
        return self.raw.fetchone()

    def fetchall(self):
        return self.raw.fetchall()


class PostgreSQLConnection:
    def __init__(self, connection):
        self.raw = connection

    def execute(self, sql, params=None):
        cursor = self.raw.cursor()
        wrapped = PostgreSQLCursor(cursor)
        return wrapped.execute(sql, params)

    def cursor(self):
        return PostgreSQLCursor(self.raw.cursor())

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


def get_db():
    if DATABASE_URL:
        try:
            import psycopg
            from psycopg.rows import tuple_row
        except ImportError as exc:
            raise RuntimeError(
                "Install psycopg[binary] to use Supabase PostgreSQL."
            ) from exc
        connection = psycopg.connect(
            DATABASE_URL,
            row_factory=_hybrid_row_factory,
            connect_timeout=15
        )
        return PostgreSQLConnection(connection)

    # Local-only fallback. On Render, configure DATABASE_URL to use Supabase.
    connection = sqlite3.connect(os.path.join(BASE_DIR, "innovation_hub.db"))
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
    if DATABASE_URL:
        return {
            row["column_name"]
            for row in cursor.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = ?
            """, (table_name,)).fetchall()
        }
    return {
        row["name"]
        for row in cursor.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def migrate_columns(cursor, table_name, migrations):
    existing_columns = get_table_columns(cursor, table_name)
    for column_name, definition in migrations.items():
        if column_name not in existing_columns:
            # SQLite-compatible definitions are valid PostgreSQL column types.
            cursor.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
            )
            existing_columns.add(column_name)




def first_innovation_image(row):
    legacy = row["image"] if "image" in row.keys() else ""
    if legacy:
        return legacy
    try:
        raw = row["images"] if "images" in row.keys() else ""
        parsed = json.loads(raw) if isinstance(raw, str) and raw else raw
        return parsed[0] if isinstance(parsed, list) and parsed else ""
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""


def storage_is_configured():
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and SUPABASE_STORAGE_BUCKET)


def storage_public_url(object_path):
    encoded_path = quote(str(object_path).lstrip("/"), safe="/")
    return (
        f"{SUPABASE_URL}/storage/v1/object/public/"
        f"{quote(SUPABASE_STORAGE_BUCKET, safe='')}/{encoded_path}"
    )


def upload_to_supabase(file_storage, object_path):
    if not storage_is_configured():
        raise RuntimeError(
            "Supabase Storage is not configured. Set SUPABASE_URL, "
            "SUPABASE_SERVICE_ROLE_KEY and SUPABASE_STORAGE_BUCKET."
        )
    data = file_storage.read()
    if not data:
        raise ValueError("The uploaded file is empty.")
    content_type = (
        getattr(file_storage, "mimetype", None)
        or mimetypes.guess_type(file_storage.filename or "")[0]
        or "application/octet-stream"
    )
    encoded_path = quote(object_path.lstrip("/"), safe="/")
    bucket = quote(SUPABASE_STORAGE_BUCKET, safe="")
    url = f"{SUPABASE_URL}/storage/v1/object/{bucket}/{encoded_path}"
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Content-Type": content_type,
            "x-upsert": "true"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase Storage upload failed: {detail}") from exc
    return storage_public_url(object_path)


def delete_from_supabase(public_url_or_path):
    if not storage_is_configured() or not public_url_or_path:
        return
    value = str(public_url_or_path)
    marker = f"/storage/v1/object/public/{SUPABASE_STORAGE_BUCKET}/"
    if marker in value:
        object_path = value.split(marker, 1)[1]
    else:
        object_path = value.lstrip("/")
    object_path = object_path.split("?", 1)[0]
    if not object_path:
        return
    bucket = quote(SUPABASE_STORAGE_BUCKET, safe="")
    encoded_path = quote(object_path, safe="/")
    req = urllib.request.Request(
        f"{SUPABASE_URL}/storage/v1/object/{bucket}/{encoded_path}",
        method="DELETE",
        headers={
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "apikey": SUPABASE_SERVICE_ROLE_KEY
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        # A missing object should not prevent deleting its database record.
        if exc.code != 404:
            raise


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
            WHERE is_read = FALSE
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

        # Innovator accounts and project ownership (safe for existing databases)
        migrate_columns(cursor, "innovators", {
            "password_hash": "TEXT",
            "account_status": "TEXT NOT NULL DEFAULT 'Active'"
        })
        migrate_columns(cursor, "innovations", {
            "innovator_id": "INTEGER"
        })
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_innovations_innovator_id ON innovations(innovator_id)")

        # INNOVATION LIKES
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS innovation_likes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                innovation_id INTEGER NOT NULL,
                visitor_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(innovation_id, visitor_id),
                FOREIGN KEY (innovation_id)
                    REFERENCES innovations(id) ON DELETE CASCADE
            )
        """)

        # INNOVATION COMMENTS
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS innovation_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                innovation_id INTEGER NOT NULL,
                name TEXT NOT NULL DEFAULT 'Visitor',
                comment TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (innovation_id)
                    REFERENCES innovations(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_innovation_likes_innovation
            ON innovation_likes(innovation_id)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_innovation_comments_innovation
            ON innovation_comments(innovation_id)
        """)

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
        SET is_read = FALSE
        WHERE is_read IS NULL
       """) 

        # PostgreSQL timestamp columns cannot be compared with an empty
        # string. Checking NULL works with both PostgreSQL and SQLite.
        cursor.execute("""
            UPDATE notifications
            SET created_at = ?
            WHERE created_at IS NULL
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

        try:
            image_url = upload_to_supabase(
                image_file,
                f"gallery/{unique_filename}"
            )
        except Exception:
            app.logger.exception("Gallery image upload to Supabase failed.")
            flash("Image upload failed. Please check Supabase Storage settings.", "error")
            return redirect(url_for("gallery"))

        db = get_db()

        try:

            db.execute("""
                INSERT INTO gallery
                (title, description, image, date)
                VALUES (?, ?, ?, ?)
            """, (
                title,
                description,
                image_url,
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
            request.headers.get("X-Requested-With", "").lower() == "xmlhttprequest"
            or "application/json" in request.headers.get("Accept", "").lower()
            or request.headers.get("Sec-Fetch-Mode") == "cors"
        )

        # Every new project must belong to the signed-in innovator account.
        if not session.get("innovator_id"):
            message = "Please log in as an innovator before submitting a project."
            if is_ajax:
                return jsonify({"success": False, "message": message}), 401
            flash(message, "error")
            return redirect(url_for("innovator_login"))

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

        saved_images = []
        db = get_db()

        try:

            # SAVE IMAGE FILES
            for image in image_files:

                original_name = secure_filename(image.filename)
                if "." not in original_name:
                    raise ValueError("Invalid uploaded image filename.")

                extension = original_name.rsplit(".", 1)[1].lower()

                unique_filename = (
                    secrets.token_hex(16) + "." + extension
                )

                image_url = upload_to_supabase(
                    image,
                    f"innovations/{unique_filename}"
                )
                saved_images.append(image_url)

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
                    images,
                    innovator_id
                )
                VALUES (?, ?, ?, ?, ?, ?, 'Pending', ?, ?, ?, ?, ?, ?, ?)
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
                images_json,
                session.get("innovator_id")
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

            for image_url in saved_images:
                try:
                    delete_from_supabase(image_url)
                except Exception:
                    app.logger.exception("Could not clean up failed innovation image upload.")

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
                "status": "Pending",
                "message_type": "success"
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

    except Exception:
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


@app.route("/innovator_register", methods=["GET", "POST"])
def innovator_register():
    if request.method == "GET":
        return redirect(url_for("explore") + "#register")

    full_name = form_value("full_name", "name")
    email = form_value("email").lower()
    phone = form_value("phone")
    county = form_value("county")
    institution = form_value("institution")
    field = form_value("field", "area_of_expertise")
    innovation_name = form_value("innovation_name", "innovation")
    description = form_value("description")
    stage = form_value("stage")
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not all([full_name, email, phone, field, password, confirm_password]):
        flash("Please complete all required fields, including your password.", "error")
        return redirect(url_for("explore") + "#register")
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        flash("Please enter a valid email address.", "error")
        return redirect(url_for("explore") + "#register")
    if len(password) < 8:
        flash("Your password must contain at least 8 characters.", "error")
        return redirect(url_for("explore") + "#register")
    if password != confirm_password:
        flash("The passwords do not match.", "error")
        return redirect(url_for("explore") + "#register")

    db = get_db()
    try:
        submission_date = current_time()
        password_hash = generate_password_hash(password)
        cursor = db.execute("""
            INSERT INTO innovators (
                full_name, email, phone, county, institution, field,
                innovation_name, description, stage, status, registration_date,
                password_hash, account_status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pending', ?, ?, 'Active')
        """, (
            full_name, email, phone, county, institution, field,
            innovation_name, description, stage, submission_date, password_hash
        ))
        innovator_id = cursor.lastrowid
        db.execute("""
            INSERT INTO notifications (title, message, category, url, is_read, created_at)
            VALUES (?, ?, ?, ?, 0, ?)
        """, (
            "New Innovator Registration",
            f"{full_name} has created an innovator account.",
            "innovator", "/admin_dashboard", submission_date
        ))
        db.commit()
        session.clear()
        session["innovator_id"] = innovator_id
        session["innovator_name"] = full_name
        session["innovator_email"] = email
        flash("Your innovator account has been created. Welcome to the Hub!", "success")
        return redirect(url_for("innovator_dashboard"))
    except Exception:
        db.rollback()
        flash("This email address already has an account. Please log in instead.", "error")
    except Exception:
        db.rollback()
        app.logger.exception("Innovator registration failed.")
        flash("Unable to create your account. Please try again.", "error")
    finally:
        db.close()
    return redirect(url_for("explore") + "#register")


@app.route("/innovator_login", methods=["GET", "POST"])
def innovator_login():
    if session.get("innovator_id"):
        return redirect(url_for("innovator_dashboard"))
    if request.method == "POST":
        email = form_value("email").lower()
        password = request.form.get("password", "")
        db = get_db()
        try:
            innovator = db.execute("""
                SELECT * FROM innovators WHERE email = ? COLLATE NOCASE LIMIT 1
            """, (email,)).fetchone()
            if (not innovator or not innovator["password_hash"]
                    or not check_password_hash(innovator["password_hash"], password)):
                flash("Invalid email or password. Please try again.", "error")
            elif innovator["account_status"] != "Active":
                flash("Your account is currently disabled. Contact the Hub administrator.", "error")
            else:
                session.clear()
                session["innovator_id"] = innovator["id"]
                session["innovator_name"] = innovator["full_name"]
                session["innovator_email"] = innovator["email"]
                flash("Welcome back, " + innovator["full_name"] + "!", "success")
                return redirect(url_for("innovator_dashboard"))
        finally:
            db.close()
    return render_template("innovator_login.html")


@app.route("/innovator_logout")
def innovator_logout():
    session.pop("innovator_id", None)
    session.pop("innovator_name", None)
    session.pop("innovator_email", None)
    flash("You have been logged out.", "success")
    return redirect(url_for("explore"))


@app.route("/innovator_dashboard")
def innovator_dashboard():
    innovator_id = session.get("innovator_id")
    if not innovator_id:
        flash("Please log in to access your innovator account.", "error")
        return redirect(url_for("innovator_login"))
    db = get_db()
    try:
        innovator = db.execute("SELECT * FROM innovators WHERE id = ?", (innovator_id,)).fetchone()
        if not innovator:
            session.clear()
            flash("Your innovator account could not be found. Please log in again.", "error")
            return redirect(url_for("innovator_login"))
        projects = db.execute("""
            SELECT * FROM innovations WHERE innovator_id = ? ORDER BY id DESC
        """, (innovator_id,)).fetchall()
        total_projects = len(projects)
        pending_projects = sum(1 for project in projects if (project["status"] or "").lower() == "pending")
        approved_projects = sum(1 for project in projects if (project["status"] or "").lower() in ("approved", "active"))
        return render_template(
            "innovator_dashboard.html", innovator=innovator, projects=projects,
            total_projects=total_projects, pending_projects=pending_projects,
            approved_projects=approved_projects
        )
    finally:
        db.close()


# =========================================================
# PUBLIC INNOVATIONS GALLERY API
# =========================================================

@app.route("/api/innovations", methods=["GET"])
def public_innovations_api():
    """Return approved/active innovations for the public gallery."""

    visitor_id = session.get("innovation_visitor_id")
    if not visitor_id:
        visitor_id = secrets.token_urlsafe(32)
        session["innovation_visitor_id"] = visitor_id

    db = get_db()

    try:
        rows = db.execute("""
            SELECT *
            FROM innovations
            WHERE LOWER(TRIM(COALESCE(status, ''))) IN ('active', 'approved')
            ORDER BY id DESC
        """).fetchall()

        projects = []

        for row in rows:
            project = dict(row)
            raw_images = project.get("images") or "[]"

            try:
                image_list = json.loads(raw_images) if isinstance(raw_images, str) else raw_images
            except (TypeError, ValueError, json.JSONDecodeError):
                image_list = []

            if not isinstance(image_list, list):
                image_list = []

            image_list = [str(item) for item in image_list if item]
            legacy_image = project.get("image") or ""

            if not image_list and legacy_image:
                image_list = [legacy_image]

            project["images"] = image_list
            project["image"] = legacy_image or (image_list[0] if image_list else "")
            project["title"] = project.get("name") or "Untitled innovation"
            project["uploader"] = project.get("uploader") or project.get("student_name") or ""
            project["student_name"] = project["uploader"]
            project["date"] = project.get("date") or project.get("created_at") or ""
            project["category"] = project.get("category") or "General"
            project["department"] = project.get("department") or ""
            project["description"] = project.get("description") or ""
            project["problem"] = project.get("problem") or ""
            project["solution"] = project.get("solution") or ""
            project["email"] = project.get("email") or ""
            project["phone"] = project.get("phone") or ""
            project["whatsapp"] = project.get("whatsapp") or ""

            project["likes_count"] = db.execute("""
                SELECT COUNT(*) FROM innovation_likes WHERE innovation_id = ?
            """, (project["id"],)).fetchone()[0]

            project["comments_count"] = db.execute("""
                SELECT COUNT(*) FROM innovation_comments WHERE innovation_id = ?
            """, (project["id"],)).fetchone()[0]

            project["liked"] = bool(db.execute("""
                SELECT 1 FROM innovation_likes
                WHERE innovation_id = ? AND visitor_id = ?
                LIMIT 1
            """, (project["id"], visitor_id)).fetchone())

            projects.append(project)

        return jsonify({
            "success": True,
            "innovations": projects,
            "count": len(projects)
        }), 200

    except Exception:
        app.logger.exception("Public innovations gallery API failed.")
        return jsonify({
            "success": False,
            "message": "Unable to load the innovations gallery.",
            "innovations": []
        }), 500

    finally:
        db.close()


# =========================================================
# LIKE / UNLIKE AN INNOVATION
# =========================================================

@app.route("/api/innovations/<int:innovation_id>/like", methods=["POST"])
def like_innovation(innovation_id):
    visitor_id = session.get("innovation_visitor_id")
    if not visitor_id:
        visitor_id = secrets.token_urlsafe(32)
        session["innovation_visitor_id"] = visitor_id

    db = get_db()
    try:
        innovation = db.execute("""
            SELECT id FROM innovations
            WHERE id = ?
              AND LOWER(TRIM(COALESCE(status, ''))) IN ('active', 'approved')
        """, (innovation_id,)).fetchone()

        if not innovation:
            return jsonify({"success": False, "message": "Innovation not found."}), 404

        existing = db.execute("""
            SELECT id FROM innovation_likes
            WHERE innovation_id = ? AND visitor_id = ?
        """, (innovation_id, visitor_id)).fetchone()

        if existing:
            db.execute("""
                DELETE FROM innovation_likes
                WHERE innovation_id = ? AND visitor_id = ?
            """, (innovation_id, visitor_id))
            liked = False
        else:
            db.execute("""
                INSERT OR IGNORE INTO innovation_likes
                (innovation_id, visitor_id, created_at)
                VALUES (?, ?, ?)
            """, (innovation_id, visitor_id, current_time()))
            liked = True

        db.commit()
        likes_count = db.execute("""
            SELECT COUNT(*) FROM innovation_likes WHERE innovation_id = ?
        """, (innovation_id,)).fetchone()[0]

        return jsonify({"success": True, "liked": liked, "likes_count": likes_count}), 200

    except Exception:
        db.rollback()
        app.logger.exception("Innovation like operation failed.")
        return jsonify({"success": False, "message": "Unable to update the like. Please try again."}), 500
    finally:
        db.close()


# =========================================================
# GET / POST INNOVATION COMMENTS
# =========================================================

@app.route("/api/innovations/<int:innovation_id>/comments", methods=["GET", "POST"])
def innovation_comments(innovation_id):
    db = get_db()
    try:
        innovation = db.execute("""
            SELECT id FROM innovations
            WHERE id = ?
              AND LOWER(TRIM(COALESCE(status, ''))) IN ('active', 'approved')
        """, (innovation_id,)).fetchone()

        if not innovation:
            return jsonify({"success": False, "message": "Innovation not found."}), 404

        if request.method == "POST":
            data = request.get_json(silent=True) or request.form
            name = str(data.get("name") or data.get("commenter") or "Visitor").strip()
            comment = str(data.get("comment") or data.get("content") or "").strip()

            if not name:
                name = "Visitor"
            if len(name) > 80:
                return jsonify({"success": False, "message": "Name must be 80 characters or fewer."}), 400
            if not comment:
                return jsonify({"success": False, "message": "Please write a comment before submitting."}), 400
            if len(comment) > 1500:
                return jsonify({"success": False, "message": "Comments cannot exceed 1500 characters."}), 400

            db.execute("""
                INSERT INTO innovation_comments
                (innovation_id, name, comment, created_at)
                VALUES (?, ?, ?, ?)
            """, (innovation_id, name, comment, current_time()))
            db.commit()

        rows = db.execute("""
            SELECT id, name, comment, created_at
            FROM innovation_comments
            WHERE innovation_id = ?
            ORDER BY id DESC
        """, (innovation_id,)).fetchall()

        return jsonify({
            "success": True,
            "comments": [dict(row) for row in rows],
            "comments_count": len(rows)
        }), 200

    except Exception:
        db.rollback()
        app.logger.exception("Innovation comments request failed.")
        return jsonify({"success": False, "message": "Unable to process comments. Please try again."}), 500
    finally:
        db.close()


# =========================================================
# PUBLIC INNOVATION DETAIL / SHARE LINK
# =========================================================

@app.route("/innovation/<int:innovation_id>")
def innovation_detail(innovation_id):
    db = get_db()
    try:
        row = db.execute("""
            SELECT * FROM innovations
            WHERE id = ?
              AND LOWER(TRIM(COALESCE(status, ''))) IN ('active', 'approved')
        """, (innovation_id,)).fetchone()

        if not row:
            abort(404)

        project = dict(row)
        try:
            images = json.loads(project.get("images") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            images = []
        if not isinstance(images, list):
            images = []
        if not images and project.get("image"):
            images = [project["image"]]

        title = project.get("name") or "Innovation"
        description = project.get("description") or "Explore this innovation from the MNP Innovation & Skills Hub."
        image_url = url_for("static", filename="uploads/" + str(images[0]), _external=True) if images else ""
        share_url = url_for("innovation_detail", innovation_id=innovation_id, _external=True)

        safe_title = html_escape(title)
        safe_description = html_escape(description[:300])
        safe_image = html_escape(image_url, quote=True)
        safe_url = html_escape(share_url, quote=True)
        safe_name = html_escape(project.get("uploader") or "MNP Innovation & Skills Hub")
        safe_department = html_escape(project.get("department") or "")
        safe_category = html_escape(project.get("category") or "")
        safe_date = html_escape(project.get("date") or "")
        safe_problem = html_escape(project.get("problem") or "")
        safe_solution = html_escape(project.get("solution") or "")

        image_markup = (
            f'<img src="{safe_image}" alt="{safe_title}" style="max-width:100%;max-height:560px;object-fit:contain;border-radius:14px">'
            if safe_image else ""
        )

        return render_template_string("""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} | MNP Innovation &amp; Skills Hub</title>
<meta name="description" content="{{ description }}">
<meta property="og:type" content="article"><meta property="og:title" content="{{ title }}">
<meta property="og:description" content="{{ description }}"><meta property="og:url" content="{{ share_url }}">
{% if image_url %}<meta property="og:image" content="{{ image_url }}">{% endif %}
<meta name="twitter:card" content="summary_large_image"><meta name="twitter:title" content="{{ title }}"><meta name="twitter:description" content="{{ description }}">
{% if image_url %}<meta name="twitter:image" content="{{ image_url }}">{% endif %}
<style>body{margin:0;background:#f4f7fb;color:#172033;font-family:Arial,sans-serif;line-height:1.65}.wrap{max-width:900px;margin:40px auto;padding:28px;background:#fff;border-radius:18px;box-shadow:0 8px 30px #17203312}.brand{color:#8b5e3c;font-weight:800}.meta{color:#64748b;font-size:14px}.hero{margin:22px 0;text-align:center;background:#f1f5f9;padding:15px;border-radius:14px}.tag{display:inline-block;background:#eaf3ff;color:#176fc1;border-radius:30px;padding:5px 12px;margin:4px;font-size:13px}h1{line-height:1.2}h2{margin-top:26px;color:#8b5e3c}.btn{display:inline-block;background:#176fc1;color:#fff;text-decoration:none;padding:11px 18px;border-radius:8px;margin-top:20px}@media(max-width:600px){.wrap{margin:12px;padding:18px}}</style></head>
<body><main class="wrap"><div class="brand">Meru National Polytechnic · Innovation &amp; Skills Hub</div>
<h1>{{ title }}</h1><div class="meta">Submitted by {{ uploader }}{% if department %} · {{ department }}{% endif %}{% if date %} · {{ date }}{% endif %}</div>
{% if category %}<p><span class="tag">{{ category }}</span></p>{% endif %}<div class="hero">{{ image_markup|safe }}</div>
<h2>About this innovation</h2><p>{{ description }}</p>{% if problem %}<h2>Problem addressed</h2><p>{{ problem }}</p>{% endif %}{% if solution %}<h2>Proposed solution</h2><p>{{ solution }}</p>{% endif %}
<a class="btn" href="{{ gallery_url }}">Explore more innovations</a></main></body></html>""",
            title=title,
            description=description[:300],
            share_url=share_url,
            image_url=image_url,
            uploader=project.get("uploader") or "MNP Innovation & Skills Hub",
            department=project.get("department") or "",
            date=project.get("date") or "",
            category=project.get("category") or "",
            image_markup=image_markup,
            problem=project.get("problem") or "",
            solution=project.get("solution") or "",
            gallery_url=url_for("innovations")
        )
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

    except Exception:

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
            WHERE is_read = FALSE
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
            SET is_read = TRUE
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
            SET is_read = TRUE
            WHERE is_read = FALSE
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
                "image": first_innovation_image(row),
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

    try:
        delete_from_supabase(image["image"])
    except Exception:
        app.logger.exception("Could not delete gallery image from Supabase Storage.")

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
            "message": "The uploaded files are too large. Maximum total request size is 45 MB."
        }), 413

    flash(
        "The uploaded files are too large. Maximum total request size is 45 MB.",
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
