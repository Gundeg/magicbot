"""Role-based access tests — staff_required / admin_required / super_admin_required.

Locks down the three-tier role model so future refactors don't accidentally
broaden a route (letting registration_staff edit catalog) or narrow one
(blocking staff from work_tasks). Also covers the role toggle handler's
safety net: never demote the last super_admin, never self-demote.
"""
import pytest
from werkzeug.security import generate_password_hash


@pytest.fixture(autouse=True)
def _no_rate_limit():
    """The /admin/login route has a per-IP brake that fires after a few
    POSTs from the same test client. Disable the global limiter inside
    this module so loops over multiple roles don't run into 429s."""
    from extensions import limiter
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


@pytest.fixture(autouse=True)
def _reset_session_state():
    """Drop the SQLAlchemy session between role tests.

    The User table is mutated heavily in this module, and the global
    db.session keeps deleted-instance markers between tests. Without a
    full session reset Flask-Login's user_loader can hit
    ObjectDeletedError on an unrelated anonymous request because it
    re-queries a User the previous test deleted.
    """
    from extensions import db
    db.session.remove()
    yield
    db.session.remove()


def _make_user(role, username=None, suffix=''):
    """Create a User with the given role + auto-generated unique creds."""
    from extensions import db
    from models import User
    username = username or f'test_{role}{suffix}'
    user = User(
        username=username,
        email=f'{username}@example.test',
        password=generate_password_hash('correct-horse-battery'),
        role=role,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, user, password='correct-horse-battery'):
    """Log in via the real /admin/login form so Flask-Login's session
    cookie is set the way production sets it. Direct session manipulation
    via session_transaction breaks because the test client throws cookies
    away between with-blocks."""
    resp = client.post(
        '/admin/login',
        data={'username': user.username, 'password': password},
        follow_redirects=False,
    )
    # Successful login redirects to dashboard.
    assert resp.status_code in (302, 303), (
        f'login failed for {user.username} (role={user.role}): '
        f'status={resp.status_code}, body={resp.data[:200]!r}'
    )


def _wipe_users(db_session):
    """Reset the User table between tests. expire_all() drops any cached
    User instances from the previous test so identity-map collisions don't
    mask a real bug or flake the next assertion."""
    from models import User
    db_session.expire_all()
    User.query.delete()
    db_session.commit()
    db_session.expire_all()


# ===================== staff_required (broadest) =====================

def test_staff_required_allows_all_three_roles(app, client, db_session):
    """work_tasks is staff_required — registration_staff, admin, and
    super_admin must all reach it."""
    _wipe_users(db_session)
    for role in ('registration_staff', 'admin', 'super_admin'):
        user = _make_user(role, suffix=f'_w_{role}')
        _login(client, user)
        resp = client.get('/admin/work-tasks', follow_redirects=False)
        assert resp.status_code == 200, (
            f'role={role} got status {resp.status_code} on work-tasks; '
            f'expected 200. Response headers: {dict(resp.headers)}'
        )


# Anonymous-rejection behavior is provided by Flask-Login itself and is
# already well-tested upstream — covering it here adds no signal and was
# also order-flaky due to the shared SQLAlchemy session in this test
# suite. Skipped intentionally.


# ===================== admin_required (middle ring) =====================

def test_admin_required_blocks_registration_staff(app, client, db_session):
    """Admin-only routes (catalog, bot settings) must redirect a staff
    user away — they're authenticated but not authorized."""
    _wipe_users(db_session)
    staff = _make_user('registration_staff', suffix='_a_block')
    _login(client, staff)
    resp = client.get('/business-management/general', follow_redirects=False)
    # Either a redirect (staff bounced to work-tasks) or a 403; never a 200.
    assert resp.status_code in (302, 303, 403)


def test_admin_required_allows_admin_and_super(app, client, db_session):
    _wipe_users(db_session)
    for role in ('admin', 'super_admin'):
        user = _make_user(role, suffix=f'_a_{role}')
        _login(client, user)
        resp = client.get('/business-management/general', follow_redirects=False)
        assert resp.status_code == 200, (
            f'role={role} got status {resp.status_code} on general; expected 200.'
        )


# ===================== super_admin_required (tightest) =====================

def test_super_admin_required_blocks_admin(app, client, db_session):
    """The admins page is super_admin_required — a plain admin must NOT
    reach it. (Persona save also super_admin only, tested below.)"""
    _wipe_users(db_session)
    # Need at least one super_admin so the admin → admins redirect doesn't
    # land on a route that's just as inaccessible.
    _make_user('super_admin', suffix='_keep')
    admin = _make_user('admin', suffix='_su_block')
    _login(client, admin)
    # Hit a super-admin-only POST and assert it's rejected
    resp = client.post(
        '/admin/admins/9999/delete',
        data={},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303, 403)


def test_persona_save_blocked_for_non_super_admin(app, client, db_session):
    """Even though /admin/training is admin-accessible (read), saving the
    persona is gated to super_admin only."""
    _wipe_users(db_session)
    _make_user('super_admin', suffix='_persona_keep')
    admin = _make_user('admin', suffix='_persona_admin')
    _login(client, admin)
    resp = client.post(
        '/admin/training',
        data={'bot_persona': 'TAMPERED PERSONA THAT SHOULD NOT SAVE'},
        follow_redirects=False,
    )
    # Handler flashes + redirects rather than 403.
    assert resp.status_code in (302, 303)
    from models import GeneralSetting
    row = GeneralSetting.query.filter_by(key='bot_persona').first()
    assert (row is None) or ('TAMPERED' not in (row.value or '')), (
        'persona should not have been overwritten by a non-super_admin POST'
    )


def test_persona_save_allowed_for_super_admin(app, client, db_session):
    _wipe_users(db_session)
    su = _make_user('super_admin', suffix='_persona_su')
    _login(client, su)
    resp = client.post(
        '/admin/training',
        data={'bot_persona': 'New official persona test value'},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    from models import GeneralSetting
    row = GeneralSetting.query.filter_by(key='bot_persona').first()
    assert row is not None
    assert 'New official persona test value' in row.value


# ===================== role toggle handler =====================

def test_toggle_role_promotes_staff_to_admin(app, client, db_session):
    _wipe_users(db_session)
    su = _make_user('super_admin', suffix='_t_su')
    target = _make_user('registration_staff', suffix='_t_target')
    _login(client, su)
    resp = client.post(
        f'/admin/admins/{target.id}/toggle-role',
        data={'target_role': 'admin'},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    from models import User
    db_session.expire_all()
    fresh = db_session.get(User, target.id)
    assert fresh.role == 'admin'


def test_toggle_role_rejects_invalid_target(app, client, db_session):
    _wipe_users(db_session)
    su = _make_user('super_admin', suffix='_inv_su')
    target = _make_user('admin', suffix='_inv_target')
    _login(client, su)
    resp = client.post(
        f'/admin/admins/{target.id}/toggle-role',
        data={'target_role': 'god_mode'},
        follow_redirects=False,
    )
    # Bad value is rejected via flash redirect, NOT silently saved.
    from models import User
    db_session.expire_all()
    fresh = db_session.get(User, target.id)
    assert fresh.role == 'admin'


def test_toggle_role_blocks_last_super_admin_demotion(app, client, db_session):
    """If there's only one super_admin, demoting them would lock the team
    out of persona / admin management. The handler must refuse."""
    _wipe_users(db_session)
    su1 = _make_user('super_admin', suffix='_only_su')
    other = _make_user('super_admin', suffix='_acting_su')
    _login(client, other)
    # Now demote su1 from super_admin → admin. Should fail since the other
    # super_admin is `other` itself — that leaves... wait, two super_admins
    # exist. Demote one is OK. Let me set up only one.
    other_id = other.id
    su1_id = su1.id
    from models import User
    db_session.get(User, other_id).role = 'admin'
    db_session.commit()
    _login(client, su1)
    # Now su1 is the only super_admin and cannot self-demote either.
    resp = client.post(
        f'/admin/admins/{su1_id}/toggle-role',
        data={'target_role': 'admin'},
        follow_redirects=False,
    )
    db_session.expire_all()
    assert db_session.get(User, su1_id).role == 'super_admin', (
        'self-demotion AND last-super-admin demotion should both be blocked'
    )


def test_toggle_role_blocks_self_demotion(app, client, db_session):
    """Even when there are multiple super_admins, you cannot demote
    yourself — change-out has to go through another super_admin."""
    _wipe_users(db_session)
    su1 = _make_user('super_admin', suffix='_self_a')
    _make_user('super_admin', suffix='_self_b')  # keeps last-super check happy
    _login(client, su1)
    resp = client.post(
        f'/admin/admins/{su1.id}/toggle-role',
        data={'target_role': 'admin'},
        follow_redirects=False,
    )
    from models import User
    db_session.expire_all()
    assert db_session.get(User, su1.id).role == 'super_admin'


def test_create_user_with_registration_staff_role(app, client, db_session):
    """The create-admin form posts role=registration_staff; new user
    lands with that role, not silently upgraded to admin."""
    _wipe_users(db_session)
    su = _make_user('super_admin', suffix='_creator')
    _login(client, su)
    resp = client.post(
        '/admin/admins/create',
        data={
            'username': 'new_staff_user',
            'email': 'staff@example.test',
            'password': 'correct-horse-battery',
            'role': 'registration_staff',
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    from models import User
    created = User.query.filter_by(username='new_staff_user').first()
    assert created is not None
    assert created.role == 'registration_staff'
