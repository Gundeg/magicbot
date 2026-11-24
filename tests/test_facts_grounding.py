"""The bot's company facts come from admin-entered data only (2026-11-24).

Course perks and payment terms (PocketZero installments, the free Magic Finance
licence, "4 weeks", an invented "~50,000₮ a month") used to be hard-coded in the
prompt's FUNNEL_RULES and sales playbook, where admins could neither see nor
correct them. Prod already holds them in Ерөнхий мэдээлэл and the course
descriptions, so they were simply removed from code. FACTS_SOURCE_RULE tells
the model that data beats general knowledge and earlier turns (old bot
replies, Meta automation messages, staff echoes, the summary).
"""
import services
from services import _prompt

# Facts that were hard-coded in FUNNEL_RULES / the sales playbook before.
FORMERLY_HARD_CODED = ('PocketZero', '50,000', '4 долоо хоног', '100% практик',
                       '6 сар', '1 жил')


def test_no_commercial_fact_is_hard_coded_in_the_prompt(app, db_session):
    from models import FAQ, TrainingSnippet
    # precondition: no admin data in this test DB carries these facts
    for fact in FORMERLY_HARD_CODED:
        assert not FAQ.query.filter(FAQ.answer.contains(fact)).first()
        assert not TrainingSnippet.query.filter(TrainingSnippet.body.contains(fact)).first()
    for stage in _prompt.FUNNEL_RULES:
        for state in _prompt.SESSION_RULES:
            prompt = _prompt.build_system_prompt(session_state=state, funnel_stage=stage)
            for fact in FORMERLY_HARD_CODED:
                assert fact not in prompt, f'{fact!r} is hard-coded in the prompt'


def test_facts_source_rule_is_in_the_cached_half_after_the_data(app, db_session):
    stable, volatile = _prompt.build_system_blocks()
    assert services.FACTS_SOURCE_RULE in stable
    assert services.FACTS_SOURCE_RULE not in volatile
    # it must follow every data block it points back to
    assert stable.index('ТҮГЭЭМЭЛ АСУУЛТУУД:') < stable.index('БАРИМТЫН ЭХ СУРВАЛЖ')
    # and it names earlier turns as possibly stale
    assert 'автомат' in services.FACTS_SOURCE_RULE and 'хураангуй' in services.FACTS_SOURCE_RULE
