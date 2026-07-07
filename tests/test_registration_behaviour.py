"""Registration behaviour after retiring the global google_form_url form:
the prompt no longer injects the hard-coded registration block, and the
'ready'-stage nudge quotes the website instead of the dead form link.
"""
import pytest


@pytest.fixture
def _clean_settings(app, db_session):
    from extensions import db
    from models import GeneralSetting
    yield
    GeneralSetting.query.filter(
        GeneralSetting.key.in_(['google_form_url', 'business_website_url'])
    ).delete(synchronize_session=False)
    db.session.commit()


def test_prompt_has_no_hardcoded_registration_form_block(app, db_session, _clean_settings):
    from extensions import db
    from models import GeneralSetting
    from services._prompt import build_system_prompt

    # Even with a stale google_form_url row present, the block must be gone.
    db.session.add(GeneralSetting(key='google_form_url',
                                  value='https://example.com/OLD_FORM'))
    db.session.commit()

    prompt = build_system_prompt()
    assert 'БҮРТГЭЛИЙН ЛИНК (ӨӨРӨӨ БӨГЛӨЖ' not in prompt
    assert 'https://example.com/OLD_FORM' not in prompt


def test_ready_nudge_quotes_website_not_form(app, db_session, _clean_settings):
    from extensions import db
    from models import FacebookUser, GeneralSetting
    from services import _nudge_message_for

    db.session.add(GeneralSetting(key='business_website_url',
                                  value='https://magicgroup.mn'))
    db.session.commit()

    user = FacebookUser(facebook_id='psid-nudge-ready', name='Test',
                        funnel_stage='ready')
    msg = _nudge_message_for(user)
    assert 'https://magicgroup.mn' in msg
