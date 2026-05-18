"""verify_facebook_signature() unit tests.

The webhook's entire authentication story hinges on this 10-line function.
A regression here means an attacker can forge Messenger traffic.
"""
import hashlib
import hmac


def test_valid_signature_accepted():
    from services import verify_facebook_signature, FACEBOOK_APP_SECRET
    body = b'{"hello":"world"}'
    sig = hmac.new(FACEBOOK_APP_SECRET.encode('utf-8'), body, hashlib.sha256).hexdigest()
    assert verify_facebook_signature(body, f'sha256={sig}') is True


def test_tampered_body_rejected():
    from services import verify_facebook_signature, FACEBOOK_APP_SECRET
    body = b'{"hello":"world"}'
    sig = hmac.new(FACEBOOK_APP_SECRET.encode('utf-8'), body, hashlib.sha256).hexdigest()
    assert verify_facebook_signature(b'{"hello":"forged"}', f'sha256={sig}') is False


def test_missing_header_rejected():
    from services import verify_facebook_signature
    assert verify_facebook_signature(b'{}', '') is False
    assert verify_facebook_signature(b'{}', None) is False


def test_wrong_algorithm_prefix_rejected():
    from services import verify_facebook_signature
    # SHA-1 prefix was the v1 signature scheme; we only accept v2.
    assert verify_facebook_signature(b'{}', 'sha1=deadbeef') is False


def test_truncated_signature_rejected():
    from services import verify_facebook_signature, FACEBOOK_APP_SECRET
    body = b'{"hello":"world"}'
    sig = hmac.new(FACEBOOK_APP_SECRET.encode('utf-8'), body, hashlib.sha256).hexdigest()
    assert verify_facebook_signature(body, f'sha256={sig[:-2]}') is False
