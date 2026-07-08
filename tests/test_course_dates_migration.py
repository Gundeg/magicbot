"""make_course_dates_nullable() migration.

Proves the one-off rebuild that heals the prod-only NOT NULL constraint on
course.start_date / course.end_date: it flips them to nullable, preserves
every row, unblocks NULL-date (self-paced) inserts, and is idempotent. Runs
against a throwaway file-backed SQLite DB so it never touches the app's DB.
"""

_LEGACY_COURSE_DDL = (
    "CREATE TABLE course ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " course_number INTEGER UNIQUE,"
    " name VARCHAR(255) NOT NULL,"
    " course_type VARCHAR(100) NOT NULL,"
    " start_date DATETIME NOT NULL,"      # the legacy bug
    " end_date DATETIME NOT NULL,"
    " time VARCHAR(50) NOT NULL,"
    " price FLOAT NOT NULL,"
    " description TEXT,"
    " is_active BOOLEAN,"
    " created_at DATETIME)"
)


def test_make_course_dates_nullable_rebuilds_and_preserves(app, tmp_path):
    from sqlalchemy import create_engine, inspect as sa_inspect
    from services import make_course_dates_nullable

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql(_LEGACY_COURSE_DDL)
        conn.exec_driver_sql(
            "INSERT INTO course (id, name, course_type, start_date, end_date, time, price)"
            " VALUES (1, 'Legacy', 'Classroom', '2026-01-01 00:00:00',"
            " '2026-02-01 00:00:00', '10:00', 100000)"
        )

    # Precondition: start_date is NOT NULL — the bug that 500s self-paced saves.
    before = {c['name']: c['nullable'] for c in sa_inspect(engine).get_columns('course')}
    assert before['start_date'] is False

    result = make_course_dates_nullable(engine=engine)
    assert result.startswith('DONE'), result

    after = {c['name']: c['nullable'] for c in sa_inspect(engine).get_columns('course')}
    assert after['start_date'] is True
    assert after['end_date'] is True

    # Existing row preserved through the rebuild.
    with engine.connect() as conn:
        assert conn.exec_driver_sql('SELECT id, name FROM course').fetchall() == [(1, 'Legacy')]

    # The actual fix: a self-paced course with NULL dates now inserts.
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO course (id, name, course_type, start_date, end_date, time, price)"
            " VALUES (2, 'Self-paced', '100% Online', NULL, NULL, 'anytime', 0)"
        )
    with engine.connect() as conn:
        assert conn.exec_driver_sql('SELECT COUNT(*) FROM course').scalar() == 2

    # Idempotent: a second run is a no-op.
    assert make_course_dates_nullable(engine=engine).startswith('OK')
    engine.dispose()
