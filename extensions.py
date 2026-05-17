"""Flask extension instances, bare.

Instances are created here (not bound to an app) so models, route files, and
service modules can import them without pulling in the full Flask app. The
app is wired up by calling `extension.init_app(app)` in `app.py`.
"""
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from flask_migrate import Migrate

db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()
migrate = Migrate()
