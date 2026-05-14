import os
import re
import json
import hmac
import hashlib
import requests
from collections import defaultdict, deque
from pathlib import Path
from datetime import datetime, timedelta
from functools import wraps

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash
from openai import OpenAI
from threading import Thread, Lock
import time

PHONE_RE = re.compile(r'(?:\+?976[\s-]?)?[89]\d{7}')

# Initialize Flask app
app = Flask(__name__)
_secret_key = os.environ.get('SECRET_KEY')
if not _secret_key:
    raise RuntimeError("SECRET_KEY environment variable is required")
app.config['SECRET_KEY'] = _secret_key
# Default to local SQLite for dev; override SQLALCHEMY_DATABASE_URI in
# production to point at a Render Persistent Disk path (e.g.
# sqlite:////var/data/magic_bot.db) or a Postgres connection string.
_default_db_uri = 'sqlite:///magic_bot.db'
_db_uri = os.environ.get('SQLALCHEMY_DATABASE_URI', '') or _default_db_uri


def _ensure_sqlite_path_writable(uri):
    """Make sure the parent directory of a sqlite:/// path exists & is writable.

    Returns the original URI if it's usable, otherwise the default URI.
    Skips non-sqlite URIs (e.g. postgresql://). Prevents the common Render
    misconfig of setting SQLALCHEMY_DATABASE_URI before mounting the disk
    from taking the whole service down.
    """
    if not uri.startswith('sqlite:'):
        return uri
    # sqlite:////absolute/path/db -> path is /absolute/path/db
    # sqlite:///relative/path/db  -> path is relative/path/db
    after_scheme = uri[len('sqlite:'):].lstrip('/')
    sqlite_path = '/' + after_scheme if uri.startswith('sqlite:////') else after_scheme
    parent = os.path.dirname(sqlite_path)
    if not parent:
        return uri
    try:
        os.makedirs(parent, exist_ok=True)
        # Verify writable
        probe = os.path.join(parent, '.write_test')
        with open(probe, 'w') as f:
            f.write('ok')
        os.remove(probe)
        return uri
    except OSError as e:
        print(
            f"WARNING: SQLALCHEMY_DATABASE_URI={uri!r} is not usable "
            f"({e!s}). Falling back to {_default_db_uri!r}. "
            "Likely you set the URI before adding a Render Persistent Disk."
        )
        return _default_db_uri


app.config['SQLALCHEMY_DATABASE_URI'] = _ensure_sqlite_path_writable(_db_uri)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize extensions
db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Initialize OpenAI client
client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

# Facebook API credentials
FACEBOOK_PAGE_ID = os.environ.get('FACEBOOK_PAGE_ID', '')
FACEBOOK_ACCESS_TOKEN = os.environ.get('FACEBOOK_ACCESS_TOKEN', '')
if not FACEBOOK_ACCESS_TOKEN:
    raise RuntimeError("FACEBOOK_ACCESS_TOKEN environment variable is required")
# App Secret is used to verify X-Hub-Signature-256 on incoming webhooks.
# Optional — if unset we log a warning and accept all payloads (dev mode).
FACEBOOK_APP_SECRET = os.environ.get('FACEBOOK_APP_SECRET', '')
if not FACEBOOK_APP_SECRET:
    print(
        "WARNING: FACEBOOK_APP_SECRET not set — accepting all webhook payloads "
        "unverified. Live-mode deploys MUST set this or Facebook traffic can be "
        "forged."
    )
GOOGLE_FORM_URL = os.environ.get('GOOGLE_FORM_URL', '')

# Training content: env override wins so a single codebase can serve multiple
# businesses without forking pasted_content.txt. Fallback chain:
#   1. TRAINING_CONTENT env var (paste a multi-line blob into Render Environment)
#   2. ./pasted_content.txt next to the script
#   3. A tiny default string so the prompt never breaks if both are missing.
TRAINING_PATH = Path(__file__).parent / 'pasted_content.txt'
_env_training = os.environ.get('TRAINING_CONTENT', '').strip()
if _env_training:
    TRAINING_CONTENT = _env_training
else:
    try:
        TRAINING_CONTENT = TRAINING_PATH.read_text(encoding='utf-8')
    except FileNotFoundError:
        TRAINING_CONTENT = "Манай сургалтын төв нь 2007 оноос хойш үйл ажиллагаа явуулж байгаа."

# Bot persona: the opening "you are..." line of the system prompt. Override
# per-deployment so the bot can represent a different business without code
# changes. Defaults to the Magic Financial Group persona.
_DEFAULT_PERSONA = (
    "Та Мэжик Санхүүгийн Группын Facebook чат туслах. Сэтгэл судлалын "
    "ойлголттой, маркетингийн ур чадвартай, нягтлан бодох сургалтын зөвлөх. "
    "Үргэлж монгол хэлээр, амьд хүн шиг ойлгомжтой, дотночоор хариулна. "
    "Англи үг бичихгүй (тусгай нэр, программын нэрийг эс тооцох)."
)
BOT_PERSONA = os.environ.get('BOT_PERSONA', '').strip() or _DEFAULT_PERSONA

# ===================== DATABASE MODELS =====================

class User(UserMixin, db.Model):
    """Admin user model"""
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    role = db.Column(db.String(20), default='admin')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class FacebookUser(db.Model):
    """Facebook user/conversation participant"""
    id = db.Column(db.Integer, primary_key=True)
    facebook_id = db.Column(db.String(100), unique=True, nullable=False)
    name = db.Column(db.String(255))
    phone = db.Column(db.String(20))
    is_lead = db.Column(db.Boolean, default=False)
    lead_status = db.Column(db.String(50), default='new')  # new, contacted, converted
    # Funnel state: 'curious' -> 'exploring_courses' -> 'pricing' -> 'ready'
    funnel_stage = db.Column(db.String(30), default='curious')
    # Last time we sent a proactive nudge (kept null until first nudge fires)
    last_nudge_at = db.Column(db.DateTime)
    # When set and > now(), the bot will not auto-reply to this user — a human
    # has been pinged and should take over. Cleared automatically once expired.
    bot_muted_until = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Message(db.Model):
    """Chat message history"""
    id = db.Column(db.Integer, primary_key=True)
    facebook_user_id = db.Column(db.Integer, db.ForeignKey('facebook_user.id'), nullable=False)
    sender = db.Column(db.String(20), nullable=False)  # 'user' or 'bot'
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    facebook_user = db.relationship('FacebookUser', backref=db.backref('messages', lazy=True))

class Course(db.Model):
    """Training course"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    course_type = db.Column(db.String(100), nullable=False)  # '100% Online', 'Hybrid', etc.
    start_date = db.Column(db.DateTime, nullable=False)
    time = db.Column(db.String(50), nullable=False)  # e.g., "10:00-13:00"
    price = db.Column(db.Float, nullable=False)
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class FAQ(db.Model):
    """Frequently asked questions"""
    id = db.Column(db.Integer, primary_key=True)
    question = db.Column(db.Text, nullable=False)
    answer = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class AdminIssue(db.Model):
    """Issues assigned to admin (unresolved by bot)"""
    id = db.Column(db.Integer, primary_key=True)
    facebook_user_id = db.Column(db.Integer, db.ForeignKey('facebook_user.id'), nullable=False)
    issue_type = db.Column(db.String(50), nullable=False)  # 'unresolved_query', 'complaint', 'suggestion'
    content = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default='open')  # 'open', 'in_progress', 'resolved'
    assigned_to = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    resolved_at = db.Column(db.DateTime)
    facebook_user = db.relationship('FacebookUser', backref=db.backref('admin_issues', lazy=True))
    assigned_user = db.relationship('User', backref=db.backref('assigned_issues', lazy=True))

class PagePost(db.Model):
    """Facebook Page posts (for auto-commenting)"""
    id = db.Column(db.Integer, primary_key=True)
    facebook_post_id = db.Column(db.String(100), unique=True, nullable=False)
    content = db.Column(db.Text)
    post_type = db.Column(db.String(50))  # 'training', 'celebration', 'other'
    comment_posted = db.Column(db.Boolean, default=False)
    comment_text = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class GeneralSetting(db.Model):
    """General training center settings"""
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class TeamMember(db.Model):
    """Staff/teacher directory the bot can reference when clients ask
    who teaches what or who handles a given topic."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(120))         # e.g. "Багш", "Хариуцагч", "Зөвлөх"
    specialty = db.Column(db.String(255))    # e.g. "Татварын асуудал, Magic Finance"
    bio = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class TrainingSnippet(db.Model):
    """Additive, per-topic training notes. Managers add a new row when a
    real chat surfaces something the bot got wrong, instead of editing one
    giant blob and risking overwrite of someone else's work."""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(100))           # free-form tag
    priority = db.Column(db.String(20), default='normal')  # 'high' | 'normal'
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class BusinessLine(db.Model):
    """Other services the Facebook page promotes beyond training (e.g.
    Magic Finance app, consulting, accounting outsourcing). Each line has
    an action telling the bot whether to answer briefly or refer to staff."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text)
    action = db.Column(db.String(20), default='refer')  # 'answer' | 'refer'
    contact_info = db.Column(db.String(255))            # e.g. phone, email, dept
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

# ===================== LOGIN MANAGER =====================

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

ADMIN_ROLES = ('admin', 'super_admin')


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in ADMIN_ROLES:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def super_admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in ADMIN_ROLES:
            return redirect(url_for('login'))
        if current_user.role != 'super_admin':
            flash('Энэ үйлдэлд супер админ эрх шаардлагатай.', 'error')
            return redirect(url_for('admins'))
        return f(*args, **kwargs)
    return decorated_function

# ===================== WEBHOOK SECURITY + RATE LIMIT =====================

def verify_facebook_signature(raw_body, header_value):
    """Validate X-Hub-Signature-256 against HMAC-SHA256(raw_body, app_secret).

    If FACEBOOK_APP_SECRET is unset we skip verification with a warning so dev
    deploys don't break before the secret is wired. On Live Mode you MUST set
    the secret — Facebook will sign every payload and an unsigned request
    means an attacker is forging Messenger traffic.
    """
    if not FACEBOOK_APP_SECRET:
        return True
    if not header_value or not header_value.startswith('sha256='):
        return False
    expected = hmac.new(
        FACEBOOK_APP_SECRET.encode('utf-8'),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    received = header_value.split('=', 1)[1].strip()
    return hmac.compare_digest(expected, received)


# Sliding-window rate limit: at most RATE_LIMIT_MAX messages per RATE_LIMIT_WINDOW
# from a single Facebook sender_id. Defends OpenAI cost from one chatty/spammy
# user. State is in-memory per worker — fine for a single Render instance; if
# you scale horizontally swap this for Redis.
RATE_LIMIT_MAX = int(os.environ.get('RATE_LIMIT_MAX', '5'))
RATE_LIMIT_WINDOW = timedelta(seconds=int(os.environ.get('RATE_LIMIT_WINDOW_SECONDS', '60')))
_rate_state = defaultdict(deque)
_rate_state_lock = Lock()


def check_rate_limit(sender_id):
    """Return True if sender is within the limit; record the hit. False if over."""
    now = datetime.utcnow()
    cutoff = now - RATE_LIMIT_WINDOW
    with _rate_state_lock:
        dq = _rate_state[sender_id]
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= RATE_LIMIT_MAX:
            return False
        dq.append(now)
        # Cheap periodic GC so dormant senders don't leak memory.
        if len(_rate_state) > 5000:
            for sid in [k for k, v in _rate_state.items() if not v]:
                del _rate_state[sid]
        return True


RATE_LIMIT_REPLY = (
    "Та маш олон мессеж бичиж байна. 1 минутын дараа дахин оролдоорой. 🙏"
)


# ===================== FACEBOOK API HELPERS =====================

def send_facebook_message(recipient_id, message_text):
    """Send a message via Facebook Messenger API"""
    url = f"https://graph.facebook.com/v18.0/me/messages"
    headers = {"Content-Type": "application/json"}
    params = {"access_token": FACEBOOK_ACCESS_TOKEN}
    data = {
        "recipient": {"id": recipient_id},
        "message": {"text": message_text}
    }
    try:
        response = requests.post(url, json=data, headers=headers, params=params)
        if response.status_code != 200:
            # Surface the FB error body so token/permission issues stop being
            # silent. Common codes: (#190) bad token, (#10)/(#200) missing
            # pages_messaging, (#100) bad recipient_id, (#551) outside 24h.
            body = response.text[:500] if response.text else '<empty>'
            print(
                f"Send API FAILED recipient={recipient_id} "
                f"status={response.status_code} body={body}"
            )
            return False
        return True
    except Exception as e:
        print(f"Error sending message to recipient={recipient_id}: {e}")
        return False

def get_facebook_user_info(facebook_id):
    """Get user info from Facebook's User Profile API.

    Returns the JSON dict on success, {} on any failure. Failures are logged
    with status + body preview because the most common cause of empty names
    is the app being in Development mode without Advanced Access on
    pages_messaging — which silently returns 4xx with a 'permissions'
    error. Without the log it looks identical to a successful empty response.

    Note: 'email' usually isn't returned for Messenger PSIDs even on Live mode,
    so only 'name' is reliably useful here.
    """
    url = f"https://graph.facebook.com/v18.0/{facebook_id}"
    params = {"fields": "name,first_name,last_name", "access_token": FACEBOOK_ACCESS_TOKEN}
    try:
        response = requests.get(url, params=params, timeout=5)
        if response.status_code == 200:
            data = response.json() or {}
            if not data.get('name'):
                # 200 but no name field is unusual — log it so we don't
                # invisibly fall back to "Unknown".
                print(
                    f"FB user profile: 200 OK but missing name for psid={facebook_id} "
                    f"body={str(data)[:200]}"
                )
            return data
        print(
            f"FB user profile failed psid={facebook_id} status={response.status_code} "
            f"body={response.text[:300]}"
        )
        return {}
    except Exception as e:
        print(f"FB user profile exception psid={facebook_id}: {e}")
        return {}


def refresh_facebook_user_name(fb_user):
    """Re-fetch the display name for a single FacebookUser and persist if we
    get something useful. Returns the new name (or '' if still unknown).

    Used in two places: (a) on a follow-up message when the existing row's
    name is empty/Unknown, and (b) the /admin/api/backfill-names sweep that
    retries every Unknown user after permissions are fixed.
    """
    info = get_facebook_user_info(fb_user.facebook_id)
    name = (info.get('name') or '').strip()
    if not name:
        return ''
    fb_user.name = name
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Refresh name commit failed for psid={fb_user.facebook_id}: {e}")
        return ''
    return name

def get_recent_messages():
    """Poll for recent messages from Facebook Messenger"""
    url = f"https://graph.facebook.com/v18.0/{FACEBOOK_PAGE_ID}/conversations"
    params = {
        "fields": "id,senders,participants,former_participants,wallpaper,snippet,updated_time,message_count,unread_count,subject,can_reply,former_participants,info,link,name,email,page_name,wallpaper,former_participants",
        "access_token": FACEBOOK_ACCESS_TOKEN
    }
    try:
        response = requests.get(url, params=params)
        if response.status_code == 200:
            return response.json().get('data', [])
        return []
    except Exception as e:
        print(f"Error getting messages: {e}")
        return []

def get_page_posts():
    """Poll for recent posts from Facebook Page"""
    url = f"https://graph.facebook.com/v18.0/{FACEBOOK_PAGE_ID}/feed"
    params = {
        "fields": "id,message,created_time,type,story",
        "limit": 10,
        "access_token": FACEBOOK_ACCESS_TOKEN
    }
    try:
        response = requests.get(url, params=params)
        if response.status_code == 200:
            return response.json().get('data', [])
        return []
    except Exception as e:
        print(f"Error getting posts: {e}")
        return []

def post_comment_on_page(post_id, comment_text):
    """Post a comment on a Facebook Page post"""
    url = f"https://graph.facebook.com/v18.0/{post_id}/comments"
    params = {"access_token": FACEBOOK_ACCESS_TOKEN}
    data = {"message": comment_text}
    try:
        response = requests.post(url, json=data, params=params)
        return response.status_code == 201
    except Exception as e:
        print(f"Error posting comment: {e}")
        return False

# ===================== SCHEMA MIGRATION =====================

def ensure_schema():
    """Idempotent schema migration for columns added after the first deploy.

    SQLite-style ALTER TABLE ADD COLUMN works on Postgres too, so this stays
    portable if the user later switches the SQLALCHEMY_DATABASE_URI.
    """
    from sqlalchemy import inspect as sa_inspect

    inspector = sa_inspect(db.engine)
    if 'facebook_user' not in inspector.get_table_names():
        return  # create_all() will handle a brand-new DB

    existing = {col['name'] for col in inspector.get_columns('facebook_user')}
    additions = []
    if 'funnel_stage' not in existing:
        additions.append("ADD COLUMN funnel_stage VARCHAR(30) DEFAULT 'curious'")
    if 'last_nudge_at' not in existing:
        additions.append("ADD COLUMN last_nudge_at DATETIME")
    if 'bot_muted_until' not in existing:
        additions.append("ADD COLUMN bot_muted_until DATETIME")

    if not additions:
        return

    with db.engine.begin() as conn:
        for clause in additions:
            try:
                conn.exec_driver_sql(f"ALTER TABLE facebook_user {clause}")
                print(f"Schema migration: facebook_user {clause}")
            except Exception as e:
                print(f"Schema migration skipped ({clause}): {e}")


# ===================== SESSION + FUNNEL CLASSIFIERS =====================

# Time thresholds for the four session states. Tunable per business need.
SESSION_ACTIVE_WINDOW = timedelta(hours=2)   # within -> 'active'
SESSION_GAP_WINDOW = timedelta(hours=24)     # active..this -> 'gap'; beyond -> 'returning'


def classify_session(last_msg_at):
    """Return 'new' | 'active' | 'gap' | 'returning' based on time since last_msg_at."""
    if last_msg_at is None:
        return 'new'
    delta = datetime.utcnow() - last_msg_at
    if delta < SESSION_ACTIVE_WINDOW:
        return 'active'
    if delta < SESSION_GAP_WINDOW:
        return 'gap'
    return 'returning'


# Mongolian-keyword funnel detection. Matching is case-insensitive substring,
# so stems catch inflected forms (e.g. 'бүртгүүл' matches 'бүртгүүлмээр').
FUNNEL_KEYWORDS = {
    'ready': [
        'бүртгүүл', 'бүртгэх', 'бүртгээч', 'бүртгүүлье', 'элсэх', 'элсье',
        'утасны дугаар', 'дугаараа', 'холбогд', 'эхэлмээр', 'оролцмоор',
        'сонгомоор', 'хэзээ эхэлдэг', 'хэзээ эхэлж байгаа',
    ],
    'pricing': [
        'үнэ', 'төлбөр', 'хэд', 'хэдэн төгрөг', 'хямдрал', 'хямд',
        'хуваан төлөх', 'хуваан', 'pocketzero', 'хямдра', 'төлөх',
        'хөнгөлөлт', 'дискаунт', 'хямдар',
    ],
    'exploring_courses': [
        'хичээл', 'анги', 'хөтөлбөр', 'сургалт', 'долоо хоног',
        'агуулга', 'юу заадаг', 'юу үздэг', 'танхим', 'онлайн',
        'хосолсон', 'хэлбэр', 'цаг', 'хэдэн цагт', 'хуваарь',
    ],
}

STAGE_RANK = {'curious': 0, 'exploring_courses': 1, 'pricing': 2, 'ready': 3}


def detect_funnel_stage(message_text, current_stage='curious'):
    """Classify the user's intent. Never regresses: only moves forward in the funnel."""
    current = current_stage or 'curious'
    if not message_text:
        return current
    text = message_text.lower()
    for stage in ('ready', 'pricing', 'exploring_courses'):
        if any(kw in text for kw in FUNNEL_KEYWORDS[stage]):
            return stage if STAGE_RANK[stage] > STAGE_RANK.get(current, 0) else current
    return current


def first_name_of(full_name):
    """Pull a clean first name from a Facebook display name, or '' if unknown."""
    if not full_name or full_name.strip().lower() == 'unknown':
        return ''
    return full_name.strip().split()[0]


SESSION_RULES = {
    'new': (
        '0. Энэ бол хэрэглэгчтэй анх удаа ярилцаж байгаа явдал. Богино, '
        'дулаахан мэндчилгээгээр эхлээд (жишээ нь "Сайн байна уу!") шууд '
        'тэдний асуултанд ороорой.'
    ),
    'active': (
        '0. ЭНЭ БОЛ ҮРГЭЛЖИЛСЭН ЯРИА. Хэрэглэгчтэй сүүлийн хэдэн минутын дотор '
        'ярилцаж байсан тул "Сайн байна уу", "Тавтай морил", өөрийгөө дахин '
        'танилцуулах гэх мэтийг БҮҮ оруул. Өмнөх контекстийг санаж буй мэт '
        'товч, ноорог байдлаар шууд асуултын хариулт руу ор.'
    ),
    'gap': (
        '0. Хэрэглэгчтэй өнөөдөр аль хэдийн ярилцаад, цөөн цагийн дараа эргэж '
        'ирлээ. Дахин мэндлэхгүйгээр шууд асуултанд найрсагаар хариул. Хэрэгтэй '
        'үед "Үргэлжлүүлээд тусалъя" гэх мэт богино гүүр хэллэг л ашигла.'
    ),
    'returning': (
        '0. Хэрэглэгч 1+ хоногийн дараа эргэн ирлээ. Богино, дулаахан '
        'мэндчилгээгээр (жишээ нь "Сайн байна уу, эргэж ирсэнд таатай байна!") '
        'эхэлж, өмнө ярьсан зүйлээ зөөлөн санагдуулаад, "Юу тодруулах хэрэгтэй '
        'байна?" гэж асуу.'
    ),
}

FUNNEL_RULES = {
    'curious': (
        'ХЭРЭГЛЭГЧИЙН ҮЕ ШАТ: Сонирхож буй. Сургалтын үндсэн давуу талыг товч '
        'танилцуулаад "Ямар чиглэлийн хичээл сонирхож байна?" гэх мэт нээлттэй '
        'асуулт асуу. Үнэ, бүртгэлийн талаар хэт эрт бүү ярь.'
    ),
    'exploring_courses': (
        'ХЭРЭГЛЭГЧИЙН ҮЕ ШАТ: Сургалт сонгож буй. Тодорхой ангийн агуулга, '
        '4 долоо хоногийн хөтөлбөр, цаг хуваарь, танхим/онлайн хэлбэрийг '
        'тодруулж тайлбарла. Аль анги нь тэдэнд тохиромжтой байж болохыг нь '
        'эелдгээр асуу.'
    ),
    'pricing': (
        'ХЭРЭГЛЭГЧИЙН ҮЕ ШАТ: Үнэ судалж буй. Дөрвөн ангийн үнэ, PocketZero-оор '
        '4-6 хуваан хүүгүй төлөх боломж, Magic Finance программын 6 сарын '
        'үнэгүй ашиглах эрхийг онцолж тайлбарла. Төсөвт нь тохирох '
        'хувилбарыг санал болго.'
    ),
    'ready': (
        'ХЭРЭГЛЭГЧИЙН ҮЕ ШАТ: Бүртгүүлэхэд бэлэн. Богино урамшуулал ("Маш сайн '
        'сонголт!") хийж, БҮРТГЭЛИЙН ЛИНК-ийг хариултдаа оруул. Утасны '
        'дугаараа үлдээх хүсэлтийг найрсагаар нэмж асуу.'
    ),
}


# ===================== LLM HELPERS =====================

def get_setting(key, default=''):
    """Read a value from GeneralSetting. Returns default if missing or blank."""
    row = GeneralSetting.query.filter_by(key=key).first()
    if row and row.value and row.value.strip():
        return row.value
    return default


def get_training_content():
    """Bot's training corpus. Priority: DB row -> env var -> file -> tiny default."""
    return get_setting('training_content', TRAINING_CONTENT)


def get_bot_persona():
    """Bot's persona line. Priority: DB row -> env var -> default."""
    return get_setting('bot_persona', BOT_PERSONA)


def _format_training_snippets():
    """Build the additive-snippets section. High-priority snippets surface
    first so the model weights them more heavily. Returns '' when there are
    no active rows."""
    snippets = (TrainingSnippet.query
                .filter_by(is_active=True)
                .order_by(
                    db.case((TrainingSnippet.priority == 'high', 0), else_=1),
                    TrainingSnippet.sort_order.asc(),
                    TrainingSnippet.created_at.asc(),
                )
                .all())
    if not snippets:
        return ''
    lines = []
    for s in snippets:
        tag = f" [{s.category}]" if s.category else ''
        marker = '★ ' if s.priority == 'high' else ''
        lines.append(f"{marker}{s.title}{tag}:\n{s.body}")
    return "\n\n".join(lines)


def _format_team_members():
    members = (TeamMember.query
               .filter_by(is_active=True)
               .order_by(TeamMember.sort_order.asc(), TeamMember.id.asc())
               .all())
    if not members:
        return ''
    lines = []
    for m in members:
        parts = [m.name]
        if m.role:
            parts.append(f"({m.role})")
        line = ' '.join(parts)
        if m.specialty:
            line += f" — мэргэшил: {m.specialty}"
        if m.bio:
            line += f". {m.bio}"
        lines.append(f"- {line}")
    return "\n".join(lines)


def _format_business_lines():
    """Return (answer_block, refer_block) — two sections so the prompt can
    instruct the bot differently for each action."""
    lines = (BusinessLine.query
             .filter_by(is_active=True)
             .order_by(BusinessLine.sort_order.asc(), BusinessLine.id.asc())
             .all())
    answer, refer = [], []
    for b in lines:
        desc = b.description or ''
        contact = f" (Холбоо барих: {b.contact_info})" if b.contact_info else ''
        entry = f"- {b.name}: {desc}{contact}"
        if (b.action or 'refer') == 'answer':
            answer.append(entry)
        else:
            refer.append(entry)
    return "\n".join(answer), "\n".join(refer)


def build_system_prompt(session_state='new', funnel_stage='curious', user_first_name=''):
    """Build system prompt with training, FAQ, session-state and funnel context."""
    training = get_training_content()
    persona = get_bot_persona()
    faqs = FAQ.query.all()
    faq_text = "\n".join([f"Q: {faq.question}\nA: {faq.answer}" for faq in faqs])

    courses = Course.query.filter_by(is_active=True).all()
    courses_text = "\n".join([
        f"- {c.name} ({c.course_type}): {int(c.price):,}₮, эхлэх: {c.start_date.strftime('%Y-%m-%d')}, цаг: {c.time}"
        for c in courses
    ])

    snippets_text = _format_training_snippets()
    team_text = _format_team_members()
    answer_lines, refer_lines = _format_business_lines()

    registration_block = (
        f"БҮРТГЭЛИЙН ЛИНК:\n{GOOGLE_FORM_URL}\n" if GOOGLE_FORM_URL else ""
    )

    if user_first_name:
        name_block = (
            f"ХЭРЭГЛЭГЧИЙН НЭР: {user_first_name}\n"
            f"Хариултдаа тохиромжтой үед нэрээр нь хандаж болно (жишээ нь "
            f'"{user_first_name} аа,"). Гэхдээ нэрийг хэт олон бүү давт, '
            "байгалийн ярианд тохируулж хэрэглэ.\n"
        )
    else:
        name_block = ""

    session_rule = SESSION_RULES.get(session_state, SESSION_RULES['new'])
    funnel_rule = FUNNEL_RULES.get(funnel_stage, FUNNEL_RULES['curious'])

    snippets_section = (
        f"\nНЭМЭЛТ ТАЙЛБАР, ТОДРУУЛГА (★ = өндөр ач холбогдолтой):\n{snippets_text}\n"
        if snippets_text else ''
    )
    team_section = (
        f"\nМАНАЙ БАГ (хэрэглэгч багш/ажилтны талаар асуувал ашиглана):\n{team_text}\n"
        if team_text else ''
    )

    biz_section = ''
    if answer_lines or refer_lines:
        biz_section = "\nКОМПАНИЙН БУСАД ҮЙЛЧИЛГЭЭ:\n"
        if answer_lines:
            biz_section += (
                "Доорх үйлчилгээний талаар асуувал ТОВЧ хариулж болно "
                "(сонирхол хадгалж, дэлгэрэнгүйг ажилтнаас лавлахыг санал болго):\n"
                f"{answer_lines}\n"
            )
        if refer_lines:
            biz_section += (
                "Доорх үйлчилгээний талаар асуувал өөрөө хариулахгүй, "
                "\"Энэ чиглэлийг манай тусгай ажилтан хариуцдаг. Утасны "
                "дугаараа үлдээгээрэй, бид удахгүй холбогдоно\" гэж "
                "найрсагаар чиглүүл:\n"
                f"{refer_lines}\n"
            )

    system_prompt = f"""{persona}

СУРГАЛТЫН ТӨВИЙН МЭДЭЭЛЭЛ:
{training}
{snippets_section}{team_section}{biz_section}
ИДЭВХТЭЙ АНГИУД:
{courses_text}

ТҮГЭЭМЭЛ АСУУЛТУУД:
{faq_text}

{registration_block}{name_block}{funnel_rule}

ЧУХАЛ ДҮРМҮҮД:
{session_rule}
1. Хариултыг товч (3 өгүүлбэрээс ихгүй) бөгөөд тодорхой бичнэ. Урт жагсаалт оруулахаас зайлсхий.
2. Сургалтын давуу талыг хэт зар сурталчилгаа маягтай бус, итгэлтэй найз шиг зөвлөнө.
3. Хэрэглэгч бүртгүүлэх хүсэлтэй болсон тохиолдолд л БҮРТГЭЛИЙН ЛИНК-ийг хариултдаа бичнэ. Үе шат хүрээгүй үед линкийг бүү тулга. Утасны дугаараа үлдээх хүсэлтийг ч найрсагаар нэмж асуу.
4. Хэрэглэгч утасны дугаар бичсэн бол баярлал илэрхийлж, "Манай ажилтан удахгүй тантай холбогдоно" гэж мэдэгд.
5. Шийдэх боломжгүй буюу мэдэхгүй асуудал тулгарвал "Энэ асуудлыг манай ажилтан тантай эргэж холбогдож тодруулна" гэж хэлэх.
6. Эмодзи цөөн (1-2) хэрэглэж, илүү гар бичмэл маяг бүү аватарла.
7. Өгүүлбэрүүдийн эхлэлийг сольж бай, "Сургалт..." гэх мэт ижил үгээр дандаа бүү эхэл.
8. Сургалтаас өөр чиглэлийн (компанийн бусад үйлчилгээ) асуултанд дээрх "КОМПАНИЙН БУСАД ҮЙЛЧИЛГЭЭ" хэсгийн дүрмийн дагуу хариулна. Жагсаалтад байхгүй чиглэл бол "Тантай ажилтан холбогдох уу?" гэж асууж дугаар лав."""
    return system_prompt


def generate_bot_response(user_message, conversation_history,
                          session_state='new', funnel_stage='curious',
                          user_first_name=''):
    """Generate bot response using OpenAI"""
    try:
        messages = [{
            "role": "system",
            "content": build_system_prompt(
                session_state=session_state,
                funnel_stage=funnel_stage,
                user_first_name=user_first_name,
            ),
        }]
        messages.extend(conversation_history)
        messages.append({"role": "user", "content": user_message})
        
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.7,
            max_tokens=500
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"Error generating response: {e}")
        return "Уучлаарай, түр зуурын саатал гарсан байна. Хэдхэн минутын дараа дахин бичээрэй."

def analyze_and_comment_on_post(post_content):
    """Analyze post and generate appropriate comment"""
    try:
        analysis_prompt = f"""Энэ Facebook постыг анализ хийж, тохирох коммент бичнэ үү.

ПОСТ КОНТЕНТ:
{post_content}

ДҮРМҮҮД:
1. Сургалттай холбоотой: "Сургалтын мэдээллийг танд чатаар илгээсэн шүү, та чатаа шалгаарай"
2. Баяр ёслол, шагнал: Баяр хүргэх коммент
3. Бусад: тохирох, сонирхолтой коммент

Монгол хэлээр л хариулна. Коммент текст л өгнө, бусад зүйл бичихгүй."""
        
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": analysis_prompt}],
            temperature=0.7,
            max_tokens=200
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"Error analyzing post: {e}")
        return None

# ===================== POLLING BACKGROUND TASK =====================

def polling_task():
    """Background task to poll Facebook posts and auto-comment.

    Messages are handled via the /webhook endpoint, not here.
    Runs only when ENABLE_POLLING=true and requires an app context for db access.
    """
    while True:
        try:
            with app.app_context():
                posts = get_page_posts()
                for post in posts:
                    post_id = post.get('id')
                    existing = PagePost.query.filter_by(facebook_post_id=post_id).first()

                    if not existing:
                        post_content = post.get('message') or post.get('story', '')
                        comment_text = analyze_and_comment_on_post(post_content)

                        if comment_text:
                            if post_comment_on_page(post_id, comment_text):
                                page_post = PagePost(
                                    facebook_post_id=post_id,
                                    content=post_content,
                                    comment_posted=True,
                                    comment_text=comment_text
                                )
                                db.session.add(page_post)
                                db.session.commit()

            time.sleep(60)
        except Exception as e:
            print(f"Polling error: {e}")
            time.sleep(60)


# ===================== NUDGE BACKGROUND TASK =====================

# Funnel-aware follow-up nudges sent when a user goes silent for several hours.
# Polished Mongolian phrasings, one per stage. Each one is conversational, not
# salesy, and avoids re-greeting since the user already engaged earlier today.
def _nudge_message_for(user):
    stage = (user.funnel_stage or 'curious').lower()
    name_prefix = ''
    fname = first_name_of(user.name)
    if fname:
        name_prefix = f"{fname} аа, "

    if stage == 'ready':
        link = GOOGLE_FORM_URL or ''
        link_line = f"\n\nБүртгэлийн линк: {link}" if link else ""
        return (
            f"{name_prefix}та бүртгүүлэх талаар бодож үзсэн байх. "
            "Утасны дугаараа үлдээвэл бид өөрсдөө эргэж холбогдоно. "
            "Эсвэл доорх линкээр шууд бүртгүүлж болно." + link_line
        )
    if stage == 'pricing':
        return (
            f"{name_prefix}өмнө нь сургалтын үнийн талаар асууж байсан шүү. "
            "PocketZero-оор 4-6 хуваан, хүүгүй төлөх боломж байгаа. "
            "Тодруулах зүйл байвал чөлөөтэй бичээрэй."
        )
    if stage == 'exploring_courses':
        return (
            f"{name_prefix}таны сонирхож байсан ангийн талаар нэмж тодруулах "
            "зүйл байвал бичээрэй. Өөрт тань тохирох хэлбэрийг олоход баяртайгаар "
            "туслана."
        )
    # curious / fallback
    return (
        f"{name_prefix}танд сургалттай холбоотой ямар нэг асуулт үлдсэн "
        "бол хариулахад баяртай байх болно 😊"
    )


def nudge_pending_leads():
    """Send one follow-up to each user whose last message is 4-12h old.

    Designed to be safe to call from a thread loop or from a request handler.
    A user is only nudged once every 7 days to avoid spam.
    """
    now = datetime.utcnow()
    window_min = now - timedelta(hours=12)
    window_max = now - timedelta(hours=4)
    nudge_throttle = now - timedelta(days=7)

    last_msg_subq = (db.session.query(
        Message.facebook_user_id.label('uid'),
        db.func.max(Message.created_at).label('last_at'),
    ).group_by(Message.facebook_user_id).subquery())

    candidates = (db.session.query(FacebookUser, last_msg_subq.c.last_at)
                  .join(last_msg_subq, FacebookUser.id == last_msg_subq.c.uid)
                  .filter(last_msg_subq.c.last_at >= window_min)
                  .filter(last_msg_subq.c.last_at <= window_max)
                  .filter(db.or_(
                      FacebookUser.last_nudge_at == None,  # noqa: E711
                      FacebookUser.last_nudge_at < nudge_throttle,
                  ))
                  .all())

    sent = 0
    for user, last_at in candidates:
        msg = _nudge_message_for(user)
        if not send_facebook_message(user.facebook_id, msg):
            continue
        db.session.add(Message(
            facebook_user_id=user.id,
            sender='bot',
            content=msg,
        ))
        user.last_nudge_at = now
        db.session.commit()
        sent += 1
    if sent:
        print(f"Nudge: sent {sent} follow-up(s).")
    return sent


def nudge_task():
    """Background loop running nudge_pending_leads() every 30 minutes."""
    while True:
        try:
            with app.app_context():
                nudge_pending_leads()
        except Exception as e:
            print(f"Nudge error: {e}")
        time.sleep(30 * 60)


# ===================== HUMAN HANDOFF =====================

# Keyword sets per sensitivity tier. Conservative is the launch default; admins
# can raise sensitivity from /admin/settings when they're short-staffed or on
# vacation. Substring match, lowercase. Stems are deliberately short so that
# Mongolian agglutinative suffixes don't defeat them — 'ажилтан' catches
# ажилтантай / ажилтанд / ажилтны / ажилтанаас / ажилтны / ажилтанаа etc.
HANDOFF_KEYWORDS_EXPLICIT = [
    # Mongolian noun stems — saying any of these in chat almost always
    # signals "I want a human", not a casual reference.
    'ажилтан',       # ажилтан/тантай/тны/анд/наас/нтай
    'оператор',
    'менежер',
    'админтай',      # talk to admin
    'жинхэнэ хүн',   # a real person
    'хүнтэй',        # with a person — "хүнтэй ярья", "хүнтэй холбогдоё"
    'хүн рүү',       # to a person
    'хүн руу',
    'хүний хариу',   # human response
    'хүнээр',        # by a person
    # English fallbacks
    'live agent', 'real agent', 'real person', 'human agent',
    'speak to human', 'talk to human', 'operator please',
]
HANDOFF_KEYWORDS_FRUSTRATION = [
    'болохгүй байна', 'ойлгохгүй', 'ойлгомжгүй', 'муухай', 'үнэхээр муу',
    'гомдол', 'буруу хариу', 'хариулж чадахгүй', 'юу яриад байгаа',
    'хэрэггүй бот', 'утгагүй', 'ойлгосонгүй',
]

HANDOFF_USER_REPLY = (
    "Таны асуултыг манай ажилтан хариуцаж авлаа. Удахгүй холбогдох болно. "
    "Түр хүлээгээрэй 🙏"
)


def get_handoff_sensitivity():
    """conservative | balanced | aggressive — read from settings, default conservative."""
    value = (get_setting('handoff_sensitivity', 'conservative') or '').strip().lower()
    return value if value in ('conservative', 'balanced', 'aggressive') else 'conservative'


def get_mute_duration_hours():
    """How long the bot stays silent after a handoff is triggered."""
    raw = (get_setting('mute_duration_hours', '2') or '2').strip()
    try:
        hours = int(raw)
    except ValueError:
        hours = 2
    return max(0, min(hours, 168))  # clamp 0..7 days


def get_telegram_chat_ids():
    """Comma-separated chat IDs from settings. Returns [] when empty."""
    raw = (get_setting('telegram_chat_ids', '') or '').strip()
    if not raw:
        return []
    return [chunk.strip() for chunk in raw.replace(';', ',').split(',') if chunk.strip()]


def get_sound_alerts_enabled():
    return (get_setting('sound_alerts_enabled', 'on') or '').strip().lower() in ('on', 'true', '1', 'yes')


def _matches_refer_business_line(text):
    """True if the user message clearly names a business line flagged 'refer'.
    Used as one of the conservative-tier handoff signals."""
    if not text:
        return False
    lower = text.lower()
    refer_lines = (BusinessLine.query
                   .filter_by(is_active=True, action='refer')
                   .all())
    for line in refer_lines:
        if line.name and line.name.lower() in lower:
            return True
    return False


def should_handoff(message_text, fb_user):
    """Decide whether the bot should escalate to a human.

    Returns (bool, reason_label). reason_label is a short tag used in the
    AdminIssue row and Telegram message — not shown to the end user.
    Cheap to call: only string scans + one small DB query per inbound message.
    """
    if not message_text:
        return False, ''
    text = message_text.lower()
    sensitivity = get_handoff_sensitivity()

    for kw in HANDOFF_KEYWORDS_EXPLICIT:
        if kw in text:
            return True, f"explicit:{kw}"

    if _matches_refer_business_line(message_text):
        return True, 'business_line_refer'

    if sensitivity in ('balanced', 'aggressive'):
        for kw in HANDOFF_KEYWORDS_FRUSTRATION:
            if kw in text:
                return True, f"frustration:{kw}"

    if sensitivity == 'aggressive':
        # Same exact message 3+ times in the last hour from this user — they're
        # clearly stuck and the bot isn't getting through.
        cutoff = datetime.utcnow() - timedelta(hours=1)
        recent = (Message.query
                  .filter_by(facebook_user_id=fb_user.id, sender='user')
                  .filter(Message.created_at >= cutoff)
                  .order_by(Message.created_at.desc())
                  .limit(5)
                  .all())
        same = [m for m in recent if (m.content or '').strip().lower() == text.strip()]
        if len(same) >= 2:  # plus the current one = 3
            return True, 'repeated_question'

    return False, ''


def send_telegram_notification(text):
    """Send a Telegram message to every configured chat ID. Silent no-op if
    TELEGRAM_BOT_TOKEN is not set or no chat IDs are configured.

    The token stays in an env var (it's a credential); chat IDs live in DB
    settings so admins can add/remove themselves from the UI."""
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat_ids = get_telegram_chat_ids()
    if not token or not chat_ids:
        return 0
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    sent = 0
    for chat_id in chat_ids:
        try:
            resp = requests.post(url, json={
                'chat_id': chat_id,
                'text': text,
                'disable_web_page_preview': True,
            }, timeout=5)
            if resp.status_code == 200:
                sent += 1
            else:
                print(f"Telegram send failed for {chat_id}: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            print(f"Telegram send error for {chat_id}: {e}")
    return sent


def trigger_handoff(fb_user, reason, user_message):
    """Run the full handoff flow: mute the bot for the configured window,
    create an AdminIssue, ping admins via Telegram, send the user a polite
    waiting message. Safe to call inside the webhook handler."""
    hours = get_mute_duration_hours()
    if hours > 0:
        fb_user.bot_muted_until = datetime.utcnow() + timedelta(hours=hours)

    issue = AdminIssue(
        facebook_user_id=fb_user.id,
        issue_type='handoff',
        content=f"[{reason}] {user_message}"[:4000],
        status='open',
    )
    db.session.add(issue)
    db.session.commit()

    # User-visible message — short, polite, not robotic.
    send_facebook_message(fb_user.facebook_id, HANDOFF_USER_REPLY)
    db.session.add(Message(
        facebook_user_id=fb_user.id,
        sender='bot',
        content=HANDOFF_USER_REPLY,
    ))
    db.session.commit()

    # Admin notification via Telegram (no-op if not configured).
    display_name = fb_user.name or fb_user.facebook_id
    phone_part = f"\n📞 {fb_user.phone}" if fb_user.phone else ''
    tg_text = (
        f"🤝 Шинэ handoff хүсэлт\n"
        f"👤 {display_name}{phone_part}\n"
        f"📝 Шалтгаан: {reason}\n"
        f"💬 Мессеж: {user_message[:500]}\n\n"
        f"Facebook Page Inbox дээрээс хариулна уу."
    )
    send_telegram_notification(tg_text)
    return issue


# ===================== ROUTES =====================

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/privacy')
def privacy():
    """Public-facing Mongolian privacy policy. Required by Facebook App Review.

    All company-specific text is env-var driven so the same template serves
    every bot deployment. Submit https://<your-render-url>/privacy as the
    Privacy Policy URL in the Facebook Developer Console -> App Settings.
    """
    contact_email = (
        os.environ.get('PRIVACY_CONTACT_EMAIL')
        or os.environ.get('ADMIN_EMAIL')
        or 'info@magicfinance.mn'
    )
    return render_template(
        'privacy.html',
        contact_email=contact_email,
        last_updated=datetime.utcnow().strftime('%Y-%m-%d'),
        year=datetime.utcnow().year,
        company_legal_name=os.environ.get(
            'COMPANY_LEGAL_NAME', 'Мэжик Санхүүгийн Групп ХХК'),
        company_short_name=os.environ.get('COMPANY_SHORT_NAME', 'Мэжик'),
        company_address=os.environ.get(
            'COMPANY_ADDRESS',
            'Улаанбаатар хот, БЗД, 13-р хороолол, Натурын зам, '
            'UB Tower Plus, 5-р давхар, 509 тоот'),
        company_facebook_url=os.environ.get(
            'COMPANY_FACEBOOK_URL',
            'https://www.facebook.com/MagicFinancialGroup'),
        company_facebook_label=os.environ.get(
            'COMPANY_FACEBOOK_LABEL', 'Magic Financial Group'),
        product_name=os.environ.get('PRODUCT_NAME', 'Магик Финанс'),
    )

@app.route('/admin/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        
        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('dashboard'))
        else:
            return render_template('login.html', error='Invalid credentials')
    
    return render_template('login.html')

@app.route('/admin/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/admin/dashboard')
@login_required
@admin_required
def dashboard():
    leads_count = FacebookUser.query.filter_by(is_lead=True).count()
    hot_prospects_count = (FacebookUser.query
                           .filter_by(is_lead=False)
                           .filter(FacebookUser.funnel_stage.in_(['pricing', 'ready']))
                           .count())
    open_issues = AdminIssue.query.filter_by(status='open').count()
    total_messages = Message.query.count()

    return render_template('dashboard.html',
                         leads_count=leads_count,
                         hot_prospects_count=hot_prospects_count,
                         open_issues=open_issues,
                         total_messages=total_messages)

@app.route('/admin/courses', methods=['GET', 'POST'])
@login_required
@admin_required
def courses():
    if request.method == 'POST':
        data = request.get_json()
        action = data.get('action')
        
        if action == 'add':
            course = Course(
                name=data.get('name'),
                course_type=data.get('course_type'),
                start_date=datetime.fromisoformat(data.get('start_date')),
                time=data.get('time'),
                price=float(data.get('price')),
                description=data.get('description')
            )
            db.session.add(course)
            db.session.commit()
            return jsonify({'success': True, 'id': course.id})
        
        elif action == 'edit':
            course = Course.query.get(data.get('id'))
            if course:
                course.name = data.get('name')
                course.course_type = data.get('course_type')
                course.start_date = datetime.fromisoformat(data.get('start_date'))
                course.time = data.get('time')
                course.price = float(data.get('price'))
                course.description = data.get('description')
                db.session.commit()
                return jsonify({'success': True})
        
        elif action == 'delete':
            course = Course.query.get(data.get('id'))
            if course:
                db.session.delete(course)
                db.session.commit()
                return jsonify({'success': True})
    
    courses = Course.query.all()
    return render_template('courses.html', courses=courses)

@app.route('/admin/faq', methods=['GET', 'POST'])
@login_required
@admin_required
def faq():
    if request.method == 'POST':
        data = request.get_json()
        action = data.get('action')
        
        if action == 'add':
            faq_item = FAQ(
                question=data.get('question'),
                answer=data.get('answer'),
                category=data.get('category')
            )
            db.session.add(faq_item)
            db.session.commit()
            return jsonify({'success': True, 'id': faq_item.id})
        
        elif action == 'edit':
            faq_item = FAQ.query.get(data.get('id'))
            if faq_item:
                faq_item.question = data.get('question')
                faq_item.answer = data.get('answer')
                faq_item.category = data.get('category')
                db.session.commit()
                return jsonify({'success': True})
        
        elif action == 'delete':
            faq_item = FAQ.query.get(data.get('id'))
            if faq_item:
                db.session.delete(faq_item)
                db.session.commit()
                return jsonify({'success': True})
    
    faqs = FAQ.query.all()
    return render_template('faq.html', faqs=faqs)

@app.route('/admin/leads')
@login_required
@admin_required
def leads():
    """Two buckets in one page:
      1) Confirmed leads — dropped a phone number (is_lead=True). Today's only path.
      2) Hot prospects — reached the 'pricing' or 'ready' funnel stage but haven't
         shared a phone yet. These are warm and worth proactive follow-up.
    """
    confirmed = (FacebookUser.query
                 .filter_by(is_lead=True)
                 .order_by(FacebookUser.created_at.desc())
                 .all())

    last_msg_subq = (db.session.query(
        Message.facebook_user_id.label('uid'),
        db.func.max(Message.created_at).label('last_at'),
    ).group_by(Message.facebook_user_id).subquery())

    hot_rows = (db.session.query(FacebookUser, last_msg_subq.c.last_at)
                .join(last_msg_subq, FacebookUser.id == last_msg_subq.c.uid)
                .filter(FacebookUser.is_lead == False)  # noqa: E712
                .filter(FacebookUser.funnel_stage.in_(['pricing', 'ready']))
                .order_by(last_msg_subq.c.last_at.desc())
                .all())

    hot_prospects = []
    for user, last_at in hot_rows:
        last_user_msg = (Message.query
                         .filter_by(facebook_user_id=user.id, sender='user')
                         .order_by(Message.created_at.desc())
                         .first())
        hot_prospects.append({
            'user': user,
            'last_at': last_at,
            'last_message': (last_user_msg.content if last_user_msg else '') or '',
            'message_count': Message.query.filter_by(facebook_user_id=user.id).count(),
        })

    return render_template('leads.html', leads=confirmed, hot_prospects=hot_prospects)

@app.route('/admin/issues', methods=['GET', 'POST'])
@login_required
@admin_required
def issues():
    if request.method == 'POST':
        data = request.get_json()
        action = data.get('action')

        if action == 'update_status':
            issue = AdminIssue.query.get(data.get('id'))
            if issue:
                issue.status = data.get('status')
                if data.get('status') == 'resolved':
                    issue.resolved_at = datetime.utcnow()
                db.session.commit()
                return jsonify({'success': True})

        if action == 'unmute':
            user_id = data.get('user_id')
            user = FacebookUser.query.get(user_id)
            if not user:
                return jsonify({'success': False, 'error': 'user not found'}), 404
            user.bot_muted_until = None
            db.session.commit()
            return jsonify({'success': True})

        return jsonify({'success': False, 'error': 'unknown action'}), 400

    issues = AdminIssue.query.filter_by(status='open').order_by(AdminIssue.created_at.desc()).all()
    now = datetime.utcnow()
    muted_users = (FacebookUser.query
                   .filter(FacebookUser.bot_muted_until != None)  # noqa: E711
                   .filter(FacebookUser.bot_muted_until > now)
                   .order_by(FacebookUser.bot_muted_until.asc())
                   .all())
    return render_template('issues.html', issues=issues, muted_users=muted_users, now=now)

@app.route('/admin/api/test-telegram', methods=['POST'])
@login_required
@admin_required
def test_telegram():
    """Send a 'this is a test' message to every configured Telegram chat ID
    so admins can verify the wiring without faking a handoff via Facebook.

    Returns per-recipient status so the failing one stands out immediately —
    most "no message arrived" issues are one wrong digit in a chat ID or the
    token env var not actually being set on the Render service.
    """
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat_ids = get_telegram_chat_ids()

    result = {
        'token_set': bool(token),
        'token_preview': (token[:6] + '…' + token[-4:]) if token else '',
        'chat_ids': chat_ids,
        'attempts': [],
        'success_count': 0,
    }

    if not token:
        result['error'] = (
            'TELEGRAM_BOT_TOKEN орчны хувьсагч тогтоогдоогүй байна. '
            'Render dashboard → Environment руу орж нэмж, дахин deploy хийнэ үү.'
        )
        return jsonify(result)
    if not chat_ids:
        result['error'] = (
            'Chat ID-ууд хоосон байна. Доорх "Telegram chat ID-ууд" хэсэгт '
            'утгаа оруулаад "Save Settings" дарсан эсэхээ шалгана уу.'
        )
        return jsonify(result)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    test_msg = (
        f"✅ Test from MagicBot admin panel. "
        f"Sent by: {current_user.username}. "
        f"If you see this, your Telegram alerts are wired correctly."
    )
    for cid in chat_ids:
        item = {'chat_id': cid, 'ok': False, 'status': None, 'error': None}
        try:
            resp = requests.post(url, json={
                'chat_id': cid,
                'text': test_msg,
                'disable_web_page_preview': True,
            }, timeout=10)
            item['status'] = resp.status_code
            if resp.status_code == 200:
                item['ok'] = True
                result['success_count'] += 1
            else:
                item['error'] = resp.text[:300]
        except Exception as e:
            item['error'] = str(e)
        result['attempts'].append(item)

    return jsonify(result)


@app.route('/admin/api/backfill-names', methods=['POST'])
@login_required
@admin_required
def backfill_names():
    """Retry the Facebook profile lookup for every user currently named 'Unknown'.

    Use after pages_messaging gets Advanced Access (App Review) — historical
    rows created in Dev mode will then become populatable. Caps at 200 users
    per call so a manual click doesn't accidentally hammer the Graph API on
    a huge backlog; re-run as needed.
    """
    targets = (FacebookUser.query
               .filter(db.or_(
                   FacebookUser.name == None,  # noqa: E711
                   FacebookUser.name == '',
                   FacebookUser.name == 'Unknown',
               ))
               .order_by(FacebookUser.id.asc())
               .limit(200)
               .all())
    updated = 0
    for u in targets:
        if refresh_facebook_user_name(u):
            updated += 1
    return jsonify({
        'attempted': len(targets),
        'updated': updated,
        'remaining_unknown_after': FacebookUser.query.filter(db.or_(
            FacebookUser.name == None,  # noqa: E711
            FacebookUser.name == '',
            FacebookUser.name == 'Unknown',
        )).count(),
    })


@app.route('/admin/api/handoff-poll')
@login_required
@admin_required
def handoff_poll():
    """Lightweight polling endpoint for the dashboard sound/badge.

    Returns the count of open handoff issues and the id+timestamp of the
    newest one, so the client JS can play a sound when a new id appears.
    Kept cheap: two indexed queries, no LLM calls. Polled every 15s.
    """
    open_q = AdminIssue.query.filter_by(issue_type='handoff', status='open')
    latest = open_q.order_by(AdminIssue.created_at.desc()).first()
    return jsonify({
        'open_count': open_q.count(),
        'latest_id': latest.id if latest else None,
        'latest_at': latest.created_at.isoformat() if latest else None,
        'sound_enabled': get_sound_alerts_enabled(),
    })


@app.route('/admin/logs')
@login_required
@admin_required
def logs():
    messages = Message.query.order_by(Message.created_at.desc()).limit(100).all()
    return render_template('logs.html', messages=messages)

@app.route('/admin/settings', methods=['GET', 'POST'])
@login_required
@admin_required
def settings():
    if request.method == 'POST':
        data = request.get_json()
        for key, value in data.items():
            setting = GeneralSetting.query.filter_by(key=key).first()
            if setting:
                setting.value = value
            else:
                setting = GeneralSetting(key=key, value=value)
            db.session.add(setting)
        db.session.commit()
        return jsonify({'success': True})
    
    settings = {s.key: s.value for s in GeneralSetting.query.all()}
    return render_template('settings.html', settings=settings)


# ===================== BOT TRAINING =====================

@app.route('/admin/training', methods=['GET', 'POST'])
@login_required
@admin_required
def training():
    """Edit the bot's training corpus and persona directly from the dashboard.

    Saving a blank value clears the DB row so the env-var / file fallback
    takes over again — that's the "reset to default" path.
    """
    if request.method == 'POST':
        new_training = request.form.get('training_content', '').strip()
        new_persona = request.form.get('bot_persona', '').strip()

        for key, value in [('training_content', new_training), ('bot_persona', new_persona)]:
            row = GeneralSetting.query.filter_by(key=key).first()
            if row:
                row.value = value
            else:
                row = GeneralSetting(key=key, value=value)
                db.session.add(row)
        db.session.commit()
        flash(
            'Хадгаллаа. Дараагийн мессежээс эхлэн бот шинэ агуулгаар хариулна.',
            'success',
        )
        return redirect(url_for('training'))

    snippets = (TrainingSnippet.query
                .order_by(
                    db.case((TrainingSnippet.priority == 'high', 0), else_=1),
                    TrainingSnippet.sort_order.asc(),
                    TrainingSnippet.created_at.desc(),
                )
                .all())
    return render_template(
        'training.html',
        training_value=get_setting('training_content', ''),
        training_fallback=TRAINING_CONTENT,
        persona_value=get_setting('bot_persona', ''),
        persona_fallback=BOT_PERSONA,
        snippets=snippets,
    )


# ===================== TRAINING SNIPPETS =====================

@app.route('/admin/training/snippets', methods=['POST'])
@login_required
@admin_required
def training_snippets():
    """Add / edit / delete / toggle one training snippet at a time so multiple
    managers can append knowledge without overwriting each other's work."""
    data = request.get_json() or {}
    action = data.get('action')

    if action == 'add':
        snippet = TrainingSnippet(
            title=(data.get('title') or '').strip(),
            body=(data.get('body') or '').strip(),
            category=(data.get('category') or '').strip() or None,
            priority='high' if data.get('priority') == 'high' else 'normal',
            is_active=True,
        )
        if not snippet.title or not snippet.body:
            return jsonify({'success': False, 'error': 'Гарчиг болон агуулга шаардлагатай.'}), 400
        db.session.add(snippet)
        db.session.commit()
        return jsonify({'success': True, 'id': snippet.id})

    if action == 'edit':
        snippet = TrainingSnippet.query.get(data.get('id'))
        if not snippet:
            return jsonify({'success': False}), 404
        snippet.title = (data.get('title') or '').strip()
        snippet.body = (data.get('body') or '').strip()
        snippet.category = (data.get('category') or '').strip() or None
        snippet.priority = 'high' if data.get('priority') == 'high' else 'normal'
        if not snippet.title or not snippet.body:
            return jsonify({'success': False, 'error': 'Гарчиг болон агуулга шаардлагатай.'}), 400
        db.session.commit()
        return jsonify({'success': True})

    if action == 'toggle':
        snippet = TrainingSnippet.query.get(data.get('id'))
        if not snippet:
            return jsonify({'success': False}), 404
        snippet.is_active = not snippet.is_active
        db.session.commit()
        return jsonify({'success': True, 'is_active': snippet.is_active})

    if action == 'delete':
        snippet = TrainingSnippet.query.get(data.get('id'))
        if not snippet:
            return jsonify({'success': False}), 404
        db.session.delete(snippet)
        db.session.commit()
        return jsonify({'success': True})

    return jsonify({'success': False, 'error': 'unknown action'}), 400


# ===================== TEAM MEMBERS =====================

@app.route('/admin/team', methods=['GET', 'POST'])
@login_required
@admin_required
def team():
    if request.method == 'POST':
        data = request.get_json() or {}
        action = data.get('action')

        if action == 'add':
            member = TeamMember(
                name=(data.get('name') or '').strip(),
                role=(data.get('role') or '').strip() or None,
                specialty=(data.get('specialty') or '').strip() or None,
                bio=(data.get('bio') or '').strip() or None,
                is_active=True,
            )
            if not member.name:
                return jsonify({'success': False, 'error': 'Нэр шаардлагатай.'}), 400
            db.session.add(member)
            db.session.commit()
            return jsonify({'success': True, 'id': member.id})

        if action == 'edit':
            member = TeamMember.query.get(data.get('id'))
            if not member:
                return jsonify({'success': False}), 404
            member.name = (data.get('name') or '').strip()
            member.role = (data.get('role') or '').strip() or None
            member.specialty = (data.get('specialty') or '').strip() or None
            member.bio = (data.get('bio') or '').strip() or None
            if not member.name:
                return jsonify({'success': False, 'error': 'Нэр шаардлагатай.'}), 400
            db.session.commit()
            return jsonify({'success': True})

        if action == 'toggle':
            member = TeamMember.query.get(data.get('id'))
            if not member:
                return jsonify({'success': False}), 404
            member.is_active = not member.is_active
            db.session.commit()
            return jsonify({'success': True, 'is_active': member.is_active})

        if action == 'delete':
            member = TeamMember.query.get(data.get('id'))
            if not member:
                return jsonify({'success': False}), 404
            db.session.delete(member)
            db.session.commit()
            return jsonify({'success': True})

        return jsonify({'success': False, 'error': 'unknown action'}), 400

    members = (TeamMember.query
               .order_by(TeamMember.sort_order.asc(), TeamMember.id.asc())
               .all())
    return render_template('team.html', members=members)


# ===================== BUSINESS LINES =====================

@app.route('/admin/business-lines', methods=['GET', 'POST'])
@login_required
@admin_required
def business_lines():
    """Other services the page promotes (Magic Finance app, consulting, etc.).
    Each line tells the bot to either answer briefly or refer to a human."""
    if request.method == 'POST':
        data = request.get_json() or {}
        action = data.get('action')

        if action in ('add', 'edit'):
            if action == 'add':
                line = BusinessLine(is_active=True)
                db.session.add(line)
            else:
                line = BusinessLine.query.get(data.get('id'))
                if not line:
                    return jsonify({'success': False}), 404
            line.name = (data.get('name') or '').strip()
            line.description = (data.get('description') or '').strip() or None
            line.action = 'answer' if data.get('line_action') == 'answer' else 'refer'
            line.contact_info = (data.get('contact_info') or '').strip() or None
            if not line.name:
                db.session.rollback()
                return jsonify({'success': False, 'error': 'Нэр шаардлагатай.'}), 400
            db.session.commit()
            return jsonify({'success': True, 'id': line.id})

        if action == 'toggle':
            line = BusinessLine.query.get(data.get('id'))
            if not line:
                return jsonify({'success': False}), 404
            line.is_active = not line.is_active
            db.session.commit()
            return jsonify({'success': True, 'is_active': line.is_active})

        if action == 'delete':
            line = BusinessLine.query.get(data.get('id'))
            if not line:
                return jsonify({'success': False}), 404
            db.session.delete(line)
            db.session.commit()
            return jsonify({'success': True})

        return jsonify({'success': False, 'error': 'unknown action'}), 400

    lines = (BusinessLine.query
             .order_by(BusinessLine.sort_order.asc(), BusinessLine.id.asc())
             .all())
    return render_template('business_lines.html', lines=lines)


# ===================== DOCUMENTATION =====================

@app.route('/admin/docs')
@login_required
@admin_required
def docs():
    """In-app admin user manual + technical specification. Static content,
    kept under the same auth as the rest of the admin panel so it isn't
    accidentally indexable."""
    return render_template('docs.html')


# ===================== ADMIN USER MANAGEMENT =====================

MIN_ADMIN_PASSWORD_LENGTH = 12


@app.route('/admin/admins', methods=['GET'])
@login_required
@admin_required
def admins():
    all_admins = User.query.order_by(User.created_at.asc()).all()
    return render_template(
        'admins.html',
        admins=all_admins,
        min_password_length=MIN_ADMIN_PASSWORD_LENGTH,
    )


@app.route('/admin/admins/create', methods=['POST'])
@login_required
@super_admin_required
def create_admin():
    username = (request.form.get('username') or '').strip()
    email = (request.form.get('email') or '').strip()
    password = request.form.get('password') or ''
    make_super = request.form.get('make_super') == 'on'

    if not username or not email or not password:
        flash('Бүх талбарыг бөглөнө үү.', 'error')
        return redirect(url_for('admins'))

    if len(password) < MIN_ADMIN_PASSWORD_LENGTH:
        flash(
            f'Нууц үг хамгийн багадаа {MIN_ADMIN_PASSWORD_LENGTH} тэмдэгт байх ёстой.',
            'error',
        )
        return redirect(url_for('admins'))

    if User.query.filter_by(username=username).first():
        flash(f'"{username}" нэртэй админ аль хэдийн бүртгэлтэй байна.', 'error')
        return redirect(url_for('admins'))

    if User.query.filter_by(email=email).first():
        flash(f'"{email}" имэйл хаяг аль хэдийн бүртгэлтэй байна.', 'error')
        return redirect(url_for('admins'))

    new_admin = User(
        username=username,
        email=email,
        password=generate_password_hash(password),
        role='super_admin' if make_super else 'admin',
    )
    db.session.add(new_admin)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash('Админ нэмэхэд алдаа гарлаа. Дахин оролдоно уу.', 'error')
        return redirect(url_for('admins'))

    role_label = 'супер админ' if make_super else 'админ'
    flash(f'"{username}" {role_label}-ыг амжилттай нэмлээ.', 'success')
    return redirect(url_for('admins'))


@app.route('/admin/admins/<int:admin_id>/delete', methods=['POST'])
@login_required
@super_admin_required
def delete_admin(admin_id):
    target = User.query.get(admin_id)
    if not target:
        flash('Тухайн админ олдсонгүй.', 'error')
        return redirect(url_for('admins'))

    if target.id == current_user.id:
        flash('Та өөрийнхөө бүртгэлийг устгаж болохгүй.', 'error')
        return redirect(url_for('admins'))

    if User.query.count() <= 1:
        flash('Сүүлийн админыг устгах боломжгүй.', 'error')
        return redirect(url_for('admins'))

    if target.role == 'super_admin' and User.query.filter_by(role='super_admin').count() <= 1:
        flash('Сүүлийн супер админыг устгах боломжгүй.', 'error')
        return redirect(url_for('admins'))

    username = target.username
    db.session.delete(target)
    db.session.commit()
    flash(f'"{username}" админыг устгалаа.', 'success')
    return redirect(url_for('admins'))


@app.route('/admin/admins/change-password', methods=['POST'])
@login_required
@admin_required
def change_my_password():
    current_password = request.form.get('current_password') or ''
    new_password = request.form.get('new_password') or ''
    confirm_password = request.form.get('confirm_password') or ''

    if not check_password_hash(current_user.password, current_password):
        flash('Одоогийн нууц үг буруу байна.', 'error')
        return redirect(url_for('admins'))

    if len(new_password) < MIN_ADMIN_PASSWORD_LENGTH:
        flash(
            f'Шинэ нууц үг хамгийн багадаа {MIN_ADMIN_PASSWORD_LENGTH} тэмдэгт байх ёстой.',
            'error',
        )
        return redirect(url_for('admins'))

    if new_password != confirm_password:
        flash('Шинэ нууц үг таарахгүй байна.', 'error')
        return redirect(url_for('admins'))

    if check_password_hash(current_user.password, new_password):
        flash('Шинэ нууц үг хуучин нууц үгтэй ижил байж болохгүй.', 'error')
        return redirect(url_for('admins'))

    current_user.password = generate_password_hash(new_password)
    db.session.commit()
    logout_user()
    flash('Нууц үг амжилттай солигдлоо. Шинэ нууц үгээрээ нэвтэрнэ үү.', 'success')
    return redirect(url_for('login'))


@app.route('/admin/admins/<int:admin_id>/reset-password', methods=['POST'])
@login_required
@super_admin_required
def reset_admin_password(admin_id):
    target = User.query.get(admin_id)
    if not target:
        flash('Тухайн админ олдсонгүй.', 'error')
        return redirect(url_for('admins'))

    new_password = request.form.get('new_password') or ''
    if len(new_password) < MIN_ADMIN_PASSWORD_LENGTH:
        flash(
            f'Шинэ нууц үг хамгийн багадаа {MIN_ADMIN_PASSWORD_LENGTH} тэмдэгт байх ёстой.',
            'error',
        )
        return redirect(url_for('admins'))

    target.password = generate_password_hash(new_password)
    db.session.commit()
    flash(
        f'"{target.username}"-ийн нууц үгийг шинэчиллээ. Шинэ нууц үгийг тухайн хэрэглэгчид өгнө үү.',
        'success',
    )
    return redirect(url_for('admins'))


@app.route('/admin/admins/<int:admin_id>/toggle-role', methods=['POST'])
@login_required
@super_admin_required
def toggle_admin_role(admin_id):
    target = User.query.get(admin_id)
    if not target:
        flash('Тухайн админ олдсонгүй.', 'error')
        return redirect(url_for('admins'))

    if target.id == current_user.id:
        flash('Та өөрийнхөө эрхийг өөрчилж болохгүй. Өөр супер админаар өөрчлүүл.', 'error')
        return redirect(url_for('admins'))

    if target.role == 'super_admin':
        if User.query.filter_by(role='super_admin').count() <= 1:
            flash('Сүүлийн супер админыг буулгах боломжгүй.', 'error')
            return redirect(url_for('admins'))
        target.role = 'admin'
        msg = f'"{target.username}"-ыг энгийн админ болголоо.'
    else:
        target.role = 'super_admin'
        msg = f'"{target.username}"-ыг супер админ болголоо.'

    db.session.commit()
    flash(msg, 'success')
    return redirect(url_for('admins'))


@app.route('/webhook', methods=['POST'])
def webhook():
    """Facebook Messenger webhook"""
    raw_body = request.get_data()
    sig_header = request.headers.get('X-Hub-Signature-256', '')
    if not verify_facebook_signature(raw_body, sig_header):
        # Surface header presence + body size so we can distinguish "unsigned
        # forged request" from "real Facebook delivery rejected because the app
        # secret env var doesn't match the FB app's secret".
        sig_preview = (sig_header[:14] + '…') if sig_header else '<missing>'
        print(
            f"Webhook rejected: signature mismatch "
            f"sig_header={sig_preview} body_len={len(raw_body)} "
            f"app_secret_set={bool(FACEBOOK_APP_SECRET)}"
        )
        return jsonify({'error': 'invalid signature'}), 403

    data = request.get_json(silent=True) or {}

    # One line per delivery so Render logs prove Facebook is actually hitting us
    # and which senders are in the payload. Critical when "bot doesn't reply"
    # turns out to be "webhook never fired".
    entries = data.get('entry', []) if isinstance(data, dict) else []
    senders = [
        ev.get('sender', {}).get('id')
        for entry in entries
        for ev in entry.get('messaging', [])
    ]
    print(
        f"Webhook received object={data.get('object')!r} "
        f"entries={len(entries)} senders={senders}"
    )

    if data.get('object') == 'page':
        for entry in entries:
            for messaging_event in entry.get('messaging', []):
                sender_id = messaging_event.get('sender', {}).get('id')
                recipient_id = messaging_event.get('recipient', {}).get('id')
                event_keys = [k for k in messaging_event.keys() if k not in ('sender', 'recipient', 'timestamp')]
                print(
                    f"Webhook event sender={sender_id} recipient={recipient_id} "
                    f"kinds={event_keys}"
                )

                if messaging_event.get('message'):
                    message_text = messaging_event['message'].get('text')

                    # Per-sender rate limit. Reply once with a polite throttle
                    # message so the user knows their input was received, then
                    # skip the OpenAI call.
                    if sender_id and not check_rate_limit(sender_id):
                        send_facebook_message(sender_id, RATE_LIMIT_REPLY)
                        continue
                    
                    # Get or create Facebook user (handle webhook retries racing on the same sender)
                    fb_user = FacebookUser.query.filter_by(facebook_id=sender_id).first()
                    if not fb_user:
                        user_info = get_facebook_user_info(sender_id)
                        fb_user = FacebookUser(
                            facebook_id=sender_id,
                            name=(user_info.get('name') or '').strip() or 'Unknown'
                        )
                        db.session.add(fb_user)
                        try:
                            db.session.commit()
                        except IntegrityError:
                            db.session.rollback()
                            fb_user = FacebookUser.query.filter_by(facebook_id=sender_id).first()
                    elif (fb_user.name or '').strip().lower() in ('', 'unknown'):
                        # First attempt may have failed (Dev mode, rate limit, etc.) —
                        # try again on this message. Best-effort; failure is logged
                        # and we move on.
                        refresh_facebook_user_name(fb_user)
                    
                    # Classify session state from the PRIOR conversation (before we save
                    # the new inbound message). Drives the greeting behaviour.
                    last_msg = (Message.query
                                .filter_by(facebook_user_id=fb_user.id)
                                .order_by(Message.created_at.desc())
                                .first())
                    session_state = classify_session(last_msg.created_at if last_msg else None)

                    # Advance funnel stage from the current message (no regression).
                    new_stage = detect_funnel_stage(message_text, fb_user.funnel_stage or 'curious')
                    if new_stage != fb_user.funnel_stage:
                        fb_user.funnel_stage = new_stage
                        db.session.commit()

                    # Save user message
                    user_msg = Message(
                        facebook_user_id=fb_user.id,
                        sender='user',
                        content=message_text
                    )
                    db.session.add(user_msg)
                    db.session.commit()

                    # Handoff in progress — bot is muted, human takes over via FB
                    # Page Inbox. We still log the inbound message so the admin can
                    # see context, but skip the OpenAI call and the auto-reply.
                    now = datetime.utcnow()
                    if fb_user.bot_muted_until and fb_user.bot_muted_until > now:
                        continue

                    # Auto-clear an expired mute so we don't carry stale state.
                    if fb_user.bot_muted_until and fb_user.bot_muted_until <= now:
                        fb_user.bot_muted_until = None
                        db.session.commit()

                    # Decide whether this message needs a human before spending an
                    # OpenAI call. Avoids the awkward case where the bot answers
                    # AND we tell the user "human will reply" in the same turn.
                    handoff, reason = should_handoff(message_text, fb_user)
                    if handoff:
                        trigger_handoff(fb_user, reason, message_text)
                        continue

                    # Get conversation history
                    history = Message.query.filter_by(facebook_user_id=fb_user.id).order_by(Message.created_at).all()
                    conversation = [
                        {"role": "user" if m.sender == 'user' else "assistant", "content": m.content}
                        for m in history[-10:]  # Last 10 messages
                    ]

                    # Generate bot response
                    bot_response = generate_bot_response(
                        message_text,
                        conversation,
                        session_state=session_state,
                        funnel_stage=fb_user.funnel_stage or 'curious',
                        user_first_name=first_name_of(fb_user.name),
                    )

                    # Save bot message
                    bot_msg = Message(
                        facebook_user_id=fb_user.id,
                        sender='bot',
                        content=bot_response
                    )
                    db.session.add(bot_msg)
                    db.session.commit()

                    # Send response via Facebook
                    send_facebook_message(sender_id, bot_response)
                    
                    # Check if phone number was provided
                    phone_match = PHONE_RE.search(message_text)
                    if phone_match and not fb_user.is_lead:
                        fb_user.phone = phone_match.group(0)
                        fb_user.is_lead = True
                        fb_user.lead_status = 'contacted'
                        db.session.commit()

                        # Send handoff message
                        handoff_msg = (
                            "Баярлалаа, дугаараа үлдээснийг тань хүлээж авлаа. "
                            "Манай бүртгэлийн ажилтан удахгүй тантай эргэж холбогдох болно."
                        )
                        send_facebook_message(sender_id, handoff_msg)
    
    return jsonify({'status': 'ok'}), 200

@app.route('/webhook', methods=['GET'])
def webhook_verify():
    """Facebook webhook verification"""
    verify_token = os.getenv('VERIFY_TOKEN', 'magic_bot_verify_token')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    
    if token == verify_token:
        return challenge
    return 'Invalid token', 403

# ===================== INITIALIZATION =====================

def seed_courses_and_faqs():
    """Populate Courses and FAQs with Magic Financial Group defaults if empty.

    Idempotent: only inserts when the table is empty, so subsequent boots
    won't duplicate or overwrite admin edits.

    Skipped entirely when SEED_DEFAULTS=false — set this on any non-MagicBot
    deployment so it doesn't inherit Magic Financial Group's catalog.
    """
    if os.environ.get('SEED_DEFAULTS', 'true').strip().lower() != 'true':
        print("SEED_DEFAULTS=false — skipping default course/FAQ seed.")
        return

    default_start = datetime.utcnow() + timedelta(days=14)

    if Course.query.count() == 0:
        courses = [
            Course(
                name='Нягтлан-Нярвын хосолсон сургалт — 100% Онлайн',
                course_type='100% Online',
                start_date=default_start,
                time='Хүссэн үедээ судлах',
                price=360000,
                description=(
                    'Бие даан судлах онлайн сургалт. Видео хичээл, бодлогууд, '
                    'Magic Finance программын 6 сарын үнэгүй эрх багтана.'
                ),
                is_active=True,
            ),
            Course(
                name='Нягтлан-Нярвын хосолсон сургалт — Хосолсон хэлбэр',
                course_type='Hybrid',
                start_date=default_start,
                time='Даваа/Лхагва/Баасан танхимд, бусад өдөр онлайн',
                price=440000,
                description=(
                    '7 хоногийн 3 өдөр танхимаар, үлдсэн өдрүүдэд онлайнаар '
                    'хичээллэх уян хатан хөтөлбөр.'
                ),
                is_active=True,
            ),
            Course(
                name='Нягтлан-Нярвын хосолсон сургалт — Багштай онлайн',
                course_type='Online with Teacher',
                start_date=default_start,
                time='Даваа-Баасан онлайн',
                price=660000,
                description=(
                    '1-5 дахь өдөр шууд багштай онлайн хичээл, асуулт-хариулт, '
                    'бодит жишээний дадлагатай хөтөлбөр.'
                ),
                is_active=True,
            ),
            Course(
                name='Нягтлан-Нярвын хосолсон сургалт — Танхим',
                course_type='Classroom',
                start_date=default_start,
                time='Даваа-Баасан, өглөө 10:00-13:00 эсвэл орой 18:00-21:00',
                price=880000,
                description=(
                    'UB Tower Plus, 5 давхар 509 тоот танхимд тогтмол '
                    'хичээллэх бүрэн хэмжээний сургалт. Багштай шууд харилцана.'
                ),
                is_active=True,
            ),
        ]
        for c in courses:
            db.session.add(c)
        print(f'Seeded {len(courses)} default courses.')

    if FAQ.query.count() == 0:
        faqs = [
            FAQ(question='Сургалт хэдэн долоо хоног үргэлжилдэг вэ?',
                answer=(
                    'Сургалт нийт 4 долоо хоног үргэлжилнэ. '
                    '1-р долоо хоногт нярвын тайлан гаргахыг сурна. '
                    '2-р долоо хоногт санхүүгийн тайлан, '
                    '3-р долоо хоногт татварын хичээл, '
                    '4-р долоо хоногт Magic Finance программ дээр '
                    'тайлан гаргахыг үздэг.'
                ),
                category='Хөтөлбөр'),
            FAQ(question='Сургалтын үнэ хэд вэ?',
                answer=(
                    '100% Онлайн — 360,000₮, Хосолсон хэлбэр — 440,000₮, '
                    'Багштай онлайн — 660,000₮, Танхим — 880,000₮. '
                    'PocketZero-оор 4-6 хуваан, хүүгүй шимтгэлгүй төлөх '
                    'боломжтой.'
                ),
                category='Төлбөр'),
            FAQ(question='Хичээл хэдэн цагт ордог вэ?',
                answer=(
                    'Өглөөний анги 10:00–13:00, оройн анги 18:00–21:00 '
                    'хооронд хичээллэдэг. Та цагаа сонгоход тань туслана.'
                ),
                category='Цаг'),
            FAQ(question='Сургалтын төв хаана байрладаг вэ?',
                answer=(
                    'Манай сургалт БЗД 13-р хороолол, Натурын замд байрлах '
                    'UB Tower Plus, 5 давхар, 509 тоотод явагддаг.'
                ),
                category='Хаяг'),
            FAQ(question='Сургалтын дараа сертификат олгодог уу?',
                answer=(
                    'Тийм. 4 долоо хоногийн хичээл дуусаад шалгалт өгсний '
                    'дараа Мэжик Санхүүгийн Группын албан ёсны сертификат '
                    'олгоно. Мөн Magic Finance программыг 6 сар үнэгүй '
                    'ашиглах эрх бэлэглэнэ.'
                ),
                category='Сертификат'),
            FAQ(question='Ямар мэргэжилтэй хүн сурч болох вэ?',
                answer=(
                    'Та ямар ч мэргэжилтэй, ямар ч түвшний мэдлэгтэй байсан '
                    'хамаагүй. Бид эхнээс нь ойлгомжтой, бодит жишээн дээр '
                    'тулгуурлан заадаг тул шинэхэн суралцагч ч амжилттай '
                    'төгсөж чадна.'
                ),
                category='Бүртгэл'),
            FAQ(question='Төлбөрөө хуваан төлж болох уу?',
                answer=(
                    'Болно. PocketZero апп ашиглан 4-6 хуваан, хүүгүй '
                    'шимтгэлгүй төлөх боломжтой. Эсвэл сургалтын эхэнд '
                    'хагасыг нь төлж, хичээл явагдах хугацаандаа үлдсэнийг '
                    'нөхөн төлж болно.'
                ),
                category='Төлбөр'),
            FAQ(question='Magic Finance программ гэж юу вэ?',
                answer=(
                    'Magic Finance бол санхүүгийн тайлан гаргахад '
                    'зориулагдсан, манай өөрсдийн хөгжүүлсэн программ. '
                    'Гар аргаар хийдэг ажлыг 80% хүртэл хөнгөвчилж, '
                    'хяналтаа сайжруулна. Суралцагч бүрт 6 сар үнэгүй '
                    'ашиглах эрх олгоно.'
                ),
                category='Программ'),
        ]
        for f in faqs:
            db.session.add(f)
        print(f'Seeded {len(faqs)} default FAQs.')

    db.session.commit()


def init_db():
    """Initialize database tables, seed admin user + default Courses/FAQs.

    Also self-heals existing deployments: if there are admin users but none
    are super_admin (e.g. DB was created before the role tier landed), promote
    the oldest one so management routes remain usable.
    """
    with app.app_context():
        db.create_all()
        ensure_schema()
        seed_courses_and_faqs()

        if not User.query.filter_by(username='admin').first():
            initial_password = os.environ.get('INITIAL_ADMIN_PASSWORD')
            if initial_password:
                admin = User(
                    username='admin',
                    password=generate_password_hash(initial_password),
                    email=os.environ.get('ADMIN_EMAIL', 'admin@magicfinance.mn'),
                    role='super_admin',
                )
                db.session.add(admin)
                db.session.commit()
                print("Default admin user created with username 'admin' (super_admin).")
            else:
                print("INITIAL_ADMIN_PASSWORD not set — skipping default admin creation. "
                      "Set it and redeploy to create the admin user.")

        if User.query.count() > 0 and not User.query.filter_by(role='super_admin').first():
            oldest = User.query.order_by(User.created_at.asc()).first()
            oldest.role = 'super_admin'
            db.session.commit()
            print(f"No super_admin found — promoted '{oldest.username}' to super_admin.")

        # Emergency forgot-password recovery via env vars. Set both
        # RESET_ADMIN_USERNAME and RESET_ADMIN_PASSWORD on Render, redeploy,
        # log in with the new password, then REMOVE both vars and redeploy
        # again. Leaving them set means every restart resets the password.
        reset_user = os.environ.get('RESET_ADMIN_USERNAME')
        reset_pw = os.environ.get('RESET_ADMIN_PASSWORD')
        if reset_user and reset_pw:
            if len(reset_pw) < MIN_ADMIN_PASSWORD_LENGTH:
                print(
                    f"!!! RESET_ADMIN_PASSWORD too short (need >= "
                    f"{MIN_ADMIN_PASSWORD_LENGTH} chars). Skipping reset."
                )
            else:
                target = User.query.filter_by(username=reset_user).first()
                if target:
                    target.password = generate_password_hash(reset_pw)
                    db.session.commit()
                    print(
                        f"!!! WARNING: password reset for '{reset_user}' via env var. "
                        f"REMOVE RESET_ADMIN_USERNAME and RESET_ADMIN_PASSWORD now "
                        f"and redeploy, or the password resets on every boot."
                    )
                else:
                    print(f"!!! RESET_ADMIN_USERNAME='{reset_user}' not found.")


# Run at import so gunicorn workers initialize the DB on boot.
init_db()

# Optional background polling for Page post auto-commenting.
# Disabled by default to avoid duplicate work across gunicorn workers.
if os.environ.get('ENABLE_POLLING', 'false').lower() == 'true':
    Thread(target=polling_task, daemon=True).start()

# Optional follow-up nudges to silent leads. Off by default; turn on only
# after upgrading from Render Free, otherwise the worker spins down before
# the loop wakes up.
if os.environ.get('ENABLE_NUDGE', 'false').lower() == 'true':
    Thread(target=nudge_task, daemon=True).start()


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
