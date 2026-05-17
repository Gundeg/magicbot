"""All `/admin/*` HTTP routes.

Routes use `@app.route(...)` against the shared Flask instance defined in
`app.py`. This keeps endpoint names flat (`url_for('dashboard')`) so existing
templates don't have to learn a Blueprint prefix.
"""
import os
import time
from datetime import datetime, timedelta

import requests
from flask import (flash, jsonify, redirect, render_template, request,
                   url_for)
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
from werkzeug.security import check_password_hash, generate_password_hash

from app import app
from auth import admin_required, super_admin_required
from extensions import db
from models import (AdminIssue, AuditEntry, BusinessLine, Course, FacebookUser,
                    FAQ, GeneralSetting, HandoffKeyword, Message, Product,
                    ProductLink, Service, Software, TeamMember,
                    TrainingSnippet, User)
from services import (ALLOWED_COURSE_TYPES, BOT_PERSONA,
                      FACEBOOK_ACCESS_TOKEN, FACEBOOK_APP_SECRET,
                      SELF_PACED_COURSE_TYPE, TRAINING_CONTENT,
                      advance_recurring_courses, archive_past_courses,
                      build_system_prompt, classify_conversation,
                      get_handoff_poll_payload, get_setting,
                      get_sound_alerts_enabled, get_telegram_chat_ids,
                      lint_training_data, log_admin_action,
                      refresh_facebook_user_name)


MIN_ADMIN_PASSWORD_LENGTH = 12

# How old an open issue must be before it surfaces on the Inbox as "aging".
INBOX_AGING_HOURS = int(os.environ.get('INBOX_AGING_HOURS', '12'))

LOGS_PAGE_SIZE = 100


# ===================== AUTH =====================

@app.route('/admin/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('dashboard'))
        else:
            return render_template('login.html', error='Invalid credentials')

    return render_template('login.html')


@app.route('/admin/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


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


# ===================== COURSES =====================

def _parse_course_payload(data, existing=None):
    """Validate + coerce the JSON payload from the course modal into model
    fields. Returns (kwargs, error_message_or_None). Centralized so add/edit
    share the same checks: course_type allow-list, unique course_number,
    self-paced rule (no start/end date for 100% Online)."""
    name = (data.get('name') or '').strip()
    if not name:
        return None, 'Сургалтын нэр шаардлагатай.'

    course_type = (data.get('course_type') or '').strip()
    if course_type not in ALLOWED_COURSE_TYPES:
        return None, (
            f'course_type "{course_type}" зөвшөөрөгдөөгүй. '
            f'Дараахаас сонгоно уу: {", ".join(ALLOWED_COURSE_TYPES)}'
        )

    is_self_paced = course_type == SELF_PACED_COURSE_TYPE

    raw_number = data.get('course_number')
    if raw_number in (None, '', 'null'):
        return None, (
            'Курсын дугаар (course_number) шаардлагатай. Жишээ: 1881. '
            'Адилхан нэртэй ангиудыг ялгахад ашиглана.'
        )
    try:
        course_number = int(raw_number)
    except (TypeError, ValueError):
        return None, 'course_number бүхэл тоо байх ёстой.'
    clash = Course.query.filter_by(course_number=course_number).first()
    if clash and (existing is None or clash.id != existing.id):
        return None, f'#{course_number} дугаартай анги аль хэдийн бүртгэлтэй (id={clash.id}).'

    raw_duration = data.get('duration_days')
    duration_days = None
    if raw_duration not in (None, '', 'null'):
        try:
            duration_days = int(raw_duration)
            if duration_days < 0:
                raise ValueError
        except (TypeError, ValueError):
            return None, 'duration_days эерэг бүхэл тоо байх ёстой.'

    if is_self_paced:
        start_date = None
        end_date = None
    else:
        start_raw = data.get('start_date')
        if not start_raw:
            return None, 'Эхлэх огноо шаардлагатай (зөвхөн 100% Online анги хоосон үлдээнэ).'
        try:
            start_date = datetime.fromisoformat(start_raw)
        except ValueError:
            return None, 'start_date YYYY-MM-DD форматтай байна.'
        end_raw = data.get('end_date')
        end_date = None
        if end_raw:
            try:
                end_date = datetime.fromisoformat(end_raw)
            except ValueError:
                return None, 'end_date YYYY-MM-DD форматтай байна.'

    try:
        price = float(data.get('price') or 0)
    except (TypeError, ValueError):
        return None, 'price тоон утга байх ёстой.'

    return {
        'name': name,
        'course_type': course_type,
        'course_number': course_number,
        'start_date': start_date,
        'end_date': end_date,
        'time': (data.get('time') or '').strip(),
        'price': price,
        'description': data.get('description'),
        'duration_days': duration_days,
        'is_recurring': bool(data.get('is_recurring')),
    }, None


@app.route('/admin/courses', methods=['GET', 'POST'])
@login_required
@admin_required
def courses():
    if request.method == 'POST':
        data = request.get_json()
        action = data.get('action')

        if action == 'add':
            fields, err = _parse_course_payload(data)
            if err:
                return jsonify({'success': False, 'error': err}), 400
            course = Course(**fields)
            db.session.add(course)
            db.session.commit()
            return jsonify({'success': True, 'id': course.id})

        elif action == 'edit':
            course = Course.query.get(data.get('id'))
            if not course:
                return jsonify({'success': False, 'error': 'Анги олдсонгүй.'}), 404
            fields, err = _parse_course_payload(data, existing=course)
            if err:
                return jsonify({'success': False, 'error': err}), 400
            for key, value in fields.items():
                setattr(course, key, value)
            db.session.commit()
            return jsonify({'success': True})

        elif action == 'toggle':
            course = Course.query.get(data.get('id'))
            if course:
                course.is_active = not course.is_active
                course.status_note = (data.get('status_note') or '').strip() or None
                db.session.commit()
                log_admin_action(
                    'course.toggle', 'course', course.id, course.name,
                    detail=('Идэвхжүүлсэн' if course.is_active else 'Түр зогсоосон') +
                           (f". Тэмдэглэл: {course.status_note}" if course.status_note else '')
                )
                return jsonify({'success': True, 'is_active': course.is_active})

        elif action == 'delete':
            course = Course.query.get(data.get('id'))
            if course:
                label = course.name
                cid = course.id
                db.session.delete(course)
                db.session.commit()
                log_admin_action('course.delete', 'course', cid, label, detail='Устгасан')
                return jsonify({'success': True})

        elif action == 'refresh_dates':
            advanced = advance_recurring_courses()
            archived = archive_past_courses()
            return jsonify({
                'success': True,
                'updated': advanced,
                'archived': archived,
            })

    courses_rows = Course.query.all()
    return render_template('courses.html', courses=courses_rows)


# ===================== FAQ =====================

@app.route('/admin/faq', methods=['GET', 'POST'])
@login_required
@admin_required
def faq():
    if request.method == 'POST':
        data = request.get_json()
        action = data.get('action')

        if action == 'add':
            faq_item = FAQ(
                question=data.get('question'),
                answer=data.get('answer'),
                category=data.get('category')
            )
            db.session.add(faq_item)
            db.session.commit()
            return jsonify({'success': True, 'id': faq_item.id})

        elif action == 'edit':
            faq_item = FAQ.query.get(data.get('id'))
            if faq_item:
                faq_item.question = data.get('question')
                faq_item.answer = data.get('answer')
                faq_item.category = data.get('category')
                db.session.commit()
                return jsonify({'success': True})

        elif action == 'delete':
            faq_item = FAQ.query.get(data.get('id'))
            if faq_item:
                db.session.delete(faq_item)
                db.session.commit()
                return jsonify({'success': True})

    faqs = FAQ.query.all()
    return render_template('faq.html', faqs=faqs)


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


# ===================== TELEGRAM / BACKFILL / CLASSIFY APIs =====================

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


# ===================== SETTINGS =====================

@app.route('/admin/settings', methods=['GET', 'POST'])
@login_required
@admin_required
def settings():
    if request.method == 'POST':
        data = request.get_json()
        for key, value in data.items():
            setting = GeneralSetting.query.filter_by(key=key).first()
            if setting:
                setting.value = value
            else:
                setting = GeneralSetting(key=key, value=value)
            db.session.add(setting)
        db.session.commit()
        log_admin_action(
            'settings.save', 'setting', None, ', '.join(sorted(data.keys()))[:255],
            detail=f'{len(data)} key шинэчилсэн'
        )
        return jsonify({'success': True})

    settings_dict = {s.key: s.value for s in GeneralSetting.query.all()}
    return render_template('settings.html', settings=settings_dict)


# ===================== TRAINING =====================

@app.route('/admin/training', methods=['GET', 'POST'])
@login_required
@admin_required
def training():
    """Edit the bot's training corpus and persona. Only super_admin can save persona/base content."""
    if request.method == 'POST':
        if current_user.role != 'super_admin':
            flash('Бот персонал болон үндсэн сургалтын мэдээллийг зөвхөн супер админ засах боломжтой.', 'error')
            return redirect(url_for('training'))
        new_training = request.form.get('training_content', '').strip()
        new_persona = request.form.get('bot_persona', '').strip()

        for key, value in [('training_content', new_training), ('bot_persona', new_persona)]:
            row = GeneralSetting.query.filter_by(key=key).first()
            if row:
                row.value = value
            else:
                row = GeneralSetting(key=key, value=value)
                db.session.add(row)
        db.session.commit()
        log_admin_action(
            'training.save', 'setting', None, 'training_content + bot_persona',
            detail=f'Шинэ training_content урт: {len(new_training)} тэмдэгт; persona урт: {len(new_persona)} тэмдэгт'
        )
        flash(
            'Хадгаллаа. Дараагийн мессежээс эхлэн бот шинэ агуулгаар хариулна.',
            'success',
        )
        return redirect(url_for('training'))

    snippets = (TrainingSnippet.query
                .order_by(
                    db.case((TrainingSnippet.priority == 'high', 0), else_=1),
                    TrainingSnippet.sort_order.asc(),
                    TrainingSnippet.created_at.desc(),
                )
                .all())
    return render_template(
        'training.html',
        training_value=get_setting('training_content', ''),
        training_fallback=TRAINING_CONTENT,
        persona_value=get_setting('bot_persona', ''),
        persona_fallback=BOT_PERSONA,
        snippets=snippets,
    )


@app.route('/admin/training/snippets', methods=['POST'])
@login_required
@admin_required
def training_snippets():
    """Add / edit / delete / toggle one training snippet at a time."""
    data = request.get_json() or {}
    action = data.get('action')

    if action == 'add':
        snippet = TrainingSnippet(
            title=(data.get('title') or '').strip(),
            body=(data.get('body') or '').strip(),
            category=(data.get('category') or '').strip() or None,
            priority='high' if data.get('priority') == 'high' else 'normal',
            is_active=True,
        )
        if not snippet.title or not snippet.body:
            return jsonify({'success': False, 'error': 'Гарчиг болон агуулга шаардлагатай.'}), 400
        db.session.add(snippet)
        db.session.commit()
        return jsonify({'success': True, 'id': snippet.id})

    if action == 'edit':
        snippet = TrainingSnippet.query.get(data.get('id'))
        if not snippet:
            return jsonify({'success': False}), 404
        snippet.title = (data.get('title') or '').strip()
        snippet.body = (data.get('body') or '').strip()
        snippet.category = (data.get('category') or '').strip() or None
        snippet.priority = 'high' if data.get('priority') == 'high' else 'normal'
        if not snippet.title or not snippet.body:
            return jsonify({'success': False, 'error': 'Гарчиг болон агуулга шаардлагатай.'}), 400
        db.session.commit()
        return jsonify({'success': True})

    if action == 'toggle':
        snippet = TrainingSnippet.query.get(data.get('id'))
        if not snippet:
            return jsonify({'success': False}), 404
        snippet.is_active = not snippet.is_active
        db.session.commit()
        return jsonify({'success': True, 'is_active': snippet.is_active})

    if action == 'delete':
        snippet = TrainingSnippet.query.get(data.get('id'))
        if not snippet:
            return jsonify({'success': False}), 404
        db.session.delete(snippet)
        db.session.commit()
        return jsonify({'success': True})

    return jsonify({'success': False, 'error': 'unknown action'}), 400


# ===================== TEAM =====================

@app.route('/admin/team', methods=['GET', 'POST'])
@login_required
@admin_required
def team():
    if request.method == 'POST':
        data = request.get_json() or {}
        action = data.get('action')

        if action == 'add':
            member = TeamMember(
                name=(data.get('name') or '').strip(),
                role=(data.get('role') or '').strip() or None,
                specialty=(data.get('specialty') or '').strip() or None,
                bio=(data.get('bio') or '').strip() or None,
                is_active=True,
            )
            if not member.name:
                return jsonify({'success': False, 'error': 'Нэр шаардлагатай.'}), 400
            db.session.add(member)
            db.session.commit()
            return jsonify({'success': True, 'id': member.id})

        if action == 'edit':
            member = TeamMember.query.get(data.get('id'))
            if not member:
                return jsonify({'success': False}), 404
            member.name = (data.get('name') or '').strip()
            member.role = (data.get('role') or '').strip() or None
            member.specialty = (data.get('specialty') or '').strip() or None
            member.bio = (data.get('bio') or '').strip() or None
            if not member.name:
                return jsonify({'success': False, 'error': 'Нэр шаардлагатай.'}), 400
            db.session.commit()
            return jsonify({'success': True})

        if action == 'toggle':
            member = TeamMember.query.get(data.get('id'))
            if not member:
                return jsonify({'success': False}), 404
            member.is_active = not member.is_active
            db.session.commit()
            return jsonify({'success': True, 'is_active': member.is_active})

        if action == 'delete':
            member = TeamMember.query.get(data.get('id'))
            if not member:
                return jsonify({'success': False}), 404
            db.session.delete(member)
            db.session.commit()
            return jsonify({'success': True})

        return jsonify({'success': False, 'error': 'unknown action'}), 400

    members = (TeamMember.query
               .order_by(TeamMember.sort_order.asc(), TeamMember.id.asc())
               .all())
    return render_template('team.html', members=members)


# ===================== BUSINESS LINES =====================

@app.route('/admin/business-lines', methods=['GET', 'POST'])
@login_required
@admin_required
def business_lines():
    if request.method == 'POST':
        data = request.get_json() or {}
        action = data.get('action')

        if action in ('add', 'edit'):
            if action == 'add':
                line = BusinessLine(is_active=True)
                db.session.add(line)
            else:
                line = BusinessLine.query.get(data.get('id'))
                if not line:
                    return jsonify({'success': False}), 404
            line.name = (data.get('name') or '').strip()
            line.description = (data.get('description') or '').strip() or None
            line.action = 'answer' if data.get('line_action') == 'answer' else 'refer'
            line.contact_info = (data.get('contact_info') or '').strip() or None
            line.status_note = (data.get('status_note') or '').strip() or None
            line.address = (data.get('address') or '').strip() or None
            line.email = (data.get('email') or '').strip() or None
            line.signup_form_url = (data.get('signup_form_url') or '').strip() or None
            line.signup_phone = (data.get('signup_phone') or '').strip() or None
            line.exam_form_url = (data.get('exam_form_url') or '').strip() or None

            def _opt_int(field):
                raw = data.get(field)
                if raw in (None, '', 'null'):
                    return None
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    return 'invalid'

            for field in ('established_year', 'num_products_or_services',
                          'total_clients_or_users'):
                value = _opt_int(field)
                if value == 'invalid':
                    db.session.rollback()
                    return jsonify({'success': False,
                                    'error': f'{field} бүхэл тоо байх ёстой.'}), 400
                setattr(line, field, value)

            if not line.name:
                db.session.rollback()
                return jsonify({'success': False, 'error': 'Нэр шаардлагатай.'}), 400
            db.session.commit()
            return jsonify({'success': True, 'id': line.id})

        if action == 'toggle':
            line = BusinessLine.query.get(data.get('id'))
            if not line:
                return jsonify({'success': False}), 404
            line.is_active = not line.is_active
            line.status_note = (data.get('status_note') or '').strip() or None
            db.session.commit()
            log_admin_action(
                'business_line.toggle', 'business_line', line.id, line.name,
                detail=('Идэвхжүүлсэн' if line.is_active else 'Түр зогсоосон') +
                       (f". Тэмдэглэл: {line.status_note}" if line.status_note else '')
            )
            return jsonify({'success': True, 'is_active': line.is_active})

        if action == 'delete':
            line = BusinessLine.query.get(data.get('id'))
            if not line:
                return jsonify({'success': False}), 404
            label, lid = line.name, line.id
            db.session.delete(line)
            db.session.commit()
            log_admin_action('business_line.delete', 'business_line', lid, label, detail='Устгасан')
            return jsonify({'success': True})

        return jsonify({'success': False, 'error': 'unknown action'}), 400

    lines = (BusinessLine.query
             .order_by(BusinessLine.sort_order.asc(), BusinessLine.id.asc())
             .all())
    return render_template('business_lines.html', lines=lines)


# ===================== PRODUCTS =====================

def _parse_product_payload(data, existing=None):
    """Validate + coerce the JSON payload from the product modal. Returns
    (kwargs_for_product, links_list, error). The links_list replaces the
    Product's existing ProductLink rows wholesale on save."""
    name = (data.get('name') or '').strip()
    if not name:
        return None, None, 'Бүтээгдэхүүний нэр шаардлагатай.'

    bl_id = data.get('business_line_id')
    try:
        bl_id = int(bl_id)
    except (TypeError, ValueError):
        return None, None, 'business_line_id шаардлагатай.'
    if not BusinessLine.query.get(bl_id):
        return None, None, 'Сонгосон бизнесийн чиглэл олдсонгүй.'

    raw_number = data.get('product_number')
    if raw_number in (None, '', 'null'):
        return None, None, (
            'Бүтээгдэхүүний дугаар (product_number) шаардлагатай. '
            'Жишээ: 2001. Давтагдашгүй бүхэл тоо.'
        )
    try:
        product_number = int(raw_number)
    except (TypeError, ValueError):
        return None, None, 'product_number бүхэл тоо байх ёстой.'
    clash = Product.query.filter_by(product_number=product_number).first()
    if clash and (existing is None or clash.id != existing.id):
        return None, None, (
            f'#{product_number} дугаартай бүтээгдэхүүн аль хэдийн бүртгэлтэй '
            f'(id={clash.id}).'
        )

    raw_links = data.get('links') or []
    if not isinstance(raw_links, list):
        return None, None, 'links талбар жагсаалт байх ёстой.'
    links = []
    for i, link in enumerate(raw_links):
        kind = (link.get('kind') or '').strip()
        url = (link.get('url') or '').strip()
        if not kind and not url:
            continue  # let admin leave empty rows
        if not kind or not url:
            return None, None, f'Холбоос #{i+1}: kind болон URL хоёулаа шаардлагатай.'
        links.append({
            'kind': kind[:40],
            'label': (link.get('label') or '').strip()[:160] or None,
            'url': url[:500],
            'is_active': bool(link.get('is_active', True)),
            'sort_order': int(link.get('sort_order') or i),
        })

    return {
        'business_line_id': bl_id,
        'product_number': product_number,
        'name': name,
        'vendor': (data.get('vendor') or '').strip() or None,
        'description': (data.get('description') or '').strip() or None,
        'is_main_product': bool(data.get('is_main_product')),
    }, links, None


@app.route('/admin/products', methods=['GET', 'POST'])
@login_required
@admin_required
def products():
    """CRUD for Products and their child ProductLinks. One POST round-trip
    saves a Product plus its full link list — simpler than tracking link
    ids on the client. Toggle/delete are separate actions like Courses."""
    if request.method == 'POST':
        data = request.get_json() or {}
        action = data.get('action')

        if action == 'add':
            fields, links, err = _parse_product_payload(data)
            if err:
                return jsonify({'success': False, 'error': err}), 400
            product = Product(**fields)
            db.session.add(product)
            db.session.flush()
            for link in links:
                db.session.add(ProductLink(product_id=product.id, **link))
            db.session.commit()
            log_admin_action(
                'product.create', 'product', product.id, product.name,
                detail=f'#{product.product_number} нэмсэн ({len(links)} холбоос)'
            )
            return jsonify({'success': True, 'id': product.id})

        if action == 'edit':
            product = Product.query.get(data.get('id'))
            if not product:
                return jsonify({'success': False, 'error': 'Бүтээгдэхүүн олдсонгүй.'}), 404
            fields, links, err = _parse_product_payload(data, existing=product)
            if err:
                return jsonify({'success': False, 'error': err}), 400
            for key, value in fields.items():
                setattr(product, key, value)
            ProductLink.query.filter_by(product_id=product.id).delete()
            for link in links:
                db.session.add(ProductLink(product_id=product.id, **link))
            db.session.commit()
            log_admin_action(
                'product.edit', 'product', product.id, product.name,
                detail=f'Засварласан ({len(links)} холбоос)'
            )
            return jsonify({'success': True})

        if action == 'toggle':
            product = Product.query.get(data.get('id'))
            if not product:
                return jsonify({'success': False}), 404
            product.is_active = not product.is_active
            product.status_note = (data.get('status_note') or '').strip() or None
            db.session.commit()
            log_admin_action(
                'product.toggle', 'product', product.id, product.name,
                detail=('Идэвхжүүлсэн' if product.is_active else 'Түр зогсоосон') +
                       (f". Тэмдэглэл: {product.status_note}" if product.status_note else '')
            )
            return jsonify({'success': True, 'is_active': product.is_active})

        if action == 'delete':
            product = Product.query.get(data.get('id'))
            if not product:
                return jsonify({'success': False}), 404
            label, pid = product.name, product.id
            db.session.delete(product)
            db.session.commit()
            log_admin_action('product.delete', 'product', pid, label, detail='Устгасан')
            return jsonify({'success': True})

        return jsonify({'success': False, 'error': 'unknown action'}), 400

    all_products = (Product.query
                    .options(joinedload(Product.business_line),
                             joinedload(Product.links))
                    .order_by(Product.business_line_id.asc(),
                              Product.sort_order.asc(),
                              Product.id.asc())
                    .all())
    # Pre-serialize each product's links to a plain dict list. Jinja2's dict
    # literal can't contain a Python list comprehension, so building the
    # data-product JSON inline in the template fails to parse.
    for p in all_products:
        p._links_json = [
            {
                'kind': l.kind,
                'label': l.label or '',
                'url': l.url,
                'is_active': bool(l.is_active),
                'sort_order': l.sort_order,
            }
            for l in p.links
        ]
    lines = BusinessLine.query.order_by(BusinessLine.sort_order.asc(),
                                        BusinessLine.id.asc()).all()
    return render_template('products.html', products=all_products, business_lines=lines)


# ===================== TRAINING PREVIEW + LINT =====================

@app.route('/admin/training/preview')
@login_required
@admin_required
def training_preview():
    """Return the fully-assembled system prompt the AI sees right now.
    Lets admins spot bad data (duplicate courses, contradictory FAQs,
    stale snippets) without having to ping the Messenger bot for real."""
    return jsonify({
        'prompt': build_system_prompt(
            session_state='new',
            funnel_stage='curious',
            user_first_name='',
        ),
    })


@app.route('/admin/training/lint')
@login_required
@admin_required
def training_lint():
    """Run the heuristic consistency check and return findings."""
    return jsonify({'findings': lint_training_data()})


# ===================== HANDOFF KEYWORDS =====================

@app.route('/admin/handoff-keywords', methods=['GET', 'POST'])
@login_required
@admin_required
def handoff_keywords():
    if request.method == 'POST':
        data = request.get_json() or {}
        action = data.get('action')

        if action in ('add', 'edit'):
            if action == 'add':
                kw = HandoffKeyword()
                db.session.add(kw)
            else:
                kw = HandoffKeyword.query.get(data.get('id'))
                if not kw:
                    return jsonify({'success': False}), 404
            kw.keyword = (data.get('keyword') or '').strip().lower()
            kw.keyword_type = 'frustration' if data.get('keyword_type') == 'frustration' else 'explicit'
            kw.note = (data.get('note') or '').strip() or None
            if not kw.keyword:
                db.session.rollback()
                return jsonify({'success': False, 'error': 'Keyword шаардлагатай.'}), 400
            db.session.commit()
            return jsonify({'success': True, 'id': kw.id})

        if action == 'toggle':
            kw = HandoffKeyword.query.get(data.get('id'))
            if not kw:
                return jsonify({'success': False}), 404
            kw.is_active = not kw.is_active
            db.session.commit()
            return jsonify({'success': True, 'is_active': kw.is_active})

        if action == 'delete':
            kw = HandoffKeyword.query.get(data.get('id'))
            if not kw:
                return jsonify({'success': False}), 404
            db.session.delete(kw)
            db.session.commit()
            return jsonify({'success': True})

        return jsonify({'success': False, 'error': 'unknown action'}), 400

    keywords = HandoffKeyword.query.order_by(
        HandoffKeyword.keyword_type.asc(),
        HandoffKeyword.keyword.asc(),
    ).all()
    return render_template('handoff_keywords.html', keywords=keywords)


# ===================== DOCS =====================

@app.route('/admin/docs')
@login_required
@admin_required
def docs():
    return render_template('docs.html')


# ===================== AUDIT LOG =====================

@app.route('/admin/audit-log')
@login_required
@admin_required
def audit_log():
    page_size = 100
    try:
        offset = max(0, int(request.args.get('offset', 0)))
    except (TypeError, ValueError):
        offset = 0
    entries = (AuditEntry.query
               .order_by(AuditEntry.created_at.desc())
               .offset(offset)
               .limit(page_size + 1)
               .all())
    has_more = len(entries) > page_size
    entries = entries[:page_size]
    total = AuditEntry.query.count()
    return render_template(
        'audit_log.html',
        entries=entries,
        offset=offset,
        page_size=page_size,
        has_more=has_more,
        total=total,
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


# ===================== SERVICES =====================

@app.route('/admin/services', methods=['GET', 'POST'])
@login_required
@admin_required
def services():
    if request.method == 'POST':
        data = request.get_json() or {}
        action = data.get('action')

        if action in ('add', 'edit'):
            if action == 'add':
                item = Service(is_active=True)
                db.session.add(item)
            else:
                item = Service.query.get(data.get('id'))
                if not item:
                    return jsonify({'success': False}), 404
            item.name = (data.get('name') or '').strip()
            item.description = (data.get('description') or '').strip() or None
            price_raw = data.get('price')
            item.price = float(price_raw) if price_raw not in (None, '') else None
            item.duration = (data.get('duration') or '').strip() or None
            if not item.name:
                db.session.rollback()
                return jsonify({'success': False, 'error': 'Нэр шаардлагатай.'}), 400
            db.session.commit()
            return jsonify({'success': True, 'id': item.id})

        if action == 'toggle':
            item = Service.query.get(data.get('id'))
            if not item:
                return jsonify({'success': False}), 404
            item.is_active = not item.is_active
            item.status_note = (data.get('status_note') or '').strip() or None
            db.session.commit()
            log_admin_action(
                'service.toggle', 'service', item.id, item.name,
                detail=('Идэвхжүүлсэн' if item.is_active else 'Түр зогсоосон') +
                       (f". Тэмдэглэл: {item.status_note}" if item.status_note else '')
            )
            return jsonify({'success': True, 'is_active': item.is_active})

        if action == 'delete':
            item = Service.query.get(data.get('id'))
            if not item:
                return jsonify({'success': False}), 404
            label, sid = item.name, item.id
            db.session.delete(item)
            db.session.commit()
            log_admin_action('service.delete', 'service', sid, label, detail='Устгасан')
            return jsonify({'success': True})

        return jsonify({'success': False, 'error': 'unknown action'}), 400

    items = (Service.query
             .order_by(Service.sort_order.asc(), Service.id.asc())
             .all())
    return render_template('services.html', items=items)


# ===================== SOFTWARE =====================

@app.route('/admin/software', methods=['GET', 'POST'])
@login_required
@admin_required
def software():
    if request.method == 'POST':
        data = request.get_json() or {}
        action = data.get('action')

        if action in ('add', 'edit'):
            if action == 'add':
                item = Software(is_active=True)
                db.session.add(item)
            else:
                item = Software.query.get(data.get('id'))
                if not item:
                    return jsonify({'success': False}), 404
            item.name = (data.get('name') or '').strip()
            item.description = (data.get('description') or '').strip() or None
            price_raw = data.get('price')
            item.price = float(price_raw) if price_raw not in (None, '') else None
            item.vendor = (data.get('vendor') or '').strip() or None
            if not item.name:
                db.session.rollback()
                return jsonify({'success': False, 'error': 'Нэр шаардлагатай.'}), 400
            db.session.commit()
            return jsonify({'success': True, 'id': item.id})

        if action == 'toggle':
            item = Software.query.get(data.get('id'))
            if not item:
                return jsonify({'success': False}), 404
            item.is_active = not item.is_active
            item.status_note = (data.get('status_note') or '').strip() or None
            db.session.commit()
            log_admin_action(
                'software.toggle', 'software', item.id, item.name,
                detail=('Идэвхжүүлсэн' if item.is_active else 'Түр зогсоосон') +
                       (f". Тэмдэглэл: {item.status_note}" if item.status_note else '')
            )
            return jsonify({'success': True, 'is_active': item.is_active})

        if action == 'delete':
            item = Software.query.get(data.get('id'))
            if not item:
                return jsonify({'success': False}), 404
            label, sid = item.name, item.id
            db.session.delete(item)
            db.session.commit()
            log_admin_action('software.delete', 'software', sid, label, detail='Устгасан')
            return jsonify({'success': True})

        return jsonify({'success': False, 'error': 'unknown action'}), 400

    items = (Software.query
             .order_by(Software.sort_order.asc(), Software.id.asc())
             .all())
    return render_template('software.html', items=items)


# ===================== ADMIN USER MANAGEMENT =====================

@app.route('/admin/admins', methods=['GET'])
@login_required
@admin_required
def admins():
    all_admins = User.query.order_by(User.created_at.asc()).all()
    return render_template(
        'admins.html',
        admins=all_admins,
        min_password_length=MIN_ADMIN_PASSWORD_LENGTH,
    )


@app.route('/admin/admins/create', methods=['POST'])
@login_required
@super_admin_required
def create_admin():
    username = (request.form.get('username') or '').strip()
    email = (request.form.get('email') or '').strip()
    password = request.form.get('password') or ''
    make_super = request.form.get('make_super') == 'on'

    if not username or not email or not password:
        flash('Бүх талбарыг бөглөнө үү.', 'error')
        return redirect(url_for('admins'))

    if len(password) < MIN_ADMIN_PASSWORD_LENGTH:
        flash(
            f'Нууц үг хамгийн багадаа {MIN_ADMIN_PASSWORD_LENGTH} тэмдэгт байх ёстой.',
            'error',
        )
        return redirect(url_for('admins'))

    if User.query.filter_by(username=username).first():
        flash(f'"{username}" нэртэй админ аль хэдийн бүртгэлтэй байна.', 'error')
        return redirect(url_for('admins'))

    if User.query.filter_by(email=email).first():
        flash(f'"{email}" имэйл хаяг аль хэдийн бүртгэлтэй байна.', 'error')
        return redirect(url_for('admins'))

    new_admin = User(
        username=username,
        email=email,
        password=generate_password_hash(password),
        role='super_admin' if make_super else 'admin',
    )
    db.session.add(new_admin)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash('Админ нэмэхэд алдаа гарлаа. Дахин оролдоно уу.', 'error')
        return redirect(url_for('admins'))

    role_label = 'супер админ' if make_super else 'админ'
    log_admin_action(
        'admin.create', 'user', new_admin.id, new_admin.username,
        detail=f'Шинэ {role_label} нэмсэн'
    )
    flash(f'"{username}" {role_label}-ыг амжилттай нэмлээ.', 'success')
    return redirect(url_for('admins'))


@app.route('/admin/admins/<int:admin_id>/delete', methods=['POST'])
@login_required
@super_admin_required
def delete_admin(admin_id):
    target = User.query.get(admin_id)
    if not target:
        flash('Тухайн админ олдсонгүй.', 'error')
        return redirect(url_for('admins'))

    if target.id == current_user.id:
        flash('Та өөрийнхөө бүртгэлийг устгаж болохгүй.', 'error')
        return redirect(url_for('admins'))

    if User.query.count() <= 1:
        flash('Сүүлийн админыг устгах боломжгүй.', 'error')
        return redirect(url_for('admins'))

    if target.role == 'super_admin' and User.query.filter_by(role='super_admin').count() <= 1:
        flash('Сүүлийн супер админыг устгах боломжгүй.', 'error')
        return redirect(url_for('admins'))

    username = target.username
    tid = target.id
    db.session.delete(target)
    db.session.commit()
    log_admin_action('admin.delete', 'user', tid, username, detail='Админыг устгасан')
    flash(f'"{username}" админыг устгалаа.', 'success')
    return redirect(url_for('admins'))


@app.route('/admin/admins/change-password', methods=['POST'])
@login_required
@admin_required
def change_my_password():
    current_password = request.form.get('current_password') or ''
    new_password = request.form.get('new_password') or ''
    confirm_password = request.form.get('confirm_password') or ''

    if not check_password_hash(current_user.password, current_password):
        flash('Одоогийн нууц үг буруу байна.', 'error')
        return redirect(url_for('admins'))

    if len(new_password) < MIN_ADMIN_PASSWORD_LENGTH:
        flash(
            f'Шинэ нууц үг хамгийн багадаа {MIN_ADMIN_PASSWORD_LENGTH} тэмдэгт байх ёстой.',
            'error',
        )
        return redirect(url_for('admins'))

    if new_password != confirm_password:
        flash('Шинэ нууц үг таарахгүй байна.', 'error')
        return redirect(url_for('admins'))

    if check_password_hash(current_user.password, new_password):
        flash('Шинэ нууц үг хуучин нууц үгтэй ижил байж болохгүй.', 'error')
        return redirect(url_for('admins'))

    current_user.password = generate_password_hash(new_password)
    db.session.commit()
    log_admin_action(
        'admin.change_own_password', 'user', current_user.id, current_user.username,
        detail='Өөрийн нууц үгийг сольсон'
    )
    logout_user()
    flash('Нууц үг амжилттай солигдлоо. Шинэ нууц үгээрээ нэвтэрнэ үү.', 'success')
    return redirect(url_for('login'))


@app.route('/admin/admins/<int:admin_id>/reset-password', methods=['POST'])
@login_required
@super_admin_required
def reset_admin_password(admin_id):
    target = User.query.get(admin_id)
    if not target:
        flash('Тухайн админ олдсонгүй.', 'error')
        return redirect(url_for('admins'))

    new_password = request.form.get('new_password') or ''
    if len(new_password) < MIN_ADMIN_PASSWORD_LENGTH:
        flash(
            f'Шинэ нууц үг хамгийн багадаа {MIN_ADMIN_PASSWORD_LENGTH} тэмдэгт байх ёстой.',
            'error',
        )
        return redirect(url_for('admins'))

    target.password = generate_password_hash(new_password)
    db.session.commit()
    log_admin_action(
        'admin.reset_password', 'user', target.id, target.username,
        detail='Супер админ нууц үгийг шинэчилсэн'
    )
    flash(
        f'"{target.username}"-ийн нууц үгийг шинэчиллээ. Шинэ нууц үгийг тухайн хэрэглэгчид өгнө үү.',
        'success',
    )
    return redirect(url_for('admins'))


@app.route('/admin/admins/<int:admin_id>/toggle-role', methods=['POST'])
@login_required
@super_admin_required
def toggle_admin_role(admin_id):
    target = User.query.get(admin_id)
    if not target:
        flash('Тухайн админ олдсонгүй.', 'error')
        return redirect(url_for('admins'))

    if target.id == current_user.id:
        flash('Та өөрийнхөө эрхийг өөрчилж болохгүй. Өөр супер админаар өөрчлүүл.', 'error')
        return redirect(url_for('admins'))

    if target.role == 'super_admin':
        if User.query.filter_by(role='super_admin').count() <= 1:
            flash('Сүүлийн супер админыг буулгах боломжгүй.', 'error')
            return redirect(url_for('admins'))
        target.role = 'admin'
        msg = f'"{target.username}"-ыг энгийн админ болголоо.'
        new_role = 'admin'
    else:
        target.role = 'super_admin'
        msg = f'"{target.username}"-ыг супер админ болголоо.'
        new_role = 'super_admin'

    db.session.commit()
    log_admin_action(
        'admin.toggle_role', 'user', target.id, target.username,
        detail=f'Эрх → {new_role}'
    )
    flash(msg, 'success')
    return redirect(url_for('admins'))
