"""Гүйцэтгэл page: the data markers it needs, the report it computes, and the
admin-only page itself. 2026-11-16 is a Monday; UB = UTC+8."""
import hashlib
import hmac
import json
from datetime import datetime, timedelta

import pytest

from services import _schedule as sch


# ------------------------------------------------------------------ helpers

def _sign(body):
    from services import FACEBOOK_APP_SECRET
    return hmac.new(FACEBOOK_APP_SECRET.encode('utf-8'), body, hashlib.sha256).hexdigest()


def _post(client, payload):
    body = json.dumps(payload).encode('utf-8')
    return client.post('/webhook', data=body, headers={
        'X-Hub-Signature-256': f'sha256={_sign(body)}', 'Content-Type': 'application/json'})


def _user(psid, **kw):
    """Fresh customer; clears orphan rows on a reused SQLite id."""
    from extensions import db
    from models import AdminIssue, FacebookUser, Message
    FacebookUser.query.filter_by(facebook_id=psid).delete()
    db.session.commit()
    u = FacebookUser(facebook_id=psid, name=kw.pop('name', 'Тест'), **kw)
    db.session.add(u)
    db.session.commit()
    Message.query.filter_by(facebook_user_id=u.id).delete()
    AdminIssue.query.filter_by(facebook_user_id=u.id).delete()
    db.session.commit()
    return u


def _cleanup(*uids):
    from extensions import db
    from models import AdminIssue, FacebookUser, Message
    for uid in uids:
        Message.query.filter_by(facebook_user_id=uid).delete()
        AdminIssue.query.filter_by(facebook_user_id=uid).delete()
        FacebookUser.query.filter_by(id=uid).delete()
    db.session.commit()


@pytest.fixture
def reply_stubs(app, db_session, monkeypatch):
    import routes.webhook as wh
    monkeypatch.setattr(wh, 'generate_bot_response', lambda *a, **k: 'Сайн байна уу.')
    monkeypatch.setattr(wh, 'send_facebook_message', lambda *a, **k: True)
    monkeypatch.setattr(wh, 'send_sender_action', lambda *a, **k: None)
    monkeypatch.setattr(wh, 'classify_user_topics', lambda *a, **k: None)
    monkeypatch.setattr(wh, 'refresh_user_summary', lambda *a, **k: None)
    monkeypatch.setattr(wh, 'check_rate_limit', lambda sid: True)
    return wh


# ------------------------------------------------- Task 1: data markers

def test_phone_capture_stamps_lead_at(client, reply_stubs):
    from extensions import db
    from models import FacebookUser
    u = _user('psid-perf-phone')
    _post(client, {'object': 'page', 'entry': [{'messaging': [{
        'sender': {'id': 'psid-perf-phone'}, 'recipient': {'id': 'PAGE_ID'},
        'message': {'text': 'Миний утас 99112233'}}]}]})
    db.session.expire_all()
    got = db.session.get(FacebookUser, u.id)
    assert got.is_lead is True and got.lead_at is not None
    _cleanup(u.id)


def test_promote_to_lead_stamps_lead_at(client, app, db_session):
    from extensions import db
    from models import FacebookUser, User
    from werkzeug.security import generate_password_hash
    User.query.filter_by(username='pytest-perf-promote').delete()
    db.session.commit()
    admin = User(username='pytest-perf-promote', password=generate_password_hash('x'),
                 email='pytest-perf-promote@example.com', role='admin')
    db.session.add(admin)
    db.session.commit()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(admin.id)
        sess['_fresh'] = True
    u = _user('psid-perf-promote')
    client.post('/admin/work-tasks', json={'action': 'promote_to_lead', 'user_id': u.id})
    db.session.expire_all()
    assert db.session.get(FacebookUser, u.id).lead_at is not None
    _cleanup(u.id)
    User.query.filter_by(id=admin.id).delete()
    db.session.commit()


def test_registration_stamps_lead_at(reply_stubs, monkeypatch):
    from extensions import db
    from models import FacebookUser
    wh = reply_stubs
    monkeypatch.setattr(wh, 'submit_registration_form', lambda data: (True, '777'))
    monkeypatch.setattr(wh, 'send_telegram_notification', lambda text: 1)
    u = _user('psid-perf-register')
    wh.complete_registration(u, {'firstname': 'Бат', 'phone': '99112233'}, 'Бүртгэл үүслээ.')
    db.session.expire_all()
    assert db.session.get(FacebookUser, u.id).lead_at is not None
    _cleanup(u.id)


def test_lead_at_not_overwritten(client, reply_stubs):
    from extensions import db
    from models import FacebookUser
    first = datetime(2026, 11, 1, 3, 0)
    u = _user('psid-perf-keep', is_lead=False, lead_at=first)
    _post(client, {'object': 'page', 'entry': [{'messaging': [{
        'sender': {'id': 'psid-perf-keep'}, 'recipient': {'id': 'PAGE_ID'},
        'message': {'text': '88001122'}}]}]})
    db.session.expire_all()
    assert db.session.get(FacebookUser, u.id).lead_at == first
    _cleanup(u.id)


def test_step_in_reply_is_kind_step_in(reply_stubs):
    from models import Message
    wh = reply_stubs
    u = _user('psid-perf-kind')
    wh.process_inbound_reply(u.id, 'асуулт', 'active', waited_minutes=16)
    wh.process_inbound_reply(u.id, 'дахин асуулт', 'active')
    kinds = [m.kind for m in Message.query.filter_by(facebook_user_id=u.id, sender='bot')
             .order_by(Message.id).all()]
    assert kinds == ['step_in', None]
    _cleanup(u.id)


def test_nudge_is_kind_nudge(app, db_session, monkeypatch):
    from extensions import db
    from models import Message
    import services
    u = _user('psid-perf-nudge')
    db.session.add(Message(facebook_user_id=u.id, sender='user', content='сонирхож байна',
                           created_at=datetime.utcnow() - timedelta(hours=6)))
    db.session.commit()
    monkeypatch.setattr(services, 'send_facebook_message', lambda *a, **k: True)
    services.nudge_pending_leads()
    nudges = Message.query.filter_by(facebook_user_id=u.id, sender='bot').all()
    assert nudges and all(m.kind == 'nudge' for m in nudges)
    _cleanup(u.id)
