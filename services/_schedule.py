"""Bot working schedule: when staff answer Messenger and when the bot does.

One GeneralSetting row (`bot_schedule`, JSON) is the single source of truth
for staff hours. The bot replies whenever staff are NOT on duty (nights, the
lunch break, weekends), and the same hours drive what the bot promises a
customer about when staff will answer — so the two can never contradict.

    {"mode": "schedule" | "always_on" | "off",
     "days": [7 x {"staff": bool, "start": "HH:MM", "end": "HH:MM",
                   "break_start": "HH:MM" | "", "break_end": "HH:MM" | ""}],
     "step_in": {"enabled": bool, "minutes": int}}

`days[0]` is Monday (datetime.weekday()). All times are Ulaanbaatar local
(UTC+8, no DST); shifts and breaks are start-inclusive, end-exclusive.
`step_in` is the safety net: during staff hours, a customer left unanswered
for `minutes` gets a bot reply (see routes/webhook.py:step_in_sweep).

Pure functions take the schedule and a UB-local `at` so they are testable
without a clock or a database; only get_bot_schedule touches the DB.
"""
import copy
import json
import logging
import re
from datetime import datetime, timedelta

from models import GeneralSetting

logger = logging.getLogger(__name__)

SCHEDULE_KEY = 'bot_schedule'
MODES = ('schedule', 'always_on', 'off')
STEP_IN_MIN, STEP_IN_MAX = 1, 240
UB_OFFSET = timedelta(hours=8)

DAY_NAMES = ('Даваа', 'Мягмар', 'Лхагва', 'Пүрэв', 'Баасан', 'Бямба', 'Ням')

_WORKDAY = {'staff': True, 'start': '09:00', 'end': '18:00',
            'break_start': '13:00', 'break_end': '14:00'}
_DAY_OFF = {'staff': False, 'start': '09:00', 'end': '18:00',
            'break_start': '13:00', 'break_end': '14:00'}

# Gundeg's schedule (2026-11-16): staff Mon–Fri 09–13 + 14–18, the bot
# covers the rest; step-in after 15 unanswered minutes.
DEFAULT_SCHEDULE = {
    'mode': 'schedule',
    'days': [dict(_WORKDAY) for _ in range(5)] + [dict(_DAY_OFF) for _ in range(2)],
    'step_in': {'enabled': True, 'minutes': 15},
}

_TIME_RE = re.compile(r'^(\d{1,2}):(\d{2})$')


def _utcnow():
    return datetime.utcnow()


def ub_now():
    """Current Ulaanbaatar wall-clock time as a naive datetime."""
    return _utcnow() + UB_OFFSET


def _minutes(hhmm):
    """'HH:MM' → minutes since midnight, or None if malformed/out of range."""
    m = _TIME_RE.match((hhmm or '').strip()) if isinstance(hhmm, str) else None
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return h * 60 + mi


def _fmt(minutes):
    return f'{minutes // 60:02d}:{minutes % 60:02d}'


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ('on', 'true', '1', 'yes')
    return bool(value)


def validate_schedule(data):
    """Check and normalise an admin-submitted schedule.

    Returns (clean, None) or (None, Mongolian error message). `clean` is a new
    dict with all 7 days, times as zero-padded HH:MM and minutes as int."""
    if not isinstance(data, dict):
        return None, 'Хуваарийн өгөгдөл буруу байна.'
    mode = data.get('mode')
    if mode not in MODES:
        return None, 'Ажиллах горим буруу байна.'
    days = data.get('days')
    if not isinstance(days, list) or len(days) != 7:
        return None, '7 хоногийн 7 өдрийн хуваарь шаардлагатай.'

    clean_days = []
    for name, day in zip(DAY_NAMES, days):
        if not isinstance(day, dict):
            return None, f'{name}: хуваарь буруу байна.'
        staff = _truthy(day.get('staff'))
        start, end = _minutes(day.get('start')), _minutes(day.get('end'))
        raw_bs, raw_be = (day.get('break_start') or ''), (day.get('break_end') or '')
        bs, be = _minutes(raw_bs), _minutes(raw_be)
        has_break = bool(str(raw_bs).strip() or str(raw_be).strip())
        if staff:
            if start is None or end is None:
                return None, f'{name}: ажлын цагийг ЦЦ:ММ хэлбэрээр оруулна уу.'
            if start >= end:
                return None, f'{name}: ажил дуусах цаг эхлэхээс хойш байх ёстой.'
            if has_break:
                if bs is None or be is None:
                    return None, f'{name}: цайны цагийн эхлэл, төгсгөлийг хоёуланг нь оруулна уу.'
                if not (start <= bs < be <= end):
                    return None, f'{name}: цайны цаг ажлын цагийн дотор, эхлэл нь төгсгөлөөсөө өмнө байх ёстой.'
        clean_days.append({
            'staff': staff,
            'start': _fmt(start) if start is not None else '',
            'end': _fmt(end) if end is not None else '',
            'break_start': _fmt(bs) if (has_break and bs is not None) else '',
            'break_end': _fmt(be) if (has_break and be is not None) else '',
        })

    step_in = data.get('step_in') or {}
    if not isinstance(step_in, dict):
        return None, 'Ажилтны оронд хариулах тохиргоо буруу байна.'
    try:
        minutes = int(step_in.get('minutes', DEFAULT_SCHEDULE['step_in']['minutes']))
    except (TypeError, ValueError):
        minutes = None
    if minutes is None or not (STEP_IN_MIN <= minutes <= STEP_IN_MAX):
        return None, f'Хүлээх хугацаа {STEP_IN_MIN}–{STEP_IN_MAX} минутын хооронд байна.'

    return {
        'mode': mode,
        'days': clean_days,
        'step_in': {'enabled': _truthy(step_in.get('enabled')), 'minutes': minutes},
    }, None


def get_bot_schedule():
    """The saved schedule, or the defaults when none is saved or the row is
    unreadable — a bad row must never break the webhook."""
    row = GeneralSetting.query.filter_by(key=SCHEDULE_KEY).first()
    if row and (row.value or '').strip():
        try:
            clean, error = validate_schedule(json.loads(row.value))
        except (TypeError, ValueError):
            clean, error = None, 'not JSON'
        if clean:
            return clean
        logger.warning('bot_schedule row ignored (%s); using defaults', error)
    return copy.deepcopy(DEFAULT_SCHEDULE)


def _segments(day):
    """On-duty (start, end) minute ranges for one day; [] on a day off."""
    if not day.get('staff'):
        return []
    start, end = _minutes(day.get('start')), _minutes(day.get('end'))
    if start is None or end is None or start >= end:
        return []
    bs, be = _minutes(day.get('break_start')), _minutes(day.get('break_end'))
    if bs is not None and be is not None and start <= bs < be <= end:
        return [(s, e) for s, e in ((start, bs), (be, end)) if s < e]
    return [(start, end)]


def staff_on_duty(schedule, at):
    t = at.hour * 60 + at.minute
    return any(s <= t < e for s, e in _segments(schedule['days'][at.weekday()]))


def bot_should_reply(at=None, schedule=None):
    schedule = schedule or get_bot_schedule()
    if schedule['mode'] == 'always_on':
        return True
    if schedule['mode'] == 'off':
        return False
    return not staff_on_duty(schedule, at or ub_now())


def next_staff_start(schedule, at):
    """The earliest moment >= `at` when staff are on duty (`at` itself if they
    are on duty now), or None when no day has staff."""
    t = at.hour * 60 + at.minute
    for offset in range(8):
        day_start = (at + timedelta(days=offset)).replace(hour=0, minute=0, second=0, microsecond=0)
        for s, e in _segments(schedule['days'][day_start.weekday()]):
            if offset == 0 and s <= t < e:
                return at
            if offset > 0 or s >= t:
                return day_start + timedelta(minutes=s)
    return None


ALWAYS_ON_STAFF_ETA = 'боломжтой болмогц'


def staff_available(schedule, at):
    """Are staff actually there to answer? 'always_on' is the holiday /
    staff-meeting mode (the bot covers everything), so never; otherwise the
    weekly staff hours decide."""
    if schedule['mode'] == 'always_on':
        return False
    return staff_on_duty(schedule, at)


def describe_next_staff_time(at=None, schedule=None):
    """'өнөөдөр 14:00 цагаас' / 'маргааш 09:00 цагаас' / 'Даваа гарагт 09:00
    цагаас' — for telling an off-hours customer when staff will answer. In
    'always_on' mode (a holiday or meeting) no time can be promised:
    'боломжтой болмогц'."""
    schedule = schedule or get_bot_schedule()
    if schedule['mode'] == 'always_on':
        return ALWAYS_ON_STAFF_ETA
    at = at or ub_now()
    nxt = next_staff_start(schedule, at)
    if nxt is None:
        return ''
    if nxt == at:
        return 'одоо'
    clock = nxt.strftime('%H:%M')
    days_ahead = (nxt.date() - at.date()).days
    if days_ahead == 0:
        return f'өнөөдөр {clock} цагаас'
    if days_ahead == 1:
        return f'маргааш {clock} цагаас'
    return f'{DAY_NAMES[nxt.weekday()]} гарагт {clock} цагаас'


def _day_text(day):
    segs = _segments(day)
    if not segs:
        return 'амралтын өдөр'
    text = f"{_fmt(segs[0][0])}–{_fmt(segs[-1][1])}"
    if len(segs) == 2:
        text += f" (цайны цаг {_fmt(segs[0][1])}–{_fmt(segs[1][0])})"
    return text


def describe_staff_hours(schedule):
    """Staff hours in one line, consecutive identical days grouped:
    'Даваа–Баасан 09:00–18:00 (цайны цаг 13:00–14:00); Бямба, Ням — амралтын өдөр'."""
    groups = []   # [first_index, last_index, text]
    for i, day in enumerate(schedule['days']):
        text = _day_text(day)
        if groups and groups[-1][2] == text:
            groups[-1][1] = i
        else:
            groups.append([i, i, text])
    parts = []
    for first, last, text in groups:
        if first == last:
            label = DAY_NAMES[first]
        elif last == first + 1:
            label = f'{DAY_NAMES[first]}, {DAY_NAMES[last]}'
        else:
            label = f'{DAY_NAMES[first]}–{DAY_NAMES[last]}'
        sep = ' — ' if text == 'амралтын өдөр' else ' '
        parts.append(f'{label}{sep}{text}')
    return '; '.join(parts)


def bot_status(at=None, schedule=None):
    """The bot's state right now, for the admin UI: {'replying', 'state',
    'label'} — state is 'off' | 'staff' (staff hours) | 'bot'."""
    schedule = schedule or get_bot_schedule()
    at = at or ub_now()
    if schedule['mode'] == 'off':
        return {'replying': False, 'state': 'off',
                'label': 'Бот унтарсан — хэрэглэгчид хариулахгүй'}
    if schedule['mode'] == 'always_on':
        return {'replying': True, 'state': 'bot', 'label': 'Бот үргэлж хариулж байна'}
    if staff_on_duty(schedule, at):
        t = at.hour * 60 + at.minute
        until = next(e for s, e in _segments(schedule['days'][at.weekday()]) if s <= t < e)
        label = f'Ажилтны цаг — бот хариулахгүй ({_fmt(until)} хүртэл)'
        step_in = schedule['step_in']
        if step_in['enabled']:
            label += f"; {step_in['minutes']} минутад ажилтан хариулаагүй бол бот хариулна"
        return {'replying': False, 'state': 'staff', 'label': label}
    when = describe_next_staff_time(at, schedule)
    label = 'Бот хариулж байна'
    if when:
        label += f' — ажилтан {when} ажиллана'
    return {'replying': True, 'state': 'bot', 'label': label}
