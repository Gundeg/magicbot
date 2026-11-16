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


# ------------------------------------------------- Task 2: the report
# Frozen far in the future so only this file's rows fall inside the window.
# 2030-01-07 is a Monday; UB = UTC+8. NOW = Mon 20:00 UB.
NOW = datetime(2030, 1, 7, 12, 0)
SINCE = NOW - timedelta(days=7)


def ub(day, hh, mm=0):
    """A UTC timestamp for the given Ulaanbaatar wall time in Jan 2030."""
    return datetime(2030, 1, day, hh, mm) - sch.UB_OFFSET


def _m(u, sender, at, text='x', kind=None):
    from extensions import db
    from models import Message
    m = Message(facebook_user_id=u.id, sender=sender, content=text, created_at=at, kind=kind)
    db.session.add(m)
    db.session.commit()
    return m


def _report(days=7):
    from services._performance import performance_report
    return performance_report(days, now=NOW)


@pytest.fixture
def fresh(app, db_session):
    """Users created here are cleaned up after the test."""
    made = []

    def make(psid, **kw):
        u = _user(psid, **kw)
        made.append(u.id)
        return u
    yield make
    _cleanup(*made)


@pytest.fixture
def no_staff_history(app, db_session):
    """Team metrics count from the first ever staff message — start clean."""
    from extensions import db
    from models import Message
    Message.query.filter_by(sender='staff').delete()
    db.session.commit()


def _tracker(fresh):
    """A staff reply before the window: staff-reply tracking has started."""
    t = fresh('psid-perf-tracker')
    _m(t, 'staff', SINCE - timedelta(days=1))
    return t


def test_waits_are_per_conversation(fresh):
    a, b = fresh('psid-perf-a'), fresh('psid-perf-b')
    t0 = ub(6, 11)                                    # Sunday: bot hours
    _m(a, 'user', t0)
    _m(b, 'user', t0 + timedelta(minutes=1))
    _m(a, 'bot', t0 + timedelta(minutes=2))
    _m(b, 'bot', t0 + timedelta(minutes=3))
    assert _report()['bot']['median_reply_seconds'] == 120


def test_reply_to_pre_window_message_is_not_a_wait(fresh):
    u = fresh('psid-perf-pre')
    _m(u, 'user', SINCE - timedelta(hours=1))
    _m(u, 'bot', SINCE + timedelta(hours=1))
    r = _report()
    assert r['bot']['median_reply_seconds'] is None
    assert r['bot']['replies'] == 1


def test_staff_hours_buckets_sum_to_waits(fresh, no_staff_history):
    _tracker(fresh)
    users = [fresh(f'psid-perf-bucket-{i}') for i in range(4)]
    t0 = ub(7, 10)                                    # Monday 10:00 UB: staff hours
    for u in users:
        _m(u, 'user', t0)
    _m(users[0], 'staff', t0 + timedelta(minutes=5))
    _m(users[1], 'bot', t0 + timedelta(minutes=16), kind='step_in')
    _m(users[2], 'bot', t0 + timedelta(minutes=1))
    team = _report()['team']
    assert (team['waits'], team['staff'], team['step_in'], team['bot'], team['unanswered']) == (4, 1, 1, 1, 1)
    assert team['staff_pct'] == 25


def test_wait_at_0900_is_staff_hours(fresh, no_staff_history):
    _tracker(fresh)
    early, on_time = fresh('psid-perf-0859'), fresh('psid-perf-0900')
    _m(early, 'user', ub(7, 8, 59))
    _m(on_time, 'user', ub(7, 9, 0))
    assert _report()['team']['waits'] == 1


def test_team_ignores_waits_before_first_staff_message(fresh, no_staff_history):
    before, after = fresh('psid-perf-before'), fresh('psid-perf-after')
    _m(before, 'user', ub(7, 10))
    _m(after, 'staff', ub(7, 10, 30))                 # tracking starts here
    _m(after, 'user', ub(7, 11))
    team = _report()['team']
    assert team['tracking_since'] == ub(7, 10, 30)
    assert team['waits'] == 1


def test_median_staff_minutes(fresh, no_staff_history):
    _tracker(fresh)
    for i, delay in enumerate((2, 6, 10)):
        u = fresh(f'psid-perf-median-{i}')
        _m(u, 'user', ub(7, 10))
        _m(u, 'staff', ub(7, 10) + timedelta(minutes=delay))
    assert _report()['team']['median_staff_minutes'] == 6


def test_long_waits_include_unanswered(fresh, no_staff_history):
    _tracker(fresh)
    slow, quick, ignored = fresh('psid-perf-slow'), fresh('psid-perf-quick'), fresh('psid-perf-ignored')
    for u in (slow, quick, ignored):
        _m(u, 'user', ub(7, 10))
    _m(slow, 'staff', ub(7, 10, 20))
    _m(quick, 'staff', ub(7, 10, 5))
    team = _report()['team']
    assert team['threshold_minutes'] == 15
    assert team['long_waits'] == 2


def test_bot_metrics(fresh):
    from extensions import db
    u1, u2, u3, u4 = (fresh(f'psid-perf-bot-{i}') for i in range(4))
    t0 = ub(6, 12)
    _m(u1, 'user', t0)
    _m(u1, 'bot', t0 + timedelta(seconds=30))
    _m(u1, 'bot', t0 + timedelta(minutes=5))
    _m(u2, 'bot', t0)
    _m(u3, 'bot', t0, kind='nudge')
    _m(u4, 'bot', t0, kind='step_in')
    u2.registered_at = t0
    u2.lead_at = t0
    db.session.commit()
    bot = _report()['bot']
    assert (bot['people'], bot['replies'], bot['step_ins']) == (3, 4, 1)
    assert (bot['registrations'], bot['new_leads']) == (1, 1)
    assert bot['median_reply_seconds'] == 30


def _issue(u, content, created, **kw):
    from extensions import db
    from models import AdminIssue
    i = AdminIssue(facebook_user_id=u.id, issue_type='handoff', content=content,
                   created_at=created, **kw)
    db.session.add(i)
    db.session.commit()
    return i


def test_handoff_categories_parsed_and_other(fresh):
    u = fresh('psid-perf-handoff')
    _issue(u, '[бот: мэдлэг дутуу — нэвтрэх заавар] Платформд яаж нэвтрэх вэ?', ub(7, 10))
    _issue(u, '[бот: гомдол — удаан хариу][OFF-HOURS] Яагаад хариулахгүй байна', ub(7, 11))
    _issue(u, '[manual] гараар нээсэн', ub(7, 12))
    h = _report()['bot']['handoffs']
    assert (h['мэдлэг дутуу'], h['гомдол'], h['ажилтантай ярих хүсэлт'], h['бусад']) == (1, 1, 0, 1)


def test_staff_rows_from_audit(fresh):
    from extensions import db
    from models import AuditEntry, User
    from werkzeug.security import generate_password_hash
    User.query.filter_by(username='pytest-perf-staff').delete()
    db.session.commit()
    staff = User(username='pytest-perf-staff', password=generate_password_hash('x'),
                 email='pytest-perf-staff@example.com', role='registration_staff')
    db.session.add(staff)
    db.session.commit()
    t0 = ub(7, 10)
    for action, detail in [
        ('issue.status_change', 'Work Tasks-аас шийдсэн'),
        ('issue.status_change', 'open → resolved'),
        ('issue.status_change', 'open → in_progress'),
        ('bot.take_over', 'x ботыг 4ц зогсоож яриаг хариуцлаа'),
        ('lead.mark_contacted', 'Work Tasks-аас холбогдсон гэж тэмдэглэв'),
        ('lead.status_change', 'new → contacted'),
        ('lead.status_change', 'contacted → converted'),
        ('lead.drop', 'Hot Prospect-оос орхив. Шалтгаан: x'),
        ('lead.status_change', 'new → dropped. Шалтгаан: x'),
    ]:
        db.session.add(AuditEntry(actor_id=staff.id, actor_username=staff.username,
                                  action=action, detail=detail, created_at=t0))
    u = fresh('psid-perf-resolve')
    _issue(u, '[бот: гомдол] a', ub(7, 10), status='resolved',
           resolved_at=ub(7, 10, 40), updated_by_id=staff.id)
    _issue(u, '[бот: гомдол] b', ub(7, 11), status='resolved',
           resolved_at=ub(7, 11, 20), updated_by_id=staff.id)
    db.session.commit()
    row = next(r for r in _report()['staff'] if r['username'] == 'pytest-perf-staff')
    assert (row['resolved'], row['takeovers'], row['contacted'], row['converted'], row['dropped']) == (2, 1, 2, 1, 2)
    assert row['avg_resolution_minutes'] == 30
    AuditEntry.query.filter_by(actor_id=staff.id).delete()
    User.query.filter_by(id=staff.id).delete()
    db.session.commit()


def test_questions_grouped_by_reason(fresh):
    u = fresh('psid-perf-questions')
    _issue(u, '[бот: мэдлэг дутуу — Нэвтрэх заавар] Яаж нэвтрэх вэ?', ub(7, 9))
    _issue(u, '[бот: мэдлэг дутуу — нэвтрэх заавар ] Нууц үгээ мартсан', ub(7, 12))
    _issue(u, '[бот: мэдлэг дутуу — Татварын хуулийн өөрчлөлт] 2030 онд юу өөрчлөгдөх вэ?', ub(7, 10))
    _issue(u, '[бот: гомдол — удаан] энэ бол асуулт биш', ub(7, 13))
    qs = _report()['questions']
    assert [q['count'] for q in qs] == [2, 1]
    assert qs[0]['sample'] == 'Нууц үгээ мартсан'        # newest message of the group
    assert qs[0]['user_id'] == u.id


def test_heatmap_buckets_in_ub_time(fresh):
    u = fresh('psid-perf-heat')
    _m(u, 'user', datetime(2030, 1, 6, 23, 30))          # Sun 23:30 UTC = Mon 07:30 UB
    heat = _report()['heatmap']
    assert heat['grid'][0][7] == 1 and heat['max'] >= 1
    assert (0, 9) in heat['staff_cells'] and (0, 13) not in heat['staff_cells']
    assert (5, 10) not in heat['staff_cells']


def test_empty_window_has_no_crash(app, db_session):
    from services._performance import performance_report
    r = performance_report(7, now=datetime(2035, 1, 1, 12, 0))
    assert r['team']['waits'] == 0 and r['team']['staff_pct'] is None
    assert r['bot']['median_reply_seconds'] is None
    assert r['questions'] == [] and r['heatmap']['max'] == 0
