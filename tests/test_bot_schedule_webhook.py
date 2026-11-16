"""Bot schedule in the live pipeline: the webhook stays quiet in staff hours,
staff Page-inbox replies are stamped, and the step-in sweep answers a
customer staff left waiting — exactly once, never over a staff reply.

The clock is frozen through services._schedule._utcnow; 2026-11-16 is a Monday.
"""
import hashlib
import hmac
import json
from datetime import datetime, timedelta

import pytest

from services import _schedule as sch

MON_10_UB = datetime(2026, 11, 16, 10, 0)    # staff on duty
MON_19_UB = datetime(2026, 11, 16, 19, 0)    # bot on duty
NOW_UTC = MON_10_UB - sch.UB_OFFSET          # the sweep's "now" (staff hours)


def _sign(body):
    from services import FACEBOOK_APP_SECRET
    return hmac.new(FACEBOOK_APP_SECRET.encode('utf-8'), body, hashlib.sha256).hexdigest()


def _post(client, payload):
    body = json.dumps(payload).encode('utf-8')
    return client.post('/webhook', data=body, headers={
        'X-Hub-Signature-256': f'sha256={_sign(body)}', 'Content-Type': 'application/json'})


def _inbound(psid, text):
    return {'object': 'page', 'entry': [{'messaging': [{
        'sender': {'id': psid}, 'recipient': {'id': 'PAGE_ID'}, 'message': {'text': text}}]}]}


def _save_schedule(mode='schedule', enabled=True, minutes=15):
    from extensions import db
    from models import GeneralSetting
    s = json.loads(json.dumps(sch.DEFAULT_SCHEDULE))
    s.update(mode=mode, step_in={'enabled': enabled, 'minutes': minutes})
    GeneralSetting.query.filter_by(key=sch.SCHEDULE_KEY).delete()
    db.session.add(GeneralSetting(key=sch.SCHEDULE_KEY, value=json.dumps(s)))
    db.session.commit()


@pytest.fixture
def pipeline(app, db_session, monkeypatch):
    """Stub every network edge of the reply path; record what happens."""
    import routes.webhook as wh
    rec = {'gen': [], 'sent': [], 'classified': []}

    def _gen(*a, **k):
        rec['gen'].append(k)
        return 'Сайн байна уу, туслъя.'
    monkeypatch.setattr(wh, 'generate_bot_response', _gen)
    monkeypatch.setattr(wh, 'send_facebook_message', lambda *a, **k: rec['sent'].append(a))
    monkeypatch.setattr(wh, 'send_sender_action', lambda *a, **k: None)
    monkeypatch.setattr(wh, 'check_rate_limit', lambda sid: True)
    monkeypatch.setattr(wh, 'refresh_user_summary', lambda *a, **k: None)
    monkeypatch.setattr(wh, 'classify_user_topics', lambda uid: rec['classified'].append(uid))
    return rec


def _freeze(monkeypatch, ub_time):
    monkeypatch.setattr(sch, '_utcnow', lambda: ub_time - sch.UB_OFFSET)


def _user(psid, **kw):
    """A fresh customer. SQLite reuses a deleted row's id and other tests
    delete users without their messages, so clear any orphans on this id.
    Pre-creating also keeps the webhook off the Graph profile lookup."""
    from extensions import db
    from models import FacebookUser, Message
    FacebookUser.query.filter_by(facebook_id=psid).delete()
    db.session.commit()
    u = FacebookUser(facebook_id=psid, name='Тест', **kw)
    db.session.add(u)
    db.session.commit()
    Message.query.filter_by(facebook_user_id=u.id).delete()
    db.session.commit()
    return u


def _cleanup(*user_ids):
    from extensions import db
    from models import FacebookUser, Message
    for uid in user_ids:
        Message.query.filter_by(facebook_user_id=uid).delete()
        FacebookUser.query.filter_by(id=uid).delete()
    db.session.commit()


def _msg(user, sender, minutes_ago, text='асуулт'):
    from extensions import db
    from models import Message
    m = Message(facebook_user_id=user.id, sender=sender, content=text,
                created_at=NOW_UTC - timedelta(minutes=minutes_ago))
    db.session.add(m)
    db.session.commit()
    return m


def _bot_replies(user):
    from models import Message
    return Message.query.filter_by(facebook_user_id=user.id, sender='bot').count()


# ----------------------------------------------------------- webhook gate

def test_staff_hours_inbound_saved_but_no_reply(client, pipeline, monkeypatch):
    from models import Message
    _save_schedule()
    _freeze(monkeypatch, MON_10_UB)
    u = _user('psid-sched-staff')
    assert _post(client, _inbound('psid-sched-staff', 'үнэ хэд вэ')).status_code == 200
    assert Message.query.filter_by(facebook_user_id=u.id, sender='user').count() == 1
    assert pipeline['gen'] == [] and pipeline['sent'] == []
    assert pipeline['classified'] == [u.id]          # Work Tasks topics stay current
    _cleanup(u.id)


def test_bot_hours_inbound_gets_reply(client, pipeline, monkeypatch):
    _save_schedule()
    _freeze(monkeypatch, MON_19_UB)
    u = _user('psid-sched-bot')
    _post(client, _inbound('psid-sched-bot', 'үнэ хэд вэ'))
    assert len(pipeline['sent']) == 1
    _cleanup(u.id)


def test_mode_off_never_replies(client, pipeline, monkeypatch):
    _save_schedule(mode='off')
    _freeze(monkeypatch, MON_19_UB)
    u = _user('psid-sched-off')
    _post(client, _inbound('psid-sched-off', 'сайн уу'))
    assert pipeline['sent'] == []
    _cleanup(u.id)


def test_staff_echo_stamps_last_staff_reply_at(client, db_session):
    from extensions import db
    from models import FacebookUser
    u = _user('psid-sched-echo')
    payload = {'object': 'page', 'entry': [{'messaging': [{
        'sender': {'id': 'PAGE_ID'}, 'recipient': {'id': 'psid-sched-echo'},
        'message': {'is_echo': True, 'text': 'Сайн байна уу, Давка байна'}}]}]}
    _post(client, payload)
    db.session.expire_all()
    stamped = db.session.get(FacebookUser, u.id).last_staff_reply_at
    assert stamped is not None and stamped > datetime.utcnow() - timedelta(minutes=1)
    _cleanup(u.id)


# ----------------------------------------------------------- step-in sweep

def test_step_in_replies_after_n_minutes(pipeline):
    import routes.webhook as wh
    _save_schedule(minutes=15)
    u = _user('psid-stepin-1')
    _msg(u, 'user', 16, 'Сургалт хэзээ эхлэх вэ?')
    assert wh.step_in_sweep(now=NOW_UTC) == [u.id]
    assert len(pipeline['sent']) == 1 and _bot_replies(u) == 1
    assert pipeline['gen'][0]['waited_minutes'] >= 15
    _cleanup(u.id)


def test_step_in_skips_when_staff_replied(pipeline):
    import routes.webhook as wh
    _save_schedule()
    u = _user('psid-stepin-staff', last_staff_reply_at=NOW_UTC - timedelta(minutes=5))
    _msg(u, 'user', 16)
    assert wh.step_in_sweep(now=NOW_UTC) == []
    assert pipeline['sent'] == []
    _cleanup(u.id)


def test_step_in_skips_muted(pipeline):
    import routes.webhook as wh
    _save_schedule()
    u = _user('psid-stepin-muted', bot_muted_until=NOW_UTC + timedelta(hours=2))
    _msg(u, 'user', 16)
    assert wh.step_in_sweep(now=NOW_UTC) == []
    _cleanup(u.id)


@pytest.mark.parametrize('minutes_ago', [10, 80])
def test_step_in_skips_too_young_and_too_old(pipeline, minutes_ago):
    import routes.webhook as wh
    _save_schedule(minutes=15)
    u = _user(f'psid-stepin-age-{minutes_ago}')
    _msg(u, 'user', minutes_ago)
    assert wh.step_in_sweep(now=NOW_UTC) == []
    _cleanup(u.id)


@pytest.mark.parametrize('mode, enabled', [('schedule', False), ('always_on', True), ('off', True)])
def test_step_in_disabled_or_mode_not_schedule(pipeline, mode, enabled):
    import routes.webhook as wh
    _save_schedule(mode=mode, enabled=enabled)
    u = _user(f'psid-stepin-{mode}-{enabled}')
    _msg(u, 'user', 16)
    assert wh.step_in_sweep(now=NOW_UTC) == []
    _cleanup(u.id)


def test_step_in_once_for_burst(pipeline):
    import routes.webhook as wh
    _save_schedule(minutes=15)
    u = _user('psid-stepin-burst')
    for ago in (20, 18, 16):
        _msg(u, 'user', ago)
    wh.step_in_sweep(now=NOW_UTC)
    wh.step_in_sweep(now=NOW_UTC + timedelta(minutes=1))
    assert len(pipeline['sent']) == 1 and _bot_replies(u) == 1
    _cleanup(u.id)


def test_step_in_waits_from_first_unanswered_message(pipeline):
    """A customer who keeps writing must not push the step-in back: the wait
    counts from their first unanswered message."""
    import routes.webhook as wh
    _save_schedule(minutes=15)
    u = _user('psid-stepin-chatty')
    _msg(u, 'user', 16)
    _msg(u, 'user', 3)
    assert wh.step_in_sweep(now=NOW_UTC) == [u.id]
    _cleanup(u.id)


def test_step_in_claim_is_atomic(app, db_session):
    import routes.webhook as wh
    u = _user('psid-stepin-claim')
    since = NOW_UTC - timedelta(minutes=16)
    assert wh._claim_step_in(u.id, since, NOW_UTC) is True
    assert wh._claim_step_in(u.id, since, NOW_UTC) is False   # the other process loses
    _cleanup(u.id)


# ----------------------------------------------------- final-review fixes

def test_step_in_ignores_stale_unanswered_history(pipeline):
    """Review #1: a day-old unanswered message must not make the bot jump in
    one minute after the customer's new message (and claim a 1440-min wait)."""
    import routes.webhook as wh
    _save_schedule(minutes=15)
    u = _user('psid-stepin-stale')
    _msg(u, 'user', 24 * 60)
    _msg(u, 'user', 1)
    assert wh.step_in_sweep(now=NOW_UTC) == []
    assert wh.step_in_sweep(now=NOW_UTC + timedelta(minutes=15)) == [u.id]
    assert 15 <= pipeline['gen'][0]['waited_minutes'] <= 15 + wh.STEP_IN_ACTIVE_WINDOW_MINUTES
    _cleanup(u.id)


def test_bot_hours_reply_in_flight_blocks_step_in(client, pipeline, monkeypatch):
    """Review #2: at a handover a webhook reply may still be generating when
    the sweep runs — the sweep must not answer the same wait a second time."""
    from extensions import db
    from models import Message
    import routes.webhook as wh
    _save_schedule(minutes=15)
    _freeze(monkeypatch, MON_19_UB)                       # bot hours
    queued = []
    monkeypatch.setattr(wh, 'enqueue_background', lambda *a, **k: queued.append(a))
    u = _user('psid-stepin-inflight')
    db.session.add(Message(facebook_user_id=u.id, sender='user', content='өмнөх асуулт',
                           created_at=datetime.utcnow() - timedelta(minutes=16)))
    db.session.commit()
    _post(client, _inbound('psid-stepin-inflight', 'шинэ асуулт'))
    assert queued                                          # webhook reply in flight
    assert wh.step_in_sweep(now=datetime.utcnow()) == []
    _cleanup(u.id)


def test_staff_echo_keeps_a_longer_takeover(client, db_session):
    """Review #4: one inbox reply must not cut a Хариуцах takeover to 30 min."""
    from extensions import db
    from models import FacebookUser
    u = _user('psid-takeover-long', bot_muted_until=datetime.utcnow() + timedelta(hours=4))
    payload = {'object': 'page', 'entry': [{'messaging': [{
        'sender': {'id': 'PAGE_ID'}, 'recipient': {'id': 'psid-takeover-long'},
        'message': {'is_echo': True, 'text': 'Би хариуцаж байна'}}]}]}
    _post(client, payload)
    db.session.expire_all()
    assert db.session.get(FacebookUser, u.id).bot_muted_until > datetime.utcnow() + timedelta(hours=3)
    _cleanup(u.id)
