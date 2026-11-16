"""Staff replies typed in the Facebook Page inbox are stored as messages
(sender='staff'), so the bot's memory, the summary and the admin views see
them — and the coming performance dashboard can measure staff reply times."""
import hashlib
import hmac
import json

import pytest


def _sign(body):
    from services import FACEBOOK_APP_SECRET
    return hmac.new(FACEBOOK_APP_SECRET.encode('utf-8'), body, hashlib.sha256).hexdigest()


def _post(client, payload):
    body = json.dumps(payload).encode('utf-8')
    return client.post('/webhook', data=body, headers={
        'X-Hub-Signature-256': f'sha256={_sign(body)}', 'Content-Type': 'application/json'})


def _echo(psid, message):
    return {'object': 'page', 'entry': [{'messaging': [{
        'sender': {'id': 'PAGE_ID'}, 'recipient': {'id': psid},
        'message': {'is_echo': True, **message}}]}]}


def _user(psid):
    from extensions import db
    from models import FacebookUser, Message
    FacebookUser.query.filter_by(facebook_id=psid).delete()
    db.session.commit()
    u = FacebookUser(facebook_id=psid, name='Тест')
    db.session.add(u)
    db.session.commit()
    Message.query.filter_by(facebook_user_id=u.id).delete()   # SQLite id reuse
    db.session.commit()
    return u


def _cleanup(uid):
    from extensions import db
    from models import FacebookUser, Message
    Message.query.filter_by(facebook_user_id=uid).delete()
    FacebookUser.query.filter_by(id=uid).delete()
    db.session.commit()


def _staff(uid):
    from models import Message
    return Message.query.filter_by(facebook_user_id=uid, sender='staff').all()


def test_staff_echo_saved_as_staff_message(client, db_session):
    u = _user('psid-staff-1')
    _post(client, _echo('psid-staff-1', {'mid': 'm.staff-1', 'text': 'Сайн байна уу, Давка байна'}))
    rows = _staff(u.id)
    assert [(r.content, r.mid) for r in rows] == [('Сайн байна уу, Давка байна', 'm.staff-1')]
    _cleanup(u.id)


def test_bot_echo_not_saved_again(client, db_session):
    from services import BOT_ECHO_TAG
    u = _user('psid-staff-bot')
    _post(client, _echo('psid-staff-bot', {'mid': 'm.bot-1', 'text': 'bot reply',
                                           'metadata': BOT_ECHO_TAG}))
    from models import Message
    assert Message.query.filter_by(facebook_user_id=u.id).count() == 0
    _cleanup(u.id)


def test_repeated_echo_saved_once(client, db_session):
    u = _user('psid-staff-dup')
    for _ in range(2):
        _post(client, _echo('psid-staff-dup', {'mid': 'm.staff-dup', 'text': 'Тийм ээ'}))
    assert len(_staff(u.id)) == 1
    _cleanup(u.id)


def test_attachment_only_echo_saved_with_placeholder(client, db_session):
    u = _user('psid-staff-img')
    _post(client, _echo('psid-staff-img', {'mid': 'm.staff-img',
                                           'attachments': [{'type': 'image'}]}))
    assert [r.content for r in _staff(u.id)] == ['[хавсралт]']
    _cleanup(u.id)


def test_echo_for_unknown_customer_saves_nothing(client, db_session):
    from models import FacebookUser, Message
    before = Message.query.filter_by(sender='staff').count()
    _post(client, _echo('psid-never-seen', {'mid': 'm.ghost', 'text': 'hi'}))
    assert Message.query.filter_by(sender='staff').count() == before
    assert FacebookUser.query.filter_by(facebook_id='psid-never-seen').first() is None


def test_staff_reply_reaches_the_bots_memory(app, db_session, monkeypatch):
    """The bot sees what staff said, as the Page's side of the chat."""
    from datetime import datetime, timedelta
    from extensions import db
    from models import Message
    import routes.webhook as wh
    u = _user('psid-staff-memory')
    t = datetime.utcnow() - timedelta(minutes=40)
    for i, (sender, text) in enumerate([('user', 'Үнэ хэд вэ?'),
                                        ('staff', 'Танхимын анги 880,000₮'),
                                        ('user', 'Хэзээ эхлэх вэ?')]):
        db.session.add(Message(facebook_user_id=u.id, sender=sender, content=text,
                               created_at=t + timedelta(minutes=i)))
    db.session.commit()
    seen = {}

    def _gen(message_text, conversation, **k):
        seen['conversation'] = conversation
        return 'Дараагийн анги 12-р сарын 1-нд.'
    monkeypatch.setattr(wh, 'generate_bot_response', _gen)
    monkeypatch.setattr(wh, 'send_facebook_message', lambda *a, **k: None)
    monkeypatch.setattr(wh, 'send_sender_action', lambda *a, **k: None)
    monkeypatch.setattr(wh, 'classify_user_topics', lambda *a, **k: None)
    monkeypatch.setattr(wh, 'refresh_user_summary', lambda *a, **k: None)
    wh.process_inbound_reply(u.id, 'Хэзээ эхлэх вэ?', 'active')
    assert {'role': 'assistant', 'content': 'Танхимын анги 880,000₮'} in seen['conversation']
    _cleanup(u.id)


def test_summary_labels_staff_lines(app, db_session, monkeypatch):
    from extensions import db
    from models import Message
    import services
    u = _user('psid-staff-summary')
    for sender, text in [('user', 'Сайн уу'), ('staff', 'Сайн байна уу, Давка байна'),
                         ('user', 'Үнэ хэд вэ?'), ('bot', 'Танхим 880,000₮'),
                         ('user', 'За баярлалаа')]:
        db.session.add(Message(facebook_user_id=u.id, sender=sender, content=text))
    db.session.commit()
    prompts = []

    def _create(**kw):
        prompts.append(kw['messages'][0]['content'])
        raise RuntimeError('stop here')          # summary is fail-soft
    monkeypatch.setattr(services.client.messages, 'create', _create)
    services.refresh_user_summary(u)
    assert prompts and 'Ажилтан: Сайн байна уу, Давка байна' in prompts[0]
    _cleanup(u.id)


@pytest.fixture
def staff_user(app, db_session, client):
    from extensions import db
    from models import User
    from werkzeug.security import generate_password_hash
    User.query.filter_by(username='pytest-staff-view').delete()
    db.session.commit()
    user = User(username='pytest-staff-view', password=generate_password_hash('x'),
                email='pytest-staff-view@example.com', role='registration_staff')
    db.session.add(user)
    db.session.commit()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
    yield user
    User.query.filter_by(id=user.id).delete()
    db.session.commit()


def test_admin_views_label_staff(client, staff_user):
    from extensions import db
    from models import Message
    u = _user('psid-staff-view')
    db.session.add(Message(facebook_user_id=u.id, sender='staff', content='Давка байна'))
    db.session.commit()
    conv = client.get(f'/admin/users/{u.id}/conversation').get_data(as_text=True)
    assert 'msg-row staff' in conv and 'Ажилтан ·' in conv
    logs = client.get('/admin/logs').get_data(as_text=True)
    assert '<option value="staff">' in logs
    _cleanup(u.id)
