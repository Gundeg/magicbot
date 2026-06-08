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
from services import (PHONE_RE, bot_response_implies_handoff,
                      check_rate_limit, classify_session, classify_user_topics,
                      detect_funnel_stage, enqueue_background,
                      extract_handoff_marker, extract_name_from_reply,
                      first_name_of,
                      generate_bot_response, get_facebook_user_info,
                      is_in_handoff, refresh_facebook_user_name,
                      send_facebook_message, should_handoff,
                      trigger_handoff,
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

                    # Over the per-sender rate limit: silently drop. We used to
                    # reply "Та маш олон мессеж бичиж байна…", but that spammed
                    # chatty customers AND fired even during a human takeover,
                    # because this runs before the mute check below. The limiter
                    # still protects against floods / cost — it just no longer
                    # talks back.
                    if sender_id and not check_rate_limit(sender_id):
                        continue

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

                    last_msg = (Message.query
                                .filter_by(facebook_user_id=fb_user.id)
                                .order_by(Message.created_at.desc())
                                .first())
                    session_state = classify_session(last_msg.created_at if last_msg else None)

                    new_stage = detect_funnel_stage(message_text, fb_user.funnel_stage or 'curious')
                    if new_stage != fb_user.funnel_stage:
                        fb_user.funnel_stage = new_stage
                        db.session.commit()

                    user_msg = Message(
                        facebook_user_id=fb_user.id,
                        sender='user',
                        content=message_text
                    )
                    db.session.add(user_msg)
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
                    handoff_pending = is_in_handoff(fb_user)
                    handoff_just_triggered = False

                    # Skip the keyword-handoff check if the user is already
                    # in advisory mode — they've been routed to staff
                    # already, no need to trigger again.
                    if not handoff_pending:
                        handoff, reason = should_handoff(message_text, fb_user)
                        if handoff:
                            # Fire the AdminIssue + Telegram ping but DON'T
                            # send the static user message — fall through
                            # to the LLM so the acknowledgement is warm,
                            # mood-aware, and quotes the right office-hours
                            # ETA. The HANDOFF_JUST_TRIGGERED rule the
                            # prompt builder will inject overrides advisory
                            # mode for this one turn so the bot can still
                            # mention staff routing.
                            trigger_handoff(
                                fb_user, reason, message_text,
                                send_user_message=False,
                            )
                            handoff_pending = True
                            handoff_just_triggered = True

                    # Pull only the last 10 messages instead of the entire
                    # conversation. For a chatty user `.all()` was loading
                    # the full history just to slice the tail.
                    recent_desc = (Message.query
                                   .filter_by(facebook_user_id=fb_user.id)
                                   .order_by(Message.created_at.desc())
                                   .limit(10)
                                   .all())
                    conversation = [
                        {"role": "user" if m.sender == 'user' else "assistant", "content": m.content}
                        for m in reversed(recent_desc)
                    ]

                    bot_response = generate_bot_response(
                        message_text,
                        conversation,
                        session_state=session_state,
                        funnel_stage=fb_user.funnel_stage or 'curious',
                        user_first_name=first_name_of(fb_user.name),
                        handoff_pending=handoff_pending,
                        handoff_just_triggered=handoff_just_triggered,
                    )

                    # Knowledge-gap handoff: the bot prefixes its reply with a
                    # hidden [HANDOFF] marker when it lacks the info to answer
                    # and is deferring to staff (KNOWLEDGE_GAP_HANDOFF_RULE).
                    # Strip it before the customer sees anything; the handoff is
                    # fired below. Stripping is unconditional; firing is gated on
                    # not-already-in-handoff, like the deferral-phrase path.
                    bot_response, knowledge_gap_handoff = extract_handoff_marker(bot_response)

                    # A human may have jumped into the Page inbox while the LLM
                    # was generating (their echo set bot_muted_until in another
                    # request). Re-read the flag and bail BEFORE sending so the
                    # bot never talks over a human who just replied. We don't
                    # persist the unsent reply either.
                    db.session.expire(fb_user, ['bot_muted_until'])
                    if fb_user.bot_muted_until and fb_user.bot_muted_until > datetime.utcnow():
                        logger.info(
                            "Human took over mid-generation — suppressing bot reply for psid=%s",
                            sender_id,
                        )
                        continue

                    bot_msg = Message(
                        facebook_user_id=fb_user.id,
                        sender='bot',
                        content=bot_response
                    )
                    db.session.add(bot_msg)
                    db.session.commit()

                    send_facebook_message(sender_id, bot_response)

                    # Implicit handoff: if the bot's own reply was a
                    # "defer to staff" message, mute the bot, create an
                    # AdminIssue, and ping Telegram — but skip the
                    # standard handoff user-message since the bot already
                    # sent one. Catches every staff-deferral path,
                    # whether or not a user keyword matched earlier.
                    # Skipped in advisory mode: the user is already in
                    # handoff and the advisory rule should keep the bot
                    # from emitting deferral phrases anyway.
                    if not handoff_pending:
                        if knowledge_gap_handoff:
                            # Bot explicitly signalled it lacked the info and
                            # deferred to staff. Its own reply already carries
                            # the warm ETA + "anything else?" message, so we
                            # don't send the static handoff message.
                            trigger_handoff(
                                fb_user,
                                'bot_knowledge_gap',
                                message_text,
                                send_user_message=False,
                            )
                        else:
                            deferral_phrase = bot_response_implies_handoff(bot_response)
                            if deferral_phrase:
                                trigger_handoff(
                                    fb_user,
                                    f'bot_deferral:{deferral_phrase}',
                                    message_text,
                                    send_user_message=False,
                                )

                    # Fire-and-forget the topic classifier — it's an
                    # OpenAI call and shouldn't keep Facebook waiting on
                    # the webhook response. Errors are logged inside the
                    # function so failures here don't affect the user.
                    enqueue_background(classify_user_topics, fb_user.id)

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
