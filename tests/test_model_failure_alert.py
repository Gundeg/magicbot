"""Claude API account-failure alerting (credit exhausted / invalid key).

When the Anthropic account dies (401 auth, 403 permission, 400 "credit
balance is too low"), EVERY customer reply degrades to the canned fallback
AND the knowledge-gap handoff dies with it — no Work Tasks entry, no
Telegram ping. A real outage on the previous provider (2026-06-11) ran 6+
hours unnoticed because of exactly this.

These tests pin the alerting contract:
- account-level failure -> staff get ONE Telegram alert per cooldown window
- the customer still receives the fallback reply, alert or no alert
- transient failures (429 rate limit, 529 overloaded) do NOT alert
- a broken alert path can never break the customer reply
"""
from datetime import datetime, timedelta

import httpx2 as httpx
import pytest
from anthropic import (
    AuthenticationError, BadRequestError, OverloadedError, PermissionDeniedError,
    RateLimitError,
)

import services
from extensions import db
from models import GeneralSetting

FALLBACK = services.TRANSIENT_ERROR_REPLY
ALERT_STATE_KEY = services.MODEL_ALERT_STATE_KEY


def _api_error(cls, status, message, error_type):
    req = httpx.Request('POST', 'https://api.anthropic.com/v1/messages')
    body = {'type': 'error', 'error': {'type': error_type, 'message': message}}
    return cls(message, response=httpx.Response(status, request=req), body=body)


def _credit_error():
    # Anthropic reports an empty prepaid balance as a 400, not a 429.
    return _api_error(
        BadRequestError, 400,
        'Your credit balance is too low to access the Anthropic API. '
        'Please go to Plans & Billing to upgrade or purchase credits.',
        'invalid_request_error',
    )


def _auth_error():
    return _api_error(AuthenticationError, 401, 'invalid x-api-key', 'authentication_error')


def _permission_error():
    return _api_error(PermissionDeniedError, 403,
                      'Your API key does not have permission to use the specified resource.',
                      'permission_error')


def _transient_rate_limit():
    return _api_error(RateLimitError, 429,
                      "This request would exceed your organization's rate limit of "
                      "50 requests per minute.", 'rate_limit_error')


def _overloaded():
    return _api_error(OverloadedError, 529, 'Overloaded', 'overloaded_error')


def _raise_on_create(monkeypatch, exc):
    def _boom(*args, **kwargs):
        raise exc
    monkeypatch.setattr(services.client.messages, 'create', _boom)


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


def test_credit_error_returns_fallback_and_alerts_once(app, db_session, monkeypatch,
                                                       alerts, clean_alert_state):
    _raise_on_create(monkeypatch, _credit_error())
    reply = services.generate_bot_response('Hi', [])
    assert reply == FALLBACK            # customer experience unchanged
    assert len(alerts) == 1             # staff got exactly one Telegram ping
    assert 'Claude' in alerts[0]        # names the provider + console to fix


def test_auth_error_alerts(app, db_session, monkeypatch, alerts, clean_alert_state):
    """An invalid/revoked API key is just as account-fatal as no credit."""
    _raise_on_create(monkeypatch, _auth_error())
    reply = services.generate_bot_response('Hi', [])
    assert reply == FALLBACK
    assert len(alerts) == 1


def test_permission_error_alerts(app, db_session, monkeypatch, alerts, clean_alert_state):
    _raise_on_create(monkeypatch, _permission_error())
    reply = services.generate_bot_response('Hi', [])
    assert reply == FALLBACK
    assert len(alerts) == 1


def test_no_duplicate_alert_within_cooldown(app, db_session, monkeypatch,
                                            alerts, clean_alert_state):
    """A credit outage fails EVERY reply — the second failing reply inside
    the cooldown window must not produce a second alert."""
    _raise_on_create(monkeypatch, _credit_error())
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
    _raise_on_create(monkeypatch, _credit_error())
    services.generate_bot_response('Hi', [])
    assert len(alerts) == 1


def test_transient_rate_limit_does_not_alert(app, db_session, monkeypatch,
                                             alerts, clean_alert_state):
    """A 429 rate limit is self-healing — alerting on it would train staff
    to ignore the alert. Only account-level errors ping."""
    _raise_on_create(monkeypatch, _transient_rate_limit())
    reply = services.generate_bot_response('Hi', [])
    assert reply == FALLBACK
    assert alerts == []


def test_overloaded_does_not_alert(app, db_session, monkeypatch, alerts, clean_alert_state):
    """529 overloaded is Anthropic-side and transient."""
    _raise_on_create(monkeypatch, _overloaded())
    reply = services.generate_bot_response('Hi', [])
    assert reply == FALLBACK
    assert alerts == []


def test_alert_failure_never_breaks_customer_reply(app, db_session, monkeypatch,
                                                   clean_alert_state):
    """The alert path runs on the customer-reply path — if Telegram (or the
    dedupe bookkeeping) blows up, the customer must still get the fallback."""
    _raise_on_create(monkeypatch, _credit_error())

    def _telegram_down(text):
        raise RuntimeError('telegram down')
    monkeypatch.setattr(services, 'send_telegram_notification', _telegram_down)

    reply = services.generate_bot_response('Hi', [])
    assert reply == FALLBACK
