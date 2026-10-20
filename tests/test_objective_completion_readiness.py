
"""TDD acceptance tests for objective completion readiness.

These tests are expected to expose the current lifecycle gap: action_complete()
currently moves an Active objective to Completed without checking required
Key Result evaluations. Run only in the isolated strategic_management_test DB.
"""

from odoo.exceptions import ValidationError
from odoo.tests.common import tagged

from .test_strategy import StrategyFixture


@tagged("post_install", "-at_install")
class TestObjectiveCompletionReadiness(StrategyFixture):
    """An objective must not complete with missing or invalid required KPI evidence."""

    def _objective_with_required_kr(self, target_value=100.0, actual_value=None, has_actual=False):
        plan = self._mk_plan()
        objective = self._mk_org_obj(plan)
        kpi = self._mk_kpi("COMPLETION_%s" % str(target_value).replace(".", "_"))
        target = self._mk_target(
            kpi,
            scope="organization",
            start="2026-01-01",
            end="2026-03-31",
            value=target_value,
        )
        okr = self._mk_okr(objective, start="2026-01-01", end="2026-03-31")

        evaluation = None
        if has_actual:
            evaluation = self._mk_evaluation(target, actual_value, has_actual=True)
        key_result = self._mk_kr(okr, kpi, target, evaluation=evaluation, required=True)
        objective.with_user(self.head_user).action_activate()
        return objective, key_result

    def test_completion_is_blocked_when_required_kr_evaluation_is_missing(self):
        objective, key_result = self._objective_with_required_kr()

        self.assertFalse(key_result.evaluation_id)
        self.assertEqual(key_result.scoring_status, "not_evaluated")
        self.assertEqual(objective.scoring_status, "not_evaluated")

        with self.assertRaises(ValidationError):
            objective.with_user(self.head_user).action_complete()

        self.assertEqual(objective.state, "active")
        self.assertEqual(objective.scoring_status, "not_evaluated")

    def test_completion_is_blocked_when_required_kr_evaluation_is_not_evaluated(self):
        # A zero target deliberately produces Not Evaluated, even when an actual exists.
        objective, key_result = self._objective_with_required_kr(
            target_value=0.0,
            actual_value=8.0,
            has_actual=True,
        )

        self.assertTrue(key_result.evaluation_id)
        self.assertEqual(key_result.evaluation_id.status, "not_evaluated")
        self.assertEqual(key_result.scoring_status, "not_evaluated")
        self.assertEqual(objective.scoring_status, "not_evaluated")

        with self.assertRaises(ValidationError):
            objective.with_user(self.head_user).action_complete()

        self.assertEqual(objective.state, "active")

    def test_completion_succeeds_when_all_required_krs_have_valid_evaluations(self):
        objective, key_result = self._objective_with_required_kr(
            target_value=100.0,
            actual_value=100.0,
            has_actual=True,
        )

        self.assertEqual(key_result.evaluation_id.status, "valid")
        self.assertEqual(objective.scoring_status, "achieved")
        objective.with_user(self.head_user).action_complete()
        self.assertEqual(objective.state, "completed")
