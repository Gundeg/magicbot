"""Domain services: Facebook Messenger API, OpenAI helpers, rate limit,
funnel/session classifiers, settings getters, AI prompt builder, schema
migration, seed routines, background tasks, handoff flow, and audit logging.

Route modules import functions from here; they should not poke at Facebook
or OpenAI directly.
"""
import hmac
import hashlib
import json
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock

import requests
from flask import current_app
from flask_login import current_user
from openai import OpenAI
from sqlalchemy.exc import IntegrityError

from extensions import db
from models import (
    AdminIssue, AuditEntry, BusinessLine, ChatQuestionCluster, Course,
    FacebookUser, FAQ, GeneralSetting, HandoffKeyword, Message, PagePost,
    Product, ProductLink, TeamMember, TrainingSnippet, User,
)


# ===================== CONSTANTS / CONFIG =====================

PHONE_RE = re.compile(r'(?:\+?976[\s-]?)?[89]\d{7}')

# OpenAI client. Reads OPENAI_API_KEY from env at import time — the .env file
# is loaded before this module is first imported by app.py.
client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

# Facebook credentials (read once at import).
FACEBOOK_PAGE_ID = os.environ.get('FACEBOOK_PAGE_ID', '')
FACEBOOK_ACCESS_TOKEN = os.environ.get('FACEBOOK_ACCESS_TOKEN', '')
if not FACEBOOK_ACCESS_TOKEN:
    raise RuntimeError("FACEBOOK_ACCESS_TOKEN environment variable is required")
FACEBOOK_APP_SECRET = os.environ.get('FACEBOOK_APP_SECRET', '')
if not FACEBOOK_APP_SECRET:
    print(
        "WARNING: FACEBOOK_APP_SECRET not set — accepting all webhook payloads "
        "unverified. Live-mode deploys MUST set this or Facebook traffic can be "
        "forged."
    )
GOOGLE_FORM_URL = os.environ.get('GOOGLE_FORM_URL', '')

# Training content fallback chain: env → file → tiny default. The DB row in
# GeneralSetting('training_content') takes precedence at runtime; see
# get_training_content().
TRAINING_PATH = Path(__file__).parent / 'pasted_content.txt'
_env_training = os.environ.get('TRAINING_CONTENT', '').strip()
if _env_training:
    TRAINING_CONTENT = _env_training
else:
    try:
        TRAINING_CONTENT = TRAINING_PATH.read_text(encoding='utf-8')
    except FileNotFoundError:
        TRAINING_CONTENT = "Манай сургалтын төв нь 2007 оноос хойш үйл ажиллагаа явуулж байгаа."

_DEFAULT_PERSONA = (
    "Та Мэжик Санхүүгийн Группын Facebook чат туслах. Сэтгэл судлалын "
    "ойлголттой, маркетингийн ур чадвартай, нягтлан бодох сургалтын зөвлөх. "
    "Үргэлж монгол хэлээр, амьд хүн шиг ойлгомжтой, дотночоор хариулна. "
    "Англи үг бичихгүй (тусгай нэр, программын нэрийг эс тооцох)."
)
BOT_PERSONA = os.environ.get('BOT_PERSONA', '').strip() or _DEFAULT_PERSONA

# Parse defensively: an empty / non-numeric env value falls back to the
# documented default. A value of 0 means "disable rate limiting" (NOT "deny
# every message" — that was a real-world misconfig that bricked the bot).
def _safe_int_env(name, default):
    raw = (os.environ.get(name, '') or '').strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"WARNING: {name}={raw!r} is not an integer; using default {default}.")
        return default


RATE_LIMIT_MAX = _safe_int_env('RATE_LIMIT_MAX', 5)
RATE_LIMIT_WINDOW = timedelta(seconds=_safe_int_env('RATE_LIMIT_WINDOW_SECONDS', 60))
print(
    f"Rate limit: {'DISABLED' if RATE_LIMIT_MAX <= 0 else f'{RATE_LIMIT_MAX} msg / {RATE_LIMIT_WINDOW.total_seconds():.0f}s per sender'}"
)

RATE_LIMIT_REPLY = (
    "Та маш олон мессеж бичиж байна. 1 минутын дараа дахин оролдоорой. 🙏"
)

SESSION_ACTIVE_WINDOW = timedelta(hours=2)
SESSION_GAP_WINDOW = timedelta(hours=24)


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


_rate_state = defaultdict(deque)
_rate_state_lock = Lock()


def check_rate_limit(sender_id):
    """Return True if sender is within the limit; record the hit. False if over.

    RATE_LIMIT_MAX <= 0 disables rate limiting entirely. The previous
    implementation treated 0 as "block every message" — a real prod incident
    where the bot replied to every first message with the throttle text.

    When the limit fires, logs sender_id + current deque shape so a Render
    log scan can distinguish:
      - real abuse                  (one sender_id, many timestamps, all recent)
      - FB webhook retries          (one sender_id, several timestamps within
                                     a few seconds of each other)
      - state contamination         (a sender_id we wouldn't expect to see)
    """
    if RATE_LIMIT_MAX <= 0:
        return True
    now = datetime.utcnow()
    cutoff = now - RATE_LIMIT_WINDOW
    with _rate_state_lock:
        dq = _rate_state[sender_id]
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= RATE_LIMIT_MAX:
            # Diagnostic: emit deque contents so we can tell webhook retries
            # apart from genuine spam. Short deque, fine to dump inline.
            ages = [
                round((now - ts).total_seconds(), 1) for ts in dq
            ]
            print(
                f"RATE LIMIT FIRED  sender_id={sender_id!r}  "
                f"max={RATE_LIMIT_MAX}  window={RATE_LIMIT_WINDOW.total_seconds():.0f}s  "
                f"hit_ages_sec={ages}  total_tracked_senders={len(_rate_state)}"
            )
            return False
        dq.append(now)
        if len(_rate_state) > 5000:
            for sid in [k for k, v in _rate_state.items() if not v]:
                del _rate_state[sid]
        return True


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
    """Get user info from Facebook's User Profile API. Returns {} on any failure."""
    url = f"https://graph.facebook.com/v18.0/{facebook_id}"
    params = {"fields": "name,first_name,last_name", "access_token": FACEBOOK_ACCESS_TOKEN}
    try:
        response = requests.get(url, params=params, timeout=5)
        if response.status_code == 200:
            data = response.json() or {}
            if not data.get('name'):
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
    """Re-fetch the display name and persist if non-empty. Returns the new name or ''."""
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

    def add_columns(table, additions):
        existing = {col['name'] for col in inspector.get_columns(table)}
        with db.engine.begin() as conn:
            for col, clause in additions.items():
                if col not in existing:
                    try:
                        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {clause}")
                        print(f"Schema migration: {table} ADD COLUMN {col}")
                    except Exception as e:
                        print(f"Schema migration skipped ({table}.{col}): {e}")

    add_columns('facebook_user', {
        'funnel_stage': "funnel_stage VARCHAR(30) DEFAULT 'curious'",
        'last_nudge_at': 'last_nudge_at DATETIME',
        'bot_muted_until': 'bot_muted_until DATETIME',
        'conversation_topic': 'conversation_topic VARCHAR(100)',
    })

    add_columns('admin_issue', {
        'updated_by_id': 'updated_by_id INTEGER REFERENCES user(id)',
        'updated_at': 'updated_at DATETIME',
        'notes': 'notes TEXT',
    })

    add_columns('course', {
        'end_date': 'end_date DATETIME',
        'is_recurring': 'is_recurring BOOLEAN DEFAULT 0',
        'schedule_template': 'schedule_template TEXT',
        'status_note': 'status_note VARCHAR(255)',
        # Admin-assigned external code. Left nullable so existing rows don't
        # block the migration; uniqueness is enforced by the admin POST.
        'course_number': 'course_number INTEGER',
        'duration_days': 'duration_days INTEGER',
    })

    # ensure_schema() is the legacy migration path (pre-Alembic). Going forward,
    # schema changes go through migrations/versions/. We keep the existing
    # entries here so old DBs that boot without ever applying Alembic still
    # self-heal. The 5 BU columns (signup_form_url, signup_phone, exam_form_url,
    # num_products_or_services, total_clients_or_users) were removed in Phase 1
    # and intentionally NOT added back here.
    add_columns('business_line', {
        'status_note': 'status_note VARCHAR(255)',
        'address': 'address TEXT',
        'email': 'email VARCHAR(200)',
        'established_year': 'established_year INTEGER',
    })

    # Product + ProductLink were added after the initial schema; create them
    # on existing DBs that never ran create_all() against the new tables.
    if 'product' not in inspector.get_table_names():
        with db.engine.begin() as conn:
            try:
                conn.exec_driver_sql(
                    "CREATE TABLE product ("
                    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "  business_line_id INTEGER NOT NULL REFERENCES business_line(id),"
                    "  product_number INTEGER UNIQUE,"
                    "  name VARCHAR(200) NOT NULL,"
                    "  vendor VARCHAR(120),"
                    "  description TEXT,"
                    "  is_main_product BOOLEAN DEFAULT 0,"
                    "  is_active BOOLEAN DEFAULT 1,"
                    "  status_note VARCHAR(255),"
                    "  sort_order INTEGER DEFAULT 0,"
                    "  created_at DATETIME,"
                    "  updated_at DATETIME"
                    ")"
                )
                print("Schema migration: created product table")
            except Exception as e:
                print(f"Schema migration skipped (product table): {e}")
    if 'product_link' not in inspector.get_table_names():
        with db.engine.begin() as conn:
            try:
                conn.exec_driver_sql(
                    "CREATE TABLE product_link ("
                    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "  product_id INTEGER NOT NULL REFERENCES product(id),"
                    "  kind VARCHAR(40) NOT NULL,"
                    "  label VARCHAR(160),"
                    "  url VARCHAR(500) NOT NULL,"
                    "  is_active BOOLEAN DEFAULT 1,"
                    "  sort_order INTEGER DEFAULT 0,"
                    "  created_at DATETIME"
                    ")"
                )
                print("Schema migration: created product_link table")
            except Exception as e:
                print(f"Schema migration skipped (product_link table): {e}")

    if 'handoff_keyword' not in inspector.get_table_names():
        with db.engine.begin() as conn:
            try:
                conn.exec_driver_sql(
                    "CREATE TABLE handoff_keyword ("
                    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "  keyword VARCHAR(200) NOT NULL,"
                    "  keyword_type VARCHAR(20) DEFAULT 'explicit',"
                    "  is_active BOOLEAN DEFAULT 1,"
                    "  note VARCHAR(255),"
                    "  created_at DATETIME"
                    ")"
                )
                print("Schema migration: created handoff_keyword table")
            except Exception as e:
                print(f"Schema migration skipped (handoff_keyword table): {e}")


# ===================== SESSION + FUNNEL CLASSIFIERS =====================

def classify_conversation(fb_user):
    """Use gpt-4o-mini to classify what business line (or generic category)
    a user's conversation is about, then persist to facebook_user.conversation_topic."""
    try:
        recent_messages = (
            Message.query
            .filter_by(facebook_user_id=fb_user.id, sender='user')
            .order_by(Message.created_at.desc())
            .limit(15)
            .all()
        )
        if not recent_messages:
            return

        business_lines = [
            bl.name for bl in
            BusinessLine.query.filter_by(is_active=True).order_by(BusinessLine.sort_order).all()
        ]

        conversation_text = '\n'.join(
            m.content for m in reversed(recent_messages)
        )

        category_list = business_lines + ['general', 'not_related', 'other_request']
        categories_str = ', '.join(f'"{c}"' for c in category_list)

        prompt = (
            f"You are classifying a Messenger conversation for a Mongolian training/consulting center.\n"
            f"Available categories: {categories_str}\n\n"
            f"Business line names like {', '.join(repr(b) for b in business_lines)} represent specific non-training services.\n"
            f'"general" = ANY question about training courses: course names, pricing, schedules, start dates, duration, content, '
            f'teaching format (online/classroom), enrollment, registration, or any other training-center topic\n'
            f'"not_related" = clearly off-topic messages with no connection to the company or its services '
            f'(e.g. jokes, random chat, completely unrelated questions)\n'
            f'"other_request" = operational requests NOT about enrolling (e.g. VAT registration, certification follow-up, pitch scheduling)\n\n'
            f"When in doubt between \"general\" and \"not_related\", choose \"general\".\n\n"
            f"Recent user messages:\n{conversation_text}\n\n"
            f"Respond with ONLY the single most relevant category name from the list above. "
            f"No explanation, no punctuation, just the category string."
        )

        result = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=60,
        )

        raw = result.choices[0].message.content.strip().strip('"').strip("'")
        matched = next((c for c in category_list if c.lower() == raw.lower()), None)
        if matched:
            fb_user.conversation_topic = matched
            db.session.commit()
    except Exception as e:
        print(f"classify_conversation error for user {fb_user.id}: {e}")


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
        '0. ЭНЭ БОЛ ҮРГЭЛЖИЛСЭН ЯРИА (active session). Сүүлийн 2 цагийн дотор '
        'хэрэглэгчтэй ярилцсан тул хариултын ЭХНИЙ үгээс ШУУД асуултын хариу '
        'руу ор. "Сайн байна уу", "Өглөөний мэнд", "Тавтай морил", "Танд '
        'юугаар туслах вэ?", өөрийгөө дахин танилцуулах гэх мэт мэндчилгээний '
        'үгийг ХЭЗЭЭ Ч БҮҮ ОРУУЛ — энэ дүрэм персонал доторх мэндчилгээний '
        'дүрмийг дарж байна. Өмнөх ярианыхаа контекстийг санаж буй мэт товч, '
        'ноорог байдлаар үргэлжлүүл.'
    ),
    'gap': (
        '0. Хэрэглэгчтэй өнөөдөр аль хэдийн ярилцаад, цөөн цагийн дараа эргэж '
        'ирлээ. Дахин "Сайн байна уу" эсвэл "Өглөөний мэнд" гэж БҮҮ мэндэл. '
        'Шууд асуултанд найрсагаар хариул. Хэрэгтэй үед "Үргэлжлүүлээд тусалъя" '
        'эсвэл "Дахин ирсэнд таатай байна" гэх мэт богино гүүр хэллэг л ашигла.'
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
        'ХЭРЭГЛЭГЧИЙН ҮЕ ШАТ: Сонирхож буй (discovery шат).\n'
        'Эхний 1-2 өгүүлбэрээр сургалтын үндсэн давуу талыг товч танилцуул '
        '(100% практик, 4 долоо хоног, сертификат, төгсөгчдөд Magic Finance '
        'программын үнэгүй license г.м.).\n'
        'Дараа нь зайлшгүй 1 НЭЭЛТТЭЙ АСУУЛТ асууж тэдний хэрэгцээг ойлгох '
        'хэрэгтэй. Дараах асуултуудаас тохирохыг сонго, заавал нэгийг л асуу '
        '(нэг хариултанд 2-3 асуулт битгий тулга):\n'
        '  - "Та өмнө нь нягтлан/санхүүгийн чиглэлээр ажиллаж байсан уу?"\n'
        '  - "Танд танхимаар суух уу, эсвэл онлайнаар суух нь илүү тохиромжтой вэ?"\n'
        '  - "Та одоо ажил/сургуультай юу? Ямар цагт суух нь илүү таатай вэ?"\n'
        '  - "Та энэ сургалтыг яагаад үзэх гэж байгаа вэ — карьерийн өөрчлөлт, '
        'одоогийн ажил, эсвэл өөрийн бизнест?"\n'
        'ЭНЭ ШАТАНД ҮНЭ, БҮРТГЭЛИЙН ЛИНК, УТАС ЛАВЛАХ ХҮСЭЛТИЙГ БҮҮ ТУЛГА. '
        'Гол зорилго: хэрэглэгчийг сонсож, тэдэнд тохирох ангийг олж өгөх. '
        'Зөвхөн хэрэглэгч өөрөө "бүртгүүлмээр" эсвэл "холбогдмоор" гэж '
        'хэлсэн үед л дараагийн шат руу шилжинэ.'
    ),
    'exploring_courses': (
        'ХЭРЭГЛЭГЧИЙН ҮЕ ШАТ: Сургалт сонгож буй.\n'
        'Хэрэглэгчийн хүсэлтэд тохирох тодорхой ангийн агуулга, 4 долоо '
        'хоногийн хөтөлбөр, цаг хуваарь, танхим/онлайн хэлбэрийг тодруулж '
        'тайлбарла.\n'
        'Хэрэв хэрэглэгчийн нөхцөл (цаг, түвшин, хэлбэр) тодорхойгүй хэвээр '
        'байвал НЭГ ТОДРУУЛАХ АСУУЛТ нэм (жишээ: "Танд өглөө/орой/амралтын '
        'өдрийн алин тохиромжтой вэ?"). Аль анги нь яагаад тэдэнд тохирох '
        'талаар санал бодлоо хэлж, эцсийн шийдвэрийг хэрэглэгчид үлдээ.\n'
        'Үнийн дэлгэрэнгүй болон бүртгэлийн линкийг ЗӨВХӨН хэрэглэгч '
        'тодорхой "үнэ хэд?" эсвэл "яаж бүртгүүлэх вэ?" гэж асуусан үед өг.'
    ),
    'pricing': (
        'ХЭРЭГЛЭГЧИЙН ҮЕ ШАТ: Үнэ судалж буй. Дөрвөн ангийн үнэ, PocketZero-оор '
        '4-6 хуваан хүүгүй төлөх боломж, Magic Finance программын 6 сарын '
        'үнэгүй ашиглах эрхийг онцолж тайлбарла. Төсөвт нь тохирох '
        'хувилбарыг санал болго. Хэрэглэгч сонирхлоо илэрхийлбэл БҮРТГЭЛИЙН '
        'ЛИНК + утас зэрэг хоёр сонголтыг өг.'
    ),
    'ready': (
        'ХЭРЭГЛЭГЧИЙН ҮЕ ШАТ: Бүртгүүлэхэд бэлэн. Богино урамшуулал ("Маш сайн '
        'сонголт!") хийж, БҮРТГЭЛИЙН ЛИНК-ийг хариултдаа оруул. Эсвэл утасны '
        'дугаараа үлдээгээрэй гэж нэм — хэрэглэгчид хоёр сонголтыг өг.'
    ),
}


# ===================== SETTINGS GETTERS =====================

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


def get_main_office_address():
    """Fallback address used by BusinessLines that don't set their own.
    Single point of update when the head office moves."""
    return get_setting('main_office_address', '')


def get_main_office_phone():
    """Fallback general phone used by BusinessLines whose contact_info is
    blank (e.g. product lines that route through the central switchboard)."""
    return get_setting('main_office_phone', '')


ALLOWED_COURSE_TYPES = ('100% Online', 'Hybrid', 'Online with Teacher', 'Classroom')
SELF_PACED_COURSE_TYPE = '100% Online'


def get_handoff_sensitivity():
    """conservative | balanced | aggressive — read from settings, default conservative.

    NOTE: prior to the refactor this function's body had been accidentally
    pasted inside `trigger_handoff`, causing every handoff to crash with
    `NameError: get_handoff_sensitivity`. Restored to a real, callable function.
    """
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


# ===================== LLM PROMPT BUILDER =====================

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


def _format_product_entry(p):
    """One product under a business line, with its active ProductLinks
    listed beneath it. Each link's `kind` is preserved so the bot can
    semantically match user intent against it."""
    head = "  • "
    if p.product_number:
        head += f"[#{p.product_number}] "
    head += p.name
    if p.is_main_product:
        head += " ★main"
    if p.vendor:
        head += f" ({p.vendor})"
    lines = [head]
    if p.description:
        lines.append(f"    Тайлбар: {p.description}")
    if p.status_note:
        lines.append(f"    Тэмдэглэл: {p.status_note}")
    active_links = [l for l in p.links if l.is_active]
    if active_links:
        lines.append("    Холбоосууд:")
        for l in sorted(active_links, key=lambda x: (x.sort_order, x.id)):
            lines.append(f"      - {l.description}: {l.url}")
    return "\n".join(lines)


def _format_business_line_entry(b):
    """Single-line summary for a business line, with structured fields the
    admin filled in (address, sign-up channels, stats, child products)
    appended so the bot can quote them without parsing the freeform
    description. Address and main phone fall back to the global
    main_office_* settings when this line doesn't set its own."""
    parts = [f"- {b.name}"]
    if b.description:
        parts.append(f": {b.description}")

    extras = []
    if b.established_year:
        extras.append(f"үүсгэн байгуулагдсан: {b.established_year}")

    address = b.address or get_main_office_address()
    if address:
        # Tag whether the line is at its own address or the shared head
        # office, so the bot doesn't claim the line has a dedicated
        # address when it actually shares the main one.
        if b.address:
            extras.append(f"хаяг: {address}")
        else:
            extras.append(f"хаяг (Гол оффис): {address}")

    if b.contact_info:
        extras.append(f"холбоо барих: {b.contact_info}")
    else:
        main_phone = get_main_office_phone()
        if main_phone:
            extras.append(f"холбоо барих (Гол оффис): {main_phone}")

    if b.email:
        extras.append(f"имэйл: {b.email}")
    if extras:
        parts.append("\n  (" + "; ".join(extras) + ")")

    # Child Products (e.g. Magic Finance + Microsoft + Kaspersky under
    # the Program line). Sorted so the main product comes first.
    products = [p for p in (b.products or []) if p.is_active]
    if products:
        products.sort(key=lambda p: (not p.is_main_product, p.sort_order, p.id))
        parts.append("\n  Бүтээгдэхүүнүүд:\n" + "\n".join(_format_product_entry(p) for p in products))

    return "".join(parts)


def _format_business_lines():
    """Return (answer_block, refer_block, paused_block) — active lines split by
    action, plus a block for paused services so the bot knows not to sell them."""
    all_lines = (BusinessLine.query
                 .order_by(BusinessLine.sort_order.asc(), BusinessLine.id.asc())
                 .all())
    answer, refer, paused = [], [], []
    for b in all_lines:
        if not b.is_active:
            note = f" ({b.status_note})" if b.status_note else " (түр зогссон)"
            paused.append(f"- {b.name}{note}")
            continue
        entry = _format_business_line_entry(b)
        if (b.action or 'refer') == 'answer':
            answer.append(entry)
        else:
            refer.append(entry)
    return "\n".join(answer), "\n".join(refer), "\n".join(paused)


def _format_course_entry(c, today_date):
    """Single line for a course in the canonical catalog block. Self-paced
    courses (100% Online) skip start/end dates; courses with a known
    duration_days quote it; admins' status_note is appended verbatim."""
    head = f"- [#{c.course_number}] " if c.course_number else "- "
    head += f"{c.name} ({c.course_type}): {int(c.price):,}₮"

    bits = [f"цаг: {c.time}"]
    if c.course_type != SELF_PACED_COURSE_TYPE:
        if c.start_date:
            bits.append(f"эхлэх: {c.start_date.strftime('%Y-%m-%d')}")
        if c.end_date:
            bits.append(f"дуусах: {c.end_date.strftime('%Y-%m-%d')}")
    else:
        bits.append("эхлэх: бүртгүүлсэн өдрөөс")
    if c.duration_days:
        bits.append(f"үргэлжлэх хугацаа: {c.duration_days} хоног")
    line = head + " | " + ", ".join(bits)
    if c.description:
        line += f"\n  Тайлбар: {c.description}"
    if c.status_note:
        line += f"\n  Тэмдэглэл: {c.status_note}"
    return line


def _format_courses_canonical():
    """Build the canonical course catalog block. Filters out non-recurring
    courses whose start_date is already in the past — they're stale and the
    bot should not be quoting them as upcoming. Sorts ascending so the
    nearest class is offered first."""
    today = datetime.utcnow().date()
    active = Course.query.filter_by(is_active=True).all()

    def is_quotable(c):
        if c.course_type == SELF_PACED_COURSE_TYPE:
            return True  # self-paced has no start date to be stale against
        if c.is_recurring:
            return True  # the refresh_dates flow keeps these current
        if not c.start_date:
            return True
        return c.start_date.date() >= today

    def sort_key(c):
        # Nearest upcoming start first; self-paced/no-date sort last.
        if not c.start_date:
            return (1, datetime.max)
        return (0, c.start_date)

    quotable = sorted([c for c in active if is_quotable(c)], key=sort_key)
    inactive_or_stale = [c for c in active if not is_quotable(c)]

    active_text = "\n".join(_format_course_entry(c, today) for c in quotable)

    paused_or_stale = []
    inactive = Course.query.filter_by(is_active=False).all()
    for c in inactive + inactive_or_stale:
        note = c.status_note or "түр зогссон"
        tag = f"#{c.course_number} " if c.course_number else ""
        paused_or_stale.append(f"- {tag}{c.name}: {note}")

    return active_text, "\n".join(paused_or_stale)


def _format_current_time_block():
    """Render a 'CURRENT TIME' block for the system prompt so the LLM can
    apply time-of-day rules in the persona (greetings, off-hours hints,
    "what time is it?" answers) against a real value instead of guessing.

    Server runs in UTC; Mongolia is UTC+8 with no DST, so we shift by 8.
    The block also names which bucket the persona rules apply to so the
    model doesn't have to map the hour itself."""
    now_utc = datetime.utcnow()
    ub_hour = (now_utc.hour + 8) % 24
    ub_minute = now_utc.minute
    days_mn = ('Даваа', 'Мягмар', 'Лхагва', 'Пүрэв', 'Баасан', 'Бямба', 'Ням')
    weekday_mn = days_mn[now_utc.weekday()]
    if 6 <= ub_hour < 12:
        bucket = 'өглөө (06:00-12:00)'
    elif 12 <= ub_hour < 17:
        bucket = 'өдөр (12:00-17:00)'
    elif 17 <= ub_hour < 23:
        bucket = 'орой (17:00-23:00)'
    else:
        bucket = 'шөнө (23:00-06:00)'
    return (
        f"ОДООГИЙН ЦАГ (Улаанбаатарын цаг, UTC+8):\n"
        f"  {weekday_mn} гариг, {ub_hour:02d}:{ub_minute:02d}\n"
        f"  Цагийн ангилал: {bucket}\n"
        f"  Энэ цагт тохирох мэндчилгээг персонал дахь дүрмийн дагуу сонгоно — "
        f"ЗӨВХӨН чатын ЭХНИЙ хариултанд (session_state='new' буюу 'returning' "
        f"үед) л мэндэл. Үргэлжилсэн ярианы ДОТОР дахин 'сайн байна уу' гэх "
        f"мэт мэндчилгээг БҮҮ ДАВТА — хэрэглэгчид нэг сесст нэг л удаа мэндэлнэ. "
        f"Хэрэв хэрэглэгч 'одоо хэдэн цаг вэ?' гэж асуувал дээрх цагийг ашиглан хариулна.\n"
    )


def build_system_prompt(session_state='new', funnel_stage='curious', user_first_name=''):
    """Build system prompt with training, FAQ, session-state and funnel context."""
    training = get_training_content()
    persona = get_bot_persona()
    current_time_block = _format_current_time_block()
    faqs = FAQ.query.all()
    faq_text = "\n".join([f"Q: {faq.question}\nA: {faq.answer}" for faq in faqs])

    courses_text, paused_courses = _format_courses_canonical()

    snippets_text = _format_training_snippets()
    team_text = _format_team_members()
    answer_lines, refer_lines, paused_services = _format_business_lines()

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
                "Доорх үйлчилгээ — ТОВЧ хариулж болно. Хэрэглэгч асуухад "
                "доорх тайлбараас 1-2 өгүүлбэр бичээд, дэлгэрэнгүй үнэ, "
                "цагийн хувиараа ажилтнаас тодруулахыг санал болгоно:\n"
                f"{answer_lines}\n"
            )
        if refer_lines:
            biz_section += (
                "Доорх үйлчилгээний талаар хэрэглэгч асуувал ДАРААХ ДЭС "
                "ДАРААЛАЛЫГ БАРИМТАЛНА:\n"
                "  1) Доорх жагсаалтаас тухайн үйлчилгээний тайлбарыг "
                "ашиглан 1-2 богино өгүүлбэрээр \"Тийм ээ, бид энэ "
                "үйлчилгээг үзүүлдэг — ...\" гэж баталгаажуулна.\n"
                "  2) \"Сонирхож байна уу? Дэлгэрэнгүйг танд илгээх "
                "үү?\" гэж тодруулна.\n"
                "  3) Хэрэглэгч сонирхож байгаагаа илэрхийлбэл "
                "БҮРТГЭЛИЙН ЛИНК-ийг өгөөд \"Эсвэл утасны дугаараа "
                "үлдээвэл манай ажилтан танд эргэж холбогдоно\" гэж "
                "нэм. Энэ хоёрын аль нэгийг хэрэглэгчид сонгох "
                "боломж өг — заавал утас лавлахгүй.\n"
                "  4) Үнэ, тусгай нөхцөлийн дэлгэрэнгүй асуулт ирвэл "
                "ажилтан хариулна гэж зөвлөнө.\n"
                f"{refer_lines}\n"
            )
        if paused_services:
            biz_section += (
                "Доорх үйлчилгээнүүд ОДООГООР ТҮР ЗОГССОН байна. "
                "Хэрэглэгч эдгээрийн талаар асуувал зогссон гэдгийг шулуун хэлж, "
                "дахин нээгдэх үед мэдэгдэж болох эсэхийг тодруулна уу:\n"
                f"{paused_services}\n"
            )

    allowed_types_line = ", ".join(f'"{t}"' for t in ALLOWED_COURSE_TYPES)
    paused_courses_section = (
        f"\nТҮР ЗОГССОН / ХУУЧИРСАН АНГИУД (бүртгэл нээлттэй биш; "
        f"хэрэглэгч асуувал зогссон гэдгийг шулуун хэлж, эргэж нээгдэх үед "
        f"мэдэгдэнэ гэж хэлж болно):\n{paused_courses}\n"
        if paused_courses else ''
    )

    system_prompt = f"""{persona}

{current_time_block}
==========================
ИДЭВХТЭЙ АНГИУДЫН АЛБАН ЁСНЫ ЖАГСААЛТ (CANONICAL — энэ жагсаалтаас л үнэн зөв):
{courses_text}
{paused_courses_section}
ЭНЭ ЖАГСААЛТЫН ТУХАЙ ХАТУУ ДҮРЭМ:
- Ангийн нэр, цаг, эхлэх/дуусах огноо, үнэ, үргэлжлэх хугацааны тухай асуулт ирвэл ЗӨВХӨН энэ жагсаалтаас хариул.
- Жагсаалтад байхгүй цаг, үнэ, эсвэл огноо бүү зохио. Эргэлзвэл "Ажилтан танд эргэж лавлая" гэж хэлэн утас лав.
- Ангийн төрөл нь дараах нэрсээс л байна: {allowed_types_line}.
- Хэрэглэгч одоогийн ангийн цаг, огноо тохирохгүй гэвэл ЭНЭ ЖАГСААЛТААС өөр төрлийн (онлайн, оройн г.м.) эсвэл дараагийн нээгдсэн ангийг санал болго. Нэг анги л санал болгож үзээгүй мөртөө "өөр анги байхгүй" битгий хэл.
- Анги тус бүрд [#NNNN] гэсэн дугаар бий бол хариултдаа дурдвал админд хяналт тавихад тус болно (хэрэглэгчид заавал биш).
==========================

СУРГАЛТЫН ТӨВИЙН ЕРӨНХИЙ МЭДЭЭЛЭЛ:
{training}
{snippets_section}{team_section}{biz_section}
ТҮГЭЭМЭЛ АСУУЛТУУД:
{faq_text}

{registration_block}{name_block}{funnel_rule}

БОРЛУУЛАЛТЫН ЗАН ҮЙЛ (туршлагатай зөвлөгчийн загвар — найрсаг, тулгахгүй):

A. ИДЭВХТЭЙ СОНСОЛТ:
   Хэрэглэгчийн өгсөн гол үг/санааг хариултынхаа эхэнд 1 өгүүлбэрээр буцааж
   тодорхой болго ("Ойлголоо, та ... гэж хэлж байна"). Энэ нь хэрэглэгчийг
   "сонссон" гэж мэдрүүлэх ба үргэлжлүүлэхэд итгэл төрүүлнэ. Зөвхөн хэрэглэгч
   тодорхой санаа эсвэл нөхцөл бичсэн үед хэрэглэ — нэг үгийн мэндчилгээнд бүү
   ашигла.

B. ТОДОРХОЙ ӨВДӨЛТ/СААД ОЛОХ:
   Хариултыг бэлдэхээсээ өмнө "энэ хэрэглэгчийн ЯМАР асуудлыг шийдэхийг
   хүсэж байна?" гэж бод. Илэрхий биш бол шууд асуулт асуу — "Танд ямар
   асуудалд шийдэл хайж байна?" эсвэл "Энэ сургалт танд яагаад хэрэгтэй
   гэж бодов?". Зөв асуулт нь зөв шийдлээс илүү чухал.

C. ЭСРЭГ ХЭЛЛЭГ (objection handling) — ХЭРЭГЛЭГЧ ИЛЭРХИЙЛВЭЛ ЗАЙЛШГҮЙ
   ХАРИУЛНА:
   • "Үнэтэй / yntei / mungugui" → PocketZero-оор 4-6 хуваан хүүгүй төлж
     болохыг сануулж, Magic Finance программын 6 сар-1 жилийн ҮНЭГҮЙ
     license-ийг үнэ дэх давуу талаар онцол. "Сард 50,000₮ орчим
     өгөхөд тохиромжтой юу?" гэх мэт хувааж бод.
   • "Цаггүй / tsaggui / завгүй" → "100% Online" эсвэл оройн анги
     санал болго. Бие даан хийх хувилбарыг тайлбарла.
   • "Бодоод үзье / bodood uzye / эргэлзэж байна" → ШАХАЖ БҮҮ ОРУУЛ.
     "Мэдээж, чухал шийдвэр. Эргэлзээ юунд нь байгаа вэ?" гэх мэтээр
     тодорхойлох асуулт асуу. Хэрэв тэр шалтгаан нь дээрх objection-ы
     аль нэг бол түүнийг шийдэж өг.
   • "Дараа бүртгүүлнэ / daraa burtguulne" → дараагийн нээгдэх ангийн
     огноог тодорхой хэлээд "тэр үед сануулга илгээх үү?" гэж зөөлөн
     санал болго. Ангиудын онлайн хувилбар нь хүссэн үед эхлэх
     боломжтой гэж сануул.
   • "Танхим хол / tankhim khol / алслагдсан" → Hybrid эсвэл 100%
     Online хувилбарыг тайлбарлаж, тэр оршин суугаа газраас огт явахгүйгээр
     суух боломжийг онцол.
   • "Ажилтай / aiijiltai / busy" → оройн / weekend / онлайн хувилбар
     санал болго. Ажил эрхэлж байгаа хүмүүст таарах гэж онцолсон ангиудыг
     заана.

D. НИЙГМИЙН БАТАЛГАА (social proof) — БУУ ЗОХИО:
   Зөвхөн дээрх "СУРГАЛТЫН ТӨВИЙН ЕРӨНХИЙ МЭДЭЭЛЭЛ" блок, "НЭМЭЛТ
   ТАЙЛБАР, ТОДРУУЛГА" snippet-ууд, эсвэл "ТҮГЭЭМЭЛ АСУУЛТУУД" хэсэгт бий
   нь тодорхой тоо, төгсөгчийн түүх, бодит баримтыг иш тат. Дээрх блокт
   байхгүй бол ҮЛ ЗОХИО — "олон төгсөгч ажиллаж байна" гэх мэт ерөнхий
   үг ашиглаж болох ч тодорхой тоо бүү гарга.

E. TRIAL CLOSE — exploring_courses → pricing шилжих үе:
   Хэрэглэгчийн нөхцөл (цаг, түвшин, хэлбэр)-ийн дор хаяж 2-ыг ойлгосон
   үед ЗӨӨЛӨН тестийн асуулт асуу: "Хэрэв хуваариар таарвал [санал
   болгож буй ангид] суух сонирхолтой юу?" эсвэл "Үнэ, хуваариа таарах
   юм бол энэ анги танд хэр зэрэг тохирох вэ?". Хариу "тийм/яриад үзье"
   бол үнэ + бүртгэлийн линкийг өг. Хариу "үгүй/эргэлзэж байна" бол
   эсрэг хэллэгийн логикоор зөв objection-ыг олж тодор.

F. ӨМНӨХ ХАРИЛЦАА ГҮН:
   Хэрэглэгч өмнө нь юу хэлснийг иш татаж, шинэ асуултыг тэр контекстээр
   баяжуул. Жишээ: "Та эхэндээ ажилтай гэж хэлсэн — тиймээс оройн анги
   танд илүү тохиромжтой байж магадгүй." Богино санах ой = итгэлцлийн
   суурь.

ЧУХАЛ ДҮРМҮҮД:
{session_rule}
1. Хариултыг товч (3 өгүүлбэрээс ихгүй) бөгөөд тодорхой бичнэ. Урт жагсаалт оруулахаас зайлсхий.
2. Сургалтын давуу талыг хэт зар сурталчилгаа маягтай бус, итгэлтэй найз шиг зөвлөнө.
3. БҮРТГЭЛИЙН ЛИНК + УТАС ЗЭРЭГ САНАЛ — ЗӨВХӨН ДАРААХ ҮЕД АШИГЛА:
   (а) Хэрэглэгч өөрөө "бүртгүүлмээр", "холбогдмоор", "ажилтантай яримаар" гэх мэт тодорхой шилжих хүсэл илэрхийлсэн;
   (б) Хэрэглэгч pricing/ready үе шатанд ороод үнийн дэлгэрэнгүй асуусан;
   (в) Сургалтаас өөр чиглэлийн үйлчилгээ (audit, consulting, бүтээгдэхүүн г.м.)-ийн талаарх асуулт, хариулт нь "manai mergejilten" зэрэг чиглүүлгийн хариулт байх үед.
   Эдгээр үед БҮРТГЭЛИЙН ЛИНК (байвал) + "эсвэл утасны дугаараа үлдээгээрэй" гэсэн хоёр сонголтыг ЗЭРЭГ өг.
   ДИСКАВЕРИ-Н (curious/exploring_courses) шатанд утас/линк бүү тулга — энэ үед бид нээлттэй асуулт асуудаг, хэрэглэгчийн хэрэгцээг ойлгох зорилготой. Funnel rule-ийг баримтал.
4. Хэрэглэгч утасны дугаар бичсэн бол баярлал илэрхийлж, "Манай ажилтан удахгүй тантай холбогдоно" гэж мэдэгд.
5. Шийдэх боломжгүй буюу мэдэхгүй асуудал тулгарвал "Энэ асуудлыг манай ажилтан тантай эргэж холбогдож тодруулна" гэж хэлээд Дүрэм 3-ын дагуу хоёр сонголтыг өг.
6. Эмодзи цөөн (1-2) хэрэглэж, илүү гар бичмэл маяг бүү аватарла.
7. Өгүүлбэрүүдийн эхлэлийг сольж бай, "Сургалт..." гэх мэт ижил үгээр дандаа бүү эхэл.
8. Сургалтаас өөр чиглэлийн (компанийн бусад үйлчилгээ) асуултанд дээрх "КОМПАНИЙН БУСАД ҮЙЛЧИЛГЭЭ" хэсгийн дүрмийн дагуу хариулна. Жагсаалтад байхгүй чиглэл бол "Тантай ажилтан холбогдох уу?" гэж асууж дугаар лав.
9. БҮТЭЭГДЭХҮҮНИЙ ДҮРЭМ: Бот бүтээгдэхүүн (Magic Finance, Microsoft license, Kaspersky г.м.)-ийн үнэ, санал хэлэхгүй. Худалдан авах, дэмжлэг (ticket), гарын авлага (manual), community, татах (download) гэх мэт асуулт ирэхэд тухайн бүтээгдэхүүний "Холбоосууд" жагсаалтаас хэрэглэгчийн санаатай таарах нэгийг сонгож, тэр линкийг өгөөд "манай ажилтан тантай холбогдож үнэ, нөхцөл нь дэлгэрэнгүй хэлэлцэнэ" гэж нэм. Жагсаалтад тохирох холбоос байхгүй бол үнэ зохиохгүйгээр ажилтантай холбогдох санал тавь.
10. ҮЙЛЧИЛГЭЭГ ҮГҮЙСГЭХГҮЙ ДҮРЭМ:
   (а) Хэрэглэгчийн асуусан үйлчилгээ дээрх "КОМПАНИЙН БУСАД ҮЙЛЧИЛГЭЭ" жагсаалтад БАЙВАЛ — тэндхийн тайлбарыг ашиглан 1-2 өгүүлбэрээр баталгаажуул, дэс дарааллын дагуу үргэлжлүүл. ЗААВАЛ утас лавлахаас өмнө сонирхол тодруул.
   (б) Хэрэглэгчийн асуусан үйлчилгээ жагсаалтад БАЙХГҮЙ бол "бид санал болгохгүй", "манай төвд байхгүй", "энэ үйлчилгээ байхгүй" гэж ХЭЗЭЭ Ч БҮҮ ХЭЛ. Үүний оронд: "Энэ талаар манай мэргэжлийн ажилтан танд дэлгэрэнгүй хариулна — утасны дугаараа үлдээх үү?" гэж эелдгээр хариул.
   (в) "ТҮР ЗОГССОН" гэж тэмдэглэгдсэн үйлчилгээний хувьд "одоогоор зогссон" гэж шулуун хэл, дахин нээгдэх үед мэдэгдэж болохыг санал болго."""
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


# ===================== HANDOFF FLOW =====================

HANDOFF_USER_REPLY = (
    "Таны асуултыг манай ажилтан хариуцаж авлаа. Удахгүй холбогдох болно. "
    "Түр хүлээгээрэй 🙏"
)
HANDOFF_USER_REPLY_OFF_HOURS = (
    "Таны мессежийг хүлээн авлаа. Одоогоор ажлын цаг биш байна. "
    "Манай ажилтан маргааш ажлын цагт ({start}:00 – {end}:00) тантай холбогдох болно. "
    "Уучлаарай, тав тухтай амраарай 🌙"
)


def _is_off_hours():
    """Return True when current UTC hour falls in the night quiet window.
    Office hours stored as 'office_hours_start' and 'office_hours_end' (int, default 8–22)."""
    try:
        start = int(get_setting('office_hours_start', '8') or '8')
        end = int(get_setting('office_hours_end', '22') or '22')
    except ValueError:
        start, end = 8, 22
    ub_hour = (datetime.utcnow().hour + 8) % 24
    if start <= end:
        return not (start <= ub_hour < end)
    return end <= ub_hour < start


def _matches_refer_business_line(text):
    """True if the user message clearly names a business line flagged 'refer'."""
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
    """Decide whether the bot should escalate to a human."""
    if not message_text:
        return False, ''
    text = message_text.lower()
    sensitivity = get_handoff_sensitivity()

    explicit_kws = [
        k.keyword.lower() for k in
        HandoffKeyword.query.filter_by(keyword_type='explicit', is_active=True).all()
    ]
    frustration_kws = [
        k.keyword.lower() for k in
        HandoffKeyword.query.filter_by(keyword_type='frustration', is_active=True).all()
    ]

    for kw in explicit_kws:
        if kw in text:
            return True, f"explicit:{kw}"

    if _matches_refer_business_line(message_text):
        return True, 'business_line_refer'

    if sensitivity in ('balanced', 'aggressive'):
        for kw in frustration_kws:
            if kw in text:
                return True, f"frustration:{kw}"

    if sensitivity == 'aggressive':
        cutoff = datetime.utcnow() - timedelta(hours=1)
        recent = (Message.query
                  .filter_by(facebook_user_id=fb_user.id, sender='user')
                  .filter(Message.created_at >= cutoff)
                  .order_by(Message.created_at.desc())
                  .limit(5)
                  .all())
        same = [m for m in recent if (m.content or '').strip().lower() == text.strip()]
        if len(same) >= 2:
            return True, 'repeated_question'

    return False, ''


def send_telegram_notification(text):
    """Send a Telegram message to every configured chat ID. Silent no-op if
    TELEGRAM_BOT_TOKEN is not set or no chat IDs are configured."""
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


# Tiny in-memory TTL cache for the polling endpoint.
_handoff_poll_cache = {'expires_at': 0.0, 'payload': None}
_HANDOFF_POLL_TTL = 2.0


def get_handoff_poll_payload():
    """Build the polling payload for /admin/api/handoff-poll, with 2s TTL cache."""
    now = time.time()
    if _handoff_poll_cache['payload'] is not None and now < _handoff_poll_cache['expires_at']:
        return _handoff_poll_cache['payload']
    open_q = AdminIssue.query.filter_by(issue_type='handoff', status='open')
    latest = open_q.order_by(AdminIssue.created_at.desc()).first()
    payload = {
        'open_count': open_q.count(),
        'latest_id': latest.id if latest else None,
        'latest_at': latest.created_at.isoformat() if latest else None,
        'sound_enabled': get_sound_alerts_enabled(),
    }
    _handoff_poll_cache['payload'] = payload
    _handoff_poll_cache['expires_at'] = now + _HANDOFF_POLL_TTL
    return payload


def invalidate_handoff_poll_cache():
    """Drop the cached poll payload so the next request rebuilds it."""
    _handoff_poll_cache['payload'] = None
    _handoff_poll_cache['expires_at'] = 0.0


def trigger_handoff(fb_user, reason, user_message, send_user_message=True):
    """Run the full handoff flow: mute the bot for the configured window,
    create an AdminIssue, ping admins via Telegram, optionally send the
    user a polite waiting message. Safe to call inside the webhook handler.

    Pass `send_user_message=False` when the bot has already sent the
    customer a deferring reply (see _bot_response_implies_handoff) so the
    user doesn't receive two back-to-back messages."""
    hours = get_mute_duration_hours()
    if hours > 0:
        fb_user.bot_muted_until = datetime.utcnow() + timedelta(hours=hours)

    off_hours = _is_off_hours()

    issue = AdminIssue(
        facebook_user_id=fb_user.id,
        issue_type='handoff',
        content=f"[{reason}]{'[OFF-HOURS]' if off_hours else ''} {user_message}"[:4000],
        status='open',
    )
    db.session.add(issue)
    db.session.commit()

    invalidate_handoff_poll_cache()

    if send_user_message:
        if off_hours:
            try:
                oh_start = int(get_setting('office_hours_start', '8') or '8')
                oh_end = int(get_setting('office_hours_end', '22') or '22')
            except ValueError:
                oh_start, oh_end = 8, 22
            user_reply = HANDOFF_USER_REPLY_OFF_HOURS.format(start=oh_start, end=oh_end)
        else:
            user_reply = HANDOFF_USER_REPLY
        send_facebook_message(fb_user.facebook_id, user_reply)
        db.session.add(Message(
            facebook_user_id=fb_user.id,
            sender='bot',
            content=user_reply,
        ))
        db.session.commit()

    display_name = fb_user.name or fb_user.facebook_id
    phone_part = f"\n📞 {fb_user.phone}" if fb_user.phone else ''
    off_hours_tag = "🌙 [OFF-HOURS] " if off_hours else ""
    tg_text = (
        f"{off_hours_tag}🤝 Шинэ handoff хүсэлт\n"
        f"👤 {display_name}{phone_part}\n"
        f"📝 Шалтгаан: {reason}\n"
        f"💬 Мессеж: {user_message[:500]}\n\n"
        f"Facebook Page Inbox дээрээс хариулна уу."
    )
    send_telegram_notification(tg_text)
    return issue


# Phrases that indicate the bot is deferring to staff. If the bot's own
# response contains any of these, treat it as an implicit handoff (mute
# the bot + create AdminIssue + Telegram ping) WITHOUT sending an extra
# user-facing message — the bot's deferring reply IS the user message.
_BOT_DEFERRAL_PHRASES = (
    'мэргэжлийн ажилтан',
    'манай ажилтан танд',
    'манай ажилтан удахгүй',
    'ажилтан танд эргэж',
    'ажилтан тантай эргэж',
    'ажилтантай холбог',
    'тусгай ажилтан',
)


def bot_response_implies_handoff(bot_response):
    """Return the matched phrase if the bot's reply means "I am deferring
    to a human", else None. Used by the webhook to fire a handoff even
    when the user's message didn't match an explicit keyword (e.g. the
    customer asked about an unknown service and the bot chose to defer
    per Rule 10б of the system prompt)."""
    if not bot_response:
        return None
    text = bot_response.lower()
    for phrase in _BOT_DEFERRAL_PHRASES:
        if phrase in text:
            return phrase
    return None


# ===================== AUDIT LOG =====================

def log_admin_action(action, entity_type=None, entity_id=None, entity_label=None, detail=None):
    """Append an entry to the audit log. Safe to call from any admin route —
    swallows errors so a logging failure can't break the user-facing action.

    Call AFTER the primary db.session.commit() succeeds; this writes its own
    row in a fresh add+commit so a failed audit insert doesn't roll back the
    real change."""
    try:
        if not current_user.is_authenticated:
            return
        entry = AuditEntry(
            actor_id=current_user.id,
            actor_username=current_user.username,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_label=(entity_label or '')[:255] if entity_label else None,
            detail=detail,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"log_admin_action failed for {action!r}: {e}")


# ===================== TRAINING-DATA LINTER =====================

# Module-level regexes used by lint_training_data() to spot prices / phones /
# URLs that drift between unstructured prose and the structured catalogs.
_PRICE_RE = re.compile(r'(\d{1,3}(?:[, ]\d{3})+|\d{4,})\s*₮')
_PHONE_RE = re.compile(r'\b\d{4}[\s-]?\d{4}\b')
_URL_RE = re.compile(r'https?://[^\s)"\'<>]+')
_PRICE_THRESHOLD = 1000  # ignore tiny numbers like "100% Online — 100"


def _normalize_phone(s):
    return re.sub(r'[\s-]', '', s)


def _normalize_int(s):
    return int(re.sub(r'[^\d]', '', s))


def lint_training_data():
    """Cross-reference unstructured prose (FAQ answers, persona, training
    intro, snippets) against the structured catalogs (Course, BusinessLine).

    Flags prices/phones/URLs that show up in prose but don't match any
    structured field — usually a sign the prose got out of sync with the
    catalog. Returns a list of findings ordered worst-first."""
    findings = []

    course_prices = {int(c.price) for c in Course.query.all() if c.price}
    biz_phones = set()
    biz_urls = set()
    biz_text_prices = set()
    # Pull authoritative phone numbers + URLs the bot is allowed to quote.
    # Sources: per-BU contact_info, plus every active ProductLink under each BU.
    # Used by the lint check below to flag text references that don't match.
    for b in BusinessLine.query.all():
        for m in _PHONE_RE.finditer(b.contact_info or ''):
            biz_phones.add(_normalize_phone(m.group()))
        if b.description:
            for m in _PRICE_RE.finditer(b.description):
                try:
                    biz_text_prices.add(_normalize_int(m.group(1)))
                except ValueError:
                    pass
        for p in (b.products or []):
            for l in (p.links or []):
                if l.is_active and l.url:
                    biz_urls.add(l.url.strip())

    expected_prices = course_prices | biz_text_prices

    sources = []
    for faq in FAQ.query.all():
        sources.append(('FAQ', faq.id, faq.question or '', faq.answer or ''))
    sources.append(('Persona', 0, 'bot_persona', get_bot_persona()))
    sources.append(('Training', 0, 'training_content', get_training_content()))
    for s in TrainingSnippet.query.filter_by(is_active=True).all():
        sources.append(('Snippet', s.id, s.title or '', s.body or ''))

    for kind, sid, label, text in sources:
        if not text:
            continue
        for m in _PRICE_RE.finditer(text):
            try:
                value = _normalize_int(m.group(1))
            except ValueError:
                continue
            if value < _PRICE_THRESHOLD:
                continue
            if value not in expected_prices:
                findings.append({
                    'severity': 'warning',
                    'kind': 'price_drift',
                    'source_kind': kind,
                    'source_id': sid,
                    'label': label[:80],
                    'detail': (
                        f'"{m.group()}" гэсэн үнэ ангийн каталог болон '
                        'үйлчилгээний тайлбарт олдсонгүй.'
                    ),
                })
        for m in _PHONE_RE.finditer(text):
            ph_norm = _normalize_phone(m.group())
            if ph_norm not in biz_phones:
                findings.append({
                    'severity': 'warning',
                    'kind': 'phone_drift',
                    'source_kind': kind,
                    'source_id': sid,
                    'label': label[:80],
                    'detail': (
                        f'"{m.group()}" дугаар үйлчилгээний contact_info-д '
                        'бүртгэлтэй биш.'
                    ),
                })
        for m in _URL_RE.finditer(text):
            url = m.group().rstrip('.,);')
            if url not in biz_urls:
                findings.append({
                    'severity': 'info',
                    'kind': 'url_drift',
                    'source_kind': kind,
                    'source_id': sid,
                    'label': label[:80],
                    'detail': (
                        f'{url} линк бизнесийн чиглэлийн product_link-д '
                        'бүртгэлтэй биш.'
                    ),
                })

    today = datetime.utcnow().date()
    for c in Course.query.filter_by(is_active=True).all():
        if c.course_type != SELF_PACED_COURSE_TYPE and not c.is_recurring:
            ref = c.end_date or c.start_date
            if ref and ref.date() < today:
                findings.append({
                    'severity': 'error',
                    'kind': 'stale_course',
                    'source_kind': 'Course',
                    'source_id': c.id,
                    'label': c.name[:80],
                    'detail': (
                        f'Идэвхтэй боловч огноо ({ref.strftime("%Y-%m-%d")}) өнгөрсөн. '
                        '"Огноо шинэчлэх / Архивлах" товчийг дарна уу.'
                    ),
                })
        if c.course_type == SELF_PACED_COURSE_TYPE and (c.start_date or c.end_date):
            findings.append({
                'severity': 'warning',
                'kind': 'self_paced_has_dates',
                'source_kind': 'Course',
                'source_id': c.id,
                'label': c.name[:80],
                'detail': '100% Online анги нь огноотой байх ёсгүй. Засаж дахин хадгална уу.',
            })

    severity_order = {'error': 0, 'warning': 1, 'info': 2}
    findings.sort(key=lambda f: (severity_order.get(f['severity'], 9),
                                  f['source_kind'], f['source_id']))
    return findings


# ===================== SEED / RECURRING =====================

def advance_recurring_courses():
    """For each is_recurring Course past its end_date, set next start/end dates.
    First Monday >= 4 weeks after current end_date."""
    now = datetime.utcnow()
    updated = 0
    for course in Course.query.filter_by(is_recurring=True, is_active=True).all():
        ref_date = course.end_date or course.start_date
        if not ref_date or ref_date > now:
            continue
        candidate = ref_date + timedelta(weeks=4)
        days_until_monday = (7 - candidate.weekday()) % 7
        next_start = candidate + timedelta(days=days_until_monday)
        if course.end_date and course.start_date:
            duration = course.end_date - course.start_date
        else:
            duration = timedelta(weeks=4)
        course.start_date = next_start
        course.end_date = next_start + duration
        updated += 1
    if updated:
        db.session.commit()
        print(f"Recurring courses: advanced {updated} course(s).")
    return updated


def archive_past_courses():
    """Flip non-recurring, non-self-paced active Courses whose end_date (or
    start_date when no end is set) has already passed to is_active=False.

    Run from the admin "Refresh dates" button so a one-click pass keeps the
    bot from quoting last month's classroom session as if it were upcoming.
    Returns how many courses were flipped."""
    now = datetime.utcnow()
    archived = 0
    candidates = Course.query.filter_by(is_active=True, is_recurring=False).all()
    for course in candidates:
        if course.course_type == SELF_PACED_COURSE_TYPE:
            continue
        reference = course.end_date or course.start_date
        if not reference:
            continue
        if reference < now:
            course.is_active = False
            if not course.status_note:
                course.status_note = (
                    f"Автомат архивлагдсан ({reference.strftime('%Y-%m-%d')}-ны "
                    "огноо өнгөрсний дараа). Шинээр нээх бол огноог шинэчилнэ үү."
                )
            archived += 1
    if archived:
        db.session.commit()
        print(f"Archived {archived} past course(s).")
    return archived


def seed_products():
    """Seed 3 starter Products under the Program / Magic Finance business
    line so admins have rows to edit-in-place rather than create from
    scratch. Idempotent: skipped when Products already exist, or when
    SEED_DEFAULTS=false (i.e. non-MagicBot deployments)."""
    if os.environ.get('SEED_DEFAULTS', 'true').strip().lower() != 'true':
        return
    if Product.query.count() > 0:
        return

    # Match in Python because SQLite's ILIKE doesn't case-fold Cyrillic
    # correctly, which would otherwise miss names like "Программ ба License".
    needles = ('magic finance', 'программ', 'software', 'magic cloud')
    program_line = None
    for line in BusinessLine.query.all():
        if any(n in (line.name or '').lower() for n in needles):
            program_line = line
            break
    if not program_line:
        print("seed_products: no program/software BusinessLine found — skipping.")
        return

    starters = [
        {
            'product_number': 2001,
            'name': 'Magic Finance',
            'vendor': 'Magic Cloud LLC',
            'description': (
                'Санхүү, татварын тайлан гаргахад зориулсан программ. 90 орчим '
                'төрлийн тайлан гаргах боломжтой (Санхүүгийн, Татварын, '
                'Удирдлагын, Туслах). Сургалтын төгсөгчдөд үнэгүй license '
                'олгоно: Танхим (100% танхим) ба Багштай онлайн → 1 жил; '
                'Хосолсон ба 100% Онлайн → 6 сар.'
            ),
            'is_main_product': True,
            'sort_order': 0,
        },
        {
            'product_number': 2002,
            'name': 'Microsoft license',
            'vendor': 'Microsoft',
            'description': 'Microsoft программ хангамжийн license. Дэлгэрэнгүйг ажилтнаас лавлана уу.',
            'is_main_product': False,
            'sort_order': 1,
        },
        {
            'product_number': 2003,
            'name': 'Kaspersky license',
            'vendor': 'Kaspersky',
            'description': 'Kaspersky аюулгүй байдлын программ хангамжийн license. Дэлгэрэнгүйг ажилтнаас лавлана уу.',
            'is_main_product': False,
            'sort_order': 2,
        },
    ]
    for s in starters:
        db.session.add(Product(business_line_id=program_line.id, **s))
    db.session.commit()
    print(f"Seeded {len(starters)} starter products under '{program_line.name}'.")


_DEFAULT_HANDOFF_KEYWORDS_EXPLICIT = [
    # Cyrillic
    'ажилтан', 'оператор', 'менежер', 'админтай', 'жинхэнэ хүн',
    'хүнтэй', 'хүн рүү', 'хүн руу', 'хүний хариу', 'хүнээр',
    'хүнтэй яр', 'хүнтэй ярь', 'хүнтэй холб',
    # Latin transliterations users commonly type from a phone without
    # the Mongolian keyboard layout. Substrings work because the matcher
    # does `if keyword in lower(message_text)`.
    'azhiltan', 'azhilten', 'operator', 'menejer', 'admintai',
    'hun-tei', 'huntei', 'hun ruu', 'hun-ruu', 'hunii hariu',
    'huntei yar', 'huntei chatla', 'huntei chin chatla',
    # English equivalents
    'live agent', 'real agent', 'real person', 'human agent',
    'speak to human', 'talk to human', 'talk to a person',
    'operator please', 'real human',
]
_DEFAULT_HANDOFF_KEYWORDS_FRUSTRATION = [
    'болохгүй байна', 'ойлгохгүй', 'ойлгомжгүй', 'муухай', 'үнэхээр муу',
    'гомдол', 'буруу хариу', 'хариулж чадахгүй', 'юу яриад байгаа',
    'хэрэггүй бот', 'утгагүй', 'ойлгосонгүй',
]


def seed_handoff_keywords():
    """Ensure the default handoff keywords exist. Upsert by keyword text:
    missing defaults are added with is_active=True; existing rows (whether
    admin-customized or already-seeded) are left untouched. Lets us add
    new defaults in code without resetting any admin tweaks on prod."""
    inserted = 0
    existing_keywords = {
        k.lower() for k in db.session.query(HandoffKeyword.keyword).all()
        for k in (k[0],)
        if k
    }
    for kw in _DEFAULT_HANDOFF_KEYWORDS_EXPLICIT:
        if kw.lower() not in existing_keywords:
            db.session.add(HandoffKeyword(keyword=kw.lower(), keyword_type='explicit', is_active=True))
            existing_keywords.add(kw.lower())
            inserted += 1
    for kw in _DEFAULT_HANDOFF_KEYWORDS_FRUSTRATION:
        if kw.lower() not in existing_keywords:
            db.session.add(HandoffKeyword(keyword=kw.lower(), keyword_type='frustration', is_active=True))
            existing_keywords.add(kw.lower())
            inserted += 1
    if inserted:
        db.session.commit()
        print(f"Seeded {inserted} new default handoff keyword(s).")


def seed_courses_and_faqs():
    """Populate Courses and FAQs with Magic Financial Group defaults if empty.
    Skipped entirely when SEED_DEFAULTS=false."""
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


# ===================== BACKGROUND TASKS =====================

def polling_task(app):
    """Background task to poll Facebook posts and auto-comment.

    Pass the Flask app so we can establish an app context for db access
    inside the worker thread.
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
    return (
        f"{name_prefix}танд сургалттай холбоотой ямар нэг асуулт үлдсэн "
        "бол хариулахад баяртай байх болно 😊"
    )


def nudge_pending_leads():
    """Send one follow-up to each user whose last message is 4-12h old. Throttled to once per 7 days."""
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


def nudge_task(app):
    """Background loop running nudge_pending_leads() every 30 minutes."""
    while True:
        try:
            with app.app_context():
                nudge_pending_leads()
        except Exception as e:
            print(f"Nudge error: {e}")
        time.sleep(30 * 60)


# ===================== CHAT QUESTION CLUSTERING =====================
# Phase 5b: groups recent user messages into themes via LLM so admins can
# see "what users are actually asking" and one-click promote popular
# questions into the curated FAQ.

# Mongolian interrogative starters used by _is_question_like as a cheap
# heuristic to filter the user-message firehose to question-ish content.
_MONGOLIAN_QUESTION_STARTERS = (
    'яаж', 'ямар', 'хэр', 'хэдэн', 'хэзээ', 'хаана', 'хэн',
    'юу', 'юутай', 'юунд', 'юугаар', 'хичнээн', 'хэдий',
)


def _is_question_like(text):
    """Cheap filter applied before sending messages to the LLM. Catches
    obvious questions (ends with '?', Mongolian interrogatives at start)
    while letting the LLM make the final call on ambiguous cases. Drops
    one-word acknowledgements (za, tiim, ok)."""
    if not text:
        return False
    s = text.strip().lower()
    if len(s) < 4:
        return False
    if s.endswith('?'):
        return True
    if any(s.startswith(starter) for starter in _MONGOLIAN_QUESTION_STARTERS):
        return True
    return len(s) >= 10


def _build_clustering_prompt(questions):
    """Render the LLM prompt for clustering. Asks for strict JSON output
    so parsing stays reliable; rejects clusters with fewer than 2
    questions so admins don't see one-off noise."""
    numbered = '\n'.join(f'{i+1}. {q}' for i, q in enumerate(questions))
    return (
        "You receive a list of real user questions sent to a Mongolian Facebook "
        "Messenger chatbot for a financial-training / consulting company. Group "
        "them into 5 to 15 themes that capture what users keep asking about.\n\n"
        "Rules:\n"
        "- Only emit a cluster if at least 2 of the input questions belong in it.\n"
        "- The title must be in Mongolian, short (under 60 characters), "
        "business-relevant. Examples: \"Үнийн асуултууд\", \"Хичээлийн хуваарь\".\n"
        "- representative_question is the single question from the input that "
        "best captures the theme — quote it verbatim.\n"
        "- sample_questions is up to 5 distinct phrasings from the input that "
        "belong to this theme (verbatim quotes).\n"
        "- count is the total number of input questions that map to this theme.\n"
        "- Return ONLY a JSON object with key 'clusters' whose value is an "
        "array of objects with these exact keys: title, representative_question, "
        "sample_questions, count. No prose, no explanation, no markdown.\n\n"
        "Input questions:\n" + numbered
    )


def cluster_chat_questions(lookback_days=30, max_questions=400):
    """Pull recent user messages, ask the LLM to group them into themes,
    wholesale-replace ChatQuestionCluster rows.

    Returns the number of clusters written. Returns 0 on any failure
    rather than raising, so the weekly background thread doesn't crash on
    a transient OpenAI hiccup. Promoted clusters (admin already turned
    them into FAQ) are preserved across runs.
    """
    import json as _json

    api_key = os.environ.get('OPENAI_API_KEY', '').strip()
    if not api_key:
        print('cluster_chat_questions: OPENAI_API_KEY not set, skipping.')
        return 0

    since = datetime.utcnow() - timedelta(days=lookback_days)
    rows = (Message.query
            .filter(Message.sender == 'user')
            .filter(Message.created_at >= since)
            .order_by(Message.created_at.desc())
            .all())
    questions = []
    seen = set()
    for m in rows:
        text = (m.content or '').strip()
        if not _is_question_like(text):
            continue
        if text in seen:
            continue
        seen.add(text)
        questions.append(text)
        if len(questions) >= max_questions:
            break
    if len(questions) < 5:
        print(f'cluster_chat_questions: only {len(questions)} question-like messages, skipping.')
        return 0

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model='gpt-4o-mini',
            messages=[
                {'role': 'system', 'content': 'You output strict JSON only. No prose, no markdown.'},
                {'role': 'user', 'content': _build_clustering_prompt(questions)},
            ],
            response_format={'type': 'json_object'},
            temperature=0.2,
        )
        raw = resp.choices[0].message.content or ''
    except Exception as e:
        print(f'cluster_chat_questions: OpenAI error: {e}')
        return 0

    try:
        parsed = _json.loads(raw)
    except Exception as e:
        print(f'cluster_chat_questions: JSON parse failed: {e}; raw[:300]={raw[:300]!r}')
        return 0
    # Be permissive about the wrapper shape.
    clusters = None
    if isinstance(parsed, list):
        clusters = parsed
    elif isinstance(parsed, dict):
        for v in parsed.values():
            if isinstance(v, list):
                clusters = v
                break
    if not clusters:
        print(f'cluster_chat_questions: no cluster array in response: {raw[:300]}')
        return 0

    now = datetime.utcnow()
    # Wipe everything except clusters already promoted to FAQ.
    promoted_titles = {
        c.title for c in ChatQuestionCluster.query.filter(
            ChatQuestionCluster.promoted_to_faq_id.isnot(None)
        ).all()
    }
    ChatQuestionCluster.query.filter(
        ChatQuestionCluster.promoted_to_faq_id.is_(None)
    ).delete(synchronize_session=False)

    inserted = 0
    for c in clusters:
        if not isinstance(c, dict):
            continue
        try:
            title = (c.get('title') or '').strip()[:200]
            rep = (c.get('representative_question') or '').strip()
            samples = c.get('sample_questions') or []
            cnt = int(c.get('count') or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if not title or not rep or cnt < 2:
            continue
        if title in promoted_titles:
            continue
        if not isinstance(samples, list):
            samples = []
        samples = [str(s).strip() for s in samples if str(s).strip()][:5]
        db.session.add(ChatQuestionCluster(
            title=title,
            representative_question=rep,
            sample_questions=_json.dumps(samples, ensure_ascii=False),
            count=max(cnt, len(samples)),
            first_seen_at=since,
            last_seen_at=now,
        ))
        inserted += 1
    db.session.commit()
    print(f'cluster_chat_questions: wrote {inserted} cluster(s) from {len(questions)} questions.')
    return inserted


def cluster_task(app):
    """Background loop running cluster_chat_questions() weekly. Gated by
    ENABLE_CHAT_CLUSTERING=true so dev / non-MagicBot deploys don't burn
    OpenAI credits unnecessarily."""
    while True:
        try:
            with app.app_context():
                cluster_chat_questions()
        except Exception as e:
            print(f"Clustering error: {e}")
        # Once per week. Don't tighten without checking OpenAI cost.
        time.sleep(7 * 24 * 60 * 60)
