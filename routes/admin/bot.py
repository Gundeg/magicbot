"""Bot brain: AI training, training snippets, handoff keywords, FAQ.

Phase 5 will reorganize these under a new Bot Management section with tabs
(AI Training / Handover Keywords / FAQs / Bot Settings) and add a weekly
chat-question clustering job that surfaces FAQ candidates from real chat.
"""
from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import app
from auth import admin_required
from extensions import db
from models import FAQ, GeneralSetting, HandoffKeyword, TrainingSnippet
from services import (BOT_PERSONA, TRAINING_CONTENT, build_system_prompt,
                      get_setting, lint_training_data, log_admin_action)


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
