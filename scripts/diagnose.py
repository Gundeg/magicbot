"""One-shot production health check — run this FIRST when "the bot stopped working".

Collapses the whole telemetry checklist in CLAUDE.md / OPERATIONS.md into a
single command so an outage is diagnosed in one minute instead of an evening of
guessing. Read-only: it sends no Messenger message and writes nothing to the DB.

Run it in the Render shell of the `magicbot` service:

    python scripts/diagnose.py

Every check prints OK / FAIL / WARN plus the exact next step. Secrets are
masked, so the output is safe to paste into a chat.
"""

import os
import sys
from pathlib import Path

# Run as `python scripts/diagnose.py` and sys.path[0] is scripts/, so `app` and
# `models` would not import. Put the repo root first, exactly like conftest does.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Import-time env guards in services/ raise if credentials are missing, which
# would abort the very diagnosis we came here to run — so read the env directly
# and import lazily, per check.
import requests  # noqa: E402

GRAPH = 'https://graph.facebook.com/v18.0'

OK, FAIL, WARN, INFO = 'OK  ', 'FAIL', 'WARN', 'info'
_verdicts = []


def report(level, title, detail='', fix=''):
    _verdicts.append((level, title))
    icon = {'OK  ': '✅', 'FAIL': '❌', 'WARN': '⚠️ ', 'info': '  '}[level]
    print(f"{icon} [{level}] {title}")
    if detail:
        for line in str(detail).splitlines():
            print(f"        {line}")
    if fix:
        print(f"        → {fix}")


def mask(value):
    if not value:
        return '<unset>'
    value = str(value)
    return f"{value[:4]}…{value[-4:]} (len {len(value)})" if len(value) > 12 else '<short>'


def section(name):
    print(f"\n── {name} " + "─" * max(0, 58 - len(name)))


# ---------------------------------------------------------------- 1. env vars
def check_env():
    section('1. Environment variables')
    fb_token = os.environ.get('FACEBOOK_ACCESS_TOKEN', '')
    page_id = os.environ.get('FACEBOOK_PAGE_ID', '')
    report(OK if fb_token else FAIL, 'FACEBOOK_ACCESS_TOKEN', mask(fb_token),
           '' if fb_token else 'The app cannot even boot without it — set it on the magicbot service.')
    report(OK if page_id else WARN, 'FACEBOOK_PAGE_ID', page_id or '<unset>',
           '' if page_id else 'Unset means the Page-vs-User token check below cannot run.')
    for name, required in (('FACEBOOK_APP_SECRET', True), ('VERIFY_TOKEN', True),
                           ('OPENAI_API_KEY', True), ('SECRET_KEY', True)):
        val = os.environ.get(name, '')
        report(OK if val else (FAIL if required else WARN), name, mask(val))
    gem = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY') or ''
    provider = 'Gemini' if gem else 'OpenAI'
    model = os.environ.get('REPLY_MODEL', '').strip() or (
        'gemini-2.5-flash' if gem else 'gpt-5.3-chat-latest')
    report(INFO, f'Customer replies run on: {provider}',
           f"REPLY_MODEL={model}\n"
           f"GEMINI_REASONING_EFFORT={os.environ.get('GEMINI_REASONING_EFFORT', 'none (default)')}\n"
           f"REPLY_MAX_TOKENS={os.environ.get('REPLY_MAX_TOKENS', '500 (default)')}")
    if gem and 'flash' not in model.lower():
        effort = os.environ.get('GEMINI_REASONING_EFFORT', 'none').strip().lower()
        if effort in ('', 'none'):
            report(WARN, 'Gemini Pro/3.x with thinking unbounded',
                   "Pro/3.x reject 'none' and cannot disable thinking.",
                   "Set GEMINI_REASONING_EFFORT=low and REPLY_MAX_TOKENS=2048.")
    return fb_token, page_id, gem, model


# ------------------------------------------- 2. FB token: valid AND a Page token
def check_fb_token(fb_token, page_id):
    section('2. Facebook Page token')
    if not fb_token:
        return False
    try:
        resp = requests.get(f'{GRAPH}/me', params={'fields': 'id,name'},
                            headers={'Authorization': f'Bearer {fb_token}'}, timeout=10)
    except Exception as e:
        report(FAIL, 'Graph API unreachable', e, 'Network/DNS problem on the host.')
        return False
    if resp.status_code != 200:
        body = (resp.text or '')[:400]
        fix = 'Regenerate the token — OPERATIONS.md §4.'
        if '"code":190' in body or 'OAuthException' in body:
            fix = ('Token expired/invalidated (code 190; subcode 463 = expired, '
                   '460 = FB password changed). Regenerate a PAGE token — OPERATIONS.md §4.')
        report(FAIL, f'Token rejected (HTTP {resp.status_code})', body, fix)
        return False
    data = resp.json() or {}
    me_id, me_name = str(data.get('id', '')), data.get('name', '')
    # THE trap: a User token also reads /me fine, and would also read the Page by
    # its ID — so only comparing /me's id against FACEBOOK_PAGE_ID proves this is
    # a Page token. The Send API posts to me/messages, so `me` MUST be the Page.
    if page_id and me_id != str(page_id):
        report(FAIL, 'This is a USER token, not a PAGE token',
               f"/me returned id={me_id} name={me_name!r}, but FACEBOOK_PAGE_ID={page_id}.\n"
               "Every send fails with GraphMethodException code:100 subcode:33 "
               "(\"Object with ID 'me' does not exist\").",
               'Graph API Explorer → "User or Page" → Page Access Tokens → the Page. '
               'OPERATIONS.md §4 step 1.')
        return False
    report(OK, 'Token is a valid Page token', f"/me → id={me_id} name={me_name!r}")
    return True


# ------------------------------------- 3. Page webhook subscription (delivery)
def check_subscriptions(fb_token):
    section('3. Page webhook subscription')
    try:
        resp = requests.get(f'{GRAPH}/me/subscribed_apps',
                            headers={'Authorization': f'Bearer {fb_token}'}, timeout=10)
    except Exception as e:
        report(WARN, 'Could not read subscribed_apps', e)
        return
    if resp.status_code != 200:
        report(WARN, f'subscribed_apps HTTP {resp.status_code}', (resp.text or '')[:300])
        return
    fields = sorted({f for a in (resp.json() or {}).get('data', [])
                     for f in a.get('subscribed_fields', [])})
    if not fields:
        report(FAIL, 'Page is subscribed to NO webhook fields',
               'Facebook is delivering nothing — the bot never hears a message.',
               'Admin → Bot management → the webhook-subscription fix button, '
               'or re-subscribe the app to the Page in the Meta dashboard.')
        return
    report(OK if 'messages' in fields else FAIL, "field 'messages'",
           f"subscribed fields: {', '.join(fields)}",
           '' if 'messages' in fields else 'Without it no customer message ever reaches the webhook.')
    report(OK if 'message_echoes' in fields else WARN, "field 'message_echoes'",
           '', '' if 'message_echoes' in fields else
           'Human-takeover auto-mute is inert without it (not an outage cause).')


# --------------------------------------------- 4. reply provider, live call
def check_reply_provider(gem_key, model):
    section('4. Customer-reply model (live call)')
    from openai import OpenAI
    if gem_key:
        base = (os.environ.get('GEMINI_BASE_URL')
                or 'https://generativelanguage.googleapis.com/v1beta/openai/').strip()
        client, token_param, label = OpenAI(api_key=gem_key, base_url=base), 'max_tokens', 'Gemini'
    else:
        key = os.environ.get('OPENAI_API_KEY', '')
        if not key:
            report(FAIL, 'No reply provider key at all', 'OPENAI_API_KEY and GEMINI_API_KEY both unset.')
            return
        client, token_param, label = OpenAI(api_key=key), 'max_completion_tokens', 'OpenAI'
    try:
        max_tokens = int(os.environ.get('REPLY_MAX_TOKENS', '500') or '500')
    except ValueError:
        max_tokens = 500
    kwargs = {'model': model, 'messages': [{'role': 'user', 'content': 'Сайн байна уу?'}],
              token_param: max_tokens}
    if gem_key:
        effort = os.environ.get('GEMINI_REASONING_EFFORT', 'none').strip().lower()
        if effort in ('low', 'medium', 'high') or (effort == 'none' and 'flash' in model.lower()):
            kwargs['reasoning_effort'] = effort
    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as e:
        text = str(e)
        fix = ('Google AI Studio (aistudio.google.com) → API keys / Billing'
               if gem_key else 'platform.openai.com → Billing')
        if 'not found' in text.lower() or '404' in text:
            fix = (f"Model {model!r} is gone or not enabled on this key — a *preview* model "
                   f"can be retired without warning. Set REPLY_MODEL to a current model "
                   f"(e.g. gemini-2.5-flash) and redeploy.")
        elif '429' in text or 'quota' in text.lower() or 'exhaust' in text.lower():
            fix = f"Quota/rate limit on the {label} key — top up or raise the limit at {fix}"
        elif '401' in text or 'api key' in text.lower() or 'unauthor' in text.lower():
            fix = f"Key invalid/revoked — reissue it at {fix}"
        report(FAIL, f'{label} call failed — customers get the canned apology',
               text[:500], fix)
        return
    content = (resp.choices[0].message.content or '') if resp.choices else ''
    finish = resp.choices[0].finish_reason if resp.choices else '?'
    if not content.strip():
        report(FAIL, f'{label} returned an EMPTY reply',
               f"finish_reason={finish} — the classic thinking-ate-the-budget failure.",
               'Set GEMINI_REASONING_EFFORT=low and REPLY_MAX_TOKENS=2048 (Pro/3.x), '
               'or switch REPLY_MODEL to gemini-2.5-flash with effort none.')
        return
    report(OK, f'{label} {model} replied', f"{content.strip()[:120]}  (finish_reason={finish})")


# ------------------------------- 5. background jobs always run on OpenAI
def check_background_provider(gem_key):
    if not gem_key:
        return  # already covered by check 4
    section('5. Background jobs (always OpenAI gpt-4o-mini)')
    key = os.environ.get('OPENAI_API_KEY', '')
    if not key:
        report(FAIL, 'OPENAI_API_KEY unset on a Gemini deployment',
               'Topic classifier, FAQ clustering and page comments all break.',
               'Set OPENAI_API_KEY as well — it is required on every deployment.')
        return
    from openai import OpenAI
    try:
        OpenAI(api_key=key).chat.completions.create(
            model='gpt-4o-mini', messages=[{'role': 'user', 'content': 'ping'}], max_tokens=5)
        report(OK, 'OpenAI gpt-4o-mini reachable')
    except Exception as e:
        report(WARN, 'OpenAI background provider failing', str(e)[:300],
               'Customer replies still work (Gemini); classification/clustering do not.')


# -------------------------- 6. is Facebook actually delivering to this app?
def check_traffic():
    section('6. Recent traffic (is Facebook delivering at all?)')
    try:
        from app import app
        from extensions import db
        from models import Message
    except Exception as e:
        report(WARN, 'Could not open the app/DB', str(e)[:300],
               'Run this from the service root so app.py imports cleanly.')
        return
    with app.app_context():
        try:
            last_in = (db.session.query(Message)
                       .filter(Message.sender == 'user')
                       .order_by(Message.created_at.desc()).first())
            last_out = (db.session.query(Message)
                        .filter(Message.sender == 'bot')
                        .order_by(Message.created_at.desc()).first())
        except Exception as e:
            report(FAIL, 'Database query failed', str(e)[:300],
                   "'database is locked' → confirm the WAL/busy_timeout block in app.py.")
            return
        from datetime import datetime
        now = datetime.utcnow()
        if not last_in:
            report(WARN, 'No inbound customer message on record', 'Empty or fresh database.')
        else:
            age_h = (now - last_in.created_at).total_seconds() / 3600
            report(OK if age_h < 48 else FAIL,
                   f'Last INBOUND customer message: {age_h:.1f}h ago',
                   str(last_in.created_at),
                   '' if age_h < 48 else
                   'Facebook is not delivering: check 3 above, the app’s Live/Dev mode, '
                   'and that the Callback URL still points at this service.')
        if last_out:
            age_h = (now - last_out.created_at).total_seconds() / 3600
            gap = (last_out.created_at - last_in.created_at).total_seconds() if last_in else 0
            report(OK if (last_in and gap > -60) else WARN,
                   f'Last OUTBOUND bot message: {age_h:.1f}h ago', str(last_out.created_at),
                   '' if (last_in and gap > -60) else
                   'Inbound is arriving but the bot is not answering → the failure is in '
                   'the reply or send path (checks 2 and 4), not in delivery.')
        else:
            report(WARN, 'The bot has never sent a message', '', 'Send path never succeeded.')


def main():
    print('Magic Bot — production diagnosis\n' + '=' * 64)
    fb_token, page_id, gem_key, model = check_env()
    token_ok = check_fb_token(fb_token, page_id)
    if token_ok:
        check_subscriptions(fb_token)
    check_reply_provider(gem_key, model)
    check_background_provider(gem_key)
    check_traffic()

    section('Verdict')
    fails = [t for lvl, t in _verdicts if lvl == FAIL]
    warns = [t for lvl, t in _verdicts if lvl == WARN]
    if fails:
        print('❌ BROKEN — fix these, most likely cause first:')
        for t in fails:
            print(f'   • {t}')
    elif warns:
        print('⚠️  Nothing fatal found, but review:')
        for t in warns:
            print(f'   • {t}')
    else:
        print('✅ Every check passed. If customers still report silence, the problem is '
              'per-conversation (a mute / staff takeover / handoff), not global — '
              'check that user in Admin → Мессежийн түүх.')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
