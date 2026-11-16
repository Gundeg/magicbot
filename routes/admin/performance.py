"""Гүйцэтгэл — bot + staff performance page (admins only: it shows each
colleague's numbers). The numbers come from services/_performance.py."""
from flask import render_template, request
from flask_login import login_required

from app import app
from auth import admin_required
from services._performance import performance_report
from services._schedule import DAY_NAMES, UB_OFFSET

PERIODS = (7, 30)


@app.route('/admin/performance')
@login_required
@admin_required
def performance():
    try:
        days = int(request.args.get('days', PERIODS[0]))
    except (TypeError, ValueError):
        days = PERIODS[0]
    if days not in PERIODS:
        days = PERIODS[0]
    return render_template(
        'performance.html',
        report=performance_report(days),
        periods=PERIODS,
        day_names=DAY_NAMES,
        ub_offset=UB_OFFSET,
    )
