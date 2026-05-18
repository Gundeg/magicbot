"""Domain services: Facebook Messenger API, OpenAI helpers, rate limit,
funnel/session classifiers, settings getters, AI prompt builder, schema
migration, seed routines, background tasks, handoff flow, and audit logging.

Route modules import functions from here; they should not poke at Facebook
or OpenAI directly.
"""
import hmac
import hashlib
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock

import requests
from flask import current_app, g, has_request_context
from flask_login import current_user
from openai import OpenAI
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from extensions import db
from models import (
    AdminIssue, AuditEntry, BusinessLine, ChatQuestionCluster, Course,
    FacebookUser, FAQ, GeneralSetting, HandoffKeyword, Message, PagePost,
    Product, ProductLink, TeamMember, TrainingSnippet, User,
)

logger = logging.getLogger(__name__)


# ===================== BACKGROUND WORK QUEUE =====================
# Small thread-pool used to push slow side-effects (OpenAI classify calls,
# Telegram pings, follow-up FB messages) off the webhook request path so
# Facebook doesn't retry on slow OpenAI hops. Workers establish their own
# Flask app context so SQLAlchemy queries inside the callable still work.

from concurrent.futures import ThreadPoolExecutor  # noqa: E402

_BG_EXECUTOR = ThreadPoolExecutor(
    max_workers=int(os.environ.get('BACKGROUND_WORKERS', '4') or '4'),
    thread_name_prefix='mbot-bg',
)


def enqueue_background(func, *args, **kwargs):
    """Run `func(*args, **kwargs)` in a worker thread with a Flask app
    context. Returns the Future for tests; callers in routes ignore it.

    Important: pass primitive IDs (e.g. fb_user.id) rather than ORM
    instances, because the request-scoped session that loaded the
    instance will be torn down before the worker runs."""
    try:
        app_obj = current_app._get_current_object()
    except RuntimeError:
        app_obj = None

    def _runner():
        try:
            if app_obj is not None:
                with app_obj.app_context():
                    func(*args, **kwargs)
            else:
                func(*args, **kwargs)
        except Exception:
            logger.exception('background task %s failed', getattr(func, '__name__', func))

    return _BG_EXECUTOR.submit(_runner)


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
# Allow an explicit dev-mode opt-out so local development without the secret
# still works, but make the bypass loud and intentional rather than implicit.
FACEBOOK_ALLOW_UNVERIFIED = (
    os.environ.get('FACEBOOK_ALLOW_UNVERIFIED', '').lower() in ('1', 'true', 'yes')
)
if not FACEBOOK_APP_SECRET and not FACEBOOK_ALLOW_UNVERIFIED:
    raise RuntimeError(
        "FACEBOOK_APP_SECRET is required. Set it to your Facebook App Secret "
        "so HMAC-SHA256 webhook signature verification works. For local dev "
        "without the secret, set FACEBOOK_ALLOW_UNVERIFIED=1 — never use that "
        "in production, as it lets anyone forge Messenger traffic."
    )
# Loaded at import time as a fallback; the live value is fetched by
# get_google_form_url() which also checks the DB setting written from
# the admin panel (Business Management -> General Information).
GOOGLE_FORM_URL = os.environ.get('GOOGLE_FORM_URL', '')


def get_google_form_url():
    """Self-service registration link the bot can quote. Priority order:
       1) Admin-panel value (GeneralSetting key 'google_form_url')
       2) GOOGLE_FORM_URL env var (set at import time)
       3) Empty string — the prompt drops the registration block entirely.

    Was previously hard-wired to env-var only, which meant admin changes
    in /business-management/general were silently ignored by the bot
    even though they showed up in the form.
    """
    db_value = get_setting('google_form_url', '')
    if db_value and db_value.strip():
        return db_value.strip()
    return GOOGLE_FORM_URL

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
        logger.warning("WARNING: %s=%s is not an integer; using default %s.", name, repr(raw), default)
        return default


RATE_LIMIT_MAX = _safe_int_env('RATE_LIMIT_MAX', 5)
RATE_LIMIT_WINDOW = timedelta(seconds=_safe_int_env('RATE_LIMIT_WINDOW_SECONDS', 60))
logger.info(
    "Rate limit: %s",
    'DISABLED' if RATE_LIMIT_MAX <= 0
    else f'{RATE_LIMIT_MAX} msg / {RATE_LIMIT_WINDOW.total_seconds():.0f}s per sender',
)

RATE_LIMIT_REPLY = (
    "Та маш олон мессеж бичиж байна. 1 минутын дараа дахин оролдоорой. 🙏"
)

SESSION_ACTIVE_WINDOW = timedelta(hours=2)
SESSION_GAP_WINDOW = timedelta(hours=24)


# ===================== WEBHOOK SECURITY + RATE LIMIT =====================

def verify_facebook_signature(raw_body, header_value):
    """Validate X-Hub-Signature-256 against HMAC-SHA256(raw_body, app_secret).

    Hard-required in production: the app refuses to boot without
    FACEBOOK_APP_SECRET unless FACEBOOK_ALLOW_UNVERIFIED=1 is set explicitly
    for local dev. The latter bypass is loud and intentional — never set it
    in production, as it lets attackers forge Messenger traffic.
    """
    if not FACEBOOK_APP_SECRET:
        # Only reachable when FACEBOOK_ALLOW_UNVERIFIED=1 (dev opt-out).
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
            logger.warning(
                "RATE LIMIT FIRED sender_id=%r max=%s window=%.0fs hit_ages_sec=%s total_tracked_senders=%s",
                sender_id, RATE_LIMIT_MAX, RATE_LIMIT_WINDOW.total_seconds(),
                ages, len(_rate_state),
            )
            return False
        dq.append(now)
        if len(_rate_state) > 5000:
            for sid in [k for k, v in _rate_state.items() if not v]:
                del _rate_state[sid]
        return True


# ===================== FACEBOOK API HELPERS =====================

def _fb_auth_headers(extra=None):
    """Build the Authorization header for Graph API calls so the access
    token never lands in URL query params (and therefore not in proxy
    access logs, browser histories, etc.). `extra` merges additional
    headers like Content-Type."""
    headers = {"Authorization": f"Bearer {FACEBOOK_ACCESS_TOKEN}"}
    if extra:
        headers.update(extra)
    return headers


def send_facebook_message(recipient_id, message_text):
    """Send a message via Facebook Messenger API"""
    url = "https://graph.facebook.com/v18.0/me/messages"
    data = {
        "recipient": {"id": recipient_id},
        "message": {"text": message_text}
    }
    try:
        response = requests.post(
            url,
            json=data,
            headers=_fb_auth_headers({"Content-Type": "application/json"}),
            timeout=10,
        )
        if response.status_code != 200:
            body = response.text[:500] if response.text else '<empty>'
            logger.warning(
                "Send API FAILED recipient=%s status=%s body=%s",
                recipient_id, response.status_code, body,
            )
            return False
        return True
    except Exception as e:
        logger.exception("Error sending message to recipient=%s: %s", recipient_id, e)
        return False


def get_facebook_user_info(facebook_id):
    """Get user info from Facebook's User Profile API. Returns {} on any failure."""
    url = f"https://graph.facebook.com/v18.0/{facebook_id}"
    params = {"fields": "name,first_name,last_name"}
    try:
        response = requests.get(
            url, params=params, headers=_fb_auth_headers(), timeout=5,
        )
        if response.status_code == 200:
            data = response.json() or {}
            if not data.get('name'):
                logger.info(
                    "FB user profile: 200 OK but missing name for psid=%s body=%s",
                    facebook_id, str(data)[:200],
                )
            return data
        logger.warning(
            "FB user profile failed psid=%s status=%s body=%s",
            facebook_id, response.status_code, response.text[:300],
        )
        return {}
    except Exception as e:
        logger.exception("FB user profile exception psid=%s: %s", facebook_id, e)
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
        logger.error("Refresh name commit failed for psid=%s: %s", fb_user.facebook_id, e)
        return ''
    return name


def get_recent_messages():
    """Poll for recent messages from Facebook Messenger"""
    url = f"https://graph.facebook.com/v18.0/{FACEBOOK_PAGE_ID}/conversations"
    params = {
        "fields": "id,senders,participants,former_participants,wallpaper,snippet,updated_time,message_count,unread_count,subject,can_reply,former_participants,info,link,name,email,page_name,wallpaper,former_participants",
    }
    try:
        response = requests.get(url, params=params, headers=_fb_auth_headers(), timeout=10)
        if response.status_code == 200:
            return response.json().get('data', [])
        return []
    except Exception as e:
        logger.exception("Error getting messages: %s", e)
        return []


def get_page_posts():
    """Poll for recent posts from Facebook Page"""
    url = f"https://graph.facebook.com/v18.0/{FACEBOOK_PAGE_ID}/feed"
    params = {
        "fields": "id,message,created_time,type,story",
        "limit": 10,
    }
    try:
        response = requests.get(url, params=params, headers=_fb_auth_headers(), timeout=10)
        if response.status_code == 200:
            return response.json().get('data', [])
        return []
    except Exception as e:
        logger.exception("Error getting posts: %s", e)
        return []


def post_comment_on_page(post_id, comment_text):
    """Post a comment on a Facebook Page post"""
    url = f"https://graph.facebook.com/v18.0/{post_id}/comments"
    data = {"message": comment_text}
    try:
        response = requests.post(
            url,
            json=data,
            headers=_fb_auth_headers({"Content-Type": "application/json"}),
            timeout=10,
        )
        return response.status_code == 201
    except Exception as e:
        logger.exception("Error posting comment: %s", e)
        return False


# Schema migration lives in services/_seed.py (alongside the linter
# and seed routines, all of which read/write the same set of tables).
# Imported AFTER the settings helpers below so _seed.py's lazy
# `_svc.get_setting` lookups can resolve.

# ===================== SESSION + FUNNEL CLASSIFIERS =====================

def classify_conversation(fb_user):
    """Use gpt-4o-mini to classify what business line (or generic category)
    a user's conversation is about, then persist to facebook_user.conversation_topic.

    Accepts either a FacebookUser instance or its primary key. The id form
    is what background-thread callers use, since the request-scoped session
    that originally loaded the instance is gone by the time the worker runs."""
    try:
        if isinstance(fb_user, int):
            fb_user = db.session.get(FacebookUser, fb_user)
            if fb_user is None:
                return

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
        logger.error("classify_conversation error for user %s: %s", fb_user.id, e)


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




# ===================== SETTINGS GETTERS =====================

def get_setting(key, default=''):
    """Read a value from GeneralSetting. Returns default if missing or blank.

    Cached per-request via flask.g so build_system_prompt's 5-6 settings
    lookups during a single webhook hit collapse into 1 query. Falls back
    to an uncached fetch outside a request context (e.g. background
    threads, CLI, tests), which still uses the SQLAlchemy session-level
    cache. Note: we deliberately gate on has_request_context() rather than
    has_app_context() — `g` is bound to the app context, which can live
    for an entire test session and would otherwise serve stale values.
    """
    if has_request_context():
        cache = getattr(g, '_setting_cache', None)
        if cache is None:
            cache = {}
            g._setting_cache = cache
        if key in cache:
            value = cache[key]
            return value if value else default
        row = GeneralSetting.query.filter_by(key=key).first()
        value = row.value if (row and row.value and row.value.strip()) else None
        cache[key] = value
        return value if value else default
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
    """How long the bot stays silent after a handoff is triggered.

    Default is 0 (no auto-mute): bot enters advisory mode after handoff
    and continues helping the customer. Staff manually takes over via
    the admin "Take Over" button when ready. Set this to >0 only if you
    want the bot to also fall silent automatically on every handoff."""
    raw = (get_setting('mute_duration_hours', '0') or '0').strip()
    try:
        hours = int(raw)
    except ValueError:
        hours = 0
    return max(0, min(hours, 168))  # clamp 0..7 days


def is_in_handoff(fb_user):
    """True if there's an open handoff AdminIssue for this user. Used to
    drive advisory mode in the bot prompt — distinct from bot_muted_until
    which only fires when a staff member has manually taken over the
    chat."""
    if fb_user is None:
        return False
    return AdminIssue.query.filter_by(
        facebook_user_id=fb_user.id,
        issue_type='handoff',
        status='open',
    ).first() is not None


def take_over_chat(fb_user, hours, actor_username=None):
    """Staff "I'm taking over this conversation" action. Mutes the bot
    for `hours`, transitions the open handoff issue to 'in_progress',
    and logs the action. Called from the admin Work Tasks UI."""
    hours = max(1, min(int(hours or 4), 168))
    fb_user.bot_muted_until = datetime.utcnow() + timedelta(hours=hours)
    issue = AdminIssue.query.filter_by(
        facebook_user_id=fb_user.id,
        issue_type='handoff',
        status='open',
    ).order_by(AdminIssue.created_at.desc()).first()
    if issue:
        issue.status = 'in_progress'
        issue.updated_at = datetime.utcnow()
    db.session.commit()
    return issue


def get_telegram_chat_ids():
    """Comma-separated chat IDs from settings. Returns [] when empty."""
    raw = (get_setting('telegram_chat_ids', '') or '').strip()
    if not raw:
        return []
    return [chunk.strip() for chunk in raw.replace(';', ',').split(',') if chunk.strip()]


def get_sound_alerts_enabled():
    return (get_setting('sound_alerts_enabled', 'on') or '').strip().lower() in ('on', 'true', '1', 'yes')


# Prompt builder, section formatters, and rule tables live in services/_prompt.py.
# We import them at this point (rather than at the top of the file) because
# _prompt.py uses lazy `from services import get_setting` inside its functions,
# and those names must already exist in services.__init__'s namespace by the
# time _prompt.py is first loaded.
from services._prompt import (  # noqa: E402
    SESSION_RULES,
    FUNNEL_RULES,
    build_system_prompt,
    _format_training_snippets,
    _format_team_members,
    _format_product_entry,
    _format_business_line_entry,
    _format_business_lines,
    _format_course_entry,
    _format_courses_canonical,
    _format_current_time_block,
)


def generate_bot_response(user_message, conversation_history,
                          session_state='new', funnel_stage='curious',
                          user_first_name='', handoff_pending=False):
    """Generate bot response using OpenAI. `handoff_pending=True` enables
    advisory mode in the system prompt — bot keeps helping but doesn't
    re-route the customer to staff (since the handoff was already fired)."""
    try:
        messages = [{
            "role": "system",
            "content": build_system_prompt(
                session_state=session_state,
                funnel_stage=funnel_stage,
                user_first_name=user_first_name,
                handoff_pending=handoff_pending,
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
        logger.error("Error generating response: %s", e)
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
        logger.error("Error analyzing post: %s", e)
        return None


# ===================== HANDOFF FLOW =====================

# Better wording for the first reply when handoff fires: explicit ETA,
# phone-shortcut, and warm tone. The bot stays available to keep chatting
# afterward (advisory mode — see HANDOFF_ADVISORY_RULE below).
HANDOFF_USER_REPLY = (
    "Таны асуултыг хүлээж авлаа 🙏 Манай ажилтанд дамжуулсан тул удахгүй "
    "(ажлын цагт ердийн 10-30 минутын дотор) тантай эргэж холбогдох болно. "
    "Та энэ хооронд асуулт асууж, надтай үргэлжлүүлэн ярилцаж болно — "
    "ажилтан ирэхэд тантай шууд холбогдоно."
)
HANDOFF_USER_REPLY_OFF_HOURS = (
    "Таны мессежийг хүлээж авлаа 🌙 Одоогоор ажлын цаг ({start}:00 – {end}:00) "
    "дууссан тул ажилтан маргааш ажлын цагт хариулна. Та энэ хооронд асуулт "
    "асууж, надтай үргэлжлүүлэн ярилцаж болно."
)

# Injected into the system prompt when the user is in a handoff window
# (bot_muted_until > now). Tells the bot to keep helping the customer
# without re-routing them to staff again — that step has already fired.
HANDOFF_ADVISORY_RULE = (
    "ХАНДОФФ ХҮЛЭЭЛТИЙН ГОРИМ (advisory):\n"
    "Энэ хэрэглэгч аль хэдийн ажилтанд дамжуулагдсан. Ажилтан удахгүй "
    "(ажлын цагт ~10-30 минутын дотор) тантай холбогдох болно.\n"
    "Энэ горимд:\n"
    "  • Хэрэглэгчийн шинэ асуултанд ердийн адил тусла — анги, үнэ, "
    "хуваарь, сургалтын агуулга, FAQ-ийн бусад асуултын талаар үргэлж "
    "хариул.\n"
    "  • Гэхдээ 'утасны дугаараа үлдээгээрэй', 'ажилтан тантай эргэж "
    "холбогдоно', 'манай мэргэжлийн ажилтан' гэх мэт чиглүүлгийг ДАХИН "
    "БҮҮ ТАВЬ — хэрэглэгч аль хэдийн дараалалд орсон.\n"
    "  • Хариултын төгсгөлд богино сануулга нэмж болно ("
    "жишээ: 'Ажилтан удахгүй холбогдоно — энэ хооронд яриагаа үргэлжлүүл'). "
    "Хэдхэн хариултанд нэг л удаа давтаж, спам бүү бол.\n"
    "  • Хэрэв хэрэглэгч 'хэзээ хариулах вэ?', 'хэн хариулдаг вэ?' гэх "
    "мэт асуулт асуувал ажлын цагийн доторх ETA-г шулуун хэлж тайвшруул."
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
                logger.error("Telegram send failed for %s: %s %s", chat_id, resp.status_code, resp.text[:200])
        except Exception as e:
            logger.error("Telegram send error for %s: %s", chat_id, e)
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
    """Signal that this customer needs staff attention: create an AdminIssue
    (with type='handoff'), ping Telegram, and optionally send the user a
    polite waiting message. The bot stays in advisory mode after this — it
    keeps helping the customer without re-routing them to staff — until
    either a staff member manually "takes over" the chat (separate action
    that sets bot_muted_until) or the admin marks the issue resolved.

    Pass `send_user_message=False` when the bot has already sent the
    customer a deferring reply (see bot_response_implies_handoff) so the
    user doesn't receive two back-to-back messages.

    Auto-mute on handoff is opt-in via the mute_duration_hours setting:
    leave it at 0 (default) for the staff-takeover workflow where the
    bot stays available; set it > 0 only if you want immediate silence
    after every handoff."""
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
        logger.error("log_admin_action failed for %s: %s", repr(action), e)


# Linter, schema migration, advance/archive helpers, and all seed_* fns.
from services._seed import (  # noqa: E402
    ensure_schema,
    lint_training_data,
    advance_recurring_courses,
    archive_past_courses,
    seed_products,
    seed_discovery_phrasing_snippets,
    seed_default_magic_links,
    seed_handoff_keywords,
    seed_courses_and_faqs,
)


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
            logger.error("Polling error: %s", e)
            time.sleep(60)


def _nudge_message_for(user):
    stage = (user.funnel_stage or 'curious').lower()
    name_prefix = ''
    fname = first_name_of(user.name)
    if fname:
        name_prefix = f"{fname} аа, "

    if stage == 'ready':
        link = get_google_form_url()
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
        logger.info("Nudge: sent %s follow-up(s).", sent)
    return sent


def nudge_task(app):
    """Background loop running nudge_pending_leads() every 30 minutes."""
    while True:
        try:
            with app.app_context():
                nudge_pending_leads()
        except Exception as e:
            logger.error("Nudge error: %s", e)
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
        logger.info('cluster_chat_questions: OPENAI_API_KEY not set, skipping.')
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
        logger.info('cluster_chat_questions: only %s question-like messages, skipping.', len(questions))
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
        logger.error('cluster_chat_questions: OpenAI error: %s', e)
        return 0

    try:
        parsed = _json.loads(raw)
    except Exception as e:
        logger.error('cluster_chat_questions: JSON parse failed: %s; raw[:300]=%r', e, raw[:300])
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
        logger.info('cluster_chat_questions: no cluster array in response: %s', raw[:300])
        return 0

    now = datetime.utcnow()
    promoted_titles = {
        c.title for c in ChatQuestionCluster.query.filter(
            ChatQuestionCluster.promoted_to_faq_id.isnot(None)
        ).all()
    }

    # Validate and stage new rows BEFORE deleting the old ones. If the
    # LLM returned junk, parsing throws here and we leave the existing
    # clusters untouched instead of wiping them and ending up with
    # nothing. Previously: delete-then-insert would empty the table on
    # any parse failure.
    staged = []
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
        staged.append({
            'title': title,
            'representative_question': rep,
            'sample_questions': _json.dumps(samples, ensure_ascii=False),
            'count': max(cnt, len(samples)),
            'first_seen_at': since,
            'last_seen_at': now,
        })

    if not staged:
        logger.warning(
            'cluster_chat_questions: parsed 0 valid clusters from response; '
            'leaving existing clusters intact.'
        )
        return 0

    # Atomic swap: delete old, insert new, commit together. If anything
    # fails the rollback restores the pre-swap state.
    try:
        ChatQuestionCluster.query.filter(
            ChatQuestionCluster.promoted_to_faq_id.is_(None)
        ).delete(synchronize_session=False)
        for row in staged:
            db.session.add(ChatQuestionCluster(**row))
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception('cluster_chat_questions: atomic swap failed; rolled back.')
        return 0

    inserted = len(staged)
    logger.info(
        'cluster_chat_questions: wrote %s cluster(s) from %s questions.',
        inserted, len(questions),
    )
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
            logger.error("Clustering error: %s", e)
        # Once per week. Don't tighten without checking OpenAI cost.
        time.sleep(7 * 24 * 60 * 60)
