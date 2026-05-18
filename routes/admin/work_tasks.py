"""Operational work queue: dashboard, inbox, leads, issues, logs, conversation
viewer, and ops APIs (telegram test, backfill, classify, handoff poll).

Phase 3 will rename Inbox to Work Tasks and absorb leads/issues into it as tabs.
"""
import os
from datetime import datetime, timedelta

import requests
from flask import jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm import joinedload

from app import app
from auth import admin_required
from extensions import db
from models import (AdminIssue, BusinessLine, ConversationTopic, FacebookUser,
                    Message)
from services import (CLASSIFICATION_LOOKBACK_MAX_DAYS, FACEBOOK_ACCESS_TOKEN,
                      FACEBOOK_APP_SECRET, classify_user_topics,
                      get_classification_lookback_days,
                      get_handoff_poll_payload, get_setting,
                      get_telegram_chat_ids, log_admin_action,
                      refresh_facebook_user_name, take_over_chat)


# How old an open issue must be before it surfaces on the Inbox as "aging".
INBOX_AGING_HOURS = int(os.environ.get('INBOX_AGING_HOURS', '12'))

LOGS_PAGE_SIZE = 100


# ===================== DASHBOARD =====================

@app.route('/admin/dashboard')
@login_required
@admin_required
def dashboard():
    hot_stages_raw = get_setting('hot_prospect_stages', 'pricing,ready') or 'pricing,ready'
    hot_stages = [s.strip() for s in hot_stages_raw.split(',') if s.strip()]

    now = datetime.utcnow()

    leads_count = FacebookUser.query.filter_by(is_lead=True).count()
    hot_prospects_count = (FacebookUser.query
                           .filter_by(is_lead=False)
                           .filter(FacebookUser.funnel_stage.in_(hot_stages))
                           .count())
    open_issues = AdminIssue.query.filter_by(status='open').count()
    total_messages = Message.query.count()

    stale_threshold = now - timedelta(hours=24)
    aging_open_issues = (AdminIssue.query
                         .filter_by(status='open')
                         .filter(AdminIssue.created_at < stale_threshold)
                         .count())
    muted_users_count = (FacebookUser.query
                         .filter(FacebookUser.bot_muted_until != None)  # noqa: E711
                         .filter(FacebookUser.bot_muted_until > now)
                         .count())

    recent_issues = (AdminIssue.query
                     .options(joinedload(AdminIssue.facebook_user))
                     .order_by(AdminIssue.created_at.desc())
                     .limit(5).all())
    recent_leads = (FacebookUser.query
                    .filter_by(is_lead=True)
                    .order_by(FacebookUser.created_at.desc())
                    .limit(5).all())

    topic_rows = (
        db.session.query(FacebookUser.conversation_topic, db.func.count(FacebookUser.id))
        .group_by(FacebookUser.conversation_topic)
        .all()
    )
    topic_breakdown = {(t or 'unclassified'): c for t, c in topic_rows}

    business_lines_summary = BusinessLine.query.order_by(BusinessLine.is_active.desc(), BusinessLine.name).all()

    latest_message = (Message.query
                      .order_by(Message.created_at.desc())
                      .first())
    last_message_at = latest_message.created_at if latest_message else None
    telegram_ready = bool(os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()) and bool(get_telegram_chat_ids())
    health = {
        'openai_ready': bool(os.environ.get('OPENAI_API_KEY', '').strip()),
        'facebook_token_ready': bool(FACEBOOK_ACCESS_TOKEN),
        'facebook_signature_ready': bool(FACEBOOK_APP_SECRET),
        'telegram_ready': telegram_ready,
        'last_message_at': last_message_at,
        'last_message_age_minutes': (
            int((now - last_message_at).total_seconds() // 60)
            if last_message_at else None
        ),
    }

    return render_template('dashboard.html',
                           leads_count=leads_count,
                           hot_prospects_count=hot_prospects_count,
                           open_issues=open_issues,
                           total_messages=total_messages,
                           aging_open_issues=aging_open_issues,
                           muted_users_count=muted_users_count,
                           recent_issues=recent_issues,
                           recent_leads=recent_leads,
                           topic_breakdown=topic_breakdown,
                           business_lines_summary=business_lines_summary,
                           health=health)


# ===================== LEADS / ISSUES (legacy URLs) =====================
# The standalone Leads and Issues pages were merged into Work Tasks below;
# these endpoints now redirect with the appropriate tab pre-selected so
# external bookmarks and `url_for('leads')` / `url_for('issues')` in
# templates keep working.

@app.route('/admin/leads')
@login_required
@admin_required
def leads():
    return redirect(url_for('work_tasks', tab='leads'), code=301)


@app.route('/admin/issues')
@login_required
@admin_required
def issues():
    return redirect(url_for('work_tasks', tab='open_issues'), code=301)


# ===================== LOGS =====================

@app.route('/admin/logs')
@login_required
@admin_required
def logs():
    """Message history with cursor pagination."""
    try:
        before_id = int(request.args.get('before', 0)) or None
    except (TypeError, ValueError):
        before_id = None
    q = Message.query.options(joinedload(Message.facebook_user))
    if before_id:
        q = q.filter(Message.id < before_id)
    messages = (q.order_by(Message.id.desc())
                 .limit(LOGS_PAGE_SIZE + 1)
                 .all())
    has_more = len(messages) > LOGS_PAGE_SIZE
    messages = messages[:LOGS_PAGE_SIZE]
    next_cursor = messages[-1].id if (has_more and messages) else None
    return render_template(
        'logs.html',
        messages=messages,
        next_cursor=next_cursor,
        has_more=has_more,
        is_first_page=before_id is None,
    )


# ===================== CONVERSATION VIEWER =====================

@app.route('/admin/users/<int:user_id>/conversation')
@login_required
@admin_required
def conversation(user_id):
    """Read-only per-user thread view."""
    user = FacebookUser.query.get_or_404(user_id)
    messages = (Message.query
                .filter_by(facebook_user_id=user.id)
                .order_by(Message.created_at.asc())
                .all())
    open_issues = (AdminIssue.query
                   .filter_by(facebook_user_id=user.id, status='open')
                   .order_by(AdminIssue.created_at.desc())
                   .all())
    now = datetime.utcnow()
    return render_template(
        'conversation.html',
        fb_user=user,
        messages=messages,
        open_issues=open_issues,
        now=now,
    )


# ===================== WORK TASKS =====================
# The unified daily-work queue. Five tabs: Hot Prospects (people who showed
# buying signals but haven't dropped a phone), Leads (people who did),
# Open Issues (every unresolved AdminIssue), Aging Issues (open > N hours),
# and Muted Users (bot paused for them).

VALID_WORK_TASKS_TABS = ('hot_prospects', 'leads', 'open_issues', 'aging', 'muted')


@app.route('/admin/inbox')
@login_required
@admin_required
def inbox():
    return redirect(url_for('work_tasks'), code=301)


@app.route('/admin/work-tasks', methods=['GET', 'POST'])
@login_required
@admin_required
def work_tasks():
    """Unified daily-work queue. See module docstring."""
    if request.method == 'POST':
        data = request.get_json() or {}
        action = data.get('action')

        if action == 'mark_contacted':
            user = db.session.get(FacebookUser, data.get('user_id'))
            if not user:
                return jsonify({'success': False}), 404
            user.lead_status = 'contacted'
            db.session.commit()
            log_admin_action(
                'lead.mark_contacted', 'facebook_user', user.id,
                user.name or user.facebook_id,
                detail='Work Tasks-аас холбогдсон гэж тэмдэглэв'
            )
            return jsonify({'success': True})

        if action == 'resolve_issue':
            issue = db.session.get(AdminIssue, data.get('id'))
            if not issue:
                return jsonify({'success': False}), 404
            issue.status = 'resolved'
            issue.resolved_at = datetime.utcnow()
            issue.updated_by_id = current_user.id
            issue.updated_at = datetime.utcnow()
            # If the bot was muted via "Take Over" for this user, resolving
            # the issue is the staff's "I'm done" signal — restore the bot
            # so future messages from this customer are handled normally.
            # Doesn't matter if the customer messages the bot a year later;
            # this ensures a closed handoff issue doesn't leave the bot
            # silenced indefinitely.
            unmuted = False
            if issue.facebook_user and issue.facebook_user.bot_muted_until:
                issue.facebook_user.bot_muted_until = None
                unmuted = True
            db.session.commit()
            log_admin_action(
                'issue.status_change', 'issue', issue.id,
                (issue.facebook_user.name if issue.facebook_user else None) or f'#{issue.id}',
                detail='Work Tasks-аас шийдсэн' + (' + ботыг асаасан' if unmuted else '')
            )
            return jsonify({'success': True, 'bot_unmuted': unmuted})

        if action == 'update_status':
            issue = db.session.get(AdminIssue, data.get('id'))
            if not issue:
                return jsonify({'success': False}), 404
            new_status = data.get('status')
            old_status = issue.status
            if new_status:
                issue.status = new_status
                if new_status == 'resolved':
                    issue.resolved_at = datetime.utcnow()
            issue.updated_by_id = current_user.id
            issue.updated_at = datetime.utcnow()
            notes = (data.get('notes') or '').strip()
            if notes:
                issue.notes = notes
            db.session.commit()
            if new_status and new_status != old_status:
                log_admin_action(
                    'issue.status_change', 'issue', issue.id,
                    (issue.facebook_user.name if issue.facebook_user else None) or f'#{issue.id}',
                    detail=f'{old_status} → {new_status}'
                )
            return jsonify({'success': True})

        if action == 'unmute':
            user = db.session.get(FacebookUser, data.get('user_id'))
            if not user:
                return jsonify({'success': False}), 404
            user.bot_muted_until = None
            db.session.commit()
            log_admin_action(
                'bot.unmute', 'facebook_user', user.id, user.name or user.facebook_id,
                detail='Work Tasks-аас ботыг асаасан'
            )
            return jsonify({'success': True})

        if action == 'take_over':
            # Staff is taking over this chat — mute the bot for `hours`
            # so the customer talks to the staff member directly via
            # Facebook Page Inbox without bot interference. Default 4h
            # if the admin didn't supply a duration.
            issue = db.session.get(AdminIssue, data.get('id'))
            if not issue or not issue.facebook_user:
                return jsonify({'success': False, 'error': 'issue олдсонгүй'}), 404
            try:
                hours = int(data.get('hours') or 4)
            except (TypeError, ValueError):
                hours = 4
            take_over_chat(issue.facebook_user, hours, actor_username=current_user.username)
            log_admin_action(
                'bot.take_over', 'facebook_user', issue.facebook_user.id,
                issue.facebook_user.name or issue.facebook_user.facebook_id,
                detail=f'{current_user.username} ботыг {hours}ц зогсоож яриаг хариуцлаа'
            )
            return jsonify({'success': True, 'muted_for_hours': hours})

        return jsonify({'success': False, 'error': 'unknown action'}), 400

    now = datetime.utcnow()
    tab = request.args.get('tab', 'hot_prospects')
    if tab not in VALID_WORK_TASKS_TABS:
        tab = 'hot_prospects'

    hot_stages_raw = get_setting('hot_prospect_stages', 'pricing,ready') or 'pricing,ready'
    hot_stages = [s.strip() for s in hot_stages_raw.split(',') if s.strip()]

    # --- Tab 1: hot prospects (no phone yet, in pricing/ready funnel) ---
    hot_prospects = (FacebookUser.query
                     .filter_by(is_lead=False)
                     .filter(FacebookUser.funnel_stage.in_(hot_stages))
                     .filter(db.or_(
                         FacebookUser.lead_status == None,  # noqa: E711
                         FacebookUser.lead_status == 'new',
                     ))
                     .order_by(FacebookUser.updated_at.desc())
                     .limit(50)
                     .all())

    # --- Tab 2: confirmed leads (dropped a phone). Reuse the rich shape
    # the old leads.html used: per-lead message counts + last user message.
    leads_rows = (FacebookUser.query
                  .filter_by(is_lead=True)
                  .order_by(FacebookUser.created_at.desc())
                  .all())
    leads_msg_counts = dict(
        db.session.query(Message.facebook_user_id, db.func.count(Message.id))
        .filter(Message.facebook_user_id.in_([l.id for l in leads_rows]))
        .group_by(Message.facebook_user_id)
        .all()
    ) if leads_rows else {}

    # Pull every topic tag attached to the displayed leads in a single
    # query, then build a per-user list ordered by last_seen_at desc so
    # the most recent topic shows first in each row.
    leads_topics = {}
    if leads_rows:
        topic_rows = (ConversationTopic.query
                      .filter(ConversationTopic.facebook_user_id.in_(
                          [l.id for l in leads_rows]
                      ))
                      .order_by(ConversationTopic.last_seen_at.desc())
                      .all())
        for t in topic_rows:
            leads_topics.setdefault(t.facebook_user_id, []).append(t)

    # Optional topic filter ('?topic=Magic Finance'): when set, hide
    # leads that don't carry the chosen topic. The filter values come
    # from the same set we just collected, so it stays accurate even
    # when the catalog changes.
    topic_filter = (request.args.get('topic') or '').strip()
    if topic_filter:
        keep = {uid for uid, ts in leads_topics.items()
                if any(t.topic == topic_filter for t in ts)}
        leads_rows = [l for l in leads_rows if l.id in keep]

    # Distinct topic names (with counts) for the filter dropdown.
    all_topic_counts = dict(
        db.session.query(ConversationTopic.topic, db.func.count(ConversationTopic.id))
        .group_by(ConversationTopic.topic)
        .all()
    )

    # --- Tab 3: every open issue (not just aging) ---
    open_issues = (AdminIssue.query
                   .options(joinedload(AdminIssue.facebook_user))
                   .filter_by(status='open')
                   .order_by(AdminIssue.created_at.desc())
                   .limit(100)
                   .all())

    # --- Tab 4: aging issues (subset older than INBOX_AGING_HOURS) ---
    aging_threshold = now - timedelta(hours=INBOX_AGING_HOURS)
    aging_issues = (AdminIssue.query
                    .options(joinedload(AdminIssue.facebook_user))
                    .filter_by(status='open')
                    .filter(AdminIssue.created_at < aging_threshold)
                    .order_by(AdminIssue.created_at.asc())
                    .limit(50)
                    .all())

    # --- Tab 5: muted users (bot temporarily silenced) ---
    muted_users = (FacebookUser.query
                   .filter(FacebookUser.bot_muted_until != None)  # noqa: E711
                   .filter(FacebookUser.bot_muted_until > now)
                   .order_by(FacebookUser.bot_muted_until.asc())
                   .limit(50)
                   .all())

    return render_template(
        'work_tasks.html',
        tab=tab,
        hot_prospects=hot_prospects,
        leads=leads_rows,
        leads_msg_counts=leads_msg_counts,
        leads_topics=leads_topics,
        topic_filter=topic_filter,
        all_topic_counts=all_topic_counts,
        classification_lookback_days=get_classification_lookback_days(),
        classification_lookback_max=CLASSIFICATION_LOOKBACK_MAX_DAYS,
        open_issues=open_issues,
        aging_issues=aging_issues,
        muted_users=muted_users,
        aging_hours=INBOX_AGING_HOURS,
        now=now,
    )


# ===================== OPS APIs (TELEGRAM / BACKFILL / CLASSIFY / POLL) =====================

@app.route('/admin/api/test-telegram', methods=['POST'])
@login_required
@admin_required
def test_telegram():
    """Send a 'this is a test' message to every configured Telegram chat ID."""
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat_ids = get_telegram_chat_ids()

    result = {
        'token_set': bool(token),
        'chat_ids': chat_ids,
        'attempts': [],
        'success_count': 0,
    }

    if not token:
        result['error'] = (
            'TELEGRAM_BOT_TOKEN орчны хувьсагч тогтоогдоогүй байна. '
            'Render dashboard → Environment руу орж нэмж, дахин deploy хийнэ үү.'
        )
        return jsonify(result)
    if not chat_ids:
        result['error'] = (
            'Chat ID-ууд хоосон байна. Доорх "Telegram chat ID-ууд" хэсэгт '
            'утгаа оруулаад "Save Settings" дарсан эсэхээ шалгана уу.'
        )
        return jsonify(result)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    test_msg = (
        f"✅ Test from MagicBot admin panel. "
        f"Sent by: {current_user.username}. "
        f"If you see this, your Telegram alerts are wired correctly."
    )
    for cid in chat_ids:
        item = {'chat_id': cid, 'ok': False, 'status': None, 'error': None}
        try:
            resp = requests.post(url, json={
                'chat_id': cid,
                'text': test_msg,
                'disable_web_page_preview': True,
            }, timeout=10)
            item['status'] = resp.status_code
            if resp.status_code == 200:
                item['ok'] = True
                result['success_count'] += 1
            else:
                item['error'] = resp.text[:300]
        except Exception as e:
            item['error'] = str(e)
        result['attempts'].append(item)

    return jsonify(result)


@app.route('/admin/api/backfill-names', methods=['POST'])
@login_required
@admin_required
def backfill_names():
    """Retry the Facebook profile lookup for every user currently named 'Unknown'."""
    targets = (FacebookUser.query
               .filter(db.or_(
                   FacebookUser.name == None,  # noqa: E711
                   FacebookUser.name == '',
                   FacebookUser.name == 'Unknown',
               ))
               .order_by(FacebookUser.id.asc())
               .limit(200)
               .all())
    updated = 0
    for u in targets:
        if refresh_facebook_user_name(u):
            updated += 1
    return jsonify({
        'attempted': len(targets),
        'updated': updated,
        'remaining_unknown_after': FacebookUser.query.filter(db.or_(
            FacebookUser.name == None,  # noqa: E711
            FacebookUser.name == '',
            FacebookUser.name == 'Unknown',
        )).count(),
    })


@app.route('/admin/api/classify-conversations', methods=['POST'])
@login_required
@admin_required
def classify_conversations_backfill():
    """Re-classify up to 300 users who have sent at least one message
    within the admin-configured lookback window. Always returns JSON —
    a 500 here would surface as an "Unexpected token '<'" toast in the
    Leads tab JS because the fetch parses the response body as JSON.
    """
    try:
        lookback = get_classification_lookback_days()
        since = datetime.utcnow() - timedelta(days=lookback)
        users = (
            FacebookUser.query
            .join(Message, Message.facebook_user_id == FacebookUser.id)
            .filter(Message.sender == 'user')
            .filter(Message.created_at >= since)
            .group_by(FacebookUser.id)
            .order_by(FacebookUser.updated_at.asc())
            .limit(300)
            .all()
        )
        classified = 0
        topics_attached_total = 0
        for u in users:
            n = classify_user_topics(u)
            if n:
                classified += 1
                topics_attached_total += n
        return jsonify({
            'attempted': len(users),
            'classified': classified,
            'topics_attached': topics_attached_total,
            'lookback_days': lookback,
        })
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception(
            'classify-conversations endpoint failed: %s', e,
        )
        return jsonify({
            'error': 'classify_failed',
            'message': str(e)[:300],
            'attempted': 0,
            'classified': 0,
        }), 500


@app.route('/admin/api/handoff-poll')
@login_required
@admin_required
def handoff_poll():
    """Lightweight polling endpoint for the dashboard sound/badge."""
    return jsonify(get_handoff_poll_payload())
