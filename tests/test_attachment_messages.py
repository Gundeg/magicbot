"""A customer message with only a photo / sticker / file (no text) used to hit
the NOT NULL `content` column, the IntegrityError was taken for a Facebook
retry ("Duplicate Facebook delivery dropped"), and the customer got no reply.
"""
import hashlib
import hmac
import json

import pytest


def _sign(body):
    from services import FACEBOOK_APP_SECRET
    return hmac.new(FACEBOOK_APP_SECRET.encode('utf-8'), body, hashlib.sha256).hexdigest()


def _post(client, psid, message):
    payload = {'object': 'page', 'entry': [{'messaging': [{
        'sender': {'id': psid}, 'recipient': {'id': 'PAGE_ID'}, 'message': message}]}]}
    body = json.dumps(payload).encode('utf-8')
    return client.post('/webhook', data=body, headers={
        'X-Hub-Signature-256': f'sha256={_sign(body)}', 'Content-Type': 'application/json'})


@pytest.fixture
def customer(app, db_session):
    from extensions import db
    from models import FacebookUser, Message
    FacebookUser.query.filter_by(facebook_id='psid-attach').delete()
    db.session.commit()
    u = FacebookUser(facebook_id='psid-attach', name='Тест')
    db.session.add(u)
    db.session.commit()
    Message.query.filter_by(facebook_user_id=u.id).delete()   # SQLite id reuse
    db.session.commit()
    yield u
    Message.query.filter_by(facebook_user_id=u.id).delete()
    FacebookUser.query.filter_by(id=u.id).delete()
    db.session.commit()


@pytest.fixture
def replies(monkeypatch):
    import routes.webhook as wh
    seen = {'gen': [], 'sent': []}

    def _gen(message_text, conversation, **k):
        seen['gen'].append(message_text)
        return 'Зургийг тань хүлээн авлаа.'
    monkeypatch.setattr(wh, 'generate_bot_response', _gen)
    monkeypatch.setattr(wh, 'send_facebook_message', lambda *a, **k: seen['sent'].append(a))
    monkeypatch.setattr(wh, 'send_sender_action', lambda *a, **k: None)
    monkeypatch.setattr(wh, 'classify_user_topics', lambda *a, **k: None)
    monkeypatch.setattr(wh, 'refresh_user_summary', lambda *a, **k: None)
    monkeypatch.setattr(wh, 'check_rate_limit', lambda sid: True)
    return seen


@pytest.mark.parametrize('message, expected', [
    ({'mid': 'm.att-img', 'attachments': [{'type': 'image'}]}, '[Зураг илгээсэн]'),
    ({'mid': 'm.att-sticker', 'sticker_id': 369239263222822,
      'attachments': [{'type': 'image'}]}, '[Стикер илгээсэн]'),
    ({'mid': 'm.att-file', 'attachments': [{'type': 'file'}]}, '[Файл илгээсэн]'),
    ({'mid': 'm.att-audio', 'attachments': [{'type': 'audio'}]}, '[Дуут мессеж илгээсэн]'),
])
def test_attachment_only_message_is_saved_and_answered(client, customer, replies, message, expected):
    from models import Message
    _post(client, 'psid-attach', message)
    saved = Message.query.filter_by(facebook_user_id=customer.id, sender='user').all()
    assert [m.content for m in saved] == [expected]
    assert replies['gen'] == [expected]          # the bot knows what arrived
    assert len(replies['sent']) == 1             # and the customer gets a reply


def test_attachment_does_not_become_a_name(client, customer, replies):
    """The name-capture path must not read a placeholder as the customer's name."""
    from extensions import db
    from models import FacebookUser, Message
    customer.name = 'Unknown'
    db.session.add(Message(facebook_user_id=customer.id, sender='bot',
                           content='Таныг хэн гэдэг вэ? Нэрээ хэлээрэй.'))
    db.session.commit()
    _post(client, 'psid-attach', {'mid': 'm.att-name', 'attachments': [{'type': 'image'}]})
    db.session.expire_all()
    assert db.session.get(FacebookUser, customer.id).name == 'Unknown'


def test_real_duplicate_still_dropped_once(client, customer, replies):
    from models import Message
    for _ in range(2):
        _post(client, 'psid-attach', {'mid': 'm.att-dup', 'attachments': [{'type': 'image'}]})
    assert Message.query.filter_by(facebook_user_id=customer.id, sender='user').count() == 1
    assert len(replies['sent']) == 1
