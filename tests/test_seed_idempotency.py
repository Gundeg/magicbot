"""seed_handoff_keywords + seed_courses_and_faqs are safe to re-run.

Re-import races and admin "Re-seed" buttons should never create duplicate
rows. We seed twice and assert the row count doesn't change.
"""
import pytest


def test_seed_handoff_keywords_idempotent(app, db_session):
    from models import HandoffKeyword
    from services import seed_handoff_keywords

    HandoffKeyword.query.delete()
    db_session.commit()

    seed_handoff_keywords()
    first_count = HandoffKeyword.query.count()
    assert first_count > 0

    seed_handoff_keywords()
    second_count = HandoffKeyword.query.count()
    assert second_count == first_count


def test_seed_courses_and_faqs_idempotent(app, db_session):
    from models import Course, FAQ
    from services import seed_courses_and_faqs

    Course.query.delete()
    FAQ.query.delete()
    db_session.commit()

    seed_courses_and_faqs()
    c1, f1 = Course.query.count(), FAQ.query.count()

    seed_courses_and_faqs()
    c2, f2 = Course.query.count(), FAQ.query.count()
    assert c2 == c1
    assert f2 == f1
