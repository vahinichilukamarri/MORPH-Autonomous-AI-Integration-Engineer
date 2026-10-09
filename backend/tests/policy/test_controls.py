"""Meta-test: every policy rule and every floor rule has a positive and a negative control.

If someone adds a rule (to the policy file or the floor) without controls, this fails. If a control
stops behaving, this names the rule.
"""

import pytest

from app.policy.evaluate import BUDGET_RULE
from app.policy.floor import FLOOR_RULES
from tests.policy.controls import CONTROLS
from tests.policy.helpers import active


def all_rule_ids() -> set[str]:
    return {r.id for r in active().file.rules} | set(FLOOR_RULES) | {BUDGET_RULE}


def test_every_rule_has_controls_and_every_control_names_a_rule() -> None:
    ids = all_rule_ids()
    assert set(CONTROLS) - ids == set(), "a control names a rule that does not exist"
    assert ids - set(CONTROLS) == set(), "a rule has no positive and negative control"


@pytest.mark.parametrize("rule_id", sorted(CONTROLS))
def test_the_positive_control_fires(rule_id: str) -> None:
    assert CONTROLS[rule_id].positive() is True


@pytest.mark.parametrize("rule_id", sorted(CONTROLS))
def test_the_negative_control_stays_silent(rule_id: str) -> None:
    assert CONTROLS[rule_id].negative() is True


def test_the_count_of_controlled_rules_is_stated() -> None:
    policy_rules = len(active().file.rules)
    assert (policy_rules, len(FLOOR_RULES), len(CONTROLS)) == (7, 10, 18)
