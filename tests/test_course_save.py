"""Course save validation.

Regression guard for the "editing a course won't save" bug: many courses
(seeded or created before course_number existed) have a NULL course_number.
`_parse_course_payload` used to require course_number on every save, so
editing such a course — e.g. to update its registration link — was rejected
with a 400 and the change was silently discarded. course_number must be
OPTIONAL when editing an existing course; it stays required only when adding
a brand-new one (to nudge disambiguation of same-named classes).
"""


def test_edit_course_without_number_is_allowed(app, db_session):
    from extensions import db
    from models import Course
    from routes.admin.business import _parse_course_payload

    course = Course(name='Numberless course', course_type='100% Online',
                    price=100000, time='', is_active=True)
    db.session.add(course)
    db.session.commit()

    fields, err = _parse_course_payload({
        'name': 'Numberless course',
        'course_type': '100% Online',
        'course_number': None,   # course has no number; modal sends null
        'price': 100000,
        'time': '',
    }, existing=course)

    assert err is None, f'edit should be allowed without a number, got: {err}'
    assert fields['course_number'] is None

    Course.query.filter_by(id=course.id).delete()
    db.session.commit()


def test_add_course_without_number_is_still_rejected(app, db_session):
    from routes.admin.business import _parse_course_payload

    fields, err = _parse_course_payload({
        'name': 'Brand new course',
        'course_type': '100% Online',
        'course_number': None,
        'price': 0,
        'time': '',
    }, existing=None)

    assert err is not None
    assert 'course_number' in err


def test_edit_course_keeps_and_validates_a_provided_number(app, db_session):
    from extensions import db
    from models import Course
    from routes.admin.business import _parse_course_payload

    course = Course(name='Has number', course_type='100% Online',
                    price=0, time='', is_active=True, course_number=4242)
    db.session.add(course)
    db.session.commit()

    fields, err = _parse_course_payload({
        'name': 'Has number',
        'course_type': '100% Online',
        'course_number': 4242,
        'price': 0,
        'time': '',
    }, existing=course)

    assert err is None, err
    assert fields['course_number'] == 4242

    Course.query.filter_by(id=course.id).delete()
    db.session.commit()
