"""One-off migration runner: make course.start_date / course.end_date nullable.

Fixes the prod-only `IntegrityError: NOT NULL constraint failed: course.start_date`
that blocked saving self-paced ('100% Online') courses. The model already declares
these columns nullable; this heals older SQLite DBs whose columns were created
NOT NULL. Idempotent — safe to run more than once (a no-op once already nullable).

Run ONCE from the Render shell (single process, so no 2-worker race):

    python scripts/make_course_dates_nullable.py

Back up the DB first, e.g.:

    python -c "import os;from app import app;print(app.config['SQLALCHEMY_DATABASE_URI'])"
    cp <db-file> <db-file>.bak
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import app  # noqa: E402
from services import make_course_dates_nullable  # noqa: E402


def main():
    with app.app_context():
        result = make_course_dates_nullable()
        print(result)
        return 0 if result.startswith(('OK', 'DONE')) else 1


if __name__ == '__main__':
    raise SystemExit(main())
