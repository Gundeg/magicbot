"""Estimated Claude spend + the warning before the Console spend limit.

The Console's monthly limit is a hard stop: when it is reached every customer
reply fails until the 1st of the next month. It stopped the bot on 2026-11-12
and again on 2026-11-25 with no warning. These tests pin the contract:
- a reply's cost is priced from its usage block (cache writes included)
- the month total accumulates, one row per UTC month
- staff get ONE Telegram warning a month once the total crosses the
  threshold of MONTHLY_BUDGET_USD, none when no budget is set
- tracking can never break the customer reply
- admins see the month's spend on the dashboard, registration staff don't
"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import services
from extensions import db
from models import GeneralSetting
from services import _spend

NOV = datetime(2026, 11, 26, 12, 0)
DEC = datetime(2026, 12, 1, 0, 30)


def _usage(inp=0, out=0, read=0, write=0, write_1h=None):
    creation = None
    if write_1h is not None:
        creation = SimpleNamespace(ephemeral_5m_input_tokens=0, ephemeral_1h_input_tokens=write_1h)
    return SimpleNamespace(input_tokens=inp, output_tokens=out, cache_read_input_tokens=read,
                           cache_creation_input_tokens=write if write_1h is None else write_1h,
                           cache_creation=creation)


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(services, 'send_telegram_notification', lambda text: sent.append(text) or 1)
    return sent


@pytest.fixture
def clean_spend(app, db_session, monkeypatch):
    monkeypatch.delenv('MONTHLY_BUDGET_USD', raising=False)
    monkeypatch.delenv('SPEND_ALERT_PERCENT', raising=False)

    def _wipe():
        GeneralSetting.query.filter(GeneralSetting.key.like('api_spend_%')).delete(
            synchronize_session=False)
        db.session.commit()
    _wipe()
    yield
    _wipe()


# ------------------------------------------------------------------ pricing

def test_opus_cache_miss_is_priced_with_the_write_premium():
    # The measured 2026-11-25 average miss: the 26.6K-token stable half is
    # written at 1.25x input, plus the uncached tail and the reply.
    cost = _spend.estimate_cost_usd('claude-opus-5-5', _usage(inp=2700, out=317, write=26600))
    assert cost == pytest.approx((2700 * 4 + 317 * 20 + 26600 * 4 * 1.25) / 1e6)
    assert 0.14 < cost < 0.16


def test_cache_hit_is_an_order_of_magnitude_cheaper_than_a_miss():
    hit = _spend.estimate_cost_usd('claude-opus-5-5', _usage(inp=2700, out=317, read=26600))
    miss = _spend.estimate_cost_usd('claude-opus-5-5', _usage(inp=2700, out=317, write=26600))
    assert miss > 5 * hit


def test_one_hour_writes_cost_twice_the_input_price():
    cost = _spend.estimate_cost_usd('claude-sonnet-5-5', _usage(write_1h=10000))
    assert cost == pytest.approx(10000 * 2.00 * 2 / 1e6)


def test_unknown_model_is_priced_as_opus_so_the_estimate_never_runs_low():
    usage = _usage(inp=1000, out=100)
    assert (_spend.estimate_cost_usd('claude-typo-model', usage)
            == _spend.estimate_cost_usd('claude-opus-5-5', usage))


def test_a_usage_object_without_real_ints_costs_nothing():
    assert _spend.estimate_cost_usd('claude-opus-5-5', MagicMock()) == 0


# ------------------------------------------------------------- accumulation

def test_spend_accumulates_per_utc_month(clean_spend, alerts):
    _spend.record_spend('claude-opus-5-5', _usage(inp=1_000_000), now=NOV)   # $4.00
    _spend.record_spend('claude-opus-5-5', _usage(out=50_000), now=NOV)      # $1.00
    _spend.record_spend('claude-opus-5-5', _usage(inp=250_000), now=DEC)     # $1.00
    assert _spend.month_spend_usd(NOV) == pytest.approx(5.00)
    assert _spend.month_spend_usd(DEC) == pytest.approx(1.00)


def test_reply_records_its_spend(clean_spend, alerts, monkeypatch):
    response = SimpleNamespace(content=[SimpleNamespace(type='text', text='Сайн байна уу!')],
                               stop_reason='end_turn',
                               usage=_usage(inp=2000, out=200, read=26000))
    monkeypatch.setattr(services.client.messages, 'create', lambda **kw: response)
    assert services.generate_bot_response('Сайн уу', []) == 'Сайн байна уу!'
    expected = _spend.estimate_cost_usd(services.REPLY_MODEL, response.usage)
    assert _spend.month_spend_usd() == pytest.approx(expected, abs=1e-6)


def test_tracking_failure_never_breaks_the_reply(clean_spend, alerts, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError('database is locked')
    monkeypatch.setattr(_spend, '_add_micros', _boom)
    response = SimpleNamespace(content=[SimpleNamespace(type='text', text='За')],
                               stop_reason='end_turn', usage=_usage(inp=1000))
    monkeypatch.setattr(services.client.messages, 'create', lambda **kw: response)
    assert services.generate_bot_response('Сайн уу', []) == 'За'


# ---------------------------------------------------------------- alerting

def test_no_budget_means_no_alert(clean_spend, alerts):
    _spend.record_spend('claude-opus-5-5', _usage(inp=25_000_000), now=NOV)  # $100
    assert alerts == []


def test_crossing_the_threshold_alerts_once_a_month(clean_spend, alerts, monkeypatch):
    monkeypatch.setenv('MONTHLY_BUDGET_USD', '100')
    _spend.record_spend('claude-opus-5-5', _usage(inp=19_000_000), now=NOV)  # $76 < 80%
    assert alerts == []
    _spend.record_spend('claude-opus-5-5', _usage(inp=1_000_000), now=NOV)   # $80 -> alert
    _spend.record_spend('claude-opus-5-5', _usage(inp=2_000_000), now=NOV)   # $88, already sent
    assert len(alerts) == 1
    assert '$80.00' in alerts[0] and 'Limits' in alerts[0]
    _spend.record_spend('claude-opus-5-5', _usage(inp=21_000_000), now=DEC)  # new month, $84
    assert len(alerts) == 2


def test_alert_percent_is_configurable(clean_spend, alerts, monkeypatch):
    monkeypatch.setenv('MONTHLY_BUDGET_USD', '100')
    monkeypatch.setenv('SPEND_ALERT_PERCENT', '50')
    _spend.record_spend('claude-opus-5-5', _usage(inp=12_500_000), now=NOV)  # $50
    assert len(alerts) == 1


def test_invalid_budget_turns_alerting_off(clean_spend, alerts, monkeypatch):
    monkeypatch.setenv('MONTHLY_BUDGET_USD', 'зуу')
    _spend.record_spend('claude-opus-5-5', _usage(inp=25_000_000), now=NOV)
    assert alerts == []


# ---------------------------------------------------------------- dashboard

def _login_as(client, username, role):
    from models import User
    from werkzeug.security import generate_password_hash
    User.query.filter_by(username=username).delete()
    db.session.commit()
    user = User(username=username, password=generate_password_hash('x'),
                email=f'{username}@example.com', role=role)
    db.session.add(user)
    db.session.commit()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
    return user


def _drop(user):
    from models import User
    User.query.filter_by(id=user.id).delete()
    db.session.commit()


def test_admin_sees_month_spend_on_dashboard(client, clean_spend, alerts, monkeypatch):
    monkeypatch.setenv('MONTHLY_BUDGET_USD', '120')
    _spend.record_spend('claude-opus-5-5', _usage(inp=3_000_000))  # $12 this month
    admin = _login_as(client, 'pytest-spend-admin', 'admin')
    html = client.get('/admin/dashboard').get_data(as_text=True)
    assert 'Claude API зардал' in html
    assert '$12.00' in html and '$120 төсөв' in html
    _drop(admin)


def test_registration_staff_do_not_see_spend(client, clean_spend, alerts):
    staff = _login_as(client, 'pytest-spend-staff', 'registration_staff')
    html = client.get('/admin/dashboard').get_data(as_text=True)
    assert 'Claude API зардал' not in html
    _drop(staff)
