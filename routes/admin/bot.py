"""Bot brain: AI training, training snippets, handoff keywords, FAQ.

Phase 5 reorganizes these under a Bot Management section with four tabs:
AI Training, Handover Keywords, FAQs, and Bot Settings. The legacy item
URLs (/admin/training, /admin/handoff-keywords, /admin/faq, /admin/settings)
remain alive as POST endpoints that the new tabs target; Phase 6 may
collapse them into the new section URLs.
"""
from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import app
from auth import admin_required, super_admin_required
from extensions import db
from models import (ChatQuestionCluster, FAQ, GeneralSetting, HandoffKeyword,
                    TrainingSnippet)
from services import (BOT_PERSONA, TRAINING_CONTENT, build_system_prompt,
                      cluster_chat_questions, get_setting, lint_training_data,
                      log_admin_action)


# Settings keys that belong on the Bot Settings tab. The other half of the
# old Settings page (center_*, main_office_*, google_form_url) lives on
# Business Management > General Information.
BOT_SETTINGS_KEYS = (
    'training_comment',
    'celebration_comment',
    'default_comment',
    'handoff_sensitivity',
    'mute_duration_hours',
    'office_hours_start',
    'office_hours_end',
    'telegram_chat_ids',
    'sound_alerts_enabled',
    'hot_prospect_stages',
    'handoff_message',
    'bot_welcome',
)


# ===================== BOT MANAGEMENT — tab landing pages =====================

@app.route('/bot-management')
@login_required
@admin_required
def bot_management():
    # Lands on Bot Settings tab — admins tune handoff sensitivity /
    # Telegram alerts / office hours more often than they retrain the
    # persona, so the most-touched tab is the most useful default.
    return redirect(url_for('bot_management_settings'))


@app.route('/bot-management/training')
@login_required
@admin_required
def bot_management_training():
    """AI Training tab. Renders the same content as /admin/training but
    inside the new Bot Management layout. Saves still POST to /admin/training
    so the existing handler stays the single source of truth."""
    # Reuses templates/training.html — the same template the legacy
    # /admin/training route renders. The shared template conditionally
    # shows the bot-management tab nav at the top when `active_tab` is set.
    snippets = (TrainingSnippet.query
                .order_by(
                    db.case((TrainingSnippet.priority == 'high', 0), else_=1),
                    TrainingSnippet.sort_order.asc(),
                    TrainingSnippet.created_at.desc(),
                )
                .all())
    return render_template(
        'training.html',
        active_tab='training',
        training_value=get_setting('training_content', ''),
        training_fallback=TRAINING_CONTENT,
        persona_value=get_setting('bot_persona', ''),
        persona_fallback=BOT_PERSONA,
        snippets=snippets,
    )


@app.route('/bot-management/handover')
@login_required
@admin_required
def bot_management_handover():
    keywords = HandoffKeyword.query.order_by(
        HandoffKeyword.keyword_type.asc(),
        HandoffKeyword.keyword.asc(),
    ).all()
    return render_template(
        'handoff_keywords.html',
        active_tab='handover',
        keywords=keywords,
    )


@app.route('/bot-management/faqs')
@login_required
@admin_required
def bot_management_faqs():
    """FAQs tab. Two sections:
      1) Curated FAQs — admin-authored Q/A pairs (existing FAQ model).
      2) From Chat — LLM-clustered themes from real user messages.
         Phase 5b populates `chat_clusters`. Empty list means clustering
         hasn't been run yet (or the model found no recurring themes).
    """
    import json as _json
    faqs = FAQ.query.all()
    raw_clusters = (ChatQuestionCluster.query
                    .order_by(ChatQuestionCluster.promoted_to_faq_id.is_(None).desc(),
                              ChatQuestionCluster.count.desc(),
                              ChatQuestionCluster.id.desc())
                    .all())
    # Pre-decode the sample_questions JSON for the template.
    chat_clusters = []
    for c in raw_clusters:
        try:
            samples = _json.loads(c.sample_questions or '[]')
            if not isinstance(samples, list):
                samples = []
        except Exception:
            samples = []
        chat_clusters.append({
            'id': c.id,
            'title': c.title,
            'representative_question': c.representative_question,
            'samples': samples,
            'count': c.count,
            'last_seen_at': c.last_seen_at,
            'promoted_to_faq_id': c.promoted_to_faq_id,
        })
    return render_template(
        'faq.html',
        active_tab='faqs',
        faqs=faqs,
        chat_clusters=chat_clusters,
    )


@app.route('/admin/api/cluster-chat-questions', methods=['POST'])
@login_required
@admin_required
def cluster_chat_questions_now():
    """Manual trigger for the LLM clustering job. Lets admins refresh the
    'From Chat' section without waiting for the weekly cron. Returns the
    number of clusters written."""
    written = cluster_chat_questions()
    return jsonify({'success': True, 'clusters_written': written})


@app.route('/admin/api/promote-cluster-to-faq', methods=['POST'])
@login_required
@admin_required
def promote_cluster_to_faq():
    """Turn a chat-question cluster into a curated FAQ row. The cluster
    keeps its row (with promoted_to_faq_id set) so we know not to surface
    it again on the next clustering run."""
    data = request.get_json() or {}
    cluster = db.session.get(ChatQuestionCluster, data.get('id'))
    if not cluster:
        return jsonify({'success': False, 'error': 'Cluster олдсонгүй.'}), 404
    answer = (data.get('answer') or '').strip()
    if not answer:
        return jsonify({'success': False, 'error': 'Хариулт шаардлагатай.'}), 400
    faq_row = FAQ(
        question=cluster.representative_question,
        answer=answer,
        category=cluster.title[:100],
    )
    db.session.add(faq_row)
    db.session.flush()
    cluster.promoted_to_faq_id = faq_row.id
    db.session.commit()
    log_admin_action(
        'faq.create', 'faq', faq_row.id, cluster.title,
        detail=f'Chat кластерээс FAQ болгож хадгалав (cluster_id={cluster.id})'
    )
    return jsonify({'success': True, 'faq_id': faq_row.id})


@app.route('/bot-management/settings', methods=['GET', 'POST'])
@login_required
@admin_required
def bot_management_settings():
    """Bot Settings tab. Saves the bot-config subset of GeneralSetting."""
    if request.method == 'POST':
        data = request.get_json() or {}
        allowed = {k: v for k, v in data.items() if k in BOT_SETTINGS_KEYS}
        for key, value in allowed.items():
            row = GeneralSetting.query.filter_by(key=key).first()
            if row:
                row.value = value
            else:
                row = GeneralSetting(key=key, value=value)
                db.session.add(row)
        db.session.commit()
        log_admin_action(
            'settings.save', 'setting', None, ', '.join(sorted(allowed.keys()))[:255],
            detail=f'{len(allowed)} bot-settings key шинэчилсэн'
        )
        return jsonify({'success': True, 'saved': sorted(allowed.keys())})

    settings_dict = {
        s.key: s.value for s in GeneralSetting.query.filter(
            GeneralSetting.key.in_(BOT_SETTINGS_KEYS)
        ).all()
    }
    return render_template(
        'bot/settings.html',
        active_tab='settings',
        settings=settings_dict,
    )


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
            faq_item = db.session.get(FAQ, data.get('id'))
            if faq_item:
                faq_item.question = data.get('question')
                faq_item.answer = data.get('answer')
                faq_item.category = data.get('category')
                db.session.commit()
                return jsonify({'success': True})

        elif action == 'delete':
            faq_item = db.session.get(FAQ, data.get('id'))
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
        snippet = db.session.get(TrainingSnippet, data.get('id'))
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
        snippet = db.session.get(TrainingSnippet, data.get('id'))
        if not snippet:
            return jsonify({'success': False}), 404
        snippet.is_active = not snippet.is_active
        db.session.commit()
        return jsonify({'success': True, 'is_active': snippet.is_active})

    if action == 'delete':
        snippet = db.session.get(TrainingSnippet, data.get('id'))
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
                kw = db.session.get(HandoffKeyword, data.get('id'))
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
            kw = db.session.get(HandoffKeyword, data.get('id'))
            if not kw:
                return jsonify({'success': False}), 404
            kw.is_active = not kw.is_active
            db.session.commit()
            return jsonify({'success': True, 'is_active': kw.is_active})

        if action == 'delete':
            kw = db.session.get(HandoffKeyword, data.get('id'))
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
