"""System administration: settings, audit log, docs, admin user management.

Phase 2 will redistribute settings fields across the new Business Management
and Bot Management sections, and Phase 6 will delete this /admin/settings
page entirely. Admin user management stays in the System zone.
"""
from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required, logout_user
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from app import app, MIN_ADMIN_PASSWORD_LENGTH
from auth import admin_required, super_admin_required
from extensions import db
from models import AuditEntry, GeneralSetting, User
from services import log_admin_action


# ===================== SETTINGS =====================

# ===================== MANUAL DB MIGRATION TRIGGER =====================
# Lets a logged-in admin re-run the pending-migrations script on demand
# and see its output inline. Useful when the auto-bootstrap on boot ran
# into a partial failure and the operator wants to repair the schema
# without redeploying. Returns plain text with the script's stdout so
# the result is readable in the browser without a template.

@app.route('/admin/api/run-migration', methods=['POST'])
@login_required
@admin_required
def run_migration_now():
    import io
    import sys
    import os
    from flask import Response

    db_uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    if not db_uri.startswith('sqlite:'):
        return Response(
            f"DB URI is not sqlite ({db_uri[:40]}...), this endpoint only supports SQLite.\n",
            mimetype='text/plain; charset=utf-8',
        )

    try:
        db_path = str(db.engine.url.database)
    except Exception as e:
        return Response(
            f"Failed to resolve DB path: {e}\n",
            status=500, mimetype='text/plain; charset=utf-8',
        )

    if not db_path or not os.path.exists(db_path):
        return Response(
            f"DB file not found at {repr(db_path)}\n",
            status=500, mimetype='text/plain; charset=utf-8',
        )

    # Capture stdout from the migration script so the admin can see what
    # ran (or what failed) without checking Render logs.
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    error_text = ''
    try:
        from scripts.apply_phase_1_migration import apply_migration
        rc = apply_migration(db_path)
    except Exception as e:
        import traceback
        rc = -1
        error_text = (
            f"\n\n!!! Migration raised an exception !!!\n"
            f"{type(e).__name__}: {e}\n\n"
            f"{traceback.format_exc()}"
        )
    finally:
        sys.stdout = old_stdout

    log_admin_action(
        'system.run_migration', 'system', None, db_path,
        detail=f'Migration script triggered manually (rc={rc})'
    )
    body = (
        f"=== Manual migration run on {db_path} ===\n"
        f"Return code: {rc}\n\n"
        f"--- Script output ---\n"
        f"{buf.getvalue()}"
        f"{error_text}"
        f"\n--- End ---\n"
    )
    return Response(body, mimetype='text/plain; charset=utf-8')


# ===================== SEED DEFAULT LINKS =====================
# One-shot admin trigger to wire Magic Financial Group's known set of
# product / service / course URLs into the catalog (program download,
# manual, support ticket, Office license form, audit form, tax-report
# form, course registration form).
#
# Re-runnable: matches existing rows by URL, updating description + note
# in place. Rerun after the team rewords a link to push the new wording
# to chat without admins re-editing each row. Also one-shot rewords the
# Magic Finance product description when it still matches a known default.

@app.route('/admin/api/seed-discovery-snippets', methods=['POST'])
@login_required
@admin_required
def seed_discovery_snippets_now():
    """Adds 7 high/normal-priority training snippets that map common
    Mongolian (Cyrillic + Latin) phrasings to the right service / product
    / course. Makes the bot's routing robust against phrasing variation
    without admins having to enrich every service description by hand."""
    from flask import Response
    from services import seed_discovery_phrasing_snippets
    try:
        report = seed_discovery_phrasing_snippets()
    except Exception as e:
        import traceback
        return Response(
            f"seed_discovery_phrasing_snippets() raised:\n{type(e).__name__}: {e}\n\n"
            f"{traceback.format_exc()}",
            status=500, mimetype='text/plain; charset=utf-8',
        )
    log_admin_action(
        'system.seed_discovery_snippets', 'system', None, current_user.username,
        detail='Discovery-phrasing training snippets seeded'
    )
    return Response(
        f"=== Discovery-phrasing snippets seeder ===\n"
        f"Triggered by: {current_user.username}\n\n"
        f"{report}\n\n--- Done ---\n",
        mimetype='text/plain; charset=utf-8',
    )


@app.route('/admin/api/seed-default-links', methods=['POST'])
@login_required
@admin_required
def seed_default_links_now():
    from flask import Response
    from services import seed_default_magic_links
    try:
        report = seed_default_magic_links()
    except Exception as e:
        import traceback
        return Response(
            f"seed_default_magic_links() raised:\n{type(e).__name__}: {e}\n\n"
            f"{traceback.format_exc()}",
            status=500, mimetype='text/plain; charset=utf-8',
        )
    log_admin_action(
        'system.seed_default_links', 'system', None, current_user.username,
        detail='Magic-defaults default link map seeded into catalog'
    )
    body = (
        f"=== Magic default links seeder ===\n"
        f"Triggered by: {current_user.username}\n\n"
        f"{report}\n\n"
        f"--- Done ---\n"
        f"Tip: any 'SKIPPED' lines above mean the matching item doesn't "
        f"exist yet. Create it via the admin panel (Бизнесийн удирдлага -> "
        f"the right unit -> add product/service), then re-run this endpoint."
    )
    return Response(body, mimetype='text/plain; charset=utf-8')


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

    # Legacy GET — settings.html was removed in Phase 6. The fields
    # split across the new Business Management / Bot Settings pages,
    # so redirect to the General Information tab as the closest match.
    return redirect(url_for('business_management_general'))


# ===================== DOCS =====================

@app.route('/admin/docs')
@login_required
@admin_required
def docs():
    return render_template('docs.html')


# ===================== DEFAULTS =====================
# UI surface for the seed-default-links and seed-discovery-snippets
# endpoints. Lets non-technical admins push the canonical link map and
# routing snippets into the catalog with a single button click instead
# of curl + an admin URL they need to know.

@app.route('/admin/defaults')
@login_required
@admin_required
def system_defaults():
    return render_template('defaults.html')


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
    target = db.session.get(User, admin_id)
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
    target = db.session.get(User, admin_id)
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
    target = db.session.get(User, admin_id)
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
