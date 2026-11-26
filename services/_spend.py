"""Estimated Claude API spend per month, and a warning before the Console's
monthly spend limit stops the bot.

The limit set at console.anthropic.com → Settings → Limits is a hard stop:
once it is reached every customer reply fails until the 1st of the next month
(00:00 UTC) or until someone raises it. The bot went down that way on
2026-11-12 and again on 2026-11-25, and both times staff learned about it from
customers. This module prices every customer reply from its `usage` block,
keeps a running total per UTC month in GeneralSetting, and Telegram-pings
staff ONCE a month when the total crosses SPEND_ALERT_PERCENT (default 80) of
MONTHLY_BUDGET_USD — set that env var to the Console limit. With no budget set
it only tracks, and the dashboard shows the total.

It is an ESTIMATE. Only the customer-reply call is counted (the background
jobs run on Haiku and cost well under 1% of the bill), and anything else
billed to the same Anthropic organisation — local eval runs, scripts — never
passes through here. The Console's Usage page stays the source of truth.
"""
import logging
import os
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from extensions import db
from models import GeneralSetting

logger = logging.getLogger(__name__)

# USD per million tokens: (input, output, cache read), list prices 2026-11.
# Cache WRITES are billed at input x 1.25 (5-minute TTL) or x 2 (1-hour TTL).
# Haiku's cache-read price is taken as 10% of input, which errs high. A model
# not listed here is priced as Opus, so a typo in REPLY_MODEL never makes the
# estimate look cheaper than the bill.
PRICES = {
    'claude-opus-5-5': (4.00, 20.00, 0.20),
    'claude-sonnet-5-5': (2.00, 10.00, 0.10),
    'claude-haiku-5-5': (0.10, 0.50, 0.01),
}
_FALLBACK_PRICE = PRICES['claude-opus-5-5']
WRITE_MULTIPLIER_5M = 1.25
WRITE_MULTIPLIER_1H = 2.0

# One row per UTC month; the value is whole micro-dollars so concurrent
# replies can add to it with a single atomic SQL UPDATE.
SPEND_KEY_PREFIX = 'api_spend_micros_'
ALERT_KEY_PREFIX = 'api_spend_alerted_'

_ADD_SQL = text(
    f"UPDATE {GeneralSetting.__tablename__} "
    "SET value = CAST(value AS INTEGER) + :micros WHERE key = :key"
)


def _month(now=None):
    return (now or datetime.utcnow()).strftime('%Y-%m')


def _tokens(obj, attr):
    """A token count from a usage object, 0 when absent. Anything that is not
    a real int (a test double, None) counts as 0 rather than poisoning the sum."""
    value = getattr(obj, attr, None)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def estimate_cost_usd(model, usage):
    """Dollar cost of one Messages API call from its `usage` block."""
    price_in, price_out, price_read = PRICES.get(model, _FALLBACK_PRICE)
    creation = getattr(usage, 'cache_creation', None)
    written_5m = _tokens(creation, 'ephemeral_5m_input_tokens')
    written_1h = _tokens(creation, 'ephemeral_1h_input_tokens')
    if not (written_5m or written_1h):
        # No per-TTL breakdown: price the write as the cheaper 5-minute kind.
        written_5m = _tokens(usage, 'cache_creation_input_tokens')
    return (
        _tokens(usage, 'input_tokens') * price_in
        + _tokens(usage, 'output_tokens') * price_out
        + _tokens(usage, 'cache_read_input_tokens') * price_read
        + written_5m * price_in * WRITE_MULTIPLIER_5M
        + written_1h * price_in * WRITE_MULTIPLIER_1H
    ) / 1_000_000


def budget_usd():
    """MONTHLY_BUDGET_USD as a float; 0 (= alerting off) when unset or invalid."""
    raw = os.environ.get('MONTHLY_BUDGET_USD', '').strip()
    try:
        return max(0.0, float(raw)) if raw else 0.0
    except ValueError:
        logger.warning("MONTHLY_BUDGET_USD=%r is not a number; spend alert is off.", raw)
        return 0.0


def alert_percent():
    raw = os.environ.get('SPEND_ALERT_PERCENT', '').strip()
    try:
        return min(100, max(1, int(raw))) if raw else 80
    except ValueError:
        return 80


def month_spend_usd(now=None):
    """Estimated spend so far this UTC month, in dollars."""
    row = GeneralSetting.query.filter_by(key=SPEND_KEY_PREFIX + _month(now)).first()
    try:
        return int(row.value) / 1_000_000 if row and row.value else 0.0
    except (TypeError, ValueError):
        return 0.0


def _add_micros(key, micros):
    if db.session.execute(_ADD_SQL, {'micros': micros, 'key': key}).rowcount:
        db.session.commit()
        return
    # First reply of the month creates the row; if another worker created it a
    # moment earlier, the unique key rejects ours and the UPDATE applies.
    try:
        db.session.add(GeneralSetting(key=key, value=str(micros)))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        db.session.execute(_ADD_SQL, {'micros': micros, 'key': key})
        db.session.commit()


def _maybe_alert(now=None):
    budget = budget_usd()
    if not budget:
        return False
    percent = alert_percent()
    spent = month_spend_usd(now)
    if spent < budget * percent / 100:
        return False
    # Claim the month BEFORE sending: the unique key makes this INSERT the
    # lock, so replies crossing the line together page staff only once.
    try:
        db.session.add(GeneralSetting(key=ALERT_KEY_PREFIX + _month(now), value=f'{spent:.2f}'))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return False
    import services as _svc
    _svc.send_telegram_notification(
        f"💸 Claude API-ийн энэ сарын зардал (тооцоогоор) ${spent:.2f} болж, "
        f"сарын төсөв ${budget:.0f}-ийн {percent}%-д хүрлээ.\n\n"
        "Хязгаарт хүрмэгц бот сарын 1-ний өдөр хүртэл хэрэглэгчдэд хариулахаа "
        "болино. console.anthropic.com → Settings → Limits хуудсаар хязгаараа "
        "шалгах, эсвэл Usage хуудсаар бодит зардлаа харна уу."
    )
    return True


def record_spend(model, usage, now=None):
    """Add one reply's estimated cost to this month's total and warn staff
    once a month near the budget. Runs on the customer-reply path, so it
    never raises — a failure is logged and the reply goes out regardless."""
    try:
        micros = round(estimate_cost_usd(model, usage) * 1_000_000)
        if micros <= 0:
            return
        _add_micros(SPEND_KEY_PREFIX + _month(now), micros)
        _maybe_alert(now)
    except Exception as exc:
        logger.error("Spend tracking failed: %s", exc)
        try:
            db.session.rollback()
        except Exception:
            pass
