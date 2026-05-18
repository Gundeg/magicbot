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
