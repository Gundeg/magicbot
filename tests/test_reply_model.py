"""Customer reply on Claude (`services.client` / REPLY_MODEL).

These tests pin the request contract that must not silently drift:

- generate_bot_response targets REPLY_MODEL with REPLY_MAX_TOKENS and the
  REPLY_EFFORT depth control, carries the prompt in `system`, and never sends
  sampling params, a `thinking` override (Opus 5.5 rejects turning thinking
  off) or a forced tool_choice (also rejected);
- the defer_to_staff tool is offered in NORMAL mode and withheld in advisory /
  just-triggered modes, and a tool_use block becomes the internal
  HANDOFF_MARKER the webhook understands;
- the stored history is reshaped for the Messages API (no empty turns, no
  leading assistant turn, inbound message last);
- an empty model reply never reaches the customer.

The capture tests patch `services.client.messages.create`, so nothing here
touches the network.
"""
from types import SimpleNamespace

import services


def _text(text):
    return SimpleNamespace(type='text', text=text)


def _tool_use(name, input):
    return SimpleNamespace(type='tool_use', name=name, input=input, id='toolu_1')


def _response(*blocks, stop_reason='end_turn'):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


def _capture_create(monkeypatch, response=None):
    """Patch the client's create() and return the kwargs it was called with."""
    captured = {}
    resp = response if response is not None else _response(_text('Сайн байна уу'))

    def _create(**kwargs):
        captured.update(kwargs)
        return resp

    monkeypatch.setattr(services.client.messages, 'create', _create)
    return captured


def test_normal_mode_request_contract(app, db_session, monkeypatch):
    captured = _capture_create(monkeypatch, _response(_text('Тэгье, танд тусалъя')))
    out = services.generate_bot_response('Сайн уу', [])

    assert out == 'Тэгье, танд тусалъя'
    assert captured['model'] == services.REPLY_MODEL
    assert captured['max_tokens'] == services.REPLY_MAX_TOKENS
    assert captured['output_config'] == {'effort': services.REPLY_EFFORT}
    # The prompt travels in `system`, not as a system-role message.
    assert isinstance(captured['system'], str) and captured['system']
    assert all(m['role'] in ('user', 'assistant') for m in captured['messages'])
    assert captured['messages'][-1] == {'role': 'user', 'content': 'Сайн уу'}
    # Never sampling params, never a thinking override, never a forced tool.
    for forbidden in ('temperature', 'top_p', 'top_k', 'thinking', 'tool_choice'):
        assert forbidden not in captured
    # Normal mode offers the knowledge-gap handoff tool.
    assert captured['tools'] == services.DEFER_TO_STAFF_TOOL


def test_advisory_mode_omits_defer_tool(app, db_session, monkeypatch):
    captured = _capture_create(monkeypatch)
    services.generate_bot_response('Сайн уу', [], handoff_pending=True)
    # Already routed to staff — the tool must NOT be offered.
    assert 'tools' not in captured


def test_just_triggered_mode_omits_defer_tool(app, db_session, monkeypatch):
    captured = _capture_create(monkeypatch)
    services.generate_bot_response(
        'Сайн уу', [], handoff_pending=True, handoff_just_triggered=True,
    )
    assert 'tools' not in captured


def test_defer_tool_use_becomes_handoff_marker(app, db_session, monkeypatch):
    """A defer_to_staff tool_use block is translated into the internal
    HANDOFF_MARKER prefix; the tool call wins over any preamble text."""
    resp = _response(
        _text('Нэг минут, шалгаад үзье.'),
        _tool_use('defer_to_staff', {
            'reply_to_customer': 'Ажилтан тантай эргэж холбогдоно',
            'reason': 'platform login not in knowledge base',
        }),
        stop_reason='tool_use',
    )
    _capture_create(monkeypatch, resp)
    out = services.generate_bot_response('Нэвтрэх нууц үг минь юу вэ?', [])
    assert out.startswith(services.HANDOFF_MARKER)
    assert 'Ажилтан тантай эргэж холбогдоно' in out
    assert 'Нэг минут' not in out


def test_defer_tool_use_without_message_uses_static_reply(app, db_session, monkeypatch):
    """The customer never receives a bare marker — a warm static deferral
    fills in when the tool call carries no reply_to_customer."""
    _capture_create(monkeypatch, _response(
        _tool_use('defer_to_staff', {}), stop_reason='tool_use',
    ))
    out = services.generate_bot_response('Нэвтрэх линк?', [])
    assert out.startswith(services.HANDOFF_MARKER)
    assert out[len(services.HANDOFF_MARKER):].strip() == services._static_handoff_reply()


def test_history_is_reshaped_for_messages_api(app, db_session, monkeypatch):
    """The last-10 window can open on a bot turn and may contain empty rows;
    the Messages API rejects both, so they are dropped before the call."""
    captured = _capture_create(monkeypatch)
    history = [
        {'role': 'assistant', 'content': 'Сайн байна уу!'},   # leading bot turn
        {'role': 'user', 'content': '   '},                   # empty turn
        {'role': 'user', 'content': 'Үнэ хэд вэ?'},
        {'role': 'assistant', 'content': '350,000₮'},
    ]
    services.generate_bot_response('Хуваарь?', history)
    assert captured['messages'] == [
        {'role': 'user', 'content': 'Үнэ хэд вэ?'},
        {'role': 'assistant', 'content': '350,000₮'},
        {'role': 'user', 'content': 'Хуваарь?'},
    ]


def test_empty_reply_degrades_to_apology(app, db_session, monkeypatch):
    """Thinking can exhaust max_tokens before any text is produced; the
    customer must still get a message, never an empty send."""
    _capture_create(monkeypatch, _response(stop_reason='max_tokens'))
    out = services.generate_bot_response('Сайн уу', [])
    assert out == services.TRANSIENT_ERROR_REPLY


def test_empty_reply_after_fresh_handoff_uses_handoff_template(app, db_session, monkeypatch):
    """Right after a handoff fired, a missing model reply falls back to the
    static handoff template so the customer still hears about staff routing."""
    _capture_create(monkeypatch, _response(stop_reason='max_tokens'))
    out = services.generate_bot_response(
        'Сайн уу', [], handoff_pending=True, handoff_just_triggered=True,
    )
    assert out == services._static_handoff_reply()
    assert out != services.TRANSIENT_ERROR_REPLY


def test_model_config_invariants():
    """The import-time wiring is a Claude-only deployment."""
    assert services.REPLY_MODEL.startswith('claude-')
    assert services.BACKGROUND_MODEL.startswith('claude-')
    assert services.REPLY_EFFORT in ('low', 'medium', 'high', 'xhigh', 'max')
    assert services.REPLY_MAX_TOKENS > 0
    assert services.REPLY_PROVIDER_LABEL.startswith('Claude')
    # Messages API tool shape, not the OpenAI function-calling wrapper.
    tool = services.DEFER_TO_STAFF_TOOL[0]
    assert tool['name'] == 'defer_to_staff'
    assert tool['input_schema']['required'] == ['reply_to_customer']
    assert 'function' not in tool
