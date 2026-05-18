"""PHONE_RE captures the formats the bot promises to recognize."""
import pytest


@pytest.mark.parametrize('text,expected', [
    ('Утас: 99112233', '99112233'),
    ('My number is 88001122', '88001122'),
    ('+976 99112233', '+976 99112233'),
    ('+976-89001122', '+976-89001122'),
    ('99112233 руу залгаарай', '99112233'),
])
def test_phone_re_matches(text, expected):
    from services import PHONE_RE
    match = PHONE_RE.search(text)
    assert match is not None
    assert match.group(0) == expected


@pytest.mark.parametrize('text', [
    'no phone here',
    '1234567',           # too short (7 digits)
    '12345678',          # starts with 1, not 8/9
    '',
])
def test_phone_re_no_match(text):
    from services import PHONE_RE
    assert PHONE_RE.search(text) is None
