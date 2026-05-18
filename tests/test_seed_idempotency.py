"""Seed routines are safe to re-run.

The /admin/defaults page exposes the link + snippet seeders to non-
technical admins, so their failure modes (NameError from a missing
import, accidental duplicate rows, stomping on admin-edited descriptions)
all surface as admin-visible incidents. These tests lock that contract.
"""
from datetime import datetime

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


# ===================== seed_default_magic_links =====================

def _clear_catalog(db_session):
    """Wipe every table seed_default_magic_links touches, so each test
    starts from a known-empty state instead of inheriting prior writes."""
    from models import (BusinessLine, Course, CourseLink, GeneralSetting,
                        Product, ProductLink, Service, ServiceLink,
                        TrainingSnippet)
    for M in (ProductLink, ServiceLink, CourseLink, Product, Service,
              Course, BusinessLine, GeneralSetting, TrainingSnippet):
        M.query.delete()
    db_session.commit()


def _build_catalog(db_session, mf_description=None):
    """Minimal catalog seed_default_magic_links can find: a BusinessLine,
    Magic Finance product under it, audit + tax services, one active
    course. Mirrors what seed_products / seed_courses_and_faqs would
    produce on a fresh deploy, minus rows the link seeder doesn't query."""
    from models import BusinessLine, Course, Product, Service

    biz = BusinessLine(
        name='Програм & License',
        is_active=True,
        sort_order=0,
    )
    db_session.add(biz)
    db_session.flush()

    db_session.add(Product(
        business_line_id=biz.id,
        name='Magic Finance',
        vendor='Magic Cloud LLC',
        description=mf_description or 'Тестийн анхдагч тайлбар.',
        is_main_product=True,
        is_active=True,
    ))
    db_session.add(Service(name='Санхүүгийн аудитын үйлчилгээ', is_active=True))
    db_session.add(Service(name='Татварын мэргэшсэн зөвлөх үйлчилгээ', is_active=True))
    db_session.add(Course(
        name='Test Classroom Course',
        course_type='Classroom',
        start_date=datetime.utcnow(),
        time='10:00-13:00',
        price=100000,
        is_active=True,
    ))
    db_session.commit()


def test_seed_default_magic_links_runs_without_error(app, db_session):
    """Smoke test — catches missing imports / model field typos, the
    exact class of bug that hit prod the first time the new /admin/defaults
    page POSTed this endpoint."""
    from services import seed_default_magic_links

    _clear_catalog(db_session)
    _build_catalog(db_session)

    report = seed_default_magic_links()
    assert isinstance(report, str)
    assert 'Magic Finance' in report


def test_seed_default_magic_links_idempotent(app, db_session):
    from models import CourseLink, ProductLink, ServiceLink
    from services import seed_default_magic_links

    _clear_catalog(db_session)
    _build_catalog(db_session)

    seed_default_magic_links()
    counts_first = (
        ProductLink.query.count(),
        ServiceLink.query.count(),
        CourseLink.query.count(),
    )
    assert counts_first[0] > 0, 'first run should have seeded ProductLinks'

    seed_default_magic_links()
    counts_second = (
        ProductLink.query.count(),
        ServiceLink.query.count(),
        CourseLink.query.count(),
    )
    assert counts_first == counts_second, (
        f'second run grew rows: {counts_first} -> {counts_second}'
    )


def test_seed_default_magic_links_upserts_link_descriptions(app, db_session):
    """When a ProductLink with a known URL already exists with old
    wording, re-running the seed should refresh its description to the
    current canonical text — that's what lets admins re-apply defaults
    after a content rewrite without manually editing every row."""
    from models import Product, ProductLink
    from services import seed_default_magic_links

    _clear_catalog(db_session)
    _build_catalog(db_session)

    mf = Product.query.filter_by(name='Magic Finance').first()
    db_session.add(ProductLink(
        product_id=mf.id,
        description='Old wording from a previous seed',
        url='https://magicgroup.mn/mn/download',
        is_active=True,
        sort_order=0,
    ))
    db_session.commit()

    seed_default_magic_links()

    refreshed = ProductLink.query.filter_by(
        product_id=mf.id,
        url='https://magicgroup.mn/mn/download',
    ).first()
    assert refreshed.description == 'Програмын шинэ хувилбар'


def test_seed_default_magic_links_preserves_admin_edited_product_description(app, db_session):
    """Magic Finance.description gets one-shot rewritten ONLY when the
    current text still matches a known default. Custom admin wording
    must survive a re-seed so the button is safe to click anytime."""
    from models import Product
    from services import seed_default_magic_links

    custom_text = 'Бид өөрсдөө бичсэн өвөрмөц тайлбар.'
    _clear_catalog(db_session)
    _build_catalog(db_session, mf_description=custom_text)

    seed_default_magic_links()

    mf = Product.query.filter_by(name='Magic Finance').first()
    assert mf.description == custom_text


def test_seed_discovery_phrasing_snippets_idempotent(app, db_session):
    from models import TrainingSnippet
    from services import seed_discovery_phrasing_snippets

    TrainingSnippet.query.delete()
    db_session.commit()

    seed_discovery_phrasing_snippets()
    first_count = TrainingSnippet.query.count()
    assert first_count > 0

    seed_discovery_phrasing_snippets()
    second_count = TrainingSnippet.query.count()
    assert second_count == first_count
