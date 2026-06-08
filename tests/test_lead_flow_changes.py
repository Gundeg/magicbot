"""Tests for the lead-flow / cleanup / human-takeover changes.

Covers:
  * Hot Prospects two-outcome actions (promote_to_lead / drop_prospect).
  * Manual cleanup endpoint (hard-delete old messages + closed leads).
  * Human-takeover auto-mute driven by Messenger echo events.
  * The bot tags its own outgoing messages so it never mutes itself.

Mirrors the login/signature helpers used by test_lead_status.py and
test_webhook.py.
"""
import hashlib
import hmac
import json
from datetime import datetime, timedelta

import pytest


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

@pytest.fixture
def admin_user(app, db_session):
    from extensions import db
    from models import User
    from werkzeug.security import generate_password_hash

    User.query.filter_by(username='pytest-flow-admin').delete()
    db.session.commit()
    user = User(
        username='pytest-flow-admin',
        password=generate_password_hash('not-used'),
        email='pytest-flow-admin@example.com',
        role='super_admin',
    )
    db.session.add(user)
    db.session.commit()
    yield user
    User.query.filter_by(id=user.id).delete()
    db.session.commit()


def _login(client, admin_user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(admin_user.id)
        sess['_fresh'] = True


def _sign(body):
    from services import FACEBOOK_APP_SECRET
    return hmac.new(FACEBOOK_APP_SECRET.encode('utf-8'), body, hashlib.sha256).hexdigest()


def _post_webhook(client, payload):
    body = json.dumps(payload).encode('utf-8')
    return client.post(
        '/webhook',
        data=body,
        headers={
            'X-Hub-Signature-256': f'sha256={_sign(body)}',
            'Content-Type': 'application/json',
        },
    )


# --------------------------------------------------------------------------
# #4 Hot Prospects: promote_to_lead / drop_prospect
# --------------------------------------------------------------------------

def test_promote_to_lead_sets_new_status(client, admin_user, db_session):
    from extensions import db
    from models import FacebookUser

    u = FacebookUser(facebook_id='psid-promote', name='Promote Me',
                     is_lead=False, lead_status='contacted')
    db.session.add(u)
    db.session.commit()
    _login(client, admin_user)

    resp = client.post('/admin/work-tasks',
                       json={'action': 'promote_to_lead', 'user_id': u.id})
    assert resp.status_code == 200
    assert resp.get_json()['success'] is True

    refreshed = db.session.get(FacebookUser, u.id)
    assert refreshed.is_lead is True
    assert refreshed.lead_status == 'new'   # Шинэ, not Холбогдсон
    FacebookUser.query.filter_by(id=u.id).delete()
    db.session.commit()


def test_drop_prospect_sets_dropped(client, admin_user, db_session):
    from extensions import db
    from models import FacebookUser

    u = FacebookUser(facebook_id='psid-drop', name='Drop Me', is_lead=False)
    db.session.add(u)
    db.session.commit()
    _login(client, admin_user)

    resp = client.post('/admin/work-tasks',
                       json={'action': 'drop_prospect', 'user_id': u.id})
    assert resp.status_code == 200
    assert resp.get_json()['success'] is True

    refreshed = db.session.get(FacebookUser, u.id)
    assert refreshed.lead_status == 'dropped'   # terminal -> leaves the queue
    FacebookUser.query.filter_by(id=u.id).delete()
    db.session.commit()


# --------------------------------------------------------------------------
# #2/#3 Manual cleanup of old data
# --------------------------------------------------------------------------

def test_cleanup_deletes_old_data_keeps_recent(client, admin_user, db_session):
    from extensions import db
    from models import FacebookUser, Message

    old = datetime.utcnow() - timedelta(days=90)
    recent = datetime.utcnow() - timedelta(days=5)

    # A closed lead, untouched for 90 days, with one old message.
    stale_lead = FacebookUser(facebook_id='psid-stale-lead', name='Stale',
                              is_lead=True, lead_status='dropped',
                              updated_at=old)
    # An active lead, recently touched — must survive.
    fresh_lead = FacebookUser(facebook_id='psid-fresh-lead', name='Fresh',
                              is_lead=True, lead_status='new',
                              updated_at=recent)
    db.session.add_all([stale_lead, fresh_lead])
    db.session.commit()

    old_msg = Message(facebook_user_id=fresh_lead.id, sender='user',
                      content='ancient', created_at=old)
    new_msg = Message(facebook_user_id=fresh_lead.id, sender='user',
                      content='recent', created_at=recent)
    stale_msg = Message(facebook_user_id=stale_lead.id, sender='user',
                        content='on a doomed lead', created_at=recent)
    db.session.add_all([old_msg, new_msg, stale_msg])
    db.session.commit()
    # Capture ids up front — the rows get deleted below, after which the ORM
    # would raise ObjectDeletedError if we touched the instance's .id.
    old_msg_id, new_msg_id, stale_msg_id = old_msg.id, new_msg.id, stale_msg.id
    stale_lead_id, fresh_lead_id = stale_lead.id, fresh_lead.id

    _login(client, admin_user)
    resp = client.post('/admin/api/cleanup-old-records')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    assert data['messages_deleted'] >= 1
    assert data['leads_deleted'] >= 1

    # Old message gone; recent message kept.
    assert db.session.get(Message, old_msg_id) is None
    assert db.session.get(Message, new_msg_id) is not None
    # Stale closed lead gone, and its message removed even though recent
    # (the whole lead was deleted).
    assert db.session.get(FacebookUser, stale_lead_id) is None
    assert db.session.get(Message, stale_msg_id) is None
    # Fresh lead survives.
    assert db.session.get(FacebookUser, fresh_lead_id) is not None

    FacebookUser.query.filter_by(id=fresh_lead_id).delete()
    Message.query.filter_by(facebook_user_id=fresh_lead_id).delete()
    db.session.commit()


# --------------------------------------------------------------------------
# #5 Human-takeover auto-mute via Messenger echoes
# --------------------------------------------------------------------------

def test_untagged_echo_mutes_bot(client, db_session):
    from extensions import db
    from models import FacebookUser

    u = FacebookUser(facebook_id='psid-echo-human', name='Echo Human')
    db.session.add(u)
    db.session.commit()
    assert u.bot_muted_until is None

    # Echo: sender=Page, recipient=customer, NO metadata -> human agent.
    payload = {
        'object': 'page',
        'entry': [{
            'messaging': [{
                'sender': {'id': 'PAGE_ID'},
                'recipient': {'id': 'psid-echo-human'},
                'message': {'is_echo': True, 'text': 'Сайн байна уу, би туслах гэж'},
            }],
        }],
    }
    resp = _post_webhook(client, payload)
    assert resp.status_code == 200

    refreshed = db.session.get(FacebookUser, u.id)
    assert refreshed.bot_muted_until is not None
    assert refreshed.bot_muted_until > datetime.utcnow()
    FacebookUser.query.filter_by(id=u.id).delete()
    db.session.commit()


def test_bot_own_echo_does_not_mute(client, db_session):
    from extensions import db
    from models import FacebookUser
    from services import BOT_ECHO_TAG

    u = FacebookUser(facebook_id='psid-echo-bot', name='Echo Bot')
    db.session.add(u)
    db.session.commit()

    payload = {
        'object': 'page',
        'entry': [{
            'messaging': [{
                'sender': {'id': 'PAGE_ID'},
                'recipient': {'id': 'psid-echo-bot'},
                'message': {'is_echo': True, 'text': 'bot reply',
                            'metadata': BOT_ECHO_TAG},
            }],
        }],
    }
    resp = _post_webhook(client, payload)
    assert resp.status_code == 200

    refreshed = db.session.get(FacebookUser, u.id)
    assert refreshed.bot_muted_until is None   # the bot must not mute itself
    FacebookUser.query.filter_by(id=u.id).delete()
    db.session.commit()


def test_echo_does_not_create_page_user(client, db_session):
    from models import FacebookUser

    before = FacebookUser.query.filter_by(facebook_id='PAGE_ID').count()
    payload = {
        'object': 'page',
        'entry': [{
            'messaging': [{
                'sender': {'id': 'PAGE_ID'},
                'recipient': {'id': 'psid-never-seen'},
                'message': {'is_echo': True, 'text': 'hi'},
            }],
        }],
    }
    resp = _post_webhook(client, payload)
    assert resp.status_code == 200
    # The Page must never be persisted as a FacebookUser, and an unknown
    # recipient simply gets no mute (no row created either).
    assert FacebookUser.query.filter_by(facebook_id='PAGE_ID').count() == before
    assert FacebookUser.query.filter_by(facebook_id='psid-never-seen').count() == 0


def test_send_facebook_message_tags_metadata(monkeypatch):
    import services

    captured = {}

    class _Resp:
        status_code = 200
        text = 'ok'

    def fake_post(url, json=None, headers=None, timeout=None):
        captured['json'] = json
        return _Resp()

    monkeypatch.setattr(services.requests, 'post', fake_post)
    ok = services.send_facebook_message('psid-x', 'hello')
    assert ok is True
    assert captured['json']['message']['metadata'] == services.BOT_ECHO_TAG
