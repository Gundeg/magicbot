"""Facebook Messenger webhook.

CSRF is intentionally disabled — Facebook POSTs here directly, not a browser.
The request is authenticated by HMAC-SHA256 against the raw body using
FACEBOOK_APP_SECRET (see services.verify_facebook_signature).
"""
import os
from datetime import datetime

from flask import jsonify, request
from sqlalchemy.exc import IntegrityError

from app import app, csrf
from extensions import db
from models import FacebookUser, Message
from services import (PHONE_RE, bot_response_implies_handoff,
                      check_rate_limit, classify_conversation,
                      classify_session, detect_funnel_stage, first_name_of,
                      generate_bot_response, get_facebook_user_info,
                      is_in_handoff, refresh_facebook_user_name,
                      send_facebook_message, should_handoff,
                      trigger_handoff,
                      verify_facebook_signature, FACEBOOK_APP_SECRET,
                      RATE_LIMIT_REPLY)


@csrf.exempt
@app.route('/webhook', methods=['POST'])
def webhook():
    raw_body = request.get_data()
    sig_header = request.headers.get('X-Hub-Signature-256', '')
    if not verify_facebook_signature(raw_body, sig_header):
        sig_preview = (sig_header[:14] + '…') if sig_header else '<missing>'
        print(
            f"Webhook rejected: signature mismatch "
            f"sig_header={sig_preview} body_len={len(raw_body)} "
            f"app_secret_set={bool(FACEBOOK_APP_SECRET)}"
        )
        return jsonify({'error': 'invalid signature'}), 403

    data = request.get_json(silent=True) or {}

    entries = data.get('entry', []) if isinstance(data, dict) else []
    senders = [
        ev.get('sender', {}).get('id')
        for entry in entries
        for ev in entry.get('messaging', [])
    ]
    print(
        f"Webhook received object={data.get('object')!r} "
        f"entries={len(entries)} senders={senders}"
    )

    if data.get('object') == 'page':
        for entry in entries:
            for messaging_event in entry.get('messaging', []):
                sender_id = messaging_event.get('sender', {}).get('id')
                recipient_id = messaging_event.get('recipient', {}).get('id')
                event_keys = [k for k in messaging_event.keys() if k not in ('sender', 'recipient', 'timestamp')]
                print(
                    f"Webhook event sender={sender_id} recipient={recipient_id} "
                    f"kinds={event_keys}"
                )

                if messaging_event.get('message'):
                    message_text = messaging_event['message'].get('text')

                    if sender_id and not check_rate_limit(sender_id):
                        send_facebook_message(sender_id, RATE_LIMIT_REPLY)
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

                    # Skip the keyword-handoff check if the user is already
                    # in advisory mode — they've been routed to staff
                    # already, no need to trigger again.
                    if not handoff_pending:
                        handoff, reason = should_handoff(message_text, fb_user)
                        if handoff:
                            trigger_handoff(fb_user, reason, message_text)
                            continue

                    history = Message.query.filter_by(facebook_user_id=fb_user.id).order_by(Message.created_at).all()
                    conversation = [
                        {"role": "user" if m.sender == 'user' else "assistant", "content": m.content}
                        for m in history[-10:]
                    ]

                    bot_response = generate_bot_response(
                        message_text,
                        conversation,
                        session_state=session_state,
                        funnel_stage=fb_user.funnel_stage or 'curious',
                        user_first_name=first_name_of(fb_user.name),
                        handoff_pending=handoff_pending,
                    )

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
                        deferral_phrase = bot_response_implies_handoff(bot_response)
                        if deferral_phrase:
                            trigger_handoff(
                                fb_user,
                                f'bot_deferral:{deferral_phrase}',
                                message_text,
                                send_user_message=False,
                            )

                    try:
                        classify_conversation(fb_user)
                    except Exception:
                        pass

                    phone_match = PHONE_RE.search(message_text)
                    if phone_match and not fb_user.is_lead:
                        fb_user.phone = phone_match.group(0)
                        fb_user.is_lead = True
                        fb_user.lead_status = 'contacted'
                        db.session.commit()

                        handoff_msg = (
                            "Баярлалаа, дугаараа үлдээснийг тань хүлээж авлаа. "
                            "Манай бүртгэлийн ажилтан удахгүй тантай эргэж холбогдох болно."
                        )
                        send_facebook_message(sender_id, handoff_msg)

    return jsonify({'status': 'ok'}), 200


@app.route('/webhook', methods=['GET'])
def webhook_verify():
    """Facebook webhook verification"""
    verify_token = os.getenv('VERIFY_TOKEN', 'magic_bot_verify_token')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')

    if token == verify_token:
        return challenge
    return 'Invalid token', 403
