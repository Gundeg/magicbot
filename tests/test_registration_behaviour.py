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


def test_seed_does_not_overwrite_admin_course_link(app, db_session):
    from extensions import db
    from models import Course, CourseLink, GeneralSetting
    from services import seed_default_magic_links
    from services._seed import COURSE_REGISTRATION_LINK_DESCRIPTION

    # Course has no business_line_id column (see models.py) — mirror the
    # minimal Course used in tests/test_seed_idempotency.py::_build_catalog.
    course = Course(name='Seed Reglink Course', course_type='Classroom',
                    price=100000, time='18:00', is_active=True)
    db.session.add(course)
    db.session.commit()
    admin_url = 'https://forms.example.com/ADMIN_EDITED'
    db.session.add(CourseLink(course_id=course.id,
                              description=COURSE_REGISTRATION_LINK_DESCRIPTION,
                              url=admin_url, is_active=True))
    db.session.commit()

    seed_default_magic_links()
    db.session.expire_all()

    links = CourseLink.query.filter_by(course_id=course.id).all()
    reg_links = [l for l in links
                 if l.description == COURSE_REGISTRATION_LINK_DESCRIPTION]
    assert len(reg_links) == 1, 'seed added a duplicate registration link'
    assert reg_links[0].url == admin_url, 'seed overwrote the admin link'
    assert GeneralSetting.query.filter_by(key='google_form_url').first() is None

    CourseLink.query.filter_by(course_id=course.id).delete()
    Course.query.filter_by(id=course.id).delete()
    db.session.commit()


NEW_REG_SNIPPET_MARKER = 'аль сургалтад хамрагдахыг тодруул'


def test_registration_snippet_seeds_new_behaviour(app, db_session):
    from extensions import db
    from models import TrainingSnippet
    from services import seed_discovery_phrasing_snippets

    TrainingSnippet.query.delete()
    db.session.commit()

    seed_discovery_phrasing_snippets()
    db.session.expire_all()

    snip = TrainingSnippet.query.filter_by(
        title='Сургалтад бүртгүүлэх асуултын чиглэл').first()
    assert snip is not None
    assert snip.priority == 'high'
    assert NEW_REG_SNIPPET_MARKER in snip.body
    assert 'вэбсайт' in snip.body
    TrainingSnippet.query.delete()
    db.session.commit()


def test_registration_snippet_updates_known_old_body_but_keeps_admin_edit(app, db_session):
    from extensions import db
    from models import TrainingSnippet
    from services import seed_discovery_phrasing_snippets
    from services._seed import OLD_REGISTRATION_SNIPPET_BODY

    TrainingSnippet.query.delete()
    db.session.commit()

    # A live row still carrying the old seeded body -> should be upgraded.
    db.session.add(TrainingSnippet(
        title='Сургалтад бүртгүүлэх асуултын чиглэл',
        body=OLD_REGISTRATION_SNIPPET_BODY, category='course-routing',
        priority='high', is_active=True))
    db.session.commit()

    seed_discovery_phrasing_snippets()
    db.session.expire_all()
    snip = TrainingSnippet.query.filter_by(
        title='Сургалтад бүртгүүлэх асуултын чиглэл').first()
    assert NEW_REG_SNIPPET_MARKER in snip.body

    # An admin-customised body must NOT be touched.
    snip.body = 'МИНИЙ ГАРААР ЗАССАН ТЕКСТ'
    db.session.commit()
    seed_discovery_phrasing_snippets()
    db.session.expire_all()
    snip = TrainingSnippet.query.filter_by(
        title='Сургалтад бүртгүүлэх асуултын чиглэл').first()
    assert snip.body == 'МИНИЙ ГАРААР ЗАССАН ТЕКСТ'
    TrainingSnippet.query.delete()
    db.session.commit()
