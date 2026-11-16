"""Bot working schedule — pure schedule math, validation and wording.

Gundeg's schedule (2026-11-16): the bot works 18:00–09:00, 13:00–14:00 and all
weekend; staff are on duty Mon–Fri 09:00–13:00 and 14:00–18:00. Times are
Ulaanbaatar local (UTC+8). 2026-11-16 is a Monday.
"""
import copy
import json
from datetime import datetime

import pytest

from services import _schedule as sch

MON, TUE, FRI, SAT, SUN = 16, 17, 20, 21, 22   # November 2026


def at(day, hh, mm=0):
    return datetime(2026, 11, day, hh, mm)


def schedule(**overrides):
    s = copy.deepcopy(sch.DEFAULT_SCHEDULE)
    s['mode'] = 'schedule'
    s.update(overrides)
    return s


def test_default_matches_gundegs_schedule():
    s = schedule()
    assert sch.staff_on_duty(s, at(MON, 10)) is True
    assert sch.staff_on_duty(s, at(MON, 13, 30)) is False     # lunch → bot
    assert sch.staff_on_duty(s, at(MON, 18)) is False         # evening → bot
    assert sch.staff_on_duty(s, at(MON, 8, 59)) is False      # early morning → bot
    assert sch.staff_on_duty(s, at(SAT, 11)) is False         # weekend → bot
    assert sch.staff_on_duty(s, at(SUN, 11)) is False
    assert sch.DEFAULT_SCHEDULE['step_in'] == {'enabled': True, 'minutes': 15}


def test_boundaries_end_exclusive():
    s = schedule()
    assert sch.staff_on_duty(s, at(MON, 9)) is True
    assert sch.staff_on_duty(s, at(MON, 13)) is False
    assert sch.staff_on_duty(s, at(MON, 14)) is True
    assert sch.staff_on_duty(s, at(MON, 18)) is False


def test_ub_now_shifts_day(monkeypatch):
    # 23:30 UTC on Sunday is 07:30 on Monday in Ulaanbaatar.
    monkeypatch.setattr(sch, '_utcnow', lambda: datetime(2026, 11, 15, 23, 30))
    now = sch.ub_now()
    assert (now.weekday(), now.hour, now.minute) == (0, 7, 30)


def test_modes():
    assert sch.bot_should_reply(at(MON, 10), schedule(mode='always_on')) is True
    assert sch.bot_should_reply(at(SAT, 23), schedule(mode='off')) is False
    assert sch.bot_should_reply(at(MON, 10), schedule()) is False
    assert sch.bot_should_reply(at(MON, 19), schedule()) is True


def _with_day0(**day):
    s = schedule()
    s['days'][0] = {**s['days'][0], **day}
    return s


@pytest.mark.parametrize('bad', [
    _with_day0(start='18:00', end='09:00'),                       # end before start
    _with_day0(start='09:00', end='09:00'),                       # empty shift
    _with_day0(break_start='08:00', break_end='10:00'),           # break outside shift
    _with_day0(break_start='13:00', break_end=''),                # half a break
    _with_day0(break_start='14:00', break_end='13:00'),           # inverted break
    _with_day0(start='9h', end='18:00'),                          # not HH:MM
    _with_day0(start='25:00', end='26:00'),
    schedule(step_in={'enabled': True, 'minutes': 0}),
    schedule(step_in={'enabled': True, 'minutes': 241}),
    schedule(mode='sometimes'),
    schedule(days=[]),
])
def test_validate_rejects(bad):
    clean, error = sch.validate_schedule(bad)
    assert clean is None
    assert error


def test_validate_accepts_no_break_and_normalises():
    s = _with_day0(start='9:00', break_start='', break_end='')
    s['step_in'] = {'enabled': 'on', 'minutes': '20'}
    clean, error = sch.validate_schedule(s)
    assert error is None
    assert clean['days'][0]['start'] == '09:00'
    assert clean['step_in'] == {'enabled': True, 'minutes': 20}
    assert sch.staff_on_duty(clean, at(MON, 13, 30)) is True   # no lunch break now


def test_corrupt_row_falls_back(app, db_session):
    from extensions import db
    from models import GeneralSetting
    for value in ('{not json', json.dumps({'mode': 'x'})):
        GeneralSetting.query.filter_by(key=sch.SCHEDULE_KEY).delete()
        db.session.add(GeneralSetting(key=sch.SCHEDULE_KEY, value=value))
        db.session.commit()
        assert sch.get_bot_schedule() == sch.DEFAULT_SCHEDULE
    GeneralSetting.query.filter_by(key=sch.SCHEDULE_KEY).delete()
    db.session.commit()


def test_saved_row_is_used(app, db_session):
    from extensions import db
    from models import GeneralSetting
    saved = schedule(mode='off')
    GeneralSetting.query.filter_by(key=sch.SCHEDULE_KEY).delete()
    db.session.add(GeneralSetting(key=sch.SCHEDULE_KEY, value=json.dumps(saved)))
    db.session.commit()
    assert sch.get_bot_schedule()['mode'] == 'off'
    GeneralSetting.query.filter_by(key=sch.SCHEDULE_KEY).delete()
    db.session.commit()


def test_describe_staff_hours_default():
    assert sch.describe_staff_hours(schedule()) == (
        'Даваа–Баасан 09:00–18:00 (цайны цаг 13:00–14:00); Бямба, Ням — амралтын өдөр'
    )


@pytest.mark.parametrize('when, expected', [
    (at(MON, 13, 20), 'өнөөдөр 14:00 цагаас'),
    (at(MON, 19), 'маргааш 09:00 цагаас'),
    (at(FRI, 19), 'Даваа гарагт 09:00 цагаас'),
    (at(SAT, 10), 'Даваа гарагт 09:00 цагаас'),
    (at(SUN, 10), 'маргааш 09:00 цагаас'),
])
def test_next_staff_time_wording(when, expected):
    assert sch.describe_next_staff_time(when, schedule()) == expected


def test_no_staff_day_means_no_next_time():
    s = schedule()
    for d in s['days']:
        d['staff'] = False
    assert sch.next_staff_start(s, at(MON, 10)) is None
    assert sch.describe_next_staff_time(at(MON, 10), s) == ''


def test_bot_status_flags():
    assert sch.bot_status(at(MON, 10), schedule())['replying'] is False
    assert sch.bot_status(at(MON, 19), schedule())['replying'] is True
    assert sch.bot_status(at(MON, 10), schedule(mode='off'))['replying'] is False
    assert sch.bot_status(at(MON, 10), schedule(mode='always_on'))['replying'] is True
    assert sch.bot_status(at(MON, 10), schedule())['label']


# ---------------------------------------------------------------------------
# Office hours everywhere read the schedule (Task 2)
# ---------------------------------------------------------------------------

def _freeze_ub(monkeypatch, day, hh, mm=0):
    """Freeze the clock at a given Ulaanbaatar wall time (UTC+8)."""
    monkeypatch.setattr(sch, '_utcnow', lambda: at(day, hh, mm) - sch.UB_OFFSET)


def test_off_hours_follows_schedule(app, db_session, monkeypatch):
    import services
    _freeze_ub(monkeypatch, MON, 10)
    assert services._is_off_hours() is False
    _freeze_ub(monkeypatch, MON, 13, 30)
    assert services._is_off_hours() is True


def test_off_hours_handoff_reply_names_next_staff_time(app, db_session, monkeypatch):
    import services
    _freeze_ub(monkeypatch, MON, 19)
    reply = services._static_handoff_reply()
    assert 'маргааш 09:00 цагаас' in reply
    assert '{' not in reply


def test_prompt_lists_staff_hours(app, db_session):
    import services
    prompt = services.build_system_prompt()
    assert sch.describe_staff_hours(schedule()) in prompt


def test_waited_line_only_on_step_in(app, db_session):
    import services
    _, volatile = services.build_system_blocks(waited_minutes=15)
    assert '15 минут' in volatile
    _, volatile = services.build_system_blocks()
    assert 'минут хүлээсэн' not in volatile
