"""Quality batch (2026-11): the pieces that make the bot behave like a sales
consultant rather than a keyword router.

- the persisted copy of the inbound message is not sent to the model twice;
- the defer_to_staff tool's category + reason travel through the hidden
  marker to the webhook (and unknown / bare forms degrade safely);
- the name question is asked only when the webhook says so (lead captured);
- the customer-state block (lead, prior summary) reaches the prompt;
- refresh_user_summary writes the cross-session memory and fails soft.

No network: the Claude client is stubbed everywhere.
"""
from types import SimpleNamespace

import services
from services import _prompt


def _text(text):
    return SimpleNamespace(type='text', text=text)


def _resp(*blocks, stop_reason='end_turn'):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


# ----------------------------------------------------------- history shaping

def test_claude_messages_drops_persisted_copy_of_inbound():
    """The webhook stores the inbound row before the reply runs, so the
    last-N window ends with the message being answered. Send it once."""
    history = [
        {'role': 'user', 'content': 'Сайн уу'},
        {'role': 'assistant', 'content': 'Сайн байна уу!'},
        {'role': 'user', 'content': 'Үнэ хэд вэ?'},      # the persisted copy
    ]
    out = services._claude_messages(history, 'Үнэ хэд вэ?')
    assert out == [
        {'role': 'user', 'content': 'Сайн уу'},
        {'role': 'assistant', 'content': 'Сайн байна уу!'},
        {'role': 'user', 'content': 'Үнэ хэд вэ?'},
    ]


def test_claude_messages_keeps_a_genuine_earlier_repeat():
    history = [
        {'role': 'user', 'content': 'Сайн уу'},   # sent earlier
        {'role': 'user', 'content': 'Сайн уу'},   # persisted copy of this turn
    ]
    out = services._claude_messages(history, 'Сайн уу')
    assert out == [{'role': 'user', 'content': 'Сайн уу'}] * 2


# ----------------------------------------------------------- handoff marker

def test_format_and_extract_handoff_roundtrip():
    marker = services.format_handoff_marker('wants_human', 'хүнтэй ярих | гэсэн]\nшууд')
    cleaned, detail = services.extract_handoff(f"{marker} Ажилтан тантай холбогдоно")
    assert cleaned == 'Ажилтан тантай холбогдоно'
    assert detail['category'] == 'wants_human'
    # the reason can never break the marker syntax
    assert ']' not in detail['reason'] and '|' not in detail['reason'] and '\n' not in detail['reason']
    assert detail['reason'].startswith('хүнтэй ярих')


def test_extract_handoff_bare_marker_unknown_category_and_plain_reply(app):
    assert services.extract_handoff('[HANDOFF] Хариу') == (
        'Хариу', {'category': 'knowledge_gap', 'reason': ''})
    _, detail = services.extract_handoff('[HANDOFF:bogus|x] Хариу')
    assert detail['category'] == 'knowledge_gap'
    assert services.extract_handoff('Энгийн хариу') == ('Энгийн хариу', None)
    assert services.extract_handoff(None) == (None, None)
    # marker-only reply still produces a real message
    cleaned, detail = services.extract_handoff('[HANDOFF:complaint|гомдол]')
    assert cleaned.strip() and detail['category'] == 'complaint'


def test_generate_carries_tool_category_and_reason(app, db_session, monkeypatch):
    tool = SimpleNamespace(
        type='tool_use', name='defer_to_staff', id='toolu_1',
        input={'category': 'complaint', 'reply_to_customer': 'Уучлаарай, ойлгож байна.',
               'reason': 'үйлчилгээнд бухимдсан'},
    )
    monkeypatch.setattr(services.client.messages, 'create',
                        lambda **kw: _resp(tool, stop_reason='tool_use'))
    out = services.generate_bot_response('Маш муу үйлчилгээ', [])
    cleaned, detail = services.extract_handoff(out)
    assert cleaned == 'Уучлаарай, ойлгож байна.'
    assert detail == {'category': 'complaint', 'reason': 'үйлчилгээнд бухимдсан'}


def test_defer_tool_schema_requires_category():
    tool = services.DEFER_TO_STAFF_TOOL[0]
    assert tool['input_schema']['properties']['category']['enum'] == list(services.HANDOFF_CATEGORIES)
    assert 'category' in tool['input_schema']['required']


# ----------------------------------------------------------- prompt blocks

def test_name_question_only_when_asked(app, db_session):
    assert 'хэн гэдэг' not in _prompt.build_system_prompt()
    assert 'хэн гэдэг' in _prompt.build_system_prompt(ask_name=True)
    known = _prompt.build_system_prompt(user_first_name='Бат', ask_name=True)
    assert 'ХЭРЭГЛЭГЧИЙН НЭР: Бат' in known and 'хэн гэдэг' not in known


def test_customer_state_block(app, db_session):
    prompt = _prompt.build_system_prompt(
        is_lead=True, prior_summary='Орой суух хүсэлтэй, үнэ асуусан.',
    )
    assert 'утасны дугаараа аль хэдийн үлдээсэн' in prompt
    assert 'ӨМНӨХ ЯРИАНЫ ХУРААНГУЙ' in prompt
    assert 'Орой суух хүсэлтэй, үнэ асуусан.' in prompt

    plain = _prompt.build_system_prompt()
    assert 'ӨМНӨХ ЯРИАНЫ ХУРААНГУЙ' not in plain
    assert 'аль хэдийн үлдээсэн' not in plain


def test_generate_passes_customer_state_to_prompt(app, db_session, monkeypatch):
    captured = {}

    def _create(**kw):
        captured.update(kw)
        return _resp(_text('За'))
    monkeypatch.setattr(services.client.messages, 'create', _create)
    services.generate_bot_response('Сайн уу', [], ask_name=True, is_lead=True,
                                   prior_summary='Хосолсон анги сонирхсон.')
    stable, volatile = (b['text'] for b in captured['system'])
    # per-customer state lands in the volatile (uncached) half only
    assert 'хэн гэдэг' in volatile and 'хэн гэдэг' not in stable
    assert 'Хосолсон анги сонирхсон.' in volatile and 'Хосолсон анги' not in stable


# ----------------------------------------------------------- prompt caching split

def test_stable_half_is_identical_across_customers_and_states(app, db_session):
    a_stable, a_vol = _prompt.build_system_blocks(session_state='new', funnel_stage='curious')
    b_stable, b_vol = _prompt.build_system_blocks(
        session_state='active', funnel_stage='pricing', user_first_name='Бат',
        handoff_pending=True, is_lead=True, prior_summary='Өмнө нь үнэ асуусан.',
    )
    assert a_stable == b_stable                      # cacheable prefix
    assert a_vol != b_vol
    # everything that changes per request / per minute is in the volatile half
    for marker in ('ОДООГИЙН ЦАГ', 'ХЭРЭГЛЭГЧИЙН ҮЕ ШАТ', 'ЭНЭ ЯРИАНЫ ТӨЛӨВ'):
        assert marker not in a_stable and marker in a_vol
    assert 'ХАНДОФФ ХҮЛЭЭЛТИЙН ГОРИМ' in b_vol and 'ХАНДОФФ ХҮЛЭЭЛТИЙН ГОРИМ' not in b_stable
    # the persona + catalog + rules stay in the stable half
    # (exact headings: the volatile handoff rule refers to the sales block by name)
    for marker in ('ИДЭВХТЭЙ АНГИУДЫН АЛБАН ЁСНЫ ЖАГСААЛТ', 'ЧУХАЛ ДҮРМҮҮД:', 'БОРЛУУЛАЛТЫН ЗАН ҮЙЛ (туршлагатай'):
        assert marker in a_stable and marker not in a_vol
    # the joined form is what the string-based callers see
    assert _prompt.build_system_prompt() == f"{a_stable}\n\n{a_vol}"


# ----------------------------------------------------------- cross-session memory

def _user_with_messages(facebook_id, pairs):
    from extensions import db
    from models import FacebookUser, Message
    user = FacebookUser(facebook_id=facebook_id, name='Summary')
    db.session.add(user)
    db.session.flush()
    for sender, content in pairs:
        db.session.add(Message(facebook_user_id=user.id, sender=sender, content=content))
    db.session.commit()
    return user


def _cleanup(user):
    from extensions import db
    from models import Message
    Message.query.filter_by(facebook_user_id=user.id).delete()
    db.session.delete(user)
    db.session.commit()


FOUR_TURNS = [('user', 'Сайн уу'), ('bot', 'Сайн байна уу!'),
              ('user', 'Орой суух анги бий юу?'), ('bot', 'Бий, 100% онлайн.')]


def test_refresh_user_summary_writes_memory(app, db_session, monkeypatch):
    calls = {}

    def _create(**kw):
        calls.update(kw)
        return _resp(_text('Орой суух хүсэлтэй, онлайн ангийг сонирхсон.'))
    monkeypatch.setattr(services.client.messages, 'create', _create)

    user = _user_with_messages('psid-summary-1', FOUR_TURNS)
    try:
        out = services.refresh_user_summary(user)
        assert out == 'Орой суух хүсэлтэй, онлайн ангийг сонирхсон.'
        assert user.bot_summary == out and user.bot_summary_at is not None
        assert calls['model'] == services.BACKGROUND_MODEL
        assert 'Орой суух анги бий юу?' in calls['messages'][0]['content']
    finally:
        _cleanup(user)


def test_refresh_user_summary_skips_short_conversations(app, db_session, monkeypatch):
    monkeypatch.setattr(services.client.messages, 'create',
                        lambda **kw: (_ for _ in ()).throw(AssertionError('must not call')))
    user = _user_with_messages('psid-summary-2', FOUR_TURNS[:2])
    try:
        assert services.refresh_user_summary(user) is None
        assert user.bot_summary is None
    finally:
        _cleanup(user)


def test_refresh_user_summary_fails_soft(app, db_session, monkeypatch):
    def _boom(**kw):
        raise RuntimeError('model down')
    monkeypatch.setattr(services.client.messages, 'create', _boom)
    user = _user_with_messages('psid-summary-3', FOUR_TURNS)
    user.bot_summary = 'хуучин хураангуй'
    try:
        assert services.refresh_user_summary(user) is None
        assert user.bot_summary == 'хуучин хураангуй'   # previous memory kept
    finally:
        _cleanup(user)
