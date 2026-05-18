"""detect_funnel_stage() — monotonic forward-only funnel progression.

The bot's prompt switches behavior based on funnel stage; a regression
here changes how every customer is handled. Property: stage never
regresses.
"""
import pytest


@pytest.mark.parametrize('text,current,expected', [
    # Strong intent → ready, regardless of current stage
    ('Би бүртгүүлмээр байна', 'curious', 'ready'),
    ('Утасны дугаар: 99112233', 'curious', 'curious'),  # phone alone isn't a keyword
    # Pricing words promote curious → pricing
    ('Үнэ хэд вэ?', 'curious', 'pricing'),
    # Exploring words promote curious → exploring_courses
    ('Сургалт ямар хэлбэртэй вэ?', 'curious', 'exploring_courses'),
    # No regression: once ready, stays ready even on a course-content question
    ('Хичээл ямар цаг байна?', 'ready', 'ready'),
    ('Үнэ хэд вэ?', 'ready', 'ready'),
    # No regression: once pricing, course questions don't pull us back
    ('Сургалтын хуваарь?', 'pricing', 'pricing'),
    # Empty / None inputs keep the current stage
    ('', 'curious', 'curious'),
    (None, 'pricing', 'pricing'),
])
def test_funnel_stage_progression(text, current, expected):
    from services import detect_funnel_stage
    assert detect_funnel_stage(text, current) == expected


def test_funnel_stage_default_curious_when_current_none():
    from services import detect_funnel_stage
    assert detect_funnel_stage('Сайн уу', None) == 'curious'
