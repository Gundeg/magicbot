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
import os
from threading import Thread

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask
from werkzeug.security import generate_password_hash

from extensions import db, login_manager, csrf, migrate

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
        print(
            f"WARNING: SQLALCHEMY_DATABASE_URI={uri!r} is not usable "
            f"({e!s}). Falling back to {_default_db_uri!r}. "
            "Likely you set the URI before adding a Render Persistent Disk."
        )
        return _default_db_uri


app.config['SQLALCHEMY_DATABASE_URI'] = _ensure_sqlite_path_writable(_db_uri)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

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

# Models must be imported AFTER db.init_app(app) so the metadata binds correctly.
# noqa imports are intentional — importing the module registers models on db.
from models import User  # noqa: E402,F401


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


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


# ===================== ROUTES =====================
# Importing the routes package registers every @app.route(...) decorator
# against the Flask app constructed above. Must happen after `app` exists.
import routes  # noqa: E402,F401


# ===================== INITIALIZATION =====================

MIN_ADMIN_PASSWORD_LENGTH = 12


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
# Skipped when running migration commands (set by `flask db ...` invocations)
# to keep CLI fast and avoid seeding before migrations are applied.
if os.environ.get('FLASK_SKIP_INIT_DB', '').lower() not in ('true', '1', 'yes'):
    init_db()


# ===================== BACKGROUND TASKS =====================

# Optional Page-post auto-commenting. Disabled by default to avoid duplicate
# work across gunicorn workers.
if os.environ.get('ENABLE_POLLING', 'false').lower() == 'true':
    from services import polling_task
    Thread(target=polling_task, args=(app,), daemon=True).start()

# Optional follow-up nudges to silent leads. Off by default; turn on only
# after upgrading from Render Free, otherwise the worker spins down before
# the loop wakes up.
if os.environ.get('ENABLE_NUDGE', 'false').lower() == 'true':
    from services import nudge_task
    Thread(target=nudge_task, args=(app,), daemon=True).start()


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
