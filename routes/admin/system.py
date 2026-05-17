"""System administration: settings, audit log, docs, admin user management.

Phase 2 will redistribute settings fields across the new Business Management
and Bot Management sections, and Phase 6 will delete this /admin/settings
page entirely. Admin user management stays in the System zone.
"""
from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required, logout_user
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from app import app
from auth import admin_required, super_admin_required
from extensions import db
from models import AuditEntry, GeneralSetting, User
from services import log_admin_action


MIN_ADMIN_PASSWORD_LENGTH = 12


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
