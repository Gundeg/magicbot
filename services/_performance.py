"""Гүйцэтгэл (performance) report: bot results, team inbox response in staff
hours, per-staff admin actions, the bot's unanswered questions and a
busy-hours heat-map — all over the window [now - days, now] (UTC).

Definitions (see docs/superpowers/specs/2026-11-16-performance-page-design.md):
- A REPLY is a bot message whose kind is not 'nudge', or a staff message.
- A WAIT starts at a customer message whose previous message in that
  conversation (nudges ignored) is not the customer's; its RESPONSE is the
  next reply. A wait that began before the window is never counted.
- Team metrics cover waits that start in staff hours (today's schedule, mode
  ignored) and after the first staff message ever stored (staff replies are
  only recorded since 2026-11-16).

Whole-window queries plus one in-memory pass per conversation — no per-row
queries, so a 30-day page stays fast.
"""
import re
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median

from sqlalchemy import func

from extensions import db
from models import AdminIssue, AuditEntry, FacebookUser, Message, User

from ._schedule import UB_OFFSET, get_bot_schedule, staff_on_duty

OTHER_LABEL = 'бусад'
KNOWLEDGE_GAP_LABEL = 'мэдлэг дутуу'
MAX_QUESTION_GROUPS = 30

_HANDOFF_HEAD_RE = re.compile(r'^\[бот: ([^\]]*?)(?: — ([^\]]*))?\]')
_LEADING_TAGS_RE = re.compile(r'^(\[[^\]]*\]\s*)+')


def _is_reply(m):
    return m.sender == 'staff' or (m.sender == 'bot' and m.kind != 'nudge')


def _median_int(values):
    return int(round(median(values))) if values else None


def _waits(window_msgs, prev_by_user):
    """[(start_msg, response_msg | None)] for waits starting in the window."""
    by_user = defaultdict(list)
    for m in window_msgs:
        by_user[m.facebook_user_id].append(m)
    waits = []
    for uid, msgs in by_user.items():
        msgs.sort(key=lambda m: (m.created_at, m.id))
        prev = prev_by_user.get(uid)
        pending = prev is not None and prev.sender == 'user'
        current = None          # index into waits of the counted open wait
        for m in msgs:
            if m.sender == 'user':
                if not pending:
                    pending = True
                    waits.append([m, None])
                    current = len(waits) - 1
            elif _is_reply(m):
                if pending and current is not None:
                    waits[current][1] = m
                pending, current = False, None
    return [tuple(w) for w in waits]


def _parse_handoff(content):
    """(label, reason, customer_message) from '[бот: <label>( — <reason>)?]...'."""
    content = content or ''
    head = _HANDOFF_HEAD_RE.match(content)
    message = _LEADING_TAGS_RE.sub('', content).strip()
    if not head:
        return OTHER_LABEL, '', message
    return head.group(1).strip(), (head.group(2) or '').strip(), message


def _bot_section(window_msgs, waits, since, now, handoff_rows, label_set):
    replies = [m for m in window_msgs if m.sender == 'bot' and m.kind != 'nudge']
    bot_delays = [(r.created_at - s.created_at).total_seconds()
                  for s, r in waits if r is not None and r.sender == 'bot' and r.kind is None]
    handoffs = {label: 0 for label in label_set}
    handoffs[OTHER_LABEL] = 0
    for issue in handoff_rows:
        label = _parse_handoff(issue.content)[0]
        handoffs[label if label in label_set else OTHER_LABEL] += 1
    in_window = lambda col: (FacebookUser.query.filter(col >= since, col <= now).count())  # noqa: E731
    return {
        'people': len({m.facebook_user_id for m in replies}),
        'replies': len(replies),
        'median_reply_seconds': _median_int(bot_delays),
        'step_ins': sum(1 for m in replies if m.kind == 'step_in'),
        'registrations': in_window(FacebookUser.registered_at),
        'new_leads': in_window(FacebookUser.lead_at),
        'leads_since': db.session.query(func.min(FacebookUser.lead_at)).scalar(),
        'handoffs': handoffs,
    }


def _team_section(waits, schedule, now):
    tracking_since = (db.session.query(func.min(Message.created_at))
                      .filter(Message.sender == 'staff').scalar())
    threshold = schedule['step_in']['minutes']
    team = {'waits': 0, 'staff': 0, 'step_in': 0, 'bot': 0, 'unanswered': 0,
            'staff_pct': None, 'median_staff_minutes': None, 'long_waits': 0,
            'threshold_minutes': threshold, 'tracking_since': tracking_since}
    if tracking_since is None:
        return team
    staff_delays = []
    for start, response in waits:
        if start.created_at < tracking_since or not staff_on_duty(schedule, start.created_at + UB_OFFSET):
            continue
        team['waits'] += 1
        end = response.created_at if response is not None else now
        if (end - start.created_at) >= timedelta(minutes=threshold):
            team['long_waits'] += 1
        if response is None:
            team['unanswered'] += 1
        elif response.sender == 'staff':
            team['staff'] += 1
            staff_delays.append((response.created_at - start.created_at).total_seconds() / 60)
        elif response.kind == 'step_in':
            team['step_in'] += 1
        else:
            team['bot'] += 1
    if team['waits']:
        team['staff_pct'] = int(round(100 * team['staff'] / team['waits']))
    team['median_staff_minutes'] = _median_int(staff_delays)
    return team


def _staff_section(since, now):
    entries = defaultdict(list)
    for e in AuditEntry.query.filter(AuditEntry.created_at >= since,
                                     AuditEntry.created_at <= now).all():
        entries[e.actor_id].append(e)
    resolution = defaultdict(list)
    for issue in (AdminIssue.query
                  .filter(AdminIssue.issue_type == 'handoff',
                          AdminIssue.resolved_at >= since, AdminIssue.resolved_at <= now,
                          AdminIssue.updated_by_id.isnot(None))
                  .all()):
        resolution[issue.updated_by_id].append(
            (issue.resolved_at - issue.created_at).total_seconds() / 60)

    def count(rows, action, arrow=None):
        return sum(1 for e in rows if e.action == action
                   and (arrow is None or arrow in (e.detail or '')))

    out = []
    for user in User.query.order_by(User.username).all():
        rows = entries.get(user.id, [])
        resolved = sum(1 for e in rows if e.action == 'issue.status_change'
                       and ((e.detail or '').startswith('Work Tasks-аас шийдсэн')
                            or '→ resolved' in (e.detail or '')))
        durations = resolution.get(user.id, [])
        out.append({
            'username': user.username,
            'role': user.role,
            'resolved': resolved,
            'avg_resolution_minutes': int(round(sum(durations) / len(durations))) if durations else None,
            'takeovers': count(rows, 'bot.take_over'),
            'contacted': count(rows, 'lead.mark_contacted') + count(rows, 'lead.status_change', '→ contacted'),
            'converted': count(rows, 'lead.status_change', '→ converted'),
            'dropped': count(rows, 'lead.drop') + count(rows, 'lead.status_change', '→ dropped'),
        })
    return out


def _questions(handoff_rows):
    groups = {}
    for issue in sorted(handoff_rows, key=lambda i: i.created_at, reverse=True):
        label, reason, message = _parse_handoff(issue.content)
        if label != KNOWLEDGE_GAP_LABEL:
            continue
        key = (reason or message[:80]).strip().lower()
        if key not in groups:
            groups[key] = {'reason': reason or message[:80], 'count': 0,
                           'last_seen_ub': issue.created_at + UB_OFFSET,
                           'sample': message, 'user_id': issue.facebook_user_id}
        groups[key]['count'] += 1
    return list(groups.values())[:MAX_QUESTION_GROUPS]


def _heatmap(window_msgs, schedule):
    grid = [[0] * 24 for _ in range(7)]
    for m in window_msgs:
        if m.sender == 'user':
            t = m.created_at + UB_OFFSET
            grid[t.weekday()][t.hour] += 1
    monday = datetime(2030, 1, 7)       # any Monday: only weekday + clock matter
    staff_cells = {(d, h) for d in range(7) for h in range(24)
                   if any(staff_on_duty(schedule, monday + timedelta(days=d, hours=h, minutes=mi))
                          for mi in (0, 30))}
    return {'grid': grid, 'max': max(max(row) for row in grid), 'staff_cells': staff_cells}


def performance_report(days, now=None):
    """The whole Гүйцэтгэл report for the last `days` days (see module doc)."""
    from . import HANDOFF_CATEGORY_LABELS
    now = now or datetime.utcnow()
    since = now - timedelta(days=days)
    schedule = get_bot_schedule()

    window_msgs = (Message.query
                   .filter(Message.created_at >= since, Message.created_at <= now)
                   .all())
    uids = {m.facebook_user_id for m in window_msgs}
    prev_by_user = {}
    if uids:
        # Last non-nudge message before the window, per conversation: tells
        # whether the window opens mid-wait.
        last_before = (db.session.query(Message.facebook_user_id,
                                        func.max(Message.created_at).label('t'))
                       .filter(Message.created_at < since,
                               Message.facebook_user_id.in_(uids),
                               db.or_(Message.kind.is_(None), Message.kind != 'nudge'))
                       .group_by(Message.facebook_user_id)
                       .subquery())
        for m in (Message.query.join(
                last_before, db.and_(Message.facebook_user_id == last_before.c.facebook_user_id,
                                     Message.created_at == last_before.c.t)).all()):
            prev_by_user[m.facebook_user_id] = m
    waits = _waits(window_msgs, prev_by_user)
    handoff_rows = (AdminIssue.query
                    .filter(AdminIssue.issue_type == 'handoff',
                            AdminIssue.created_at >= since, AdminIssue.created_at <= now)
                    .all())

    return {
        'days': days,
        'since': since,
        'bot': _bot_section(window_msgs, waits, since, now, handoff_rows,
                            list(HANDOFF_CATEGORY_LABELS.values())),
        'team': _team_section(waits, schedule, now),
        'staff': _staff_section(since, now),
        'questions': _questions(handoff_rows),
        'heatmap': _heatmap(window_msgs, schedule),
    }
