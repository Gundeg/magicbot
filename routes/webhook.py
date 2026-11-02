"""Facebook Messenger webhook.

CSRF is intentionally disabled — Facebook POSTs here directly, not a browser.
The request is authenticated by HMAC-SHA256 against the raw body using
FACEBOOK_APP_SECRET (see services.verify_facebook_signature).
"""
import logging
import os
from datetime import datetime, timedelta

from flask import jsonify, request
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

from app import app, csrf
from extensions import db
from models import FacebookUser, Message
from services import (PHONE_RE, HANDOFF_CATEGORY_LABELS,
                      REGISTRATION_FALLBACK_REPLY,
                      check_rate_limit, classify_session, classify_user_topics,
                      describe_registration,
                      detect_funnel_stage, enqueue_background,
                      extract_handoff, extract_name_from_reply,
                      extract_registration, first_name_of,
                      generate_bot_response, get_facebook_user_info,
                      is_in_handoff, refresh_facebook_user_name,
                      refresh_user_summary,
                      send_facebook_message, send_sender_action,
                      send_telegram_notification, should_handoff,
                      submit_registration_form, trigger_handoff,
                      verify_facebook_signature, FACEBOOK_APP_SECRET,
                      BOT_ECHO_TAG, HUMAN_TAKEOVER_MUTE_MINUTES)


def human_takeover_pause(customer_psid):
    """Pause the bot for a customer after a human agent replies to them in the
    Page inbox. Idempotent and best-effort: a missing user or DB hiccup must
    not break webhook processing (we still return 200 to Facebook)."""
    if not customer_psid:
        return
    try:
        fb_user = FacebookUser.query.filter_by(facebook_id=customer_psid).first()
        if not fb_user:
            return
        fb_user.bot_muted_until = (
            datetime.utcnow() + timedelta(minutes=HUMAN_TAKEOVER_MUTE_MINUTES)
        )
        db.session.commit()
        logger.info(
            "Human takeover: bot paused %s min for psid=%s",
            HUMAN_TAKEOVER_MUTE_MINUTES, customer_psid,
        )
    except Exception as e:
        db.session.rollback()
        logger.exception("human_takeover_pause failed psid=%s: %s", customer_psid, e)


def complete_registration(fb_user, data, bot_reply):
    """Submit a bot-collected registration to the configured form, record the
    customer as a lead with the fields in their notes, and ping Telegram.
    Returns the reply to send: the model's confirmation when the form accepted
    the entry, otherwise a reply that promises staff follow-up instead."""
    ok, info = submit_registration_form(data)
    summary = describe_registration(data, ok, info)
    try:
        phone_match = PHONE_RE.search(data.get('phone') or '')
        if phone_match:
            fb_user.phone = phone_match.group(0)
        full_name = f"{data.get('firstname') or ''} {data.get('lastname') or ''}".strip()
        if full_name and (fb_user.name or '').strip().lower() in ('', 'unknown'):
            fb_user.name = full_name
        fb_user.is_lead = True
        if fb_user.lead_status in (None, '', 'new', 'contacted'):
            fb_user.lead_status = 'new'
        stamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M')
        fb_user.notes = f"[Бот бүртгэл {stamp}]\n{summary}" + (f"\n\n{fb_user.notes}" if fb_user.notes else '')
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.exception("registration: could not update lead for psid=%s: %s", fb_user.facebook_id, e)
    try:
        send_telegram_notification(
            f"{'📝 Шинэ бүртгэл (бот)' if ok else '⚠️ Бүртгэл — гараар оруулах'}\n"
            f"👤 {fb_user.name or fb_user.facebook_id}\n{summary}"
        )
    except Exception as e:
        logger.warning("registration: Telegram ping failed: %s", e)
    return bot_reply if ok else REGISTRATION_FALLBACK_REPLY


def process_inbound_reply(fb_user_id, message_text, session_state):
    """Slow half of inbound handling, run in a background thread so the webhook
    can ACK Facebook in <1s (a fast 200 is what stops FB retrying the delivery
    on cold starts / slow model hops). Builds the prompt, calls Claude, sends
    the reply, and fires any handoff. Runs with its own app context + DB
    session via enqueue_background; errors are logged there, so a failure here
    just means no reply — never a crash or a Facebook retry.

    The fast half (dedupe, persist inbound, rate limit, funnel/name/phone
    capture, mute gate) already ran synchronously in webhook() before enqueue.
    """
    fb_user = db.session.get(FacebookUser, fb_user_id)
    if fb_user is None:
        return
    sender_id = fb_user.facebook_id

    # A human may have taken over between enqueue and now — stay silent.
    if fb_user.bot_muted_until and fb_user.bot_muted_until > datetime.utcnow():
        return

    # Read receipt + typing bubble so the wait feels responsive while the model
    # composes the reply. Best-effort — failures never block the reply.
    send_sender_action(sender_id, 'mark_seen')
    send_sender_action(sender_id, 'typing_on')

    handoff_pending = is_in_handoff(fb_user)
    handoff_just_triggered = False
    # Skip the keyword-handoff check if already in advisory mode.
    if not handoff_pending:
        handoff, reason = should_handoff(message_text, fb_user)
        if handoff:
            # Fire the AdminIssue + Telegram ping but DON'T send the static
            # user message — let the LLM craft a warm, mood-aware ack. The
            # HANDOFF_JUST_TRIGGERED prompt rule overrides advisory mode for
            # this one turn so the bot can still mention staff routing.
            trigger_handoff(fb_user, reason, message_text, send_user_message=False)
            handoff_pending = True
            handoff_just_triggered = True

    # Cross-session memory: a customer coming back after a gap gets a fresh
    # 2-3 line summary of the earlier conversation written onto the user row
    # (one cheap background-model call), which the prompt then carries.
    if session_state in ('gap', 'returning'):
        refresh_user_summary(fb_user)

    # Last 10 messages only — the tail is all the model needs for context
    # (the prompt's summary block covers what came before).
    recent_desc = (Message.query
                   .filter_by(facebook_user_id=fb_user.id)
                   .order_by(Message.created_at.desc())
                   .limit(10)
                   .all())
    conversation = [
        {"role": "user" if m.sender == 'user' else "assistant", "content": m.content}
        for m in reversed(recent_desc)
    ]

    first_name = first_name_of(fb_user.name)
    bot_response = generate_bot_response(
        message_text,
        conversation,
        session_state=session_state,
        funnel_stage=fb_user.funnel_stage or 'curious',
        user_first_name=first_name,
        handoff_pending=handoff_pending,
        handoff_just_triggered=handoff_just_triggered,
        # Ask for the name only once the customer is a lead (phone left) —
        # that is when staff need it, and it keeps discovery replies to one
        # question.
        ask_name=(not first_name) and bool(fb_user.is_lead),
        is_lead=bool(fb_user.is_lead),
        prior_summary=fb_user.bot_summary or '',
    )

    # Bot-initiated handoff (defer_to_staff tool): strip the hidden marker
    # before the customer sees anything; the handoff is fired below with the
    # model's category + reason so staff see why.
    bot_response, handoff_detail = extract_handoff(bot_response)

    # Bot-driven registration (submit_registration tool): submit the collected
    # fields to the centre's form, mirror the customer as a lead, ping staff.
    # If the form rejects the submission the customer still gets a truthful
    # reply and staff still get every field to register by hand.
    bot_response, registration = extract_registration(bot_response)
    if registration:
        bot_response = complete_registration(fb_user, registration, bot_response)

    # A human may have jumped in while the LLM was generating. We MUST end this
    # session's read transaction first (rollback) before re-reading the mute:
    # SQLite/WAL pins a snapshot at the transaction's first query, so a
    # bot_muted_until that the human's echo committed in a PARALLEL request
    # mid-generation is invisible to a plain re-read — and the bot would talk
    # right over the human. Rollback drops the stale snapshot; the re-get opens
    # a fresh one that sees the latest committed mute.
    db.session.rollback()
    fb_user = db.session.get(FacebookUser, fb_user_id)
    if fb_user is None:
        return
    if fb_user.bot_muted_until and fb_user.bot_muted_until > datetime.utcnow():
        logger.info(
            "Human took over mid-generation — suppressing bot reply for psid=%s",
            sender_id,
        )
        return

    bot_msg = Message(facebook_user_id=fb_user.id, sender='bot', content=bot_response)
    db.session.add(bot_msg)
    db.session.commit()

    send_facebook_message(sender_id, bot_response)

    # Bot-initiated handoff: the model called defer_to_staff, so fire the
    # AdminIssue + Telegram ping (the reply it wrote IS the customer message).
    # This is the ONLY bot-driven path — the old scan of the bot's reply for
    # "ажилтан холбогдоно"-style phrases is gone (it paged staff on every lead
    # and every registration offer).
    if not handoff_pending and handoff_detail:
        label = HANDOFF_CATEGORY_LABELS.get(handoff_detail['category'], handoff_detail['category'])
        reason = f"бот: {label}"
        if handoff_detail['reason']:
            reason += f" — {handoff_detail['reason']}"
        trigger_handoff(fb_user, reason, message_text, send_user_message=False)

    # Topic classifier — another model call; we're already off the webhook's
    # critical path, so just run it inline here.
    classify_user_topics(fb_user.id)


@csrf.exempt
@app.route('/webhook', methods=['POST'])
def webhook():
    raw_body = request.get_data()
    sig_header = request.headers.get('X-Hub-Signature-256', '')
    if not verify_facebook_signature(raw_body, sig_header):
        sig_preview = (sig_header[:14] + '…') if sig_header else '<missing>'
        logger.warning(
            "Webhook rejected: signature mismatch sig_header=%s body_len=%s app_secret_set=%s",
            sig_preview, len(raw_body), bool(FACEBOOK_APP_SECRET),
        )
        return jsonify({'error': 'invalid signature'}), 403

    data = request.get_json(silent=True) or {}

    entries = data.get('entry', []) if isinstance(data, dict) else []
    senders = [
        ev.get('sender', {}).get('id')
        for entry in entries
        for ev in entry.get('messaging', [])
    ]
    logger.info(
        "Webhook received object=%r entries=%s senders=%s",
        data.get('object'), len(entries), senders,
    )

    if data.get('object') == 'page':
        for entry in entries:
            for messaging_event in entry.get('messaging', []):
                sender_id = messaging_event.get('sender', {}).get('id')
                recipient_id = messaging_event.get('recipient', {}).get('id')
                event_keys = [k for k in messaging_event.keys() if k not in ('sender', 'recipient', 'timestamp')]
                logger.info(
                    "Webhook event sender=%s recipient=%s kinds=%s",
                    sender_id, recipient_id, event_keys,
                )

                if messaging_event.get('message'):
                    msg = messaging_event['message']

                    # Echo events: Facebook sends these for EVERY message the
                    # Page emits — both the bot's own Send API calls and replies
                    # a human agent types in the Page inbox. Our outgoing
                    # messages carry BOT_ECHO_TAG in `metadata`; an untagged
                    # echo means a human just took over, so pause the bot for
                    # HUMAN_TAKEOVER_MUTE_MINUTES. Either way an echo must never
                    # fall through to the inbound-message pipeline (sender here
                    # is the Page, recipient is the customer).
                    if msg.get('is_echo'):
                        is_bot_echo = msg.get('metadata') == BOT_ECHO_TAG
                        logger.info(
                            "Echo: recipient=%s is_bot=%s text=%r",
                            recipient_id, is_bot_echo, (msg.get('text') or '')[:40],
                        )
                        if not is_bot_echo:
                            # Human agent replied in the Page inbox → pause bot.
                            human_takeover_pause(recipient_id)
                        continue

                    message_text = msg.get('text')
                    mid = msg.get('mid')

                    # Resolve (or create) the sender up front so we can dedupe
                    # this delivery and gate on mute before doing any real work.
                    fb_user = FacebookUser.query.filter_by(facebook_id=sender_id).first()
                    if not fb_user:
                        user_info = get_facebook_user_info(sender_id)
                        fb_user = FacebookUser(
                            facebook_id=sender_id,
                            name=(user_info.get('name') or '').strip() or 'Unknown'
                        )
                        db.session.add(fb_user)
                        try:
                            db.session.commit()
                        except IntegrityError:
                            db.session.rollback()
                            fb_user = FacebookUser.query.filter_by(facebook_id=sender_id).first()
                    elif (fb_user.name or '').strip().lower() in ('', 'unknown'):
                        refresh_facebook_user_name(fb_user)

                    # Session classification looks at the PREVIOUS message, so
                    # compute it before this one is persisted.
                    last_msg = (Message.query
                                .filter_by(facebook_user_id=fb_user.id)
                                .order_by(Message.created_at.desc())
                                .first())
                    session_state = classify_session(last_msg.created_at if last_msg else None)

                    # Idempotency claim — the real fix for "rate-limited on the
                    # very first message". Facebook delivers at-least-once and
                    # RETRIES the same event (same mid) when our webhook is slow
                    # (a cold start after weeks idle, or a slow model hop). We
                    # persist the inbound message under a UNIQUE mid: the first
                    # delivery wins; a retry raises IntegrityError and is dropped
                    # here — BEFORE the rate limiter (so retries can't look like
                    # a flood) and before we generate/send (so no duplicate
                    # reply). When mid is absent we can't dedupe; we process
                    # normally (multiple NULL mids are allowed by the index).
                    user_msg = Message(
                        facebook_user_id=fb_user.id,
                        sender='user',
                        content=message_text,
                        mid=mid,
                    )
                    db.session.add(user_msg)
                    try:
                        db.session.commit()
                    except IntegrityError:
                        db.session.rollback()
                        logger.info(
                            "Duplicate Facebook delivery dropped: mid=%s sender=%s",
                            mid, sender_id,
                        )
                        continue

                    # Per-sender flood / cost guard. Retries are already deduped
                    # above, so this only ever sees DISTINCT messages now.
                    if sender_id and not check_rate_limit(sender_id):
                        continue

                    new_stage = detect_funnel_stage(message_text, fb_user.funnel_stage or 'curious')
                    if new_stage != fb_user.funnel_stage:
                        fb_user.funnel_stage = new_stage
                        db.session.commit()

                    # Name capture fallback. The FB User Profile API no longer
                    # returns names at Standard Access, so the bot's prompt
                    # asks customers directly. If the previous bot turn was
                    # that ask, treat THIS message as the name reply. Silent
                    # on ambiguous input — better blank than wrong.
                    if (fb_user.name or '').strip().lower() in ('', 'unknown'):
                        prior_bot = (Message.query
                                     .filter_by(facebook_user_id=fb_user.id, sender='bot')
                                     .order_by(Message.created_at.desc())
                                     .first())
                        if prior_bot and 'нэр' in (prior_bot.content or '').lower():
                            candidate = extract_name_from_reply(message_text)
                            if candidate:
                                fb_user.name = candidate
                                db.session.commit()

                    # Phone capture runs BEFORE the bot reply so we don't
                    # double up: previously a single inbound could trigger
                    # (a) the bot's main reply, (b) a deferral handoff, and
                    # (c) a separate "phone received" thank-you. By
                    # capturing the lead state up front the prompt can ack
                    # it in the single reply and we never send the extra
                    # thank-you message.
                    phone_match = PHONE_RE.search(message_text or '') if message_text else None
                    if phone_match and not fb_user.is_lead:
                        fb_user.phone = phone_match.group(0)
                        fb_user.is_lead = True
                        # A freshly-captured lead starts at 'new' (Шинэ). Staff
                        # move it to 'contacted' (Холбогдсон) only once they've
                        # actually reached out — see the mark_contacted action.
                        fb_user.lead_status = 'new'
                        db.session.commit()

                    now = datetime.utcnow()
                    # Three states for the bot's relationship to this user:
                    #   1) Normal           — no handoff, no mute. Bot does
                    #      its usual sales/help thing.
                    #   2) Advisory mode    — open handoff AdminIssue. Bot
                    #      keeps replying with full catalog help but won't
                    #      push for phone/registration again. Drives
                    #      handoff_pending=True into the prompt.
                    #   3) Staff takeover   — bot_muted_until in the future
                    #      (set explicitly via the admin "Take Over"
                    #      button). Bot is silent so staff can chat
                    #      directly via Facebook Page Inbox without bot
                    #      interference.
                    if fb_user.bot_muted_until and fb_user.bot_muted_until > now:
                        # State 3: staff is handling this person, silent drop.
                        continue
                    if fb_user.bot_muted_until and fb_user.bot_muted_until <= now:
                        # Mute window expired; reset and continue normally.
                        fb_user.bot_muted_until = None
                        db.session.commit()

                    # Hand the slow work (Claude reply + send + handoff pings +
                    # topic classify) to a background worker and ACK Facebook
                    # immediately. A fast 200 is what stops FB retrying the
                    # delivery on a cold start or slow model hop — the root cause
                    # behind the phantom first-message rate-limit. See
                    # process_inbound_reply above.
                    enqueue_background(
                        process_inbound_reply,
                        fb_user.id, message_text, session_state,
                    )

    return jsonify({'status': 'ok'}), 200


@app.route('/webhook', methods=['GET'])
def webhook_verify():
    """Facebook webhook verification"""
    verify_token = os.getenv('VERIFY_TOKEN', '').strip()
    if not verify_token:
        # Fail closed if VERIFY_TOKEN is unset — the previous hard-coded
        # default ('magic_bot_verify_token') is documented in the repo's
        # markdown files and would let anyone subscribe arbitrary webhooks.
        return 'VERIFY_TOKEN not configured', 403
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')

    if token == verify_token:
        return challenge
    return 'Invalid token', 403
