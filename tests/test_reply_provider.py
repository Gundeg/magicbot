"""Reply-provider switch (OpenAI ⇄ Gemini via the OpenAI-compatible endpoint).

The customer reply runs on `reply_client` / `REPLY_MODEL`, chosen at import
from GEMINI_API_KEY. These tests pin the parts that must not silently drift:

- generate_bot_response calls `reply_client` (not the background `client`),
  with the provider-correct token-cap parameter name and the configured model;
- the defer_to_staff tool is offered in NORMAL mode and withheld in advisory
  mode, regardless of provider;
- the import-time invariants between REPLY_PROVIDER, reply_client, and
  REPLY_MAX_TOKENS_PARAM hold.

No GEMINI_API_KEY is set in the test env (see conftest), so the resolved
provider here is OpenAI and `reply_client is client`. The capture tests patch
`reply_client.chat.completions.create`, so they assert the contract for
whichever provider is active — they pass unchanged if a Gemini key is present.
"""
import services


class _Msg:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, content, tool_calls=None):
        self.message = _Msg(content, tool_calls)


class _Resp:
    def __init__(self, content, tool_calls=None):
        self.choices = [_Choice(content, tool_calls)]


def _capture_create(monkeypatch, content='Сайн байна уу'):
    """Patch the reply client's create() and return the dict it was called with."""
    captured = {}

    def _create(**kwargs):
        captured.update(kwargs)
        return _Resp(content)

    monkeypatch.setattr(services.reply_client.chat.completions, 'create', _create)
    return captured


def test_normal_mode_uses_reply_client_token_param_and_tool(app, db_session, monkeypatch):
    captured = _capture_create(monkeypatch, 'Тэгье, танд тусалъя')
    out = services.generate_bot_response('Сайн уу', [])

    assert out == 'Тэгье, танд тусалъя'
    # Targets the configured reply model...
    assert captured['model'] == services.REPLY_MODEL
    # ...with the provider-correct token cap param name and value...
    assert services.REPLY_MAX_TOKENS_PARAM in captured
    assert captured[services.REPLY_MAX_TOKENS_PARAM] == services.REPLY_MAX_TOKENS
    # ...and never sends an explicit temperature (gpt-5.x rejects it).
    assert 'temperature' not in captured
    # Normal mode offers the knowledge-gap handoff tool.
    assert captured.get('tools') == services.DEFER_TO_STAFF_TOOL


def test_advisory_mode_omits_defer_tool(app, db_session, monkeypatch):
    captured = _capture_create(monkeypatch, 'Ажилтан удахгүй холбогдоно')
    services.generate_bot_response('Сайн уу', [], handoff_pending=True)
    # Already routed to staff — the tool must NOT be offered.
    assert 'tools' not in captured


def test_defer_tool_call_becomes_handoff_marker(app, db_session, monkeypatch):
    """A defer_to_staff tool call is translated into the internal HANDOFF_MARKER
    prefix the webhook understands — same contract on Gemini or OpenAI."""
    tool_call = type('TC', (), {
        'function': type('F', (), {
            'name': 'defer_to_staff',
            'arguments': '{"reply_to_customer": "Ажилтан тантай эргэж холбогдоно"}',
        })(),
    })()

    def _create(**kwargs):
        return _Resp(None, tool_calls=[tool_call])

    monkeypatch.setattr(services.reply_client.chat.completions, 'create', _create)
    out = services.generate_bot_response('Нэвтрэх нууц үг минь юу вэ?', [])
    assert out.startswith(services.HANDOFF_MARKER)
    assert 'Ажилтан тантай эргэж холбогдоно' in out


def test_provider_invariants_hold():
    """The import-time wiring is internally consistent for either provider."""
    assert services.REPLY_PROVIDER in ('openai', 'gemini')
    # reply_client aliases the background client iff we're on OpenAI.
    assert (services.reply_client is services.client) == (services.REPLY_PROVIDER == 'openai')
    # Token-cap param name is the provider-correct one.
    expected_param = 'max_tokens' if services.REPLY_PROVIDER == 'gemini' else 'max_completion_tokens'
    assert services.REPLY_MAX_TOKENS_PARAM == expected_param
    # Label matches the provider.
    assert services.REPLY_PROVIDER_LABEL == ('Gemini' if services.REPLY_PROVIDER == 'gemini' else 'OpenAI')
