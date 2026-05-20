"""Tests for extract_name_from_reply — the heuristic that turns a
customer's reply to "what's your name?" into a stored display name.

Conservative on purpose: we'd rather store '' than the wrong name.
"""
from services import extract_name_from_reply


def test_single_word_mongolian_name():
    assert extract_name_from_reply('Болормаа') == 'Болормаа'


def test_two_word_full_name():
    assert extract_name_from_reply('Болормаа Батбаяр') == 'Болормаа Батбаяр'


def test_explicit_namaig_pattern():
    assert extract_name_from_reply('Намайг Болормаа гэдэг') == 'Болормаа'


def test_english_my_name_is_pattern():
    assert extract_name_from_reply('my name is Sarah') == 'Sarah'


def test_question_is_not_a_name():
    assert extract_name_from_reply('Хичээл хэдийг гарах вэ?') == ''


def test_message_with_digits_is_not_a_name():
    assert extract_name_from_reply('99112233') == ''


def test_short_acknowledgement_is_not_a_name():
    assert extract_name_from_reply('За') == ''
    assert extract_name_from_reply('тийм') == ''
    assert extract_name_from_reply('ok') == ''
    assert extract_name_from_reply('Сайн уу') == ''


def test_long_product_query_is_not_a_name():
    # Three+ tokens with no explicit name pattern → reject.
    assert extract_name_from_reply('Англи хэлний сургалт хэзээ эхлэх вэ') == ''


def test_empty_input():
    assert extract_name_from_reply('') == ''
    assert extract_name_from_reply(None) == ''


def test_overlong_input_rejected():
    assert extract_name_from_reply('a' * 100) == ''
