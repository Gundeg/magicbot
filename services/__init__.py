"""Domain services: Facebook Messenger API, Claude (Anthropic) helpers, rate
limit, funnel/session classifiers, settings getters, AI prompt builder, schema
migration, seed routines, background tasks, handoff flow, and audit logging.

Route modules import functions from here; they should not poke at Facebook
or the Claude API directly.
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

import anthropic
import requests
from anthropic import (
    AuthenticationError, BadRequestError, PermissionDeniedError, RateLimitError,
)
from flask import current_app, g, has_request_context
from flask_login import current_user
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from extensions import db
from models import (
    AdminIssue, AuditEntry, BusinessLine, ChatQuestionCluster, Course,
    FacebookUser, FAQ, GeneralSetting, HandoffKeyword, Message, PagePost,
    Product, ProductLink, TeamMember, TrainingSnippet, User,
)

# Warm the stdlib `netrc` import at startup (import for side effect only).
# `requests` does a LAZY `from netrc import ...` inside get_netrc_auth() during
# prepare_request. On a COLD gunicorn worker, a first-contact webhook (which
# calls get_facebook_user_info -> requests.get synchronously) can race a
# background reply thread that is also importing, and the two deadlock on
# CPython's import lock — the worker hangs until gunicorn SIGKILLs it at the
# 60s timeout, the webhook returns 500, and that customer gets no reply.
# Importing netrc here puts it in sys.modules once, up front, so requests'
# lazy import is a no-op and never takes the import lock mid-request.
# Observed in prod 2026-07-06 (routes/webhook.py get_facebook_user_info path).
import netrc  # noqa: E402,F401

logger = logging.getLogger(__name__)


# ===================== BACKGROUND WORK QUEUE =====================
# Small thread-pool used to push slow side-effects (model classify calls,
# Telegram pings, follow-up FB messages) off the webhook request path so
# Facebook doesn't retry on slow model hops. Workers establish their own
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

    # In tests, run inline on the current thread + app context. It keeps
    # assertions deterministic, and — critically — an in-memory SQLite test DB
    # is per-connection, so a real background thread would open a fresh, empty
    # database and see none of the test's data.
    if app_obj is not None and app_obj.config.get('TESTING'):
        try:
            func(*args, **kwargs)
        except Exception:
            logger.exception('background task %s failed', getattr(func, '__name__', func))
        return None

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

# ===================== CLAUDE (ANTHROPIC) CLIENT =====================
# ONE Anthropic client serves every model call in the app. The customer-facing
# reply runs on REPLY_MODEL; the background jobs (topic classifier, page
# auto-comments, FAQ clustering) run on the cheaper BACKGROUND_MODEL. Reads
# ANTHROPIC_API_KEY from env at import time — the .env file is loaded before
# this module is first imported by app.py.
#
# The SDK does NOT fail at construction without a key; the first request would,
# and every customer would then get the canned apology. Warn loudly in the boot
# log instead so a missing key is obvious on the Render Events/Logs page.
ANTHROPIC_API_KEY = (os.environ.get('ANTHROPIC_API_KEY') or '').strip()
if not ANTHROPIC_API_KEY:
    logger.warning(
        "ANTHROPIC_API_KEY is not set — every Claude call will fail and customers "
        "will get the canned apology. Set it in the environment (console.anthropic.com)."
    )
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY or None)

# Customer-reply model. Claude Opus 5.5 is the default: the reply has to read
# paraphrase / Latin-script / indirect intent in Mongolian, where the bigger
# tier is markedly better (the earlier OpenAI/Gemini bake-offs showed the same
# thing). REPLY_MODEL overrides it, e.g. claude-sonnet-5-5 to trade a little
# quality for cost.
REPLY_MODEL = os.environ.get('REPLY_MODEL', '').strip() or 'claude-opus-5-5'

# Background model for the cheap, high-volume jobs (classifier / comments /
# clustering). Haiku is plenty for structured tagging and grouping.
BACKGROUND_MODEL = os.environ.get('BACKGROUND_MODEL', '').strip() or 'claude-haiku-5-5'

# THINKING / EFFORT: current Claude models think adaptively and that can't be
# switched off on Opus 5.5 — depth is controlled with `output_config.effort`.
# A chat bot is latency-sensitive and the question is usually simple, so the
# default is 'low' (fast, cheap, still thinks when the message calls for it).
# Raise to 'medium'/'high' via REPLY_EFFORT if replies ever feel shallow.
_EFFORT_LEVELS = ('low', 'medium', 'high', 'xhigh', 'max')
REPLY_EFFORT = os.environ.get('REPLY_EFFORT', '').strip().lower() or 'low'
if REPLY_EFFORT not in _EFFORT_LEVELS:
    logger.warning(
        "REPLY_EFFORT=%r is not one of %s; using 'low'.", REPLY_EFFORT, _EFFORT_LEVELS,
    )
    REPLY_EFFORT = 'low'

# Prompt-cache lifetime for the stable half of the system prompt: '5m'
# (default — cheapest when a customer's messages cluster within minutes) or
# '1h' (write costs 2x but survives quiet hours; worth it only once traffic is
# steady across the day). See build_system_blocks.
REPLY_CACHE_TTL = os.environ.get('REPLY_CACHE_TTL', '').strip().lower() or '5m'
if REPLY_CACHE_TTL not in ('5m', '1h'):
    logger.warning("REPLY_CACHE_TTL=%r is not '5m' or '1h'; using '5m'.", REPLY_CACHE_TTL)
    REPLY_CACHE_TTL = '5m'

# Label + billing pointer surfaced in the account-failure Telegram alert so the
# telemetry points staff at the right console.
REPLY_PROVIDER_LABEL = 'Claude (Anthropic)'
REPLY_PROVIDER_BILLING_HINT = 'console.anthropic.com → Plans & Billing'

# Hard cap on reply length. NOTE: on Claude `max_tokens` caps the model's
# thinking AND the visible reply COMBINED, so leave headroom above the reply
# itself — too tight a cap returns an EMPTY reply (stop_reason=max_tokens),
# which generate_bot_response turns into the canned apology and logs. 2048 is
# comfortable at effort 'low'. Env-tunable without a redeploy.
try:
    REPLY_MAX_TOKENS = int(os.environ.get('REPLY_MAX_TOKENS', '2048') or '2048')
except ValueError:
    REPLY_MAX_TOKENS = 2048


def _response_text(response):
    """Concatenate the text blocks of a Messages API response (thinking and
    tool_use blocks are skipped). Returns '' when the model produced no text —
    e.g. it stopped on max_tokens mid-thinking, or only called a tool."""
    return ''.join(
        getattr(block, 'text', '') or '' for block in response.content
        if getattr(block, 'type', None) == 'text'
    ).strip()


# Structured-output schemas (output_config.format) for the two background jobs
# that must return JSON. The grammar guarantees the reply parses and matches
# the shape — no code fences, no prose — so the json.loads at the call site
# only fails on a genuinely broken response. Keep these to the plain subset
# (object / array / string / integer, required, additionalProperties).
TOPIC_TAGS_FORMAT = {
    'type': 'json_schema',
    'schema': {
        'type': 'object',
        'properties': {
            'topics': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'name': {'type': 'string'},
                        'evidence': {'type': 'string'},
                    },
                    'required': ['name', 'evidence'],
                    'additionalProperties': False,
                },
            },
        },
        'required': ['topics'],
        'additionalProperties': False,
    },
}

CLUSTERS_FORMAT = {
    'type': 'json_schema',
    'schema': {
        'type': 'object',
        'properties': {
            'clusters': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'title': {'type': 'string'},
                        'representative_question': {'type': 'string'},
                        'sample_questions': {'type': 'array', 'items': {'type': 'string'}},
                        'count': {'type': 'integer'},
                    },
                    'required': ['title', 'representative_question', 'sample_questions', 'count'],
                    'additionalProperties': False,
                },
            },
        },
        'required': ['clusters'],
        'additionalProperties': False,
    },
}

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
# Stamped onto every bot-sent Messenger message and echoed back in
# message_echoes webhook events. Lets the webhook tell its OWN outgoing
# messages apart from a human agent's reply typed in the Page inbox — the
# latter carries no tag and triggers the auto-mute below.
BOT_ECHO_TAG = 'magicbot_auto'
# When a human agent replies to a customer (detected via an untagged echo),
# pause the bot for this many minutes so it doesn't talk over the human.
HUMAN_TAKEOVER_MUTE_MINUTES = int(
    os.environ.get('HUMAN_TAKEOVER_MUTE_MINUTES', '30')
)


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


# ===================== LEAD STATUS VOCABULARY =====================
# The six allowed values for FacebookUser.lead_status. Stored as ordered
# tuples so the admin UI renders dropdowns in funnel order (new → ... →
# converted/dropped). Both `key` (DB value) and `label` (Mongolian UI
# text) are surfaced to templates; `color` maps to a Bootstrap badge
# variant so the badge styling stays consistent across tabs.
#
# Adding a new status here is the single source of truth — the
# /admin/api/lead-status endpoint accepts only these keys, and the
# templates iterate LEAD_STATUSES to build their dropdowns.
LEAD_STATUSES = (
    {'key': 'new',        'label': 'Шинэ',           'color': 'info'},
    {'key': 'contacted',  'label': 'Холбогдсон',     'color': 'primary'},
    {'key': 'qualified',  'label': 'Сонирхолтой',    'color': 'warning'},
    {'key': 'on_hold',    'label': 'Хүлээгдэж буй',  'color': 'secondary'},
    {'key': 'converted',  'label': 'Бүртгүүлсэн',    'color': 'success'},
    {'key': 'dropped',    'label': 'Орхисон',        'color': 'dark'},
)
LEAD_STATUS_KEYS = tuple(s['key'] for s in LEAD_STATUSES)
LEAD_STATUS_LABELS = {s['key']: s['label'] for s in LEAD_STATUSES}

# Statuses that remove a user from the active Hot Prospects / Leads
# work queues. The user is still reachable through the conversation
# viewer; this just hides them from the daily action list.
TERMINAL_LEAD_STATUSES = ('dropped',)


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


# Telegram-alert state for a dead Page token. Separate key from the reply-
# provider alert: the two failures are independent and each deserves its own
# cooldown window.
FB_TOKEN_ALERT_STATE_KEY = 'facebook_token_alerted_at'


# Separate window for the health check's "couldn't verify" alert: it reports a
# DIFFERENT thing (we could not reach Graph at all) and must not be silenced by,
# or silence, a confirmed dead-token alert.
FB_HEALTH_ALERT_STATE_KEY = 'facebook_health_alerted_at'


def _claim_alert_window(state_key, cooldown_hours=None):
    """Return True if an alert under `state_key` may be sent now, and claim the
    window in the same call.

    The claim is COMMITTED BEFORE the caller sends anything: a dead token fails
    every message on every worker simultaneously, and a check-then-send without
    the pre-claim pages staff hundreds of times during one outage. Returns False
    on any failure (no app context, no DB) — a broken cooldown must fail to
    silence, never to spam."""
    try:
        hours = cooldown_hours or max(1, _safe_int_env('FB_TOKEN_ALERT_COOLDOWN_HOURS', 6))
        cooldown = timedelta(hours=hours)
        now = datetime.utcnow()
        row = GeneralSetting.query.filter_by(key=state_key).first()
        if row and row.value:
            try:
                if now - datetime.fromisoformat(row.value) < cooldown:
                    return False
            except ValueError:
                pass  # unparseable timestamp -> treat as never alerted
        if row is None:
            db.session.add(GeneralSetting(key=state_key, value=now.isoformat()))
        else:
            row.value = now.isoformat()
        db.session.commit()
        return True
    except Exception as exc:
        logger.error("alert cooldown claim failed for %s: %s", state_key, exc)
        try:
            db.session.rollback()
        except Exception:
            pass
        return False


def _is_facebook_token_error(body_text):
    """True when a Send API rejection means the TOKEN ITSELF is dead, so every
    subsequent send will fail the same way until a human replaces it. Returns
    (is_token_error, short_reason).

    Paged on:
      • code 190 — token expired (subcode 463) or invalidated by a password
        change (subcode 460). This is the recurring ~60-day expiry.
      • code 102 — session invalid.
      • code 100 / subcode 33 — `me` does not resolve to a Page, i.e. a USER
        token was deployed where a Page token belongs. Authenticates fine,
        sends nothing.

    Deliberately NOT paged: permission errors (code 10, 200) and per-recipient
    messaging-window errors (code 551, 10 subcode 2018278). Those are policy or
    per-conversation problems, not a dead credential, and paging on them would
    train staff to ignore the alert."""
    text = body_text or ''
    # check_facebook_token hands us "status=401 body={...}", the send path hands
    # us the raw body. Locate the JSON either way rather than parsing blind.
    payload = text[text.index('{'):] if '{' in text else text
    try:
        error = (json.loads(payload) or {}).get('error') or {}
        code = error.get('code')
        subcode = error.get('error_subcode')
        message = error.get('message') or ''
    except (ValueError, AttributeError):
        # Truncated or non-JSON body — fall back to substring matching so a
        # clipped body still pages rather than failing silently.
        if 'OAuthException' in text and 'code":190' in text.replace(' ', ''):
            return True, text[:200]
        return False, ''

    if code == 190:
        if subcode == 463:
            return True, f"Токен хугацаа дууссан (190/463). {message}"
        if subcode == 460:
            return True, f"Facebook нууц үг солигдсоны улмаас токен хүчингүй (190/460). {message}"
        return True, f"Токен хүчингүй (190). {message}"
    if code == 102:
        return True, f"Session хүчингүй (102). {message}"
    if code == 100 and subcode == 33:
        return True, (
            "Page token биш USER token тавигдсан байна (100/33) — "
            f"`me` нь хуудас руу заахгүй байна. {message}"
        )
    return False, ''


def alert_facebook_token_failure(body_text):
    """Telegram-alert staff that the Page token is dead, at most once per
    FB_TOKEN_ALERT_COOLDOWN_HOURS (default 6). Shared by BOTH detection paths —
    send_facebook_message (fires on the first failed customer message, and runs
    on the web dynos so it survives a dead worker) and token_health_task (the
    6-hourly backstop) — so one outage produces ONE alert, not one per path.

    The wording is deliberately unambiguous. During the 2026-09-04 outage the
    health check said the bot "might" not be able to reply, which reads like a
    warning; it repeated that identical text every 6h for three days with no
    cooldown. `body_text` here is proof of a specific dead credential, so the
    message states the outage as fact and names the fix. Genuinely uncertain
    failures keep the hedged wording, in token_health_task.

    Runs on the customer-reply path, so it must never raise — every internal
    failure (including no app context, or no DB) is logged and swallowed.
    Returns True if an alert was actually sent this call."""
    try:
        is_token_error, reason = _is_facebook_token_error(body_text)
        if not is_token_error:
            return False
        if not _claim_alert_window(FB_TOKEN_ALERT_STATE_KEY):
            return False
        send_telegram_notification(
            "🚨 Facebook Page token хүчингүй боллоо — бот хэрэглэгчдэд ЯМАР Ч "
            "хариу илгээж чадахгүй байна.\n\n"
            "Бот хариугаа зөв бэлдэж байгаа ч Facebook руу илгээх алхам дээр "
            "унаж байна. Хэрэглэгчид юу ч хүлээж авахгүй.\n\n"
            "Засах: Graph API Explorer-оос ШИНЭ PAGE TOKEN аваад Render дээрх "
            "magicbot үйлчилгээний FACEBOOK_ACCESS_TOKEN-г солино уу "
            "(OPERATIONS.md §4). Засмагц бот шууд сэргэнэ.\n\n"
            f"Дэлгэрэнгүй: {reason[:300]}"
        )
        logger.error("Facebook token failure alert sent: %s", reason[:200])
        return True
    except Exception as alert_exc:
        logger.error("Facebook token alert could not be sent: %s", alert_exc)
        try:
            db.session.rollback()
        except Exception:
            pass
        return False


def send_facebook_message(recipient_id, message_text):
    """Send a message via Facebook Messenger API.

    The message carries a `metadata` tag (BOT_ECHO_TAG) that Facebook echoes
    back in message_echoes events, so the webhook can recognise its own
    outgoing messages and NOT mistake them for a human-agent takeover."""
    url = "https://graph.facebook.com/v18.0/me/messages"
    data = {
        "recipient": {"id": recipient_id},
        "message": {"text": message_text, "metadata": BOT_ECHO_TAG},
    }
    try:
        response = requests.post(
            url,
            json=data,
            headers=_fb_auth_headers({"Content-Type": "application/json"}),
            timeout=10,
        )
        if response.status_code != 200:
            raw = response.text or ''
            body = raw[:500] if raw else '<empty>'
            logger.warning(
                "Send API FAILED recipient=%s status=%s body=%s",
                recipient_id, response.status_code, body,
            )
            # A dead token fails every send until a human replaces it, so page
            # staff from here rather than waiting up to 6h for the health check.
            alert_facebook_token_failure(raw)
            return False
        return True
    except Exception as e:
        logger.exception("Error sending message to recipient=%s: %s", recipient_id, e)
        return False


def send_sender_action(recipient_id, action):
    """Send a Messenger sender_action ('mark_seen', 'typing_on', 'typing_off').

    Best-effort UX nicety: shows the customer a read receipt + typing bubble
    while the bot composes its reply, so the wait feels responsive. Failures
    are swallowed — a missing typing bubble must never block the actual reply."""
    url = "https://graph.facebook.com/v18.0/me/messages"
    data = {"recipient": {"id": recipient_id}, "sender_action": action}
    try:
        requests.post(
            url,
            json=data,
            headers=_fb_auth_headers({"Content-Type": "application/json"}),
            timeout=5,
        )
    except Exception as e:
        logger.info("sender_action %s failed for recipient=%s: %s", action, recipient_id, e)


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

def _collect_classification_topics():
    """Build the topic catalog the classifier picks from.

    Only ACTIVE rows in BusinessLine / Product / Service / Course
    contribute. The returned list pairs each topic name with its kind
    ('business_line' | 'product' | 'service' | 'course') so callers can
    persist the source for filtering in the admin UI.

    Names are deduplicated case-insensitively, with the first source
    winning (BusinessLine outranks Product outranks Service outranks
    Course). A duplicate name across kinds is rare in practice but the
    dedup keeps the prompt short and predictable.
    """
    catalog = []
    seen_lower = set()

    def _add(name, kind):
        if not name:
            return
        clean = name.strip()
        if not clean or clean.lower() in seen_lower:
            return
        seen_lower.add(clean.lower())
        catalog.append({'name': clean, 'kind': kind})

    for bl in BusinessLine.query.filter_by(is_active=True).order_by(
        BusinessLine.sort_order.asc(), BusinessLine.id.asc()
    ).all():
        _add(bl.name, 'business_line')
    for p in Product.query.filter_by(is_active=True).order_by(
        Product.sort_order.asc(), Product.id.asc()
    ).all():
        _add(p.name, 'product')
    # Service is imported lazily — older codepaths may not always have it
    # in scope when this module is first reachable.
    from models import Service as _Service
    for s in _Service.query.filter_by(is_active=True).order_by(
        _Service.sort_order.asc(), _Service.id.asc()
    ).all():
        _add(s.name, 'service')
    for c in Course.query.filter_by(is_active=True).order_by(
        Course.id.asc()
    ).all():
        _add(c.name, 'course')
    return catalog


def classify_user_topics(fb_user):
    """Tag a FacebookUser with the Magic-related topics they've asked about.

    Replaces the single-topic ``classify_conversation``. The LLM is told the
    list of allowed topics (active BusinessLine / Product / Service / Course
    names) and must return ONLY topics from that list — generic curiosity,
    off-topic chatter, and competitor questions get no topic at all. Each
    returned topic is upserted into ConversationTopic with first/last_seen
    timestamps and a short evidence snippet from the user's message.

    Reads at most the user's last 60 messages within the
    classification_lookback_days window (admin setting, capped at 30 days).
    Returns the number of topics attached (or refreshed). Catches its own
    errors and logs them — callers (webhook background queue and the
    /admin/api/classify-conversations route) must keep working even if
    the model API is having a bad day.

    Accepts either a FacebookUser instance or its primary key, mirroring
    the contract callers expect from the background queue.
    """
    from models import ConversationTopic  # local — table created by ensure_schema
    try:
        if isinstance(fb_user, int):
            fb_user = db.session.get(FacebookUser, fb_user)
            if fb_user is None:
                return 0

        lookback = get_classification_lookback_days()
        since = datetime.utcnow() - timedelta(days=lookback)
        recent_messages = (
            Message.query
            .filter_by(facebook_user_id=fb_user.id, sender='user')
            .filter(Message.created_at >= since)
            .order_by(Message.created_at.desc())
            .limit(60)
            .all()
        )
        if not recent_messages:
            return 0

        catalog = _collect_classification_topics()
        if not catalog:
            logger.info(
                'classify_user_topics: no active business lines/products/services/courses; '
                'leaving user %s untouched.', fb_user.id,
            )
            return 0

        # Build a short, numbered list of allowed topics for the prompt
        # so the LLM has zero room to invent new ones.
        topic_lines = '\n'.join(
            f'  {i+1}. {t["name"]} ({t["kind"]})' for i, t in enumerate(catalog)
        )
        conversation_text = '\n'.join(
            f'- {(m.content or "").strip()}' for m in reversed(recent_messages)
        )

        prompt = (
            "You tag Mongolian Messenger conversations with the Magic "
            "Financial Group topics they touch. The customer's messages "
            "are below. Pick ONLY the topics from the ALLOWED list that "
            "the customer actually asks about or shows interest in. If "
            "the conversation is generic small talk, off-topic, or about "
            "a service we do not offer, return an empty list — do NOT "
            "force a match.\n\n"
            f"ALLOWED TOPICS:\n{topic_lines}\n\n"
            f"USER MESSAGES (most recent last):\n{conversation_text}\n\n"
            "Return strict JSON with this shape (no prose, no markdown):\n"
            '{"topics": [{"name": "<exact name from ALLOWED list>", '
            '"evidence": "<short Mongolian quote / paraphrase from the '
            'user explaining why this topic was tagged, <= 160 chars>"}]}\n'
            "Use the names verbatim from the ALLOWED list — the name text "
            "only, WITHOUT the list number or the kind in parentheses. "
            "Maximum 5 topics. Empty list is valid."
        )

        # Structured outputs constrain the reply to TOPIC_TAGS_FORMAT. Low
        # effort: this is a lookup against a fixed list, not reasoning.
        result = client.messages.create(
            model=BACKGROUND_MODEL,
            max_tokens=1024,
            output_config={'effort': 'low', 'format': TOPIC_TAGS_FORMAT},
            system="You output strict JSON only.",
            messages=[{"role": "user", "content": prompt}],
        )
        raw = _response_text(result) or '{}'

        try:
            parsed = json.loads(raw)
        except Exception as e:
            logger.warning(
                'classify_user_topics: JSON parse failed for user %s: %s; raw[:200]=%r',
                fb_user.id, e, raw[:200],
            )
            return 0

        items = parsed.get('topics') if isinstance(parsed, dict) else None
        if not isinstance(items, list):
            return 0

        # Match LLM-returned names back to the catalog case-insensitively.
        # Anything that doesn't match an active topic is dropped silently —
        # we don't trust the model to invent topic names. Models sometimes copy
        # the "(kind)" annotation from the ALLOWED list onto the name (Haiku
        # does), so a trailing "(course)" etc. is stripped before the lookup.
        kinds = {t['kind'] for t in catalog}
        kind_suffix = re.compile(
            r'\s*\((' + '|'.join(map(re.escape, kinds)) + r')\)\s*$', re.I,
        )

        def _norm(topic_name):
            return kind_suffix.sub('', topic_name).strip().lower()

        by_name = {_norm(t['name']): t for t in catalog}
        now = datetime.utcnow()
        attached = 0
        for it in items[:10]:
            if not isinstance(it, dict):
                continue
            name = (it.get('name') or '').strip()
            evidence = (it.get('evidence') or '').strip()[:500] or None
            if not name:
                continue
            matched = by_name.get(_norm(name))
            if not matched:
                continue
            row = (ConversationTopic.query
                   .filter_by(facebook_user_id=fb_user.id, topic=matched['name'])
                   .first())
            if row is None:
                row = ConversationTopic(
                    facebook_user_id=fb_user.id,
                    topic=matched['name'],
                    topic_kind=matched['kind'],
                    evidence=evidence,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                db.session.add(row)
            else:
                row.last_seen_at = now
                row.topic_kind = matched['kind']
                if evidence:
                    row.evidence = evidence
            attached += 1
        db.session.commit()

        # Keep the legacy single-topic field populated with the most-recently-
        # seen topic so older UI surfaces don't go blank. Drop it the next time
        # the admin panel is touched if we want a clean break.
        if attached:
            latest = (ConversationTopic.query
                      .filter_by(facebook_user_id=fb_user.id)
                      .order_by(ConversationTopic.last_seen_at.desc())
                      .first())
            if latest:
                fb_user.conversation_topic = latest.topic
                db.session.commit()
        return attached
    except Exception as e:
        logger.exception('classify_user_topics error for user %s: %s',
                         getattr(fb_user, 'id', fb_user), e)
        try:
            db.session.rollback()
        except Exception:
            pass
        return 0


# Backwards-compatibility shim. Anything still calling the old
# ``classify_conversation`` (background queue in routes/webhook.py) now
# routes through the new topic classifier. Keep the name so old
# `from services import classify_conversation` imports don't break.
def classify_conversation(fb_user):
    return classify_user_topics(fb_user)


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


# Words a customer might type instead of their name when ignoring the
# bot's "what's your name?" question. Treating any of these as a name
# would be embarrassing — keep this list in lower-case, all-language.
_NON_NAME_REPLIES = frozenset({
    'тийм', 'үгүй', 'за', 'тэгье', 'ок', 'мэдсэн', 'баярлалаа',
    'сайн', 'сайн уу', 'сайн байна уу', 'байна', 'ok', 'yes', 'no',
    'thanks', 'ty', 'hi', 'hello', 'hey',
})


def extract_name_from_reply(text):
    """Best-effort name extraction from a user message.

    Used by the webhook when the bot just asked "Танай нэрийг хэн гэдэг вэ?"
    and the user replied. Returns '' rather than guessing on ambiguous input —
    we'd rather store nothing than the wrong name.
    """
    if not text:
        return ''
    s = text.strip()
    if not s or len(s) > 60:
        return ''
    if s.lower() in _NON_NAME_REPLIES:
        return ''
    import re
    # Explicit pattern: "намайг X гэдэг" / "миний нэр X" / "нэр нь X"
    m = re.search(
        r'(?:намайг|миний\s+нэр(?:\s+нь)?|нэр\s+нь|нэр\s+минь|my\s+name\s+is|i\s*am)\s+'
        r'([А-Яа-яӨөҮүЁёA-Za-z]{2,})'           # first name token (required)
        r'(?:\s+([А-Яа-яӨөҮүЁёA-Za-z]{2,}))?',  # surname token (optional)
        s, re.IGNORECASE,
    )
    if m:
        first = m.group(1).strip()
        second = (m.group(2) or '').strip()
        # Skip Mongolian sentence particles that follow names ("намайг Болормаа
        # ГЭДЭГ") — they aren't surnames.
        _particles = {
            'гэдэг', 'байна', 'байх', 'юм', 'болно', 'ажилладаг', 'байгаа',
        }
        if second and second.lower() not in _particles:
            return f'{first.title()} {second.title()}'
        return first.title()
    # Fallback: very short message, 1–2 alphabetic tokens, no punctuation/digits.
    # Single-token Mongolian first names are the dominant pattern.
    if '?' in s or re.search(r'\d', s):
        return ''
    tokens = re.findall(r'[А-Яа-яӨөҮүЁёA-Za-z]{2,}', s)
    if len(tokens) == 0 or len(tokens) > 2:
        return ''
    if sum(len(t) for t in tokens) > 25:
        return ''
    return ' '.join(t.title() for t in tokens)




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


def get_business_website_url():
    """Company-wide marketing website URL. Surfaced by the prompt so the bot
    can answer "what's your website?" / "сайт" / "вэбсайт" without admins
    having to add a separate ProductLink for it."""
    return get_setting('business_website_url', '')


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


CLASSIFICATION_LOOKBACK_DEFAULT_DAYS = 30
CLASSIFICATION_LOOKBACK_MAX_DAYS = 30


def get_classification_lookback_days():
    """How far back the topic classifier reads a user's messages, in days.

    Admin-settable via the `classification_lookback_days` GeneralSetting.
    Clamped to [1, 30]: 0 would classify nothing, and 30 days matches the
    explicit cap promised in the admin UI. Default 30 if unset.
    """
    raw = (get_setting('classification_lookback_days', '') or '').strip()
    if not raw:
        return CLASSIFICATION_LOOKBACK_DEFAULT_DAYS
    try:
        days = int(raw)
    except ValueError:
        return CLASSIFICATION_LOOKBACK_DEFAULT_DAYS
    return max(1, min(days, CLASSIFICATION_LOOKBACK_MAX_DAYS))


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
    build_system_blocks,
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


# ===================== REPLY-MODEL ACCOUNT-FAILURE ALERTING ================
# Account-level failures of the Claude API — credit balance exhausted, revoked/
# invalid key — kill every customer reply AND the knowledge-gap handoff that
# would normally page staff, so an outage is invisible from inside the product
# (the 2026-06-11 quota outage on the previous provider ran 6+ hours
# unnoticed). alert_model_failure is called from generate_bot_response's
# failure path and Telegram-pings staff directly (naming REPLY_PROVIDER_LABEL),
# deduplicated via GeneralSetting so the alert storm of an outage (every
# inbound message fails) produces at most one ping per cooldown window.

MODEL_ALERT_STATE_KEY = 'model_failure_alerted_at'


def _is_account_error(exc):
    """True only for errors that mean EVERY call will keep failing — a bad
    API key or an exhausted credit balance. Transient failures (429 rate
    limit, 529 overloaded, 5xx) self-heal and must NOT page staff.

    Anthropic reports an empty prepaid balance as a 400 invalid_request_error
    whose message says "credit balance is too low" — NOT as a 429 — so the
    billing check lives on BadRequestError. A 429 that mentions credit or a
    spend limit is treated the same way, defensively."""
    if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
        return True
    if isinstance(exc, (BadRequestError, RateLimitError)):
        msg = str(exc).lower()
        return 'credit balance' in msg or 'spend limit' in msg
    return False


def alert_model_failure(exc):
    """Telegram-alert staff about an account-wide Claude API failure, at most
    once per MODEL_ALERT_COOLDOWN_HOURS (default 6). Runs on the customer-
    reply path, so it must never raise — any internal failure is logged and
    swallowed. Returns True if an alert was sent this call."""
    try:
        if not _is_account_error(exc):
            return False
        cooldown = timedelta(hours=max(1, _safe_int_env('MODEL_ALERT_COOLDOWN_HOURS', 6)))
        now = datetime.utcnow()
        row = GeneralSetting.query.filter_by(key=MODEL_ALERT_STATE_KEY).first()
        if row and row.value:
            try:
                if now - datetime.fromisoformat(row.value) < cooldown:
                    return False
            except ValueError:
                pass  # unparseable timestamp -> treat as never alerted
        # Claim the window BEFORE sending: during an outage every inbound
        # message fails at once across workers, and committing first keeps
        # that storm from paging staff more than once.
        if row is None:
            db.session.add(GeneralSetting(key=MODEL_ALERT_STATE_KEY, value=now.isoformat()))
        else:
            row.value = now.isoformat()
        db.session.commit()
        send_telegram_notification(
            f"⚠️ {REPLY_PROVIDER_LABEL} данс ажиллахгүй байна — бот хэрэглэгчдэд "
            "бодит хариу өгч чадахгүй, шилжүүлгийн (handoff) мэдэгдэл ч "
            "ажиллахгүй.\n\n"
            "Шалтгаан: кредит дууссан эсвэл API key хүчингүй. "
            f"{REPLY_PROVIDER_BILLING_HINT} хуудсаар орж кредит/түлхүүрээ "
            "шалгана уу. Засмагц бот шууд сэргэнэ.\n\n"
            f"Дэлгэрэнгүй: {str(exc)[:300]}"
        )
        return True
    except Exception as alert_exc:
        logger.error("Model failure alert could not be sent: %s", alert_exc)
        try:
            db.session.rollback()
        except Exception:
            pass
        return False


TRANSIENT_ERROR_REPLY = (
    "Уучлаарай, түр зуурын саатал гарсан байна. Хэдхэн минутын дараа дахин бичээрэй."
)


def _degraded_reply(handoff_just_triggered):
    """What the customer hears when no model reply is available. Right after a
    handoff fired this turn the dynamic acknowledgement won't arrive, so fall
    back to the static, office-hours-aware handoff template — the customer
    still hears about staff routing instead of a generic 'try again later'."""
    if handoff_just_triggered:
        return _static_handoff_reply()
    return TRANSIENT_ERROR_REPLY


def _claude_messages(conversation_history, user_message):
    """Shape the stored conversation for the Messages API. The API rejects
    whitespace-only text blocks and requires the first message to come from
    the user, so empty turns and any leading assistant turns are dropped (the
    last-10 window can open on a bot reply). The inbound message goes last."""
    messages = []
    for m in conversation_history:
        content = (m.get('content') or '').strip()
        if not content:
            continue
        if not messages and m.get('role') != 'user':
            continue
        messages.append({'role': m['role'], 'content': content})
    # The webhook persists the inbound row BEFORE the reply runs, so the
    # last-N window normally ends with this very message. Drop that copy so
    # the model doesn't see the customer "repeating themselves".
    if (messages and messages[-1]['role'] == 'user'
            and messages[-1]['content'] == (user_message or '').strip()):
        messages.pop()
    messages.append({'role': 'user', 'content': user_message})
    return messages


def generate_bot_response(user_message, conversation_history,
                          session_state='new', funnel_stage='curious',
                          user_first_name='', handoff_pending=False,
                          handoff_just_triggered=False, ask_name=False,
                          is_lead=False, prior_summary=''):
    """Generate the customer reply on REPLY_MODEL (Claude — see the client
    block at the top of this module). `handoff_pending=True` enables
    advisory mode in the system prompt — bot keeps helping but doesn't
    re-route the customer to staff (since the handoff was already fired).
    `handoff_just_triggered=True` is set for the SINGLE reply right after
    a fresh handoff fires this turn; it overrides advisory mode so the
    bot can craft a warm, mood-aware acknowledgement that mentions staff
    routing and the office-hours ETA. `ask_name` / `is_lead` /
    `prior_summary` describe the customer (see build_system_prompt)."""
    try:
        stable, volatile = build_system_blocks(
            session_state=session_state,
            funnel_stage=funnel_stage,
            user_first_name=user_first_name,
            handoff_pending=handoff_pending,
            handoff_just_triggered=handoff_just_triggered,
            ask_name=ask_name,
            is_lead=is_lead,
            prior_summary=prior_summary,
        )
        # Prompt caching: the stable half (persona, catalog, rules — the bulk
        # of ~15K tokens) is identical for every customer and every minute,
        # so it is marked as a cache breakpoint; the volatile half (time,
        # customer state, session rule) follows it. Cache hits show up as
        # usage.cache_read_input_tokens in the `reply usage` log line.
        cache_control = {'type': 'ephemeral'}
        if REPLY_CACHE_TTL == '1h':
            cache_control['ttl'] = '1h'
        create_kwargs = {
            'model': REPLY_MODEL,
            'max_tokens': REPLY_MAX_TOKENS,
            # Thinking is adaptive and stays ON (Opus 5.5 rejects turning it
            # off); effort is the depth control — see REPLY_EFFORT. We never
            # send sampling params (temperature etc.) — current models 400.
            'output_config': {'effort': REPLY_EFFORT},
            'system': [
                {'type': 'text', 'text': stable, 'cache_control': cache_control},
                {'type': 'text', 'text': volatile},
            ],
            'messages': _claude_messages(conversation_history, user_message),
        }
        # Offer the staff-handoff tool only in NORMAL mode — i.e. exactly when
        # KNOWLEDGE_GAP_HANDOFF_RULE is in the prompt. In advisory / just-
        # triggered modes the customer is already being routed to staff.
        if not handoff_pending and not handoff_just_triggered:
            create_kwargs['tools'] = DEFER_TO_STAFF_TOOL

        response = client.messages.create(**create_kwargs)

        usage = getattr(response, 'usage', None)
        if usage is not None:
            # One line per reply so Render logs (query `reply usage`) give a
            # cheap cost/volume picture without a dashboard.
            logger.info(
                "reply usage model=%s in=%s out=%s cache_read=%s",
                REPLY_MODEL, getattr(usage, 'input_tokens', None),
                getattr(usage, 'output_tokens', None),
                getattr(usage, 'cache_read_input_tokens', None),
            )

        # If the model chose to defer (it lacks the info, the customer asked
        # for a person, or complained), translate that structured tool call
        # into the internal handoff marker the webhook detects — carrying the
        # category + reason so staff see WHY. This is reliable, unlike asking
        # the model to emit a literal marker in its text (which chat models
        # strip). The tool call wins over any preamble text in the response.
        for block in response.content:
            if block.type == 'tool_use' and block.name == 'defer_to_staff':
                args = block.input if isinstance(block.input, dict) else {}
                reply = (args.get('reply_to_customer') or '').strip() or _static_handoff_reply()
                marker = format_handoff_marker(args.get('category'), args.get('reason'))
                return f"{marker} {reply}"

        text = _response_text(response)
        if text:
            return text
        # No text at all: thinking ate the whole max_tokens budget
        # (stop_reason=max_tokens) or a safety refusal (stop_reason=refusal).
        # Never send the customer an empty message.
        logger.warning(
            "Empty reply from %s (stop_reason=%s); sending the degraded reply. "
            "If stop_reason is max_tokens, raise REPLY_MAX_TOKENS.",
            REPLY_MODEL, getattr(response, 'stop_reason', None),
        )
        return _degraded_reply(handoff_just_triggered)
    except Exception as e:
        logger.error("Error generating response: %s", e)
        # Key/credit failures also kill the defer_to_staff handoff tool, so
        # nothing would page staff — alert them directly (deduplicated).
        alert_model_failure(e)
        return _degraded_reply(handoff_just_triggered)


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

        response = client.messages.create(
            model=BACKGROUND_MODEL,
            max_tokens=512,
            output_config={'effort': 'low'},
            messages=[{"role": "user", "content": analysis_prompt}],
        )
        return _response_text(response) or None
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
# Hard rule on Mongolian fluency. Reply models occasionally slip into
# AI-translation-flavoured constructions (wrong case on subject pronouns,
# verbose noun-clause objects, machine-translated idioms). The model needs
# explicit ✗/✓ pairs to anchor on; the named-error pattern is the same
# trick we use elsewhere in the prompt to lock down behaviour.
LANGUAGE_QUALITY_RULE = (
    "ХАРИУЛТЫН ХЭЛ — ҮРГЭЛЖ МОНГОЛ (КИРИЛЛ): Хэрэглэгч латин үсгээр, орос, "
    "англи эсвэл өөр ямар ч хэлээр бичсэн байсан ч ЗӨВХӨН монгол кирилл "
    "үсгээр хариул. Хэрэглэгчийн хэлийг давтаж бүү хариул; өөр хэлээр "
    "хариулахыг хүсвэл эелдгээр монголоор үргэлжлүүл.\n"
    "МОНГОЛ ХЭЛНИЙ ЧАНАР (БҮХ ХАРИУЛТАНД):\n"
    "Полишлогдсон, амьд хүний ярианы хэлээр бич. Машин орчуулга шиг "
    "сонсогддог хатуу бүтэц, хэт албан ёсны хэллэг, утгагүй нэрлэх "
    "хэллэгээс зайлсхий. Subject pronoun-ыг үргэлж нэрлэх (nominative) "
    "хэлбэрээр бич — genitive (миний/танай/түүний) субьект болгож "
    "бүү ашигла.\n"
    "✗ 'Миний бэлэн байна'       → ✓ 'Би бэлэн байна'\n"
    "✗ 'Танай асуух уу?'           → ✓ 'Та асуух уу?' / 'Танд асуулт байна уу?'\n"
    "✗ 'Хариултын явцыг ярилцаад үзэх үү?' → ✓ 'Танд яаж туслахыг "
    "хүсэж байна?' / 'Юу тодруулж өгье?'\n"
    "✗ 'Та тусламж хүсэх боломжтой' → ✓ 'Танд тусалъя' / 'Туслахад "
    "баяртай байна'\n"
    "✗ 'Мэдээллийг өгөх боломжтой' → ✓ 'Мэдээллээ хэлж өгье'\n"
    "Хэт урт, нэр үгийн жагсаалттай өгүүлбэр бүү бич. Богино, шууд, "
    "дулаахан боловч ЦЭВЭР өгүүлбэр: ярианы сул үг ('шүү', 'шүү дээ', "
    "'л дээ', 'юм даа', 'шдээ') огт бүү хэрэглэ."
)


# Calibrated honesty on professional-domain facts (tax/accounting/audit/law).
# The reply model half-knows Mongolian financial regulation — enough to sound
# confident, not enough to be reliably correct — so without this it confidently
# fabricates specifics (e.g. inventing what form "ТТ-13" is) and folds to the
# customer's pushback (sycophancy). This rule keeps the bot ANSWERING general
# professional questions while refusing to invent specifics; the goal is
# accuracy, NOT silence. Recurring specifics should be captured as accurate
# FAQs (the admin provides the correct text — see the leverage hierarchy).
PROFESSIONAL_ACCURACY_RULE = (
    "МЭРГЭЖЛИЙН ҮНЭН ЗӨВ БАЙДАЛ (нягтлан / татвар / аудит / хууль эрх зүй):\n"
    "Чи туслах зөвлөх — гэрчилгээтэй нягтлан бодогч, татварын байцаагч "
    "эсвэл хуульч БИШ. Мэргэжлийн сэдвийн ЕРӨНХИЙ ойлголтыг (ж: санхүүгийн "
    "тайлан гэж юу вэ, аудит юу хийдэг, татварын ерөнхий төрлүүд) өөртөө "
    "итгэлтэй, ойлгомжтой тайлбарлаж ТУСАЛ. Энэ бол силенс/дамжуулалт биш.\n"
    "ГЭХДЭЭ дараах НАРИЙН зүйлсийг ЯГ, баттай мэдэхгүй бол ТААМАГЛАЖ "
    "хариу БҮҮ ЗОХИО: тайлан/маягтын код ба түүний яг утга (ж: ТТ-13, "
    "ХХОАТ/НӨАТ-ын маягтын дугаарууд), татварын яг хувь хэмжээ, тайлан "
    "тушаах хугацаа/огноо, хуулийн тодорхой зүйл заалт, нарийн журам.\n"
    "Ийм нарийн зүйл асуувал: (1) өөрийн ИТГЭЛТЭЙ мэдэх ерөнхий хэсгээр "
    "нь тусал; (2) яг нарийн/албан ёсны хариуг 'баталгаатай хэлэхийн "
    "тулд' манай мэргэжлийн нягтлан/зөвлөхтэй холбож өгье гэж ЭЕЛДЭГЭЭР "
    "санал болго (энэ нь Magic-ийн давуу тал); (3) ХУДАЛ тодорхой хариуг "
    "хэзээ ч бүү өг.\n"
    "СИКОФАНТ БҮҮ БОЛ: Хэрэглэгч 'тийм биш', 'буруу байна' гэж залруулахад, "
    "хэрэв чи зөв хариуг ЯГ мэдэхгүй бол шууд зөвшөөрөөд ӨӨР таамаг хариу "
    "БҮҮ зохио. '...байж магадгүй, би яг нарийныг нь баталгаажуулъя' гэж "
    "эелдэг хэлээд, шаардлагатай бол ажилтанд холбо."
)


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

# Injected into the system prompt for the ONE reply right after a handoff
# was just triggered. Overrides HANDOFF_ADVISORY_RULE for this turn so the
# bot CAN (and should) mention staff routing — this is the first-time
# acknowledgement. Tells the bot to read the user's emotional tone, use
# the company contact block's office hours, and keep the door open for
# continued conversation rather than emitting a cold "we got your
# message" template.
HANDOFF_JUST_TRIGGERED_RULE = (
    "ХАНДОФФ ШИНЭЭР ТРИГГЕР БОЛСОН — ЭНЭ ХАРИУЛТ НЬ АЖИЛТАНД "
    "ДАМЖУУЛСНЫ АНХ УДААГИЙН МЭДЭГДЭЛ (advisory-биш, чиглүүлж БОЛНО):\n"
    "  1) Хэрэглэгчийн сүүлийн мессежийн сэтгэл хөдлөлийг (бухимдсан, "
    "эргэлзэлтэй, яаралтай, эерэг г.м.) уншиж empathy-тэй хариулна — "
    "сэтгэлзүйн ажилтан шиг.\n"
    "  2) 'КОМПАНИЙН ХОЛБОО БАРИХ' хэсэгт байгаа ажлын цагийг ашиглаж: "
    "одоо ажлын цагийн дотор бол '~10-30 минутын дотор ажилтан "
    "хариулна' гэж хэл; ажлын цагийн гадуур бол 'манай ажилтан "
    "маргааш {start}:00–{end}:00 ажлын цагт хариулна' гэж тодорхой "
    "тоо хэлж тайвшруул.\n"
    "  3) WARM SALESMAN тоноор сонголтыг үлдээ: 'Хүлээж байх хооронд "
    "танд тусламж хэрэгтэй бол би энд байна — анги, үнэ, бусад "
    "үйлчилгээний талаар асуух зүйл байвал чөлөөтэй бичээрэй' гэх "
    "мэт. Хэрэглэгчийг ганцаардуулахгүй, ярианы хаалгыг нээлттэй "
    "үлдээ.\n"
    "  4) Хэрэглэгч сонирхож байсан асуулттай ХОЛБООТОЙ нэг "
    "follow-up өгүүлбэр санал болго (жишээ нь: 'Энэ хооронд танд "
    "тохирох ангийн талаар яриад үзэх үү?' эсвэл 'Хүсвэл аудитын "
    "процессыг тайлбарлая').\n"
    "  5) ХҮЙТЭН template-маягт хариулт битгий бичих: 'Таны мессежийг "
    "хүлээж авлаа' гэж зөвхөн хэлээд орхих ёсгүй. Шууд утга, дулаахан "
    "өнгөтэй, хэрэглэгчид тохирсон хариулт өг.\n"
    "  6) Энэ ӨВӨРМӨЦ турш HANDOFF ADVISORY-ын 'дахин үл-чиглүүл' "
    "дүрмийг ҮЛ ХАМААРНА — ажилтны тухай дурдах НЬ зөв."
)


# Injected into the system prompt in NORMAL mode (no active handoff) so the bot
# ESCALATES honestly when it lacks the information to answer, instead of
# inventing a login link / password / price it was never given. The model
# signals this by CALLING the defer_to_staff tool (reliable, structured) — NOT
# by emitting a text marker, which chat models drop unreliably. generate_bot_
# response translates that tool call into the internal HANDOFF_MARKER the webhook
# already understands. ETA + door-open phrasing relies on the ОДООГИЙН ЦАГ +
# КОМПАНИЙН ХОЛБОО БАРИХ blocks already in the prompt.
KNOWLEDGE_GAP_HANDOFF_RULE = (
    "АЖИЛТАНД ШИЛЖҮҮЛЭХ (defer_to_staff) — ЗӨВХӨН ГУРВАН ТОХИОЛДОЛД:\n"
    "  1) knowledge_gap — хариулт чиний мэдлэгт БАЙХГҮЙ: онлайн сургалтын "
    "платформд нэвтрэх, нэвтрэх нэр/нууц үг/линк, төлбөр төлсний дараах "
    "хандалт, техникийн асуудал, бүртгэлийн дараах тодорхой журам г.м. "
    "Таамаг, зохиомол хариулт ОГТ БҮҮ ӨГ; ЛИНК/URL, нууц үг, үнийг хэзээ ч "
    "бүү зохио.\n"
    "  2) wants_human — хэрэглэгч ажилтантай / хүнтэй ярихыг ӨӨРӨӨ хүссэн.\n"
    "  3) complaint — хэрэглэгч гомдолтой, бухимдсан, үйлчилгээнд сэтгэл "
    "дундуур.\n"
    "ШИЛЖҮҮЛЭХГҮЙ, ӨӨРӨӨ ШИЙД: анги, үнэ, хуваарь, FAQ, каталогт байгаа "
    "үйлчилгээ; бүртгүүлэх хүсэлт (бүртгэлийн линк + утас үлдээх сонголтыг "
    "өг); хэрэглэгч утасны дугаараа өгсөн (энэ бол lead — 'Баярлалаа, "
    "ажилтан тантай холбогдоно' гэж баталгаажуулахад хангалттай, шилжүүлэх "
    "хэрэггүй); 'үнэтэй', 'бодъё' гэх эсэргүүцэл (БОРЛУУЛАЛТЫН ЗАН ҮЙЛ-ийн "
    "дагуу хариул). Нэг мессежид чадах ба чадахгүй хэсэг хоёул байвал "
    "чадахаа хариулаад, чадахгүйн талаар л defer_to_staff дууд.\n"
    "Дуудахдаа reply_to_customer-т дулаахан, эелдэг мессеж бич: асуултыг "
    "товч хүлээн зөвшөөр; манай ажилтан тодорхой шийдэж/хэлж өгнө гэж хэл; "
    "ХУГАЦААГ мэдэгд (дээрх 'ОДООГИЙН ЦАГ'-ийг ажлын цагтай харьцуул — "
    "ажлын цагийн ДОТОР бол 'удахгүй, ~10–30 минутын дотор', ГАДУУР бол "
    "'ажилтан маргааш ажлын цагт хариулна'); мөн 'энэ хооронд өөр асуух "
    "зүйл байвал би энд байна' гэж нэм. reason-д ажилтанд зориулж юу "
    "дутуу/хүссэнийг нэг мөрөөр бич."
)

# Tool the reply model can call (normal mode only) to hand off to a human when
# it lacks the information to answer. generate_bot_response detects the
# tool_use block and re-emits HANDOFF_MARKER so the existing webhook handoff
# path fires. Claude Messages API tool shape (name / description /
# input_schema); tool choice stays on the default 'auto' — Opus 5.5 rejects a
# forced tool call, and the prompt rule already says when to use it.
DEFER_TO_STAFF_TOOL = [{
    "name": "defer_to_staff",
    "description": (
        "Hand the conversation to a human staff member. Call it ONLY in three "
        "cases: (1) knowledge_gap — the customer needs a specific fact, link, "
        "login, password, policy or procedure that is NOT in your knowledge "
        "(e.g. how to log in to the online class platform, access after "
        "payment, technical problems); (2) wants_human — the customer "
        "explicitly asks to talk to a person / staff member; (3) complaint — "
        "the customer is upset or complains about the service. NEVER call it "
        "for things you handle yourself: course info, prices, schedule, FAQs, "
        "listed services (give their link), registration intent (give the "
        "registration link and offer to leave a phone number), a customer "
        "leaving their phone number (that is a lead, not a handoff), or "
        "objections like 'too expensive' / 'let me think about it'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": ["knowledge_gap", "wants_human", "complaint"],
                "description": "Why staff must step in.",
            },
            "reply_to_customer": {
                "type": "string",
                "description": (
                    "Warm Mongolian message to send the customer now: "
                    "briefly acknowledge the question, say a staff member "
                    "will help, give the ETA (within office hours -> "
                    "~10-30 min; outside office hours -> tomorrow during "
                    "office hours, using the time + office-hours info in "
                    "the system prompt), and offer to keep helping with "
                    "anything else in the meantime."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "One-line note for staff (Mongolian is fine): what exactly "
                    "is missing or wanted."
                ),
            },
        },
        "required": ["category", "reply_to_customer"],
    },
}]


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


def should_handoff(message_text, fb_user):
    """Decide, BEFORE the model replies, whether this message is an explicit
    request for a human (admin-managed keyword list) — or, at balanced /
    aggressive sensitivity, frustration. Everything else is the model's call
    via the defer_to_staff tool: it knows what it can answer, keyword
    matching doesn't. (Mentioning a 'refer' business line used to trigger a
    handoff here too; the prompt now answers those with the service's link.)"""
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
    polite waiting message.

    The bot DOES NOT mute itself here — it stays in advisory mode and
    keeps replying to subsequent customer questions. Muting happens only
    when staff explicitly take over the chat via take_over_chat (the
    "Хариуцах" button on Work Tasks). When the staff later marks the
    issue resolved, the bot is automatically re-enabled (see the resolve
    handler in routes/admin/work_tasks.py), and staff can do that before
    the take-over timer expires.

    Pass `send_user_message=False` when the bot has already sent the
    customer a deferring reply (the defer_to_staff tool's reply_to_customer)
    so the user doesn't receive two back-to-back messages.
    """
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


# NOTE (2026-11): the old `bot_response_implies_handoff` phrase scan was
# removed. It fired a handoff whenever the bot's OWN reply contained "манай
# ажилтан удахгүй холбогдоно" and similar — exactly what the prompt tells the
# bot to say after a phone number is captured or when offering the
# registration link + phone options — so every lead and every registration
# offer became a Work Tasks entry, a Telegram ping and advisory mode. The
# defer_to_staff tool (structured, with a category) is the only bot-initiated
# handoff path now.


# ===================== CROSS-SESSION SALES MEMORY =====================
# The reply prompt only sees the last few messages. When a customer comes
# back after a gap, a 2-3 line summary of the earlier conversation (goal,
# situation, course of interest, objections, where things stood) is written
# by BACKGROUND_MODEL onto FacebookUser.bot_summary and injected into the
# prompt, so the bot picks up where it left off instead of re-discovering.

USER_SUMMARY_MAX_MESSAGES = 40
USER_SUMMARY_MIN_MESSAGES = 4
USER_SUMMARY_MAX_CHARS = 1000


def refresh_user_summary(fb_user):
    """(Re)write fb_user.bot_summary from their recent conversation. Fail-soft:
    any model/DB error is logged and leaves the previous summary in place.
    Returns the new summary, or None when nothing was written."""
    rows = (Message.query
            .filter_by(facebook_user_id=fb_user.id)
            .order_by(Message.created_at.desc())
            .limit(USER_SUMMARY_MAX_MESSAGES)
            .all())
    rows = [m for m in rows if (m.content or '').strip()]
    if len(rows) < USER_SUMMARY_MIN_MESSAGES:
        return None
    transcript = '\n'.join(
        f"{'Хэрэглэгч' if m.sender == 'user' else 'Бот'}: {m.content.strip()}"
        for m in reversed(rows)
    )
    prompt = (
        "Доорх бол сургалтын төвийн борлуулалтын ботын нэг харилцагчтай "
        "хийсэн яриа. Ботод дараагийн удаа санах зориулалттай 2-3 мөр "
        "ХУРААНГУЙ бич (монгол хэлээр, зөвхөн яриан дахь баримт):\n"
        "- Зорилго / нөхцөл (ажил, цаг, мэдлэгийн түвшин)\n"
        "- Сонирхсон анги, үйлчилгээ; дурдсан үнэ\n"
        "- Эсэргүүцэл, эргэлзээ\n"
        "- Хаана зогссон: утас өгсөн эсэх, бүртгүүлэх шийдвэр, ажилтанд "
        "шилжүүлсэн эсэх\n"
        "Зохиохгүй, 400 тэмдэгтээс хэтрүүлэхгүй, гарчиг, bullet-гүй энгийн "
        "өгүүлбэрээр.\n\n"
        f"ЯРИА:\n{transcript}"
    )
    try:
        resp = client.messages.create(
            model=BACKGROUND_MODEL,
            max_tokens=512,
            output_config={'effort': 'low'},
            system="Чи борлуулалтын ярианы хураангуй бичдэг туслах. Зөвхөн хураангуйг л буцаа.",
            messages=[{'role': 'user', 'content': prompt}],
        )
        summary = _response_text(resp)[:USER_SUMMARY_MAX_CHARS]
    except Exception as e:
        logger.warning("refresh_user_summary: model call failed for user %s: %s", fb_user.id, e)
        return None
    if not summary:
        return None
    try:
        fb_user.bot_summary = summary
        fb_user.bot_summary_at = datetime.utcnow()
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning("refresh_user_summary: could not save for user %s: %s", fb_user.id, e)
        return None
    return summary


# Hidden control marker the bot prefixes to a reply when it is deferring to
# staff (see KNOWLEDGE_GAP_HANDOFF_RULE). It is a system-only signal and MUST be
# stripped before the reply reaches the user. The full form carries the
# defer_to_staff category + the model's one-line reason for staff:
#   [HANDOFF:knowledge_gap|платформын нууц үг]
# The bare `[HANDOFF]` form is still accepted (legacy / marker-only replies).
HANDOFF_MARKER = '[HANDOFF]'
HANDOFF_CATEGORIES = ('knowledge_gap', 'wants_human', 'complaint')
HANDOFF_CATEGORY_LABELS = {
    'knowledge_gap': 'мэдлэг дутуу',
    'wants_human': 'ажилтантай ярих хүсэлт',
    'complaint': 'гомдол',
}
_HANDOFF_MARKER_RE = re.compile(r'\[HANDOFF(?::([a-z_]+))?(?:\|([^\]]*))?\]')


def format_handoff_marker(category=None, reason=None):
    """Render the marker generate_bot_response prefixes to a deferring reply.
    Unknown categories fall back to knowledge_gap; the reason is squeezed to
    one short line without the characters that would break the marker."""
    if category not in HANDOFF_CATEGORIES:
        category = 'knowledge_gap'
    reason = re.sub(r'[\[\]|\r\n]+', ' ', str(reason or '')).strip()[:120]
    return f"[HANDOFF:{category}|{reason}]"


def extract_handoff(bot_response):
    """Strip every handoff marker from the bot's reply.

    Returns (cleaned_text, detail): detail is None when no marker was present,
    else {'category': ..., 'reason': ...} from the first marker. The marker
    must never be shown to the customer; if the model emitted ONLY the marker,
    a warm static deferral is substituted so we never send an empty message."""
    if not bot_response or '[HANDOFF' not in bot_response:
        return bot_response, None
    found = _HANDOFF_MARKER_RE.search(bot_response)
    if not found:
        return bot_response, None
    category = found.group(1) if found.group(1) in HANDOFF_CATEGORIES else 'knowledge_gap'
    reason = (found.group(2) or '').strip()
    cleaned = _HANDOFF_MARKER_RE.sub('', bot_response).strip()
    if not cleaned:
        cleaned = _static_handoff_reply()
    return cleaned, {'category': category, 'reason': reason}


def _static_handoff_reply():
    """Warm, ETA-aware fallback used only if the model emits the marker with no
    message of its own — so the customer never receives an empty reply."""
    if _is_off_hours():
        try:
            start = int(get_setting('office_hours_start', '8') or '8')
            end = int(get_setting('office_hours_end', '22') or '22')
        except ValueError:
            start, end = 8, 22
        return HANDOFF_USER_REPLY_OFF_HOURS.format(start=start, end=end)
    return HANDOFF_USER_REPLY


def extract_handoff_marker(bot_response):
    """Boolean flavour of extract_handoff: (cleaned_text, had_marker)."""
    cleaned, detail = extract_handoff(bot_response)
    return cleaned, detail is not None


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
    make_course_dates_nullable,
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
        link = get_business_website_url()
        link_line = f"\n\nДэлгэрэнгүй мэдээлэл: {link}" if link else ""
        return (
            f"{name_prefix}та бүртгүүлэх талаар бодож үзсэн байх. "
            "Утасны дугаараа үлдээвэл бид өөрсдөө эргэж холбогдоно. "
            "Аль сургалтад хамрагдахаа хэлбэл тухайн ангийн бүртгэлийн "
            "мэдээллийг илгээе." + link_line
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


def check_facebook_token():
    """Ping the Graph API to verify the Page access token still works.
    Returns (ok: bool, detail: str). The recurring OAuthException 190/463
    (expired long-lived token) is the #1 cause of a silently-dead bot, so we
    surface it proactively rather than waiting for customers to notice.

    We deliberately query `me` (never FACEBOOK_PAGE_ID) and then assert the id
    it returns IS the Page: a USER token reads a Page by its numeric id just
    fine, so targeting the id would report a healthy token while every send
    fails GraphMethodException code:100 subcode:33 ("Object with ID 'me' does
    not exist"). The Send API posts to me/messages, so the only check that
    matches production is whether `me` resolves to the Page."""
    url = "https://graph.facebook.com/v18.0/me"
    try:
        resp = requests.get(
            url, params={'fields': 'id,name'},
            headers=_fb_auth_headers(), timeout=8,
        )
        if resp.status_code != 200:
            return False, f"status={resp.status_code} body={(resp.text or '')[:300]}"
        try:
            data = resp.json() or {}
        except ValueError:
            return False, "status=200 but the body was not JSON"
        me_id = str(data.get('id') or '')
        me_name = data.get('name') or ''
        if FACEBOOK_PAGE_ID and me_id != str(FACEBOOK_PAGE_ID):
            return False, (
                f"USER token, not a PAGE token: me/ returned id={me_id} "
                f"name={me_name!r} but FACEBOOK_PAGE_ID={FACEBOOK_PAGE_ID}. "
                "Every send will fail with GraphMethodException code:100 "
                "subcode:33. Re-copy the token from Graph API Explorer with "
                "'User or Page' set to Page Access Tokens."
            )
        return True, f"ok (page={me_name!r} id={me_id})"
    except Exception as e:
        return False, f"exception={e}"


def get_page_subscribed_fields():
    """Return (ok, sorted list of webhook fields this Page is subscribed to),
    or (False, error_string). This is the PAGE-level subscription (distinct
    from the App-level webhook field toggles) — Facebook only delivers an
    event type if it's listed here."""
    url = "https://graph.facebook.com/v18.0/me/subscribed_apps"
    try:
        resp = requests.get(url, headers=_fb_auth_headers(), timeout=8)
        if resp.status_code != 200:
            return False, f"status={resp.status_code} body={(resp.text or '')[:300]}"
        apps = (resp.json() or {}).get('data', [])
        fields = []
        for a in apps:
            fields.extend(a.get('subscribed_fields', []))
        return True, sorted(set(fields))
    except Exception as e:
        return False, str(e)


def ensure_page_subscriptions():
    """Make sure the Page is subscribed to `message_echoes` (required for the
    automatic human-takeover mute) WITHOUT dropping any field it already has —
    we read the current set and POST the UNION, so we never accidentally
    unsubscribe `messages` and break the bot. Returns (ok, detail_dict)."""
    ok, current = get_page_subscribed_fields()
    if not ok:
        return False, {'error': current}
    current_set = set(current)
    desired = current_set | {'messages', 'message_echoes'}
    if desired == current_set:
        return True, {'before': sorted(current_set), 'after': sorted(current_set), 'added': []}
    url = "https://graph.facebook.com/v18.0/me/subscribed_apps"
    try:
        resp = requests.post(
            url, params={'subscribed_fields': ','.join(sorted(desired))},
            headers=_fb_auth_headers(), timeout=8,
        )
        if resp.status_code != 200:
            return False, {'error': f"status={resp.status_code} body={(resp.text or '')[:300]}",
                           'before': sorted(current_set)}
    except Exception as e:
        return False, {'error': str(e), 'before': sorted(current_set)}
    ok2, after = get_page_subscribed_fields()
    return True, {
        'before': sorted(current_set),
        'after': sorted(after) if ok2 else 'unknown',
        'added': sorted(desired - current_set),
    }


def report_token_health(detail):
    """Alert on ONE failed health check, and say which kind of failure it was.

    A recognised token death (190/463, 190/460, 102, 100/33) is a CONFIRMED
    outage, so it routes through the same definite, cooled-down alert the send
    path fires — one outage produces one message, whichever path notices first.
    Only a genuinely ambiguous failure (Graph unreachable, a 500, a timeout)
    gets the hedged wording, because only there is the uncertainty real.

    Returns 'token', 'unknown' or 'quiet' (cooldown still holding). Extracted
    from token_health_task purely so this decision is testable without running
    an infinite loop."""
    if alert_facebook_token_failure(detail):
        return 'token'
    if _is_facebook_token_error(detail)[0]:
        return 'quiet'  # confirmed dead token, already alerted this window
    if _claim_alert_window(FB_HEALTH_ALERT_STATE_KEY):
        send_telegram_notification(
            "⚠️ Facebook Page token-г шалгаж чадсангүй. Graph API-тай "
            "холбогдоход алдаа гарлаа — түр зуурын сүлжээний асуудал байж "
            "болох ч, бот хариу илгээхгүй байвал токеноо шалгана уу "
            "(OPERATIONS.md §4).\n\n"
            f"Дэлгэрэнгүй: {detail[:300]}"
        )
        return 'unknown'
    return 'quiet'


def token_health_task(app):
    """Background loop: every TOKEN_CHECK_INTERVAL_HOURS, verify the Facebook
    Page token and Telegram-alert staff if it's invalid/expired — catching the
    OAuthException 190/463 BEFORE customers meet a silent bot."""
    interval = max(1, _safe_int_env('TOKEN_CHECK_INTERVAL_HOURS', 6)) * 3600
    time.sleep(60)  # small initial delay so boot logs stay clean
    while True:
        try:
            with app.app_context():
                ok, detail = check_facebook_token()
                if ok:
                    logger.info("FB token health: OK")
                else:
                    logger.warning("FB token health: FAILED %s", detail)
                    report_token_health(detail)
        except Exception as e:
            logger.error("token_health_task error: %s", e)
        time.sleep(interval)


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
    a transient model-API hiccup. Promoted clusters (admin already turned
    them into FAQ) are preserved across runs.
    """
    import json as _json

    if not ANTHROPIC_API_KEY:
        logger.info('cluster_chat_questions: ANTHROPIC_API_KEY not set, skipping.')
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
        # Structured outputs constrain the reply to CLUSTERS_FORMAT. Medium
        # effort: grouping a few hundred phrasings into themes is real
        # reasoning, and this runs once a week on the cheap model.
        resp = client.messages.create(
            model=BACKGROUND_MODEL,
            max_tokens=8192,
            output_config={'effort': 'medium', 'format': CLUSTERS_FORMAT},
            system='You output strict JSON only. No prose, no markdown.',
            messages=[{'role': 'user', 'content': _build_clustering_prompt(questions)}],
        )
        raw = _response_text(resp)
    except Exception as e:
        logger.error('cluster_chat_questions: Claude API error: %s', e)
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
    API credits unnecessarily."""
    while True:
        try:
            with app.app_context():
                cluster_chat_questions()
        except Exception as e:
            logger.error("Clustering error: %s", e)
        # Once per week. Don't tighten without checking the model cost.
        time.sleep(7 * 24 * 60 * 60)
