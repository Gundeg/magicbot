"""MagicBot — Flask application entry point.

This module:
  1. Loads env vars
  2. Creates the Flask app and configures it
  3. Initializes extensions (db, login_manager, csrf)
  4. Wires up the login user-loader and tenant context processor
  5. Imports models (registers them on db)
  6. Imports route modules (registers HTTP handlers via `@app.route`)
  7. Runs init_db() and starts optional background threads

All domain logic lives in `services.py`, models in `models.py`, route
handlers in `routes/`. This file is the wiring only.
"""
import logging
import os
from threading import Thread

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask
from werkzeug.security import generate_password_hash

from extensions import db, login_manager, csrf, migrate, limiter

# Configure logging once at import. Routes/services import `logging` directly
# and call `logger.info/warning/error/exception`; pinning the root config here
# means gunicorn workers and one-shot scripts share the same format.
logging.basicConfig(
    level=os.environ.get('LOG_LEVEL', 'INFO').upper(),
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)

# ===================== APP CONSTRUCTION =====================

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
    after_scheme = uri[len('sqlite:'):].lstrip('/')
    sqlite_path = '/' + after_scheme if uri.startswith('sqlite:////') else after_scheme
    parent = os.path.dirname(sqlite_path)
    if not parent:
        return uri
    try:
        os.makedirs(parent, exist_ok=True)
        probe = os.path.join(parent, '.write_test')
        with open(probe, 'w') as f:
            f.write('ok')
        os.remove(probe)
        return uri
    except OSError as e:
        logger.warning(
            "SQLALCHEMY_DATABASE_URI=%r is not usable (%s). Falling back to %r. "
            "Likely you set the URI before adding a Render Persistent Disk.",
            uri, e, _default_db_uri,
        )
        return _default_db_uri


app.config['SQLALCHEMY_DATABASE_URI'] = _ensure_sqlite_path_writable(_db_uri)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Cookie hardening. SESSION_COOKIE_SECURE=True is opt-out via env so local
# HTTP dev doesn't silently lose the session cookie; production should leave
# the default on. HttpOnly + SameSite=Lax protect against XSS-stolen sessions
# and most CSRF vectors (Flask-WTF still enforces the explicit token check).
_cookie_secure_default = 'false' if app.debug else 'true'
app.config.update(
    SESSION_COOKIE_SECURE=os.environ.get(
        'SESSION_COOKIE_SECURE', _cookie_secure_default
    ).lower() in ('1', 'true', 'yes'),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    REMEMBER_COOKIE_SECURE=os.environ.get(
        'SESSION_COOKIE_SECURE', _cookie_secure_default
    ).lower() in ('1', 'true', 'yes'),
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_SAMESITE='Lax',
)

# ===================== EXTENSION WIRING =====================

db.init_app(app)
login_manager.init_app(app)
login_manager.login_view = 'login'
# CSRFProtect guards every non-GET endpoint. Templates emit a hidden
# csrf_token input via {{ csrf_token() }} for form POSTs; JS fetches read
# the token from the meta tag in base.html and send it as X-CSRFToken.
# The Facebook webhook is exempted in routes/webhook.py via @csrf.exempt.
csrf.init_app(app)
# Flask-Migrate (Alembic wrapper). Schema changes go through migrations/
# versions/ now instead of services.ensure_schema(). Existing DBs are
# stamped to the baseline on first boot (see _bootstrap_alembic).
migrate.init_app(app, db)
# Storage URI defaults to in-memory; set RATELIMIT_STORAGE_URI in prod
# (e.g. redis://) so login throttling works across gunicorn workers.
app.config.setdefault(
    'RATELIMIT_STORAGE_URI',
    os.environ.get('RATELIMIT_STORAGE_URI', 'memory://'),
)
app.config.setdefault('RATELIMIT_HEADERS_ENABLED', True)
limiter.init_app(app)

# Models must be imported AFTER db.init_app(app) so the metadata binds correctly.
# noqa imports are intentional — importing the module registers models on db.
from models import User  # noqa: E402,F401


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# Tenant identity shown in the admin topbar. Driven by the same env vars as the
# /privacy page so a single codebase serves Magic Financial Group, GMC, etc.
# without UI confusion about which business the logged-in admin is managing.
@app.context_processor
def inject_tenant():
    return {
        'tenant_legal_name': os.environ.get(
            'COMPANY_LEGAL_NAME', 'Мэжик Санхүүгийн Групп ХХК'),
        'tenant_short_name': os.environ.get('COMPANY_SHORT_NAME', 'Мэжик'),
    }


# Shared constants used by route modules. Defined BEFORE `import routes`
# so admin/system.py's `from app import MIN_ADMIN_PASSWORD_LENGTH` works
# during the chained import.
MIN_ADMIN_PASSWORD_LENGTH = 12


# ===================== ROUTES =====================
# Importing the routes package registers every @app.route(...) decorator
# against the Flask app constructed above. Must happen after `app` exists.
import routes  # noqa: E402,F401


# ===================== INITIALIZATION =====================


def _run_pending_migrations():
    """Apply the Phase 1 admin-IA-reorg migration on first boot.

    Render auto-deploys from master, so the first boot after the Phase 1
    commit lands on a prod DB still in pre-Phase-1 shape. Rather than make
    operators SSH in and run a script, this runs the same migration logic
    automatically. The runner is idempotent — a second boot is a no-op.

    Future schema changes go through Alembic (`flask db upgrade`); this
    function is the one-time bootstrap to get DBs onto the Alembic chain.

    Fails soft: any error is logged but does NOT propagate. The reasoning
    is that a crashloop on prod is strictly worse than a schema-mismatch
    that the operator can debug from a running admin panel. Errors here
    will show up clearly in `render logs` so the team can intervene.
    """
    db_uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    if not db_uri.startswith('sqlite:'):
        logger.info('Phase 1 auto-bootstrap: non-sqlite DB URI, skipping (apply migrations manually).')
        return
    try:
        db_path = str(db.engine.url.database)
    except Exception as e:
        logger.error('!!! Phase 1 auto-bootstrap: could not resolve DB path (%s); skipping.', str(e))
        return
    if not db_path or not os.path.exists(db_path):
        logger.info('Phase 1 auto-bootstrap: DB not found at %s, skipping (fresh install).', repr(db_path))
        return
    try:
        from scripts.apply_phase_1_migration import apply_migration
        apply_migration(db_path)
    except Exception as e:
        # Last-resort safety net — crashing init_db means 502 for every
        # request. Log the traceback and keep going; the schema may be
        # broken but admin pages will at least render so the operator
        # can read the error and rerun the migration manually.
        import traceback
        logger.error('!!! Phase 1 auto-bootstrap raised %s: %s', type(e).__name__, str(e))
        traceback.print_exc()


def init_db():
    """Initialize database tables, seed admin user + default Courses/FAQs.

    Also self-heals existing deployments: if there are admin users but none
    are super_admin (e.g. DB was created before the role tier landed), promote
    the oldest one so management routes remain usable.
    """
    from services import (advance_recurring_courses, ensure_schema,
                          seed_courses_and_faqs, seed_handoff_keywords,
                          seed_products)

    with app.app_context():
        db.create_all()
        ensure_schema()
        _run_pending_migrations()
        seed_courses_and_faqs()
        seed_handoff_keywords()
        seed_products()
        advance_recurring_courses()

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
                logger.info("Default admin user created with username 'admin' (super_admin).")
            else:
                logger.warning(
                    "INITIAL_ADMIN_PASSWORD not set — skipping default admin creation. "
                    "Set it and redeploy to create the admin user."
                )

        if User.query.count() > 0 and not User.query.filter_by(role='super_admin').first():
            oldest = User.query.order_by(User.created_at.asc()).first()
            oldest.role = 'super_admin'
            db.session.commit()
            logger.info("No super_admin found — promoted '%s' to super_admin.", oldest.username)

        # Emergency forgot-password recovery via env vars. Set both
        # RESET_ADMIN_USERNAME and RESET_ADMIN_PASSWORD on Render, redeploy,
        # log in with the new password, then REMOVE both vars and redeploy
        # again. Leaving them set means every restart resets the password.
        reset_user = os.environ.get('RESET_ADMIN_USERNAME')
        reset_pw = os.environ.get('RESET_ADMIN_PASSWORD')
        if reset_user and reset_pw:
            if len(reset_pw) < MIN_ADMIN_PASSWORD_LENGTH:
                logger.error(
                    "!!! RESET_ADMIN_PASSWORD too short (need >= %s chars). Skipping reset.",
                    MIN_ADMIN_PASSWORD_LENGTH,
                )
            else:
                target = User.query.filter_by(username=reset_user).first()
                if target:
                    target.password = generate_password_hash(reset_pw)
                    db.session.commit()
                    logger.warning(
                        "!!! password reset for '%s' via env var. REMOVE "
                        "RESET_ADMIN_USERNAME and RESET_ADMIN_PASSWORD now and "
                        "redeploy, or the password resets on every boot.",
                        reset_user,
                    )
                else:
                    logger.error("!!! RESET_ADMIN_USERNAME='%s' not found.", reset_user)


# Run at import so gunicorn workers initialize the DB on boot.
# Skipped when running migration commands (set by `flask db ...` invocations)
# to keep CLI fast and avoid seeding before migrations are applied.
#
# Cross-worker race guard: gunicorn --workers=N forks N processes that all
# import app.py simultaneously, which would race on db.create_all(), the
# raw ALTER TABLE calls in ensure_schema, and the seeders. We serialize via
# a file lock (one of the workers wins, the others wait, the rest of the
# workers see a fully migrated DB by the time they acquire the lock).
if os.environ.get('FLASK_SKIP_INIT_DB', '').lower() not in ('true', '1', 'yes'):
    import tempfile
    _lock_path = os.path.join(tempfile.gettempdir(), 'magicbot_init_db.lock')
    try:
        # fcntl is POSIX-only; on Windows dev we just run init_db without
        # the lock — there's only one process locally anyway.
        import fcntl
        with open(_lock_path, 'w') as _lock_file:
            fcntl.flock(_lock_file.fileno(), fcntl.LOCK_EX)
            init_db()
            fcntl.flock(_lock_file.fileno(), fcntl.LOCK_UN)
    except ImportError:
        init_db()


# ===================== BACKGROUND TASKS =====================
#
# WORKER_ROLE gating: background loops (polling, nudge, clustering) only
# start when WORKER_ROLE=worker. With gunicorn --workers=N, leaving these
# threads ungated would spin up N copies of every loop — duplicate Telegram
# pings, duplicate Page comments, duplicate clustering runs. The intended
# production setup is one dyno with role=web (no background work) and one
# dyno with role=worker (only background work, behind a separate Procfile
# entry: `worker: WORKER_ROLE=worker python -c "import app; import time; time.sleep(float('inf'))"`).
#
# For solo dev / single-worker deploys, set WORKER_ROLE=all to start the
# loops alongside the web server like before.
_worker_role = os.environ.get('WORKER_ROLE', 'web').strip().lower()
_run_background_loops = _worker_role in ('worker', 'all')

# Optional Page-post auto-commenting.
if _run_background_loops and os.environ.get('ENABLE_POLLING', 'false').lower() == 'true':
    from services import polling_task
    Thread(target=polling_task, args=(app,), daemon=True).start()

# Optional follow-up nudges to silent leads. Off by default; turn on only
# after upgrading from Render Free, otherwise the worker spins down before
# the loop wakes up.
if _run_background_loops and os.environ.get('ENABLE_NUDGE', 'false').lower() == 'true':
    from services import nudge_task
    Thread(target=nudge_task, args=(app,), daemon=True).start()

# Optional weekly LLM clustering of user chat messages into FAQ candidates
# (Phase 5b). Off by default to avoid OpenAI credit burn on dev / unused
# deployments; turn on per-service in Render env. Admins can also trigger
# a one-shot run via the "Шинэчлэх" button on the FAQ tab without flipping
# this var.
if _run_background_loops and os.environ.get('ENABLE_CHAT_CLUSTERING', 'false').lower() == 'true':
    from services import cluster_task
    Thread(target=cluster_task, args=(app,), daemon=True).start()


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
