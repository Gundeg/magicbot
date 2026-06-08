"""Knowledge-gap handoff: extract_handoff_marker() strips the hidden [HANDOFF]
signal the bot prefixes to its reply when it defers to staff for lack of
information. The marker must never reach the customer, and an only-marker reply
must still produce a real message."""


def test_marker_stripped_and_flagged():
    from services import extract_handoff_marker, HANDOFF_MARKER
    reply = f"{HANDOFF_MARKER} Сайн байна уу! Үүнийг манай ажилтан хэлж өгнө."
    cleaned, had = extract_handoff_marker(reply)
    assert had is True
    assert HANDOFF_MARKER not in cleaned
    assert cleaned.startswith("Сайн байна уу")


def test_no_marker_returns_unchanged():
    from services import extract_handoff_marker
    reply = "Сургалт 4 долоо хоног үргэлжилнэ."
    cleaned, had = extract_handoff_marker(reply)
    assert had is False
    assert cleaned == reply


def test_multiple_markers_all_stripped():
    from services import extract_handoff_marker, HANDOFF_MARKER
    reply = f"{HANDOFF_MARKER} эхэлж {HANDOFF_MARKER} дунд орсон"
    cleaned, had = extract_handoff_marker(reply)
    assert had is True
    assert HANDOFF_MARKER not in cleaned


def test_empty_and_none_are_safe():
    from services import extract_handoff_marker
    assert extract_handoff_marker("") == ("", False)
    assert extract_handoff_marker(None) == (None, False)


def test_marker_only_falls_back_to_static(app):
    """If the model emits ONLY the marker, the customer must still get a real,
    non-empty message rather than a blank reply."""
    from services import extract_handoff_marker, HANDOFF_MARKER
    cleaned, had = extract_handoff_marker(HANDOFF_MARKER)
    assert had is True
    assert cleaned.strip()              # non-empty fallback
    assert HANDOFF_MARKER not in cleaned
