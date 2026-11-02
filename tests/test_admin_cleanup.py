"""Admin cleanup 2026-11-04: company identity reaches the prompt, dead
settings are gone, the keyword-handoff system is retired.

Gundeg's correction: the bot serves the whole group (audit, tax consulting,
head office, cloud, the training centre, Darkhan and Erdenet branches), so the
company name, introduction and general information must be READ by the bot —
before this they were saved by the admin UI and never reached the prompt.
"""
import pytest

import services
from services import _prompt


def _set_rows(db, rows):
    from models import GeneralSetting
    created = []
    for key, value in rows.items():
        row = GeneralSetting.query.filter_by(key=key).first()
        if row is None:
            row = GeneralSetting(key=key, value=value)
            db.session.add(row)
            created.append(row)
        else:
            row.value = value
    db.session.commit()
    return created


def test_company_identity_and_general_info_reach_the_prompt(app, db_session):
    from extensions import db
    rows = {
        'center_name': 'Magic Financial Group',
        'center_description': 'Аудит, татварын зөвлөгөө, клоуд шийдэл, нягтлангийн сургалт.',
        'training_content': 'УБ Товер 1502 — аудит, ТМЗ, төв оффис; УБ Товер Плас 509 — Клоуд; Дархан, Эрдэнэт салбар.',
        'main_office_address': 'БЗД, UB Tower 1502',
        'main_office_phone': '7712-6060',
    }
    created = _set_rows(db, rows)
    try:
        stable, volatile = _prompt.build_system_blocks()
        for text in rows.values():
            assert text in stable, text
        assert 'КОМПАНИЙН ТАНИЛЦУУЛГА БА ХОЛБОО БАРИХ' in stable
        assert 'ЕРӨНХИЙ МЭДЭЭЛЭЛ (админы оруулсан' in stable
        # identity is customer-independent, so it lives in the cached half only
        assert 'КОМПАНИЙН ТАНИЛЦУУЛГА' not in volatile
    finally:
        for row in created:
            db.session.delete(row)
        db.session.commit()


def test_general_info_section_hidden_when_empty(app, db_session):
    from extensions import db
    created = _set_rows(db, {'training_content': '   '})
    try:
        stable, _ = _prompt.build_system_blocks()
        assert 'ЕРӨНХИЙ МЭДЭЭЛЭЛ (админы оруулсан' not in stable
        assert 'СУРГАЛТЫН ТӨВИЙН ЕРӨНХИЙ МЭДЭЭЛЭЛ' not in stable
    finally:
        for row in created:
            db.session.delete(row)
        db.session.commit()


def test_dead_settings_dropped_and_general_info_editable():
    from routes.admin.bot import BOT_SETTINGS_KEYS
    from routes.admin.business import BUSINESS_GENERAL_KEYS
    for dead in ('training_comment', 'celebration_comment', 'default_comment',
                 'handoff_message', 'bot_welcome', 'handoff_sensitivity'):
        assert dead not in BOT_SETTINGS_KEYS
    for live in ('office_hours_start', 'office_hours_end', 'telegram_chat_ids',
                 'sound_alerts_enabled', 'hot_prospect_stages'):
        assert live in BOT_SETTINGS_KEYS
    for key in ('center_name', 'center_description', 'training_content',
                'main_office_address', 'main_office_phone', 'business_website_url', 'center_email'):
        assert key in BUSINESS_GENERAL_KEYS


def test_keyword_handoff_is_retired():
    import routes.webhook as wh
    for gone in ('should_handoff', 'get_handoff_sensitivity', 'get_mute_duration_hours',
                 'seed_handoff_keywords'):
        assert not hasattr(services, gone), gone
    assert not hasattr(wh, 'should_handoff')
    # the model-driven path is intact
    assert services.DEFER_TO_STAFF_TOOL[0]['name'] == 'defer_to_staff'


def test_bot_management_lands_on_training(app):
    rule = app.url_map._rules_by_endpoint
    assert 'bot_management_training' in rule and 'bot_management_handover' not in rule
    assert 'handoff_keywords' not in rule
