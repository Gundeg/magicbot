"""classify_user_topics — multi-topic, DB-driven, OpenAI stubbed.

The classifier must:
- only return topics that exist in the active catalog,
- upsert (no duplicates per user),
- update last_seen_at on a re-tag,
- attach an evidence snippet,
- silently skip when no active topics exist,
- return 0 on any LLM/parse failure (never raise).
"""
import json
from unittest.mock import patch

import pytest


def _make_user_with_messages(text='Аудит хийдэг үү?'):
    from extensions import db
    from models import (BusinessLine, ConversationTopic, FacebookUser,
                        Message, Product, Service, Course)
    # Clean slate
    Message.query.delete()
    ConversationTopic.query.delete()
    FacebookUser.query.delete()
    BusinessLine.query.delete()
    Product.query.delete()
    Service.query.delete()
    Course.query.delete()
    db.session.commit()

    bl = BusinessLine(name='Magic Consulting Audit', action='refer',
                      product_type='Service', is_active=True)
    db.session.add(bl)
    db.session.flush()
    user = FacebookUser(facebook_id='psid-test', name='Test Lead')
    db.session.add(user)
    db.session.flush()
    db.session.add(Message(facebook_user_id=user.id, sender='user', content=text))
    db.session.commit()
    return user


class _StubChoice:
    def __init__(self, content):
        self.message = type('M', (), {'content': content})()


class _StubCompletion:
    def __init__(self, content):
        self.choices = [_StubChoice(content)]


def _patch_openai(content):
    """Patch the OpenAI client's chat.completions.create to return content."""
    import services
    return patch.object(
        services.client.chat.completions, 'create',
        return_value=_StubCompletion(content),
    )


@pytest.fixture
def reset_session():
    """Drop the shared SQLAlchemy session around a test that makes an
    authenticated request. Otherwise Flask-Login's user_loader
    (db.session.get(User, id)) can hit a detached / deleted-instance marker
    left by an earlier test module. Same guard as test_roles.py."""
    from extensions import db
    db.session.remove()
    yield
    db.session.remove()


def test_classifier_attaches_matching_topic(app, db_session):
    from models import ConversationTopic
    from services import classify_user_topics

    user = _make_user_with_messages('Аудит хийлгэх үнэ хэд вэ?')
    payload = json.dumps({
        'topics': [
            {'name': 'Magic Consulting Audit', 'evidence': 'аудитын үнэ'},
        ],
    }, ensure_ascii=False)

    with _patch_openai(payload):
        n = classify_user_topics(user.id)

    assert n == 1
    tags = ConversationTopic.query.filter_by(facebook_user_id=user.id).all()
    assert len(tags) == 1
    assert tags[0].topic == 'Magic Consulting Audit'
    assert tags[0].topic_kind == 'business_line'
    assert tags[0].evidence == 'аудитын үнэ'


def test_classifier_silently_drops_hallucinated_topics(app, db_session):
    from models import ConversationTopic
    from services import classify_user_topics

    user = _make_user_with_messages('Сайн уу')
    # LLM invents a topic not in our catalog — must be ignored.
    payload = json.dumps({'topics': [{'name': 'Crypto Trading', 'evidence': 'fake'}]})

    with _patch_openai(payload):
        n = classify_user_topics(user.id)

    assert n == 0
    assert ConversationTopic.query.filter_by(facebook_user_id=user.id).count() == 0


def test_classifier_upserts_on_repeat_run(app, db_session):
    from models import ConversationTopic
    from services import classify_user_topics

    user = _make_user_with_messages('audit')
    payload = json.dumps({
        'topics': [{'name': 'Magic Consulting Audit', 'evidence': 'first'}],
    })
    with _patch_openai(payload):
        classify_user_topics(user.id)
    first = ConversationTopic.query.filter_by(facebook_user_id=user.id).one()
    first_seen = first.first_seen_at

    # Second run with updated evidence — should NOT create a duplicate.
    payload2 = json.dumps({
        'topics': [{'name': 'Magic Consulting Audit', 'evidence': 'second'}],
    })
    with _patch_openai(payload2):
        classify_user_topics(user.id)

    rows = ConversationTopic.query.filter_by(facebook_user_id=user.id).all()
    assert len(rows) == 1
    assert rows[0].first_seen_at == first_seen
    assert rows[0].last_seen_at >= first_seen
    assert rows[0].evidence == 'second'


def test_classifier_returns_zero_on_garbage_response(app, db_session):
    from services import classify_user_topics

    user = _make_user_with_messages('hi')
    with _patch_openai('not json at all'):
        n = classify_user_topics(user.id)
    assert n == 0


def test_classifier_no_active_catalog_returns_zero(app, db_session):
    from extensions import db
    from models import BusinessLine, Course, Product, Service
    from services import classify_user_topics

    user = _make_user_with_messages('hi')
    # Disable every active source so the catalog is empty.
    for model in (BusinessLine, Product, Service, Course):
        for row in model.query.all():
            row.is_active = False
    db.session.commit()

    # No OpenAI call should be needed — classifier bails early.
    n = classify_user_topics(user.id)
    assert n == 0


def test_lookback_setting_is_clamped(app, db_session):
    from extensions import db
    from flask import g, has_request_context
    from models import GeneralSetting
    from services import (CLASSIFICATION_LOOKBACK_MAX_DAYS,
                          get_classification_lookback_days)

    def _bust_cache():
        # services.get_setting caches per-request via flask.g. Within
        # one test we mutate the row repeatedly, so we must drop the
        # cache to read fresh values each time.
        if has_request_context():
            g.pop('_setting_cache', None)

    GeneralSetting.query.delete()
    db.session.commit()
    _bust_cache()
    # Default when unset
    assert get_classification_lookback_days() == 30

    db.session.add(GeneralSetting(key='classification_lookback_days', value='999'))
    db.session.commit()
    _bust_cache()
    assert get_classification_lookback_days() == CLASSIFICATION_LOOKBACK_MAX_DAYS

    GeneralSetting.query.filter_by(key='classification_lookback_days').update({'value': '0'})
    db.session.commit()
    _bust_cache()
    assert get_classification_lookback_days() == 1  # clamped to min

    GeneralSetting.query.filter_by(key='classification_lookback_days').update({'value': '14'})
    db.session.commit()
    _bust_cache()
    assert get_classification_lookback_days() == 14


def test_classify_route_stamps_last_classified_and_budget_fields(client, db_session, reset_session):
    """The manual Ангилах run must stamp last_classified_at on processed users
    (so repeated clicks advance the backlog) and report timed_out / remaining,
    so the loop can stop before gunicorn's 60s worker timeout instead of 500-ing."""
    from extensions import db, limiter
    from models import FacebookUser, User
    from werkzeug.security import generate_password_hash

    limiter.enabled = False  # the /admin/login form route is rate-limited
    user = _make_user_with_messages('Аудит хийлгэх үнэ хэд вэ?')
    user_id = user.id

    User.query.filter_by(username='pytest-classify-admin').delete()
    db.session.commit()
    admin = User(username='pytest-classify-admin',
                 password=generate_password_hash('classify-pw'),
                 email='pytest-classify-admin@example.com', role='super_admin')
    db.session.add(admin)
    db.session.commit()
    admin_id = admin.id

    # Real login form (like test_roles) — proven to work in the full suite where
    # session_transaction + the shared session trips Flask-Login's user_loader.
    login_resp = client.post('/admin/login',
                             data={'username': 'pytest-classify-admin',
                                   'password': 'classify-pw'},
                             follow_redirects=False)
    assert login_resp.status_code in (302, 303)

    payload = json.dumps({'topics': [
        {'name': 'Magic Consulting Audit', 'evidence': 'аудитын үнэ'},
    ]}, ensure_ascii=False)
    with _patch_openai(payload):
        resp = client.post('/admin/api/classify-conversations',
                           json={'lookback_days': 7})

    assert resp.status_code == 200
    data = resp.get_json()
    assert data['timed_out'] is False        # one user fits well inside the budget
    assert data['remaining'] == 0
    assert data['attempted'] >= 1
    refreshed = db.session.get(FacebookUser, user_id)
    assert refreshed.last_classified_at is not None

    User.query.filter_by(id=admin_id).delete()
    db.session.commit()
