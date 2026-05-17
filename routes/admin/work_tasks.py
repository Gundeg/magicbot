"""Operational work queue: dashboard, inbox, leads, issues, logs, conversation
viewer, and ops APIs (telegram test, backfill, classify, handoff poll).

Phase 3 will rename Inbox to Work Tasks and absorb leads/issues into it as tabs.
"""
import os
from datetime import datetime, timedelta

import requests
from flask import jsonify, render_template, request
from flask_login import current_user, login_required
from sqlalchemy.orm import joinedload

from app import app
from auth import admin_required
from extensions import db
from models import AdminIssue, BusinessLine, FacebookUser, Message
from services import (FACEBOOK_ACCESS_TOKEN, FACEBOOK_APP_SECRET,
                      classify_conversation, get_handoff_poll_payload,
                      get_setting, get_telegram_chat_ids, log_admin_action,
                      refresh_facebook_user_name)


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


# ===================== LEADS =====================

@app.route('/admin/leads')
@login_required
@admin_required
def leads():
    """Two buckets in one page:
      1) Confirmed leads — dropped a phone number (is_lead=True).
      2) Hot prospects — reached the 'pricing' or 'ready' funnel stage but haven't
         shared a phone yet.
    """
    confirmed = (FacebookUser.query
                 .filter_by(is_lead=True)
                 .order_by(FacebookUser.created_at.desc())
                 .all())

    confirmed_msg_counts = dict(
        db.session.query(Message.facebook_user_id, db.func.count(Message.id))
        .filter(Message.facebook_user_id.in_([l.id for l in confirmed]))
        .group_by(Message.facebook_user_id)
        .all()
    ) if confirmed else {}

    last_msg_subq = (db.session.query(
        Message.facebook_user_id.label('uid'),
        db.func.max(Message.created_at).label('last_at'),
    ).group_by(Message.facebook_user_id).subquery())

    msg_count_subq = (db.session.query(
        Message.facebook_user_id.label('uid'),
        db.func.count(Message.id).label('msg_count'),
    ).group_by(Message.facebook_user_id).subquery())

    last_user_msg_subq = (db.session.query(
        Message.facebook_user_id.label('uid'),
        db.func.max(Message.id).label('last_user_msg_id'),
    ).filter(Message.sender == 'user')
     .group_by(Message.facebook_user_id).subquery())

    hot_rows = (db.session.query(
        FacebookUser,
        last_msg_subq.c.last_at,
        msg_count_subq.c.msg_count,
        last_user_msg_subq.c.last_user_msg_id,
    ).join(last_msg_subq, FacebookUser.id == last_msg_subq.c.uid)
     .outerjoin(msg_count_subq, FacebookUser.id == msg_count_subq.c.uid)
     .outerjoin(last_user_msg_subq, FacebookUser.id == last_user_msg_subq.c.uid)
     .filter(FacebookUser.is_lead == False)  # noqa: E712
     .filter(FacebookUser.funnel_stage.in_(['pricing', 'ready']))
     .order_by(last_msg_subq.c.last_at.desc())
     .all())

    msg_ids = [row[3] for row in hot_rows if row[3]]
    content_by_id = {}
    if msg_ids:
        for m in Message.query.filter(Message.id.in_(msg_ids)).all():
            content_by_id[m.id] = m.content or ''

    hot_prospects = []
    for user, last_at, msg_count, last_user_msg_id in hot_rows:
        hot_prospects.append({
            'user': user,
            'last_at': last_at,
            'last_message': content_by_id.get(last_user_msg_id, ''),
            'message_count': msg_count or 0,
        })

    return render_template(
        'leads.html',
        leads=confirmed,
        hot_prospects=hot_prospects,
        confirmed_msg_counts=confirmed_msg_counts,
    )


# ===================== ISSUES =====================

@app.route('/admin/issues', methods=['GET', 'POST'])
@login_required
@admin_required
def issues():
    if request.method == 'POST':
        data = request.get_json()
        action = data.get('action')

        if action == 'update_status':
            issue = AdminIssue.query.get(data.get('id'))
            if issue:
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
            user_id = data.get('user_id')
            user = FacebookUser.query.get(user_id)
            if not user:
                return jsonify({'success': False, 'error': 'user not found'}), 404
            user.bot_muted_until = None
            db.session.commit()
            log_admin_action(
                'bot.unmute', 'facebook_user', user.id, user.name or user.facebook_id,
                detail='Ботыг гараар асаасан'
            )
            return jsonify({'success': True})

        return jsonify({'success': False, 'error': 'unknown action'}), 400

    issues_rows = (AdminIssue.query
                   .options(joinedload(AdminIssue.facebook_user))
                   .filter_by(status='open')
                   .order_by(AdminIssue.created_at.desc())
                   .all())
    now = datetime.utcnow()
    muted_users = (FacebookUser.query
                   .filter(FacebookUser.bot_muted_until != None)  # noqa: E711
                   .filter(FacebookUser.bot_muted_until > now)
                   .order_by(FacebookUser.bot_muted_until.asc())
                   .all())
    return render_template('issues.html', issues=issues_rows, muted_users=muted_users, now=now)


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


# ===================== INBOX =====================

@app.route('/admin/inbox', methods=['GET', 'POST'])
@login_required
@admin_required
def inbox():
    """Unified daily-work queue."""
    now = datetime.utcnow()

    if request.method == 'POST':
        data = request.get_json() or {}
        action = data.get('action')

        if action == 'mark_contacted':
            user = FacebookUser.query.get(data.get('user_id'))
            if not user:
                return jsonify({'success': False}), 404
            user.lead_status = 'contacted'
            db.session.commit()
            log_admin_action(
                'lead.mark_contacted', 'facebook_user', user.id,
                user.name or user.facebook_id,
                detail='Inbox-оос холбогдсон гэж тэмдэглэв'
            )
            return jsonify({'success': True})

        if action == 'resolve_issue':
            issue = AdminIssue.query.get(data.get('id'))
            if not issue:
                return jsonify({'success': False}), 404
            issue.status = 'resolved'
            issue.resolved_at = datetime.utcnow()
            issue.updated_by_id = current_user.id
            issue.updated_at = datetime.utcnow()
            db.session.commit()
            log_admin_action(
                'issue.status_change', 'issue', issue.id,
                (issue.facebook_user.name if issue.facebook_user else None) or f'#{issue.id}',
                detail='Inbox-оос шийдсэн'
            )
            return jsonify({'success': True})

        if action == 'unmute':
            user = FacebookUser.query.get(data.get('user_id'))
            if not user:
                return jsonify({'success': False}), 404
            user.bot_muted_until = None
            db.session.commit()
            log_admin_action(
                'bot.unmute', 'facebook_user', user.id, user.name or user.facebook_id,
                detail='Inbox-оос ботыг асаасан'
            )
            return jsonify({'success': True})

        return jsonify({'success': False, 'error': 'unknown action'}), 400

    hot_stages_raw = get_setting('hot_prospect_stages', 'pricing,ready') or 'pricing,ready'
    hot_stages = [s.strip() for s in hot_stages_raw.split(',') if s.strip()]
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

    aging_threshold = now - timedelta(hours=INBOX_AGING_HOURS)
    aging_issues = (AdminIssue.query
                    .options(joinedload(AdminIssue.facebook_user))
                    .filter_by(status='open')
                    .filter(AdminIssue.created_at < aging_threshold)
                    .order_by(AdminIssue.created_at.asc())
                    .limit(50)
                    .all())

    muted_users = (FacebookUser.query
                   .filter(FacebookUser.bot_muted_until != None)  # noqa: E711
                   .filter(FacebookUser.bot_muted_until > now)
                   .order_by(FacebookUser.bot_muted_until.asc())
                   .limit(50)
                   .all())

    return render_template(
        'inbox.html',
        hot_prospects=hot_prospects,
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
    """Re-classify up to 300 users who have sent at least one message."""
    users = (
        FacebookUser.query
        .join(Message, Message.facebook_user_id == FacebookUser.id)
        .filter(Message.sender == 'user')
        .group_by(FacebookUser.id)
        .order_by(
            FacebookUser.conversation_topic.is_(None).desc(),
            FacebookUser.updated_at.asc(),
        )
        .limit(300)
        .all()
    )
    classified = 0
    for u in users:
        before = u.conversation_topic
        classify_conversation(u)
        if u.conversation_topic and u.conversation_topic != before:
            classified += 1
    return jsonify({
        'attempted': len(users),
        'classified': classified,
    })


@app.route('/admin/api/handoff-poll')
@login_required
@admin_required
def handoff_poll():
    """Lightweight polling endpoint for the dashboard sound/badge."""
    return jsonify(get_handoff_poll_payload())
