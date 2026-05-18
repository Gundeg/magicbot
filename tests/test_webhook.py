"""Webhook round-trip with stubbed FB signature and OpenAI client."""
import hashlib
import hmac
import json


def _sign(body):
    from services import FACEBOOK_APP_SECRET
    return hmac.new(FACEBOOK_APP_SECRET.encode('utf-8'), body, hashlib.sha256).hexdigest()


def test_webhook_rejects_bad_signature(client):
    body = b'{}'
    resp = client.post(
        '/webhook',
        data=body,
        headers={
            'X-Hub-Signature-256': 'sha256=deadbeef',
            'Content-Type': 'application/json',
        },
    )
    assert resp.status_code == 403


def test_webhook_accepts_valid_signature_no_messages(client):
    body = json.dumps({'object': 'page', 'entry': []}).encode('utf-8')
    sig = _sign(body)
    resp = client.post(
        '/webhook',
        data=body,
        headers={
            'X-Hub-Signature-256': f'sha256={sig}',
            'Content-Type': 'application/json',
        },
    )
    assert resp.status_code == 200
    assert resp.get_json() == {'status': 'ok'}


def test_webhook_verify_get_rejects_when_token_unset(client, monkeypatch):
    monkeypatch.delenv('VERIFY_TOKEN', raising=False)
    resp = client.get('/webhook?hub.verify_token=anything&hub.challenge=abc')
    assert resp.status_code == 403


def test_webhook_verify_get_accepts_matching_token(client, monkeypatch):
    monkeypatch.setenv('VERIFY_TOKEN', 'expected-token')
    resp = client.get('/webhook?hub.verify_token=expected-token&hub.challenge=abc')
    assert resp.status_code == 200
    assert resp.data == b'abc'
