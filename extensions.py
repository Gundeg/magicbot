"""Flask extension instances, bare.

Instances are created here (not bound to an app) so models, route files, and
service modules can import them without pulling in the full Flask app. The
app is wired up by calling `extension.init_app(app)` in `app.py`.
"""
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from flask_migrate import Migrate
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()
migrate = Migrate()
# Per-IP rate limiter, used to brake brute-force login attempts. The default
# in-memory storage is fine for single-instance dev; production should set
# RATELIMIT_STORAGE_URI (e.g. to a Redis URL) so limits work across workers.
# Default limits are intentionally permissive — only the login route applies
# a tight limit; we don't want to throttle the webhook or admin API.
limiter = Limiter(key_func=get_remote_address)
