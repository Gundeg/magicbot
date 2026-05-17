"""Role-based access decorators for admin routes.

Kept separate from `app.py` so route modules don't need to import the whole
app object just to get the decorator — they only need this file.
"""
from functools import wraps

from flask import redirect, url_for, flash
from flask_login import current_user


ADMIN_ROLES = ('admin', 'super_admin')


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in ADMIN_ROLES:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def super_admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in ADMIN_ROLES:
            return redirect(url_for('login'))
        if current_user.role != 'super_admin':
            flash('Энэ үйлдэлд супер админ эрх шаардлагатай.', 'error')
            return redirect(url_for('admins'))
        return f(*args, **kwargs)
    return decorated_function
