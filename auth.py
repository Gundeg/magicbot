"""Role-based access decorators for admin routes.

Kept separate from `app.py` so route modules don't need to import the whole
app object just to get the decorator — they only need this file.

Roles (highest → lowest):
  super_admin       — full access, including persona / admin user management
  admin             — everything except managing other admins / persona
  registration_staff — daily ops only (work tasks, dashboard, logs, guide)

The decorators stack like nested rings: super_admin_required ⊂ admin_required
⊂ staff_required. A route gated by staff_required accepts all three; one
gated by super_admin_required only accepts super_admin.
"""
from functools import wraps

from flask import redirect, url_for, flash
from flask_login import current_user


# Anyone with these roles can sign in to the admin panel.
STAFF_ROLES = ('registration_staff', 'admin', 'super_admin')
# Roles that can manage catalog / bot training / system settings.
ADMIN_ROLES = ('admin', 'super_admin')


def staff_required(f):
    """Allow any authenticated user with a staff-tier role (the broadest gate).

    Daily-ops routes (work_tasks, dashboard, message log, train-ai guide) use
    this so registration_staff can do their job without being able to edit
    the catalog or bot training."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in STAFF_ROLES:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in ADMIN_ROLES:
            # Staff who try to reach an admin-only page get bounced to the
            # work-tasks dashboard rather than the login screen, since they
            # ARE logged in — just not allowed here.
            if current_user.is_authenticated and current_user.role in STAFF_ROLES:
                flash('Энэ хуудсыг харах эрх танд алга. Админд хандана уу.', 'error')
                return redirect(url_for('work_tasks'))
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def super_admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in STAFF_ROLES:
            return redirect(url_for('login'))
        if current_user.role != 'super_admin':
            flash('Энэ үйлдэлд супер админ эрх шаардлагатай.', 'error')
            return redirect(url_for('admins') if current_user.role in ADMIN_ROLES else url_for('work_tasks'))
        return f(*args, **kwargs)
    return decorated_function
