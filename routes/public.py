"""Public (no-auth) routes: index, health check, and privacy policy."""
import os
from datetime import datetime

from flask import jsonify, redirect, render_template, url_for
from flask_login import current_user

from app import app


@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/health')
def health():
    """Cheap liveness probe — no DB, no auth. Point a keep-warm pinger
    (Render Cron / UptimeRobot, every ~10 min) here so the service never
    spins down. A warm dyno means no cold-start delay and, crucially, no
    Facebook webhook retries piling up during a boot (see the webhook
    idempotency note in CLAUDE.md)."""
    return jsonify({'status': 'ok'}), 200


@app.route('/privacy')
def privacy():
    """Public-facing Mongolian privacy policy. Required by Facebook App Review.

    All company-specific text is env-var driven so the same template serves
    every bot deployment. Submit https://<your-render-url>/privacy as the
    Privacy Policy URL in the Facebook Developer Console -> App Settings.
    """
    contact_email = (
        os.environ.get('PRIVACY_CONTACT_EMAIL')
        or os.environ.get('ADMIN_EMAIL')
        or 'info@magicfinance.mn'
    )
    return render_template(
        'privacy.html',
        contact_email=contact_email,
        last_updated=datetime.utcnow().strftime('%Y-%m-%d'),
        year=datetime.utcnow().year,
        company_legal_name=os.environ.get(
            'COMPANY_LEGAL_NAME', 'Мэжик Санхүүгийн Групп ХХК'),
        company_short_name=os.environ.get('COMPANY_SHORT_NAME', 'Мэжик'),
        company_address=os.environ.get(
            'COMPANY_ADDRESS',
            'Улаанбаатар хот, БЗД, 13-р хороолол, Натурын зам, '
            'UB Tower Plus, 5-р давхар, 509 тоот'),
        company_facebook_url=os.environ.get(
            'COMPANY_FACEBOOK_URL',
            'https://www.facebook.com/MagicFinancialGroup'),
        company_facebook_label=os.environ.get(
            'COMPANY_FACEBOOK_LABEL', 'Magic Financial Group'),
        product_name=os.environ.get('PRODUCT_NAME', 'Магик Финанс'),
    )
