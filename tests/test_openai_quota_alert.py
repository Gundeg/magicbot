"""OpenAI account-failure alerting (quota exhausted / invalid key).

When the OpenAI account dies (429 insufficient_quota, 401 auth), EVERY
customer reply degrades to the canned fallback AND the knowledge-gap
handoff dies with it — no Work Tasks entry, no Telegram ping. A real
outage on 2026-06-11 ran 6+ hours unnoticed because of exactly this.

These tests pin the alerting contract:
- account-level failure -> staff get ONE Telegram alert per cooldown window
- the customer still receives the fallback reply, alert or no alert
- transient per-model rate limits do NOT alert (only account-level errors)
- a broken alert path can never break the customer reply
"""
from datetime import datetime, timedelta

import httpx
import pytest
from openai import AuthenticationError, RateLimitError

import services
from extensions import db
from models import GeneralSetting

FALLBACK = "Уучлаарай, түр зуурын саатал гарсан байна. Хэдхэн минутын дараа дахин бичээрэй."
ALERT_STATE_KEY = 'openai_failure_alerted_at'


def _api_error(cls, status, body):
    req = httpx.Request('POST', 'https://api.openai.com/v1/chat/completions')
    return cls('boom', response=httpx.Response(status, request=req), body=body)


def _quota_error():
    return _api_error(RateLimitError, 429,
                      {'code': 'insufficient_quota', 'type': 'insufficient_quota'})


def _transient_rate_limit():
    return _api_error(RateLimitError, 429,
                      {'code': 'rate_limit_exceeded', 'type': 'requests'})


def _auth_error():
    return _api_error(AuthenticationError, 401, {'code': 'invalid_api_key'})


def _raise_on_create(monkeypatch, exc):
    def _boom(*args, **kwargs):
        raise exc
    monkeypatch.setattr(services.client.chat.completions, 'create', _boom)


@pytest.fixture
def alerts(monkeypatch):
    """Capture outgoing Telegram alerts instead of hitting the network."""
    sent = []
    monkeypatch.setattr(services, 'send_telegram_notification',
                        lambda text: sent.append(text) or 1)
    return sent


@pytest.fixture
def clean_alert_state(app, db_session):
    """Each test starts and ends without a recorded last-alert timestamp."""
    GeneralSetting.query.filter_by(key=ALERT_STATE_KEY).delete()
    db.session.commit()
    yield
    GeneralSetting.query.filter_by(key=ALERT_STATE_KEY).delete()
    db.session.commit()


def test_quota_error_returns_fallback_and_alerts_once(app, db_session, monkeypatch,
                                                      alerts, clean_alert_state):
    _raise_on_create(monkeypatch, _quota_error())
    reply = services.generate_bot_response('Hi', [])
    assert reply == FALLBACK            # customer experience unchanged
    assert len(alerts) == 1             # staff got exactly one Telegram ping
    assert 'OpenAI' in alerts[0]


def test_no_duplicate_alert_within_cooldown(app, db_session, monkeypatch,
                                            alerts, clean_alert_state):
    """A quota outage fails EVERY reply — the second failing reply inside
    the cooldown window must not produce a second alert."""
    _raise_on_create(monkeypatch, _quota_error())
    services.generate_bot_response('Hi', [])
    reply = services.generate_bot_response('Anyone there?', [])
    assert reply == FALLBACK
    assert len(alerts) == 1


def test_alert_fires_again_after_cooldown(app, db_session, monkeypatch,
                                          alerts, clean_alert_state):
    """Default cooldown is 6h — a timestamp 7h old no longer suppresses."""
    db.session.add(GeneralSetting(
        key=ALERT_STATE_KEY,
        value=(datetime.utcnow() - timedelta(hours=7)).isoformat(),
    ))
    db.session.commit()
    _raise_on_create(monkeypatch, _quota_error())
    services.generate_bot_response('Hi', [])
    assert len(alerts) == 1


def test_transient_rate_limit_does_not_alert(app, db_session, monkeypatch,
                                             alerts, clean_alert_state):
    """A per-model 429 (rate_limit_exceeded) is self-healing — alerting on
    it would train staff to ignore the alert. Only account-level errors ping."""
    _raise_on_create(monkeypatch, _transient_rate_limit())
    reply = services.generate_bot_response('Hi', [])
    assert reply == FALLBACK
    assert alerts == []


def test_auth_error_alerts(app, db_session, monkeypatch, alerts, clean_alert_state):
    """An invalid/revoked API key is just as account-fatal as no quota."""
    _raise_on_create(monkeypatch, _auth_error())
    reply = services.generate_bot_response('Hi', [])
    assert reply == FALLBACK
    assert len(alerts) == 1


def test_alert_failure_never_breaks_customer_reply(app, db_session, monkeypatch,
                                                   clean_alert_state):
    """The alert path runs on the customer-reply path — if Telegram (or the
    dedupe bookkeeping) blows up, the customer must still get the fallback."""
    _raise_on_create(monkeypatch, _quota_error())

    def _telegram_down(text):
        raise RuntimeError('telegram down')
    monkeypatch.setattr(services, 'send_telegram_notification', _telegram_down)

    reply = services.generate_bot_response('Hi', [])
    assert reply == FALLBACK
