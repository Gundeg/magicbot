"""Bot schedule in the admin panel: Ботын тохиргоо saves a validated schedule
(nothing at all on a bad one), and the dashboard tells staff whether the bot
is answering right now."""
import copy
import json
from datetime import datetime

import pytest

from services import _schedule as sch


@pytest.fixture
def admin(app, db_session, client):
    from extensions import db
    from models import User
    from werkzeug.security import generate_password_hash
    User.query.filter_by(username='pytest-schedule-admin').delete()
    db.session.commit()
    user = User(username='pytest-schedule-admin', password=generate_password_hash('x'),
                email='pytest-schedule-admin@example.com', role='super_admin')
    db.session.add(user)
    db.session.commit()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
    yield user
    User.query.filter_by(id=user.id).delete()
    db.session.commit()


def _row(key):
    from models import GeneralSetting
    return GeneralSetting.query.filter_by(key=key).first()


def _payload(**overrides):
    s = copy.deepcopy(sch.DEFAULT_SCHEDULE)
    s['mode'] = 'schedule'
    s.update(overrides)
    return s


def test_save_valid_schedule(client, admin):
    s = _payload(step_in={'enabled': True, 'minutes': '20'})
    s['days'][4]['end'] = '17:00'                          # short Friday
    resp = client.post('/bot-management/settings', json={'bot_schedule': s})
    assert resp.status_code == 200 and resp.get_json()['success'] is True
    stored = json.loads(_row(sch.SCHEDULE_KEY).value)
    assert stored['days'][4]['end'] == '17:00'
    assert stored['step_in'] == {'enabled': True, 'minutes': 20}


def test_save_invalid_schedule_400_nothing_saved(client, admin):
    from extensions import db
    from models import GeneralSetting
    GeneralSetting.query.filter_by(key='telegram_chat_ids').delete()
    db.session.commit()
    s = _payload()
    s['days'][0].update(start='18:00', end='09:00')
    resp = client.post('/bot-management/settings',
                       json={'bot_schedule': s, 'telegram_chat_ids': '123'})
    assert resp.status_code == 400
    body = resp.get_json()
    assert body['success'] is False and 'Даваа' in body['error']
    assert _row(sch.SCHEDULE_KEY) is None
    assert _row('telegram_chat_ids') is None               # nothing half-saved


def test_settings_page_renders_schedule_card(client, admin):
    html = client.get('/bot-management/settings').get_data(as_text=True)
    assert 'Ботын ажлын цаг' in html
    assert html.count('data-day=') == 7
    for name in sch.DAY_NAMES:
        assert name in html
    assert 'office_hours_start' not in html


def test_dashboard_shows_bot_status(client, admin, monkeypatch):
    from extensions import db
    from models import GeneralSetting
    monkeypatch.setattr(sch, '_utcnow', lambda: datetime(2026, 11, 16, 10, 0) - sch.UB_OFFSET)
    db.session.add(GeneralSetting(key=sch.SCHEDULE_KEY, value=json.dumps(_payload())))
    db.session.commit()
    html = client.get('/admin/dashboard').get_data(as_text=True)
    assert sch.bot_status()['label'] in html
    assert 'Ажилтны цаг — бот хариулахгүй (13:00 хүртэл)' in html
