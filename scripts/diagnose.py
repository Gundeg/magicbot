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
                           ('ANTHROPIC_API_KEY', True), ('SECRET_KEY', True)):
        val = os.environ.get(name, '')
        report(OK if val else (FAIL if required else WARN), name, mask(val))
    reply_model = os.environ.get('REPLY_MODEL', '').strip() or 'claude-opus-5-5'
    bg_model = os.environ.get('BACKGROUND_MODEL', '').strip() or 'claude-haiku-5-5'
    report(INFO, 'Every model call runs on Claude (Anthropic)',
           f"REPLY_MODEL={reply_model}\n"
           f"REPLY_EFFORT={os.environ.get('REPLY_EFFORT', 'low (default)')}\n"
           f"REPLY_MAX_TOKENS={os.environ.get('REPLY_MAX_TOKENS', '2048 (default)')}\n"
           f"BACKGROUND_MODEL={bg_model}")
    for stale in ('OPENAI_API_KEY', 'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'GEMINI_REASONING_EFFORT'):
        if os.environ.get(stale):
            report(INFO, f'{stale} is set but unused since the Claude migration (2026-11-02)',
                   '', 'Safe to delete from the Render environment.')
    return fb_token, page_id, reply_model, bg_model


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


# --------------------------------------------- 4. reply model, live call
def check_reply_provider(model):
    section('4. Customer-reply model (live call)')
    key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not key:
        report(FAIL, 'ANTHROPIC_API_KEY unset — every reply is the canned apology',
               '', 'Set it on the magicbot service (console.anthropic.com → API Keys).')
        return
    import anthropic
    try:
        max_tokens = int(os.environ.get('REPLY_MAX_TOKENS', '2048') or '2048')
    except ValueError:
        max_tokens = 2048
    effort = os.environ.get('REPLY_EFFORT', '').strip().lower() or 'low'
    client = anthropic.Anthropic(api_key=key, max_retries=0, timeout=60.0)
    try:
        resp = client.messages.create(
            model=model, max_tokens=max_tokens, output_config={'effort': effort},
            messages=[{'role': 'user', 'content': 'Сайн байна уу?'}],
        )
    except Exception as e:
        text = str(e)
        low = text.lower()
        fix = 'console.anthropic.com → Plans & Billing / API Keys'
        if isinstance(e, anthropic.NotFoundError) or 'not_found' in low:
            fix = (f"Model {model!r} is gone or not enabled on this key. Set REPLY_MODEL to a "
                   f"current model (claude-opus-5-5 / claude-sonnet-5-5) and redeploy.")
        elif 'credit balance' in low:
            fix = 'Prepaid credit exhausted — top up at console.anthropic.com → Plans & Billing.'
        elif isinstance(e, anthropic.AuthenticationError) or '401' in text:
            fix = 'Key invalid/revoked — reissue it at console.anthropic.com → API Keys.'
        elif isinstance(e, anthropic.RateLimitError) or '429' in text:
            fix = 'Rate limited (transient, self-heals) — retry in a minute.'
        report(FAIL, 'Claude call failed — customers get the canned apology', text[:500], fix)
        return
    content = ''.join(b.text for b in resp.content if b.type == 'text').strip()
    if not content:
        report(FAIL, 'Claude returned an EMPTY reply',
               f"stop_reason={resp.stop_reason} — thinking ate the whole max_tokens budget.",
               'Raise REPLY_MAX_TOKENS (default 2048) or lower REPLY_EFFORT.')
        return
    report(OK, f'Claude {model} replied',
           f"{content[:120]}  (stop_reason={resp.stop_reason}, "
           f"in={resp.usage.input_tokens} out={resp.usage.output_tokens})")


# ------------------------- 5. background jobs run on BACKGROUND_MODEL (Haiku)
def check_background_provider(bg_model):
    section('5. Background jobs (classifier / clustering / page comments)')
    key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not key:
        return  # already fatal in check 4
    import anthropic
    try:
        anthropic.Anthropic(api_key=key, max_retries=0, timeout=60.0).messages.create(
            model=bg_model, max_tokens=16, output_config={'effort': 'low'},
            messages=[{'role': 'user', 'content': 'ping'}],
        )
        report(OK, f'Claude {bg_model} reachable')
    except Exception as e:
        report(WARN, f'Background model {bg_model} failing', str(e)[:300],
               'Customer replies may still work; classification / clustering / comments do '
               'not. Check the BACKGROUND_MODEL spelling and the key.')


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
    fb_token, page_id, reply_model, bg_model = check_env()
    token_ok = check_fb_token(fb_token, page_id)
    if token_ok:
        check_subscriptions(fb_token)
    check_reply_provider(reply_model)
    check_background_provider(bg_model)
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
