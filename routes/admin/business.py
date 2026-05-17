"""Business catalog: business lines, courses, services, products, team.

Phase 4 organizes these under a Business Management section with three tabs
(General Information / Business Units / Employees) plus a per-BU drill-in
page at /business-units/<id>. The drill-in renders an items grid whose form
schema is chosen by the BU's `product_type` (Course | Service | Product).
Every item type uses the same multi-link editor (description / url / note).

Legacy URLs (/admin/business-lines, /admin/courses, /admin/services,
/admin/products, /admin/team) remain as redirects or compatibility views
until Phase 6 deletes them.
"""
from datetime import datetime

from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy.orm import joinedload

from app import app
from auth import admin_required
from extensions import db
from models import (BusinessLine, Course, CourseLink, GeneralSetting, Product,
                    ProductLink, Service, ServiceLink, TeamMember)
from services import (ALLOWED_COURSE_TYPES, SELF_PACED_COURSE_TYPE,
                      advance_recurring_courses, archive_past_courses,
                      log_admin_action)


def _parse_links_payload(raw_links):
    """Validate the `links` array from any item modal (Course / Service /
    Product). Returns (cleaned_links, error_message_or_None). Empty rows
    (no description AND no URL) are silently dropped so the admin can leave
    a half-finished row without breaking the save."""
    if raw_links is None:
        return [], None
    if not isinstance(raw_links, list):
        return None, 'links талбар жагсаалт байх ёстой.'
    cleaned = []
    for i, link in enumerate(raw_links):
        description = (link.get('description') or '').strip()
        url = (link.get('url') or '').strip()
        if not description and not url:
            continue
        if not description or not url:
            return None, f'Холбоос #{i+1}: description болон URL хоёулаа шаардлагатай.'
        cleaned.append({
            'description': description[:200],
            'url': url[:500],
            'note': (link.get('note') or '').strip() or None,
            'is_active': bool(link.get('is_active', True)),
            'sort_order': int(link.get('sort_order') or i),
        })
    return cleaned, None


# Settings keys that belong on the General Information tab. Phase 2 of the
# admin IA reorg redistributed Settings; this is the Business Management
# half. Bot config keys (handoff, hours, telegram) live in Bot Settings.
BUSINESS_GENERAL_KEYS = (
    'center_name',
    'center_description',
    'center_phone',
    'center_email',
    'center_address',
    'main_office_address',
    'main_office_phone',
    'google_form_url',
)


# ===================== BUSINESS MANAGEMENT — tab landing pages =====================

@app.route('/business-management')
@login_required
@admin_required
def business_management():
    return redirect(url_for('business_management_general'))


@app.route('/business-management/general', methods=['GET', 'POST'])
@login_required
@admin_required
def business_management_general():
    if request.method == 'POST':
        data = request.get_json() or {}
        # Reject keys that don't belong on this tab so a stale or malicious
        # client can't dump arbitrary settings via the General form.
        allowed = {k: v for k, v in data.items() if k in BUSINESS_GENERAL_KEYS}
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
            detail=f'{len(allowed)} business-general key шинэчилсэн'
        )
        return jsonify({'success': True, 'saved': sorted(allowed.keys())})

    settings_dict = {
        s.key: s.value for s in GeneralSetting.query.filter(
            GeneralSetting.key.in_(BUSINESS_GENERAL_KEYS)
        ).all()
    }
    return render_template(
        'business/general.html',
        active_tab='general',
        settings=settings_dict,
        keys=BUSINESS_GENERAL_KEYS,
    )


@app.route('/business-management/units', methods=['GET', 'POST'])
@login_required
@admin_required
def business_management_units():
    """List + CRUD for Business Units. Click a row's name to drill into
    /business-units/<id> for that unit's items."""
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
            pt = (data.get('product_type') or 'Product').strip()
            if pt not in ('Course', 'Service', 'Product'):
                db.session.rollback()
                return jsonify({'success': False,
                                'error': 'product_type Course/Service/Product байх ёстой.'}), 400
            line.product_type = pt
            raw_year = data.get('established_year')
            if raw_year in (None, '', 'null'):
                line.established_year = None
            else:
                try:
                    line.established_year = int(raw_year)
                except (TypeError, ValueError):
                    db.session.rollback()
                    return jsonify({'success': False,
                                    'error': 'established_year бүхэл тоо байх ёстой.'}), 400
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
    # Compute per-BU item counts for the row summary. Until Course/Service
    # gain a business_line_id FK (deferred), we use product_type as the
    # filter: a Course-typed BU's count is the total number of Courses.
    counts = {
        'Course': Course.query.count(),
        'Service': Service.query.count(),
    }
    return render_template(
        'business/units.html',
        active_tab='units',
        lines=lines,
        item_counts=counts,
    )


@app.route('/business-management/employees', methods=['GET', 'POST'])
@login_required
@admin_required
def business_management_employees():
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
    return render_template(
        'business/employees.html',
        active_tab='employees',
        members=members,
    )


# ===================== BU drill-in page =====================

@app.route('/business-units/<int:bu_id>', methods=['GET'])
@login_required
@admin_required
def business_unit_detail(bu_id):
    """Per-BU page: collapsible detail card at top, items grid below.

    Until Course/Service gain a business_line_id FK, the items grid lists
    EVERY row of the matching type (i.e. a Course-typed BU shows every
    course in the catalog, not just courses 'owned' by this BU). This is
    acceptable today because Magic has one BU per product_type."""
    line = BusinessLine.query.get_or_404(bu_id)

    items = []
    if line.product_type == 'Course':
        items = (Course.query
                 .options(joinedload(Course.links))
                 .order_by(Course.is_active.desc(),
                           Course.start_date.asc().nullsfirst(),
                           Course.id.asc())
                 .all())
    elif line.product_type == 'Service':
        items = (Service.query
                 .options(joinedload(Service.links))
                 .order_by(Service.is_active.desc(),
                           Service.sort_order.asc(),
                           Service.id.asc())
                 .all())
    elif line.product_type == 'Product':
        items = (Product.query
                 .options(joinedload(Product.links))
                 .filter_by(business_line_id=line.id)
                 .order_by(Product.is_active.desc(),
                           Product.sort_order.asc(),
                           Product.id.asc())
                 .all())

    # Pre-serialize each item's links so the JS modal can fill the link
    # editor without a second round-trip. Same shape across types.
    for item in items:
        item._links_json = [
            {
                'description': l.description,
                'url': l.url,
                'note': l.note or '',
                'is_active': bool(l.is_active),
                'sort_order': l.sort_order,
            }
            for l in (item.links or [])
        ]

    return render_template(
        'business/unit_detail.html',
        line=line,
        items=items,
        ALLOWED_COURSE_TYPES=ALLOWED_COURSE_TYPES,
        SELF_PACED_COURSE_TYPE=SELF_PACED_COURSE_TYPE,
    )


# Legacy item-CRUD endpoints (/admin/courses, /admin/services, /admin/products,
# /admin/business-lines, /admin/team) stay below in this file. The new
# Business Management pages POST to those same endpoints, so we don't need
# parallel handlers. Phase 6 will delete them once nothing else references
# them by URL.


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
            links, link_err = _parse_links_payload(data.get('links'))
            if link_err:
                return jsonify({'success': False, 'error': link_err}), 400
            course = Course(**fields)
            db.session.add(course)
            db.session.flush()
            for link in links:
                db.session.add(CourseLink(course_id=course.id, **link))
            db.session.commit()
            return jsonify({'success': True, 'id': course.id})

        elif action == 'edit':
            course = Course.query.get(data.get('id'))
            if not course:
                return jsonify({'success': False, 'error': 'Анги олдсонгүй.'}), 404
            fields, err = _parse_course_payload(data, existing=course)
            if err:
                return jsonify({'success': False, 'error': err}), 400
            links, link_err = _parse_links_payload(data.get('links'))
            if link_err:
                return jsonify({'success': False, 'error': link_err}), 400
            for key, value in fields.items():
                setattr(course, key, value)
            # Wholesale replace links on edit, same pattern as Product.
            CourseLink.query.filter_by(course_id=course.id).delete()
            for link in links:
                db.session.add(CourseLink(course_id=course.id, **link))
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
            try:
                item.price = float(price_raw) if price_raw not in (None, '') else None
            except (TypeError, ValueError):
                db.session.rollback()
                return jsonify({'success': False, 'error': 'price тоон утга байх ёстой.'}), 400
            item.duration = (data.get('duration') or '').strip() or None
            if not item.name:
                db.session.rollback()
                return jsonify({'success': False, 'error': 'Нэр шаардлагатай.'}), 400
            links, link_err = _parse_links_payload(data.get('links'))
            if link_err:
                db.session.rollback()
                return jsonify({'success': False, 'error': link_err}), 400
            db.session.flush()
            # Wholesale replace links — same pattern as Product / Course.
            ServiceLink.query.filter_by(service_id=item.id).delete()
            for link in links:
                db.session.add(ServiceLink(service_id=item.id, **link))
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


# Software route removed in Phase 1 — software rows migrated to Product
# under the Magic Cloud business line. Magic Cloud now has product_type='Product'
# and uses the same items grid as any other Product-typed business unit.


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
            pt = (data.get('product_type') or 'Product').strip()
            if pt not in ('Course', 'Service', 'Product'):
                db.session.rollback()
                return jsonify({'success': False,
                                'error': "product_type Course/Service/Product байх ёстой."}), 400
            line.product_type = pt

            raw_year = data.get('established_year')
            if raw_year in (None, '', 'null'):
                line.established_year = None
            else:
                try:
                    line.established_year = int(raw_year)
                except (TypeError, ValueError):
                    db.session.rollback()
                    return jsonify({'success': False,
                                    'error': 'established_year бүхэл тоо байх ёстой.'}), 400

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
        description = (link.get('description') or '').strip()
        url = (link.get('url') or '').strip()
        if not description and not url:
            continue  # let admin leave empty rows
        if not description or not url:
            return None, None, f'Холбоос #{i+1}: description болон URL хоёулаа шаардлагатай.'
        links.append({
            'description': description[:200],
            'url': url[:500],
            'note': (link.get('note') or '').strip() or None,
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
                'description': l.description,
                'url': l.url,
                'note': l.note or '',
                'is_active': bool(l.is_active),
                'sort_order': l.sort_order,
            }
            for l in p.links
        ]
    lines = BusinessLine.query.order_by(BusinessLine.sort_order.asc(),
                                        BusinessLine.id.asc()).all()
    return render_template('products.html', products=all_products, business_lines=lines)


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
