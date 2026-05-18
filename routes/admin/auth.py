"""Admin authentication: login + logout."""
import logging

from flask import redirect, render_template, request, url_for
from flask_login import login_required, login_user, logout_user
from werkzeug.security import check_password_hash

from app import app
from extensions import limiter
from models import User

logger = logging.getLogger(__name__)


@app.route('/admin/login', methods=['GET', 'POST'])
# Per-IP brake on the login form. GET requests are unmetered (the form
# itself); only POSTs count against the limit so a normal user typing a
# wrong password three times still gets a useful error before getting
# locked out for a minute. Tune via env if needed.
@limiter.limit(
    '10 per minute; 50 per hour',
    methods=['POST'],
    error_message='Хэт олон удаа оролдлоо. Хэдэн минутын дараа дахин үзнэ үү.',
)
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('dashboard'))
        else:
            # Log failed attempts so abuse shows up in server logs.
            # We don't echo the username back at the page to avoid
            # account enumeration.
            logger.warning(
                'login failed: ip=%s username=%r',
                request.remote_addr, username,
            )
            return render_template('login.html', error='Invalid credentials')

    return render_template('login.html')


@app.route('/admin/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))
