"""should_handoff() — keyword-driven escalation logic.

Uses an in-memory DB seeded with HandoffKeyword rows since the real
function queries the DB. Verifies explicit keywords match, frustration
keywords only match at balanced/aggressive sensitivity, and unrelated
text doesn't trigger.
"""
import pytest


@pytest.fixture
def seeded_keywords(app):
    from extensions import db
    from models import GeneralSetting, HandoffKeyword

    HandoffKeyword.query.delete()
    GeneralSetting.query.delete()
    db.session.add_all([
        HandoffKeyword(keyword='ажилтан', keyword_type='explicit', is_active=True),
        HandoffKeyword(keyword='operator', keyword_type='explicit', is_active=True),
        HandoffKeyword(keyword='хариулж чадахгүй', keyword_type='frustration', is_active=True),
    ])
    db.session.commit()
    yield
    HandoffKeyword.query.delete()
    GeneralSetting.query.delete()
    db.session.commit()


def _set_sensitivity(level):
    from extensions import db
    from models import GeneralSetting
    row = GeneralSetting.query.filter_by(key='handoff_sensitivity').first()
    if row is None:
        row = GeneralSetting(key='handoff_sensitivity', value=level)
        db.session.add(row)
    else:
        row.value = level
    db.session.commit()


def test_explicit_keyword_triggers_at_conservative(app, seeded_keywords):
    from services import should_handoff
    _set_sensitivity('conservative')
    handoff, reason = should_handoff('ажилтантай ярья', fb_user=None)
    assert handoff is True
    assert reason.startswith('explicit:')


def test_frustration_keyword_ignored_at_conservative(app, seeded_keywords):
    from services import should_handoff
    _set_sensitivity('conservative')
    handoff, _ = should_handoff('та хариулж чадахгүй юм', fb_user=None)
    assert handoff is False


def test_frustration_keyword_triggers_at_balanced(app, seeded_keywords):
    from services import should_handoff
    _set_sensitivity('balanced')
    handoff, reason = should_handoff('та хариулж чадахгүй юм', fb_user=None)
    assert handoff is True
    assert reason.startswith('frustration:')


def test_no_match_returns_false(app, seeded_keywords):
    from services import should_handoff
    _set_sensitivity('conservative')
    handoff, _ = should_handoff('Сайн байна уу, үнэ хэд вэ?', fb_user=None)
    assert handoff is False


def test_empty_message_returns_false(app, seeded_keywords):
    from services import should_handoff
    handoff, _ = should_handoff('', fb_user=None)
    assert handoff is False
