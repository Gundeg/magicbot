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


def test_pause_bot_mutes_then_resumes(client, admin_user, db_session):
    from extensions import db
    from models import FacebookUser

    u = FacebookUser(facebook_id='psid-pause', name='Pause Me')
    db.session.add(u)
    db.session.commit()
    _login(client, admin_user)

    # Pause 30 min → muted into the future.
    resp = client.post('/admin/api/pause-bot', json={'user_id': u.id, 'minutes': 30})
    assert resp.status_code == 200 and resp.get_json()['success'] is True
    refreshed = db.session.get(FacebookUser, u.id)
    assert refreshed.bot_muted_until is not None
    assert refreshed.bot_muted_until > datetime.utcnow()

    # Resume (minutes=0) → cleared.
    resp = client.post('/admin/api/pause-bot', json={'user_id': u.id, 'minutes': 0})
    assert resp.status_code == 200
    db.session.expire(refreshed)
    assert db.session.get(FacebookUser, u.id).bot_muted_until is None

    FacebookUser.query.filter_by(id=u.id).delete()
    db.session.commit()


class _TokenResp:
    """Minimal requests.Response stand-in for the Graph /me probe."""

    def __init__(self, code, payload=None):
        self.status_code = code
        self._payload = payload or {}
        self.text = 'body'

    def json(self):
        return self._payload


def test_check_facebook_token(monkeypatch):
    import services

    monkeypatch.setattr(services, 'FACEBOOK_PAGE_ID', '123001937756085')

    monkeypatch.setattr(
        services.requests, 'get',
        lambda *a, **k: _TokenResp(200, {'id': '123001937756085',
                                         'name': 'Magic Financial Group'}),
    )
    ok, detail = services.check_facebook_token()
    assert ok is True and 'Magic Financial Group' in detail

    monkeypatch.setattr(services.requests, 'get', lambda *a, **k: _TokenResp(401))
    ok, detail = services.check_facebook_token()
    assert ok is False and 'status=401' in detail


def test_check_facebook_token_rejects_a_user_token(monkeypatch):
    """A USER token reads the Page by id happily, so the health check must
    verify that `me` resolves to the PAGE — otherwise it reports OK while every
    send fails GraphMethodException code:100 subcode:33."""
    import services

    monkeypatch.setattr(services, 'FACEBOOK_PAGE_ID', '123001937756085')
    monkeypatch.setattr(
        services.requests, 'get',
        lambda *a, **k: _TokenResp(200, {'id': '77777777', 'name': 'Some Person'}),
    )
    ok, detail = services.check_facebook_token()
    assert ok is False
    assert 'USER token' in detail and 'Some Person' in detail


def test_check_facebook_token_without_page_id_configured(monkeypatch):
    """Dev deployments leave FACEBOOK_PAGE_ID unset — a reachable token still
    counts as OK there; there is nothing to compare the id against."""
    import services

    monkeypatch.setattr(services, 'FACEBOOK_PAGE_ID', '')
    monkeypatch.setattr(
        services.requests, 'get',
        lambda *a, **k: _TokenResp(200, {'id': '999', 'name': 'Dev Page'}),
    )
    ok, _ = services.check_facebook_token()
    assert ok is True


# The verbatim Send API body from the 2026-09-04 outage — the bot composed
# every reply correctly and then failed at the last inch for three days with
# nobody paged.
EXPIRED_TOKEN_BODY = (
    '{"error":{"message":"Error validating access token: Session has expired on '
    'Friday, 04-Sep-26 07:23:45 PDT. The current time is Monday, 07-Sep-26 '
    '13:13:44 PDT.","type":"OAuthException","code":190,"error_subcode":463,'
    '"fbtrace_id":"AZQGzg5iCup-vnK-058zbD8"}}'
)
USER_TOKEN_BODY = (
    '{"error":{"message":"Object with ID \'me\' does not exist",'
    '"type":"GraphMethodException","code":100,"error_subcode":33}}'
)
PERMISSION_BODY = (
    '{"error":{"message":"Application does not have permission for this action",'
    '"type":"OAuthException","code":10}}'
)


def test_is_facebook_token_error_classifies_real_bodies():
    import services

    ok, reason = services._is_facebook_token_error(EXPIRED_TOKEN_BODY)
    assert ok is True and '190/463' in reason

    ok, reason = services._is_facebook_token_error(USER_TOKEN_BODY)
    assert ok is True and '100/33' in reason

    # Permission problems are policy issues, not a dead credential. Paging on
    # them would train staff to ignore the alert.
    assert services._is_facebook_token_error(PERMISSION_BODY)[0] is False
    assert services._is_facebook_token_error('')[0] is False
    assert services._is_facebook_token_error('<html>502 Bad Gateway</html>')[0] is False


def test_send_api_token_failure_pages_staff_once(db_session, monkeypatch):
    """A dead token fails EVERY send at once, so the alert must fire on the
    first failure and then stay quiet for the cooldown window."""
    import services
    from extensions import db
    from models import GeneralSetting

    GeneralSetting.query.filter_by(key=services.FB_TOKEN_ALERT_STATE_KEY).delete()
    db.session.commit()

    sent = []
    monkeypatch.setattr(services, 'send_telegram_notification', lambda text: sent.append(text))

    assert services.alert_facebook_token_failure(EXPIRED_TOKEN_BODY) is True
    assert len(sent) == 1
    assert 'FACEBOOK_ACCESS_TOKEN' in sent[0]

    # Same outage, next customer message — must NOT page again.
    assert services.alert_facebook_token_failure(EXPIRED_TOKEN_BODY) is False
    assert len(sent) == 1

    GeneralSetting.query.filter_by(key=services.FB_TOKEN_ALERT_STATE_KEY).delete()
    db.session.commit()


def test_send_facebook_message_alerts_on_dead_token(db_session, monkeypatch):
    """The alert must fire from the send path itself, not only from the
    6-hourly health check — that check is a no-op without Telegram and only
    runs on the worker dyno."""
    import services
    from extensions import db
    from models import GeneralSetting

    GeneralSetting.query.filter_by(key=services.FB_TOKEN_ALERT_STATE_KEY).delete()
    db.session.commit()

    class _Resp:
        status_code = 401
        text = EXPIRED_TOKEN_BODY

    sent = []
    monkeypatch.setattr(services.requests, 'post', lambda *a, **k: _Resp())
    monkeypatch.setattr(services, 'send_telegram_notification', lambda text: sent.append(text))

    assert services.send_facebook_message('psid-1', 'hi') is False
    assert len(sent) == 1

    GeneralSetting.query.filter_by(key=services.FB_TOKEN_ALERT_STATE_KEY).delete()
    db.session.commit()


def test_send_facebook_message_does_not_alert_on_permission_error(db_session, monkeypatch):
    import services
    from extensions import db
    from models import GeneralSetting

    GeneralSetting.query.filter_by(key=services.FB_TOKEN_ALERT_STATE_KEY).delete()
    db.session.commit()

    class _Resp:
        status_code = 403
        text = PERMISSION_BODY

    sent = []
    monkeypatch.setattr(services.requests, 'post', lambda *a, **k: _Resp())
    monkeypatch.setattr(services, 'send_telegram_notification', lambda text: sent.append(text))

    assert services.send_facebook_message('psid-1', 'hi') is False
    assert sent == []


def _reset_alert_windows():
    """Both alert cooldowns share the DB, and db.session is session-scoped."""
    import services
    from extensions import db
    from models import GeneralSetting

    for key in (services.FB_TOKEN_ALERT_STATE_KEY, services.FB_HEALTH_ALERT_STATE_KEY):
        GeneralSetting.query.filter_by(key=key).delete()
    db.session.commit()


def test_token_error_classifier_reads_the_health_checks_wrapped_detail():
    """check_facebook_token returns "status=401 body={...}", the send path
    returns the raw body. One classifier has to read both."""
    import services

    wrapped = 'status=401 body=' + EXPIRED_TOKEN_BODY
    ok, reason = services._is_facebook_token_error(wrapped)
    assert ok is True and '190/463' in reason


def test_health_check_uses_the_definite_alert_for_a_dead_token(db_session, monkeypatch):
    """A confirmed dead token must not be reported as "the bot MIGHT not be
    able to reply" — that hedged wording is why the 2026-09-04 outage read like
    a warning and sat unactioned over a weekend."""
    import services

    _reset_alert_windows()
    sent = []
    monkeypatch.setattr(services, 'send_telegram_notification', lambda text: sent.append(text))

    detail = 'status=401 body=' + EXPIRED_TOKEN_BODY
    assert services.report_token_health(detail) == 'token'
    assert len(sent) == 1
    assert 'ЯМАР Ч' in sent[0]          # states the outage as fact
    assert 'магадгүй' not in sent[0]    # no hedging
    _reset_alert_windows()


def test_health_check_hedges_only_when_the_failure_is_ambiguous(db_session, monkeypatch):
    """Graph unreachable is genuinely uncertain — there the soft wording is
    honest, and it still gets a cooldown so it can't repeat every 6h forever."""
    import services

    _reset_alert_windows()
    sent = []
    monkeypatch.setattr(services, 'send_telegram_notification', lambda text: sent.append(text))

    detail = 'exception=HTTPSConnectionPool(host=\'graph.facebook.com\'): timed out'
    assert services.report_token_health(detail) == 'unknown'
    assert len(sent) == 1
    assert 'шалгаж чадсангүй' in sent[0]

    # Same ambiguous failure 6h later -> silent, not a twelfth identical message.
    assert services.report_token_health(detail) == 'quiet'
    assert len(sent) == 1
    _reset_alert_windows()


def test_one_outage_produces_one_alert_across_both_paths(db_session, monkeypatch):
    """The send path and the health check share a cooldown window, so a dead
    token pages staff once — not once per detection path."""
    import services

    _reset_alert_windows()
    sent = []
    monkeypatch.setattr(services, 'send_telegram_notification', lambda text: sent.append(text))

    # Send path notices first (first failed customer message).
    assert services.alert_facebook_token_failure(EXPIRED_TOKEN_BODY) is True
    assert len(sent) == 1

    # The 6-hourly health check then sees the same outage -> stays quiet.
    assert services.report_token_health('status=401 body=' + EXPIRED_TOKEN_BODY) == 'quiet'
    assert len(sent) == 1
    _reset_alert_windows()


def test_ensure_page_subscriptions_adds_echoes_without_dropping(monkeypatch):
    import services

    class _Get:
        status_code = 200
        def json(self):
            return {'data': [{'subscribed_fields': ['messages', 'messaging_postbacks']}]}

    class _Post:
        status_code = 200
        text = 'ok'

    captured = {}
    monkeypatch.setattr(services.requests, 'get', lambda *a, **k: _Get())

    def fake_post(url, params=None, **k):
        captured['fields'] = params['subscribed_fields']
        return _Post()
    monkeypatch.setattr(services.requests, 'post', fake_post)

    ok, detail = services.ensure_page_subscriptions()
    assert ok is True
    # message_echoes added, and the existing fields are NOT dropped.
    assert 'message_echoes' in captured['fields']
    assert 'messages' in captured['fields']
    assert 'messaging_postbacks' in captured['fields']
    assert detail['added'] == ['message_echoes']


def test_professional_accuracy_rule_in_prompt(app):
    """The anti-fabrication / anti-sycophancy rule must be present in every
    built system prompt so the bot stops inventing tax/law specifics."""
    from services._prompt import build_system_prompt
    with app.app_context():
        prompt = build_system_prompt(session_state='new', funnel_stage='curious')
    assert 'МЭРГЭЖЛИЙН ҮНЭН ЗӨВ БАЙДАЛ' in prompt
    assert 'СИКОФАНТ БҮҮ БОЛ' in prompt
    assert 'ТТ-13' in prompt   # the concrete example survives into the prompt


def _inbound(psid, text):
    return {
        'object': 'page',
        'entry': [{
            'messaging': [{
                'sender': {'id': psid},
                'recipient': {'id': 'PAGE_ID'},
                'message': {'text': text},
            }],
        }],
    }


# --------------------------------------------------------------------------
# Webhook: rate-limit silent drop + takeover silence
# --------------------------------------------------------------------------

def test_rate_limited_message_is_silently_dropped(client, db_session, monkeypatch):
    """Over the rate limit -> no outbound message at all (was the spammy
    'Та маш олон мессеж бичиж байна' reply)."""
    import routes.webhook as wh

    sent = []
    monkeypatch.setattr(wh, 'check_rate_limit', lambda sid: False)
    monkeypatch.setattr(wh, 'send_facebook_message', lambda *a, **k: sent.append(a))

    resp = _post_webhook(client, _inbound('psid-rl', 'hello'))
    assert resp.status_code == 200
    assert sent == []   # silent — nothing sent back to the customer


def test_duplicate_mid_is_processed_once(client, db_session, monkeypatch):
    """Facebook retries the same delivery (same mid) when we're slow. The
    retry must be dropped: one inbound row, one LLM call, one outbound send —
    and crucially it must NOT count against the rate limiter (the root cause
    of 'rate-limited on the very first message')."""
    import routes.webhook as wh
    from extensions import db
    from models import FacebookUser, Message

    sent = []
    gen_calls = {'n': 0}
    monkeypatch.setattr(wh, 'check_rate_limit', lambda sid: True)
    monkeypatch.setattr(wh, 'send_facebook_message', lambda *a, **k: sent.append(a))
    # The reply now runs through process_inbound_reply (enqueue_background runs
    # inline under TESTING). Stub the OpenAI / network calls it makes.
    monkeypatch.setattr(wh, 'send_sender_action', lambda *a, **k: None)
    monkeypatch.setattr(wh, 'classify_user_topics', lambda *a, **k: None)

    def _gen(*a, **k):
        gen_calls['n'] += 1
        return 'reply'
    monkeypatch.setattr(wh, 'generate_bot_response', _gen)

    payload = {
        'object': 'page',
        'entry': [{'messaging': [{
            'sender': {'id': 'psid-dup'},
            'recipient': {'id': 'PAGE_ID'},
            'message': {'mid': 'm.unique-123', 'text': 'сайн уу'},
        }]}],
    }
    r1 = _post_webhook(client, payload)
    r2 = _post_webhook(client, payload)   # FB retry — identical mid
    assert r1.status_code == 200 and r2.status_code == 200

    u = FacebookUser.query.filter_by(facebook_id='psid-dup').first()
    assert u is not None
    inbound = Message.query.filter_by(facebook_user_id=u.id, sender='user').count()
    assert inbound == 1          # retry did not create a second row
    assert gen_calls['n'] == 1   # bot generated a reply only once
    assert len(sent) == 1        # customer received exactly one reply

    Message.query.filter_by(facebook_user_id=u.id).delete()
    FacebookUser.query.filter_by(id=u.id).delete()
    db.session.commit()


def test_muted_user_gets_no_bot_reply(client, db_session, monkeypatch):
    """A human takeover (bot_muted_until in the future) silences the bot:
    no LLM call, no outbound message."""
    import routes.webhook as wh
    from extensions import db
    from models import FacebookUser

    u = FacebookUser(
        facebook_id='psid-muted', name='Muted',
        bot_muted_until=datetime.utcnow() + timedelta(minutes=20),
    )
    db.session.add(u)
    db.session.commit()

    sent = []
    called = {'gen': False}
    monkeypatch.setattr(wh, 'check_rate_limit', lambda sid: True)
    monkeypatch.setattr(wh, 'send_facebook_message', lambda *a, **k: sent.append(a))

    def _boom(*a, **k):
        called['gen'] = True
        return 'should not be sent'
    monkeypatch.setattr(wh, 'generate_bot_response', _boom)

    resp = _post_webhook(client, _inbound('psid-muted', 'юу байна'))
    assert resp.status_code == 200
    assert sent == []            # bot stayed silent
    assert called['gen'] is False  # didn't even call the LLM

    FacebookUser.query.filter_by(id=u.id).delete()
    db.session.commit()


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
                       json={'action': 'drop_prospect', 'user_id': u.id,
                             'note': 'Test drop reason'})
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
