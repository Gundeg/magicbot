"""Shared pytest fixtures.

We need to populate the env vars that `app.py` reads at import time (SECRET_KEY,
FACEBOOK_ACCESS_TOKEN, FACEBOOK_APP_SECRET) before importing the app, otherwise
import-time validation crashes the test session. We also point the DB at an
in-memory SQLite so tests don't touch the dev DB.
"""
import os
import sys
from pathlib import Path

import pytest

# Ensure the repo root is on sys.path so `import app` works regardless of
# where pytest is launched from.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _seed_env():
    """Populate the bare-minimum env vars services.py / app.py read at import."""
    os.environ.setdefault('SECRET_KEY', 'test-secret-key')
    os.environ.setdefault('FACEBOOK_ACCESS_TOKEN', 'test-access-token')
    os.environ.setdefault('FACEBOOK_APP_SECRET', 'test-app-secret')
    os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
    # Skip init_db() so tests can build their own minimal schema.
    os.environ.setdefault('FLASK_SKIP_INIT_DB', '1')
    os.environ.setdefault('RATELIMIT_ENABLED', 'False')


_seed_env()


@pytest.fixture(scope='session')
def app():
    """Import the Flask app once per test session with fresh schema."""
    from app import app as flask_app
    from extensions import db

    flask_app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        RATELIMIT_ENABLED=False,
    )
    with flask_app.app_context():
        db.create_all()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db_session(app):
    """A clean DB session for each test. Rolls back any pending changes."""
    from extensions import db
    yield db.session
    db.session.rollback()
