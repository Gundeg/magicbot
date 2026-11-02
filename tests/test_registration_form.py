"""Bot-driven registration into the GreenSoft form (services/_registration).

Offline: requests.get / requests.post are stubbed with the real form's
definition shape and the reply the form gave on the approved 2026-11-03 test
(status ok, logid). Covers: configuration gate, label -> option-value mapping
from the live definition, the multipart payload (honeypot empty, widget field
names), success / rejection handling, the hidden marker round-trip, the tool
being offered + its call becoming the marker, the prompt rule gating, and the
webhook completing a registration (lead + Telegram + truthful reply).
"""
import json
from types import SimpleNamespace

import pytest

import services
from services import _prompt, _registration as reg

FORM_URL = 'https://forms.greensoft.mn/1955/form/5'
DEFINITION = {
    'inputs': [
        {'type': 'header', 'label': 'Бүртгэл'},
        {'name': 'lastname', 'type': 'text', 'label': 'Овог', 'required': 'True'},
        {'name': 'firstname', 'type': 'text', 'label': 'Нэр', 'required': 'True'},
        {'name': 'phone', 'type': 'text', 'label': 'Утас', 'required': 'True'},
        {'name': 'email', 'type': 'text', 'label': 'Имэйл', 'required': 'True'},
        {'name': 'gs_company', 'type': 'text', 'label': 'Байгууллагын нэр', 'required': 'False'},
        {'name': 'select-A', 'type': 'select', 'label': 'Та суралцах салбараа сонгоно уу',
         'values': [{'label': 'Улаанбаатар', 'value': 'option_1', 'selected': True},
                    {'label': 'Эрдэнэт', 'value': 'option_2'},
                    {'label': 'Дархан', 'value': 'option_3'}]},
        {'name': 'select-B', 'type': 'select', 'label': 'Та сонирхож байгаа сургалтаа сонгоно уу.',
         'values': [{'label': 'Танхимын сургалт', 'value': 'option_1', 'selected': True},
                    {'label': 'Танхим онлайн хосолсон сургалт', 'value': 'option_2'},
                    {'label': '100% онлайн сургалт', 'value': 'option_3'},
                    {'label': 'Magic Finance программын сургалт', 'value': ''}]},
        {'name': 'select-C', 'type': 'select', 'label': 'Та суралцах ээлжээ сонгоно уу',
         'required': 'True',
         'values': [{'label': 'Өглөө', 'value': 'option_1', 'selected': True},
                    {'label': 'Өдөр', 'value': 'option_2'},
                    {'label': 'Орой', 'value': 'option_3'}]},
    ]
}
CUSTOMER = {'lastname': 'Бат', 'firstname': 'Сараа', 'phone': '99112233',
            'email': 'saraa@example.com', 'company': '', 'branch': '',
            'course': '100% онлайн', 'shift': 'орой'}


class _Resp:
    def __init__(self, status=200, payload=None, text=''):
        self.status_code = status
        self._payload = payload
        self.text = text if text else json.dumps(payload or {}, ensure_ascii=False)
        self.headers = {'content-type': 'application/json' if payload is not None else 'text/html'}

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(reg, 'REGISTRATION_FORM_URL', FORM_URL)
    monkeypatch.setattr(reg, '_definition_cache', {'at': 0.0, 'value': None})
    monkeypatch.setattr(reg.requests, 'get', lambda *a, **k: _Resp(payload=DEFINITION))
    return FORM_URL


def test_not_configured_by_default():
    assert reg.REGISTRATION_FORM_URL == ''
    assert reg.is_configured() is False
    assert reg.submit_registration_form(CUSTOMER) == (False, 'REGISTRATION_FORM_URL not configured')


def test_payload_maps_labels_to_option_values_and_keeps_honeypot_empty(configured):
    definition = reg.load_form_definition()
    payload = reg.build_payload(CUSTOMER, definition)
    assert payload['siteid'] == '1955' and payload['formid'] == '5'
    assert payload['formdesc'] == ''                       # honeypot
    assert payload['lastname'] == 'Бат' and payload['firstname'] == 'Сараа'
    assert payload['phone'] == '99112233' and payload['email'] == 'saraa@example.com'
    assert payload['select-A'] == 'option_1'                # default branch (Улаанбаатар)
    assert payload['select-B'] == 'option_3'                # '100% онлайн' -> startswith match
    assert payload['select-C'] == 'option_3'                # 'орой' -> case-insensitive exact
    assert json.loads(payload['mainInputs']) == {'siteid': 1955, 'memberid': '', 'formid': 5}
    assert int(payload['_gs_form_token']) > 0


def test_submit_success_returns_logid(configured, monkeypatch):
    sent = {}

    def _post(url, files=None, headers=None, timeout=None):
        sent['url'] = url
        sent['fields'] = {k: v[1] for k, v in files.items()}
        return _Resp(payload={'status': 'ok', 'logid': 362, 'shared_token': 'x', 'member_id': 904105})
    monkeypatch.setattr(reg.requests, 'post', _post)
    assert reg.submit_registration_form(CUSTOMER) == (True, '362')
    assert sent['url'] == FORM_URL
    assert sent['fields']['email'] == 'saraa@example.com' and sent['fields']['formdesc'] == ''


def test_submit_rejection_and_network_error_fail_soft(configured, monkeypatch):
    monkeypatch.setattr(reg.requests, 'post', lambda *a, **k: _Resp(payload={'status': 'error'}))
    ok, info = reg.submit_registration_form(CUSTOMER)
    assert ok is False and info.startswith('HTTP 200')

    def _boom(*a, **k):
        raise reg.requests.ConnectionError('down')
    monkeypatch.setattr(reg.requests, 'post', _boom)
    ok, info = reg.submit_registration_form(CUSTOMER)
    assert ok is False and 'submit error' in info


def test_definition_unavailable_fails_soft(monkeypatch):
    monkeypatch.setattr(reg, 'REGISTRATION_FORM_URL', FORM_URL)
    monkeypatch.setattr(reg, '_definition_cache', {'at': 0.0, 'value': None})
    monkeypatch.setattr(reg.requests, 'get', lambda *a, **k: _Resp(payload={'status': 'error'}))
    assert reg.load_form_definition() is None
    assert reg.submit_registration_form(CUSTOMER) == (False, 'form definition unavailable')


def test_marker_roundtrip_and_plain_text():
    marker = reg.format_registration_marker({**CUSTOMER, 'reply_to_customer': 'ignored', 'extra': 'x'})
    cleaned, data = reg.extract_registration(f"{marker} Бүртгэл үүслээ.")
    assert cleaned == 'Бүртгэл үүслээ.'
    assert data == {**CUSTOMER}
    assert reg.extract_registration('Энгийн хариу') == ('Энгийн хариу', None)
    assert reg.extract_registration(None) == (None, None)


def test_describe_mentions_form_status():
    ok_text = reg.describe(CUSTOMER, True, '362')
    assert 'Сараа' in ok_text and '99112233' in ok_text and '№362' in ok_text
    fail_text = reg.describe(CUSTOMER, False, 'HTTP 500')
    assert 'гараар' in fail_text


# --------------------------------------------------------- reply pipeline

def _resp(*blocks, stop_reason='end_turn'):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


def test_tool_offered_only_when_configured(app, db_session, monkeypatch):
    captured = {}

    def _create(**kw):
        captured.update(kw)
        return _resp(SimpleNamespace(type='text', text='За'))
    monkeypatch.setattr(services.client.messages, 'create', _create)

    services.generate_bot_response('Сайн уу', [])
    assert [t['name'] for t in captured['tools']] == ['defer_to_staff']

    monkeypatch.setattr(reg, 'REGISTRATION_FORM_URL', FORM_URL)
    services.generate_bot_response('Сайн уу', [])
    assert [t['name'] for t in captured['tools']] == ['defer_to_staff', 'submit_registration']
    # advisory mode: no defer tool, registration still offered
    services.generate_bot_response('Сайн уу', [], handoff_pending=True)
    assert [t['name'] for t in captured['tools']] == ['submit_registration']


def test_tool_call_becomes_marker_with_fields(app, db_session, monkeypatch):
    monkeypatch.setattr(reg, 'REGISTRATION_FORM_URL', FORM_URL)
    tool = SimpleNamespace(type='tool_use', name='submit_registration', id='t1',
                           input={**CUSTOMER, 'reply_to_customer': 'Бүртгэл үүслээ, имэйлээ шалгаарай.'})
    monkeypatch.setattr(services.client.messages, 'create',
                        lambda **kw: _resp(tool, stop_reason='tool_use'))
    out = services.generate_bot_response('Тийм, зөв', [])
    cleaned, data = services.extract_registration(out)
    assert cleaned == 'Бүртгэл үүслээ, имэйлээ шалгаарай.'
    assert data['email'] == 'saraa@example.com' and data['shift'] == 'орой'


def test_registration_rule_in_prompt_only_when_configured(app, db_session, monkeypatch):
    assert 'БҮРТГЭЛ ҮҮСГЭХ' not in _prompt.build_system_prompt()
    monkeypatch.setattr(reg, 'REGISTRATION_FORM_URL', FORM_URL)
    stable, volatile = _prompt.build_system_blocks()
    assert 'БҮРТГЭЛ ҮҮСГЭХ' in stable and 'имэйл' in stable
    assert 'БҮРТГЭЛ ҮҮСГЭХ' not in volatile


# --------------------------------------------------------- webhook completion

def test_webhook_completes_registration(app, db_session, monkeypatch, configured):
    import routes.webhook as wh
    from extensions import db
    from models import FacebookUser

    pings = []
    monkeypatch.setattr(wh, 'send_telegram_notification', lambda text: pings.append(text) or 1)
    monkeypatch.setattr(reg.requests, 'post',
                        lambda *a, **k: _Resp(payload={'status': 'ok', 'logid': 500, 'member_id': 1}))
    user = FacebookUser(facebook_id='psid-reg-ok', name=None)
    db.session.add(user)
    db.session.commit()
    try:
        reply = wh.complete_registration(user, CUSTOMER, 'Бүртгэл үүслээ.')
        assert reply == 'Бүртгэл үүслээ.'
        assert user.is_lead is True and user.phone == '99112233' and user.lead_status == 'new'
        assert user.name == 'Сараа Бат'
        assert '№500' in user.notes and 'saraa@example.com' in user.notes
        assert len(pings) == 1 and 'Шинэ бүртгэл' in pings[0]
    finally:
        db.session.delete(user)
        db.session.commit()


def test_webhook_registration_failure_keeps_lead_and_is_truthful(app, db_session, monkeypatch, configured):
    import routes.webhook as wh
    from extensions import db
    from models import FacebookUser

    pings = []
    monkeypatch.setattr(wh, 'send_telegram_notification', lambda text: pings.append(text) or 1)
    monkeypatch.setattr(reg.requests, 'post', lambda *a, **k: _Resp(status=500, payload=None, text='boom'))
    user = FacebookUser(facebook_id='psid-reg-fail', name='Сараа')
    db.session.add(user)
    db.session.commit()
    try:
        reply = wh.complete_registration(user, CUSTOMER, 'Бүртгэл үүслээ.')
        assert reply == services.REGISTRATION_FALLBACK_REPLY        # no false "it's done"
        assert user.is_lead is True and user.phone == '99112233'
        assert 'гараар' in user.notes
        assert len(pings) == 1 and 'гараар' in pings[0]
    finally:
        db.session.delete(user)
        db.session.commit()
