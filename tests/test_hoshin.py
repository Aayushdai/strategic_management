"""BDD/TDD acceptance tests for Hoshin Kanri and X-Matrix behavior.

These tests intentionally define the expected model contract before the
Hoshin models are implemented. Run on the isolated strategic_management_test
 database only.
"""
from odoo.exceptions import ValidationError
from odoo.tests.common import tagged

from .test_strategy import StrategyFixture


@tagged("post_install", "-at_install")
class TestHoshinHorizons(StrategyFixture):
    """Given an existing BSC objective, Hoshin horizon is an independent axis."""

    def test_hoshin_horizon_is_independent_of_bsc_perspective_and_lifecycle(self):
        Objective = self.env["strategic.objective"]
        self.assertIn(
            "hoshin_level",
            Objective._fields,
            "Strategic objectives need a Hoshin horizon field before this behavior can be tested.",
        )

        plan = self._mk_plan()
        objective = self._mk_org_obj(plan)
        objective.write({"hoshin_level": "breakthrough"})

        self.assertEqual(objective.hoshin_level, "breakthrough")
        self.assertEqual(objective.perspective, "financial")
        self.assertEqual(objective.state, "draft")

        objective.write({"perspective": "customer"})
        self.assertEqual(objective.hoshin_level, "breakthrough")
        self.assertEqual(objective.perspective, "customer")

    def test_branch_objectives_cannot_be_classified_as_hoshin_horizons(self):
        Objective = self.env["strategic.objective"]
        self.assertIn("hoshin_level", Objective._fields)

        plan = self._mk_plan()
        parent = self._mk_org_obj(plan)
        with self.assertRaises(ValidationError):
            self.env["strategic.objective"].with_user(self.head_user).create(
                {
                    "name": "Invalid Branch Horizon",
                    "plan_id": plan.id,
                    "perspective": "financial",
                    "scope": "branch",
                    "warehouse_id": self.wh1.id,
                    "parent_objective_id": parent.id,
                    "owner_id": self.branch_user_a.id,
                    "deadline": "2026-12-31",
                    "hoshin_level": "annual",
                }
            )

    def test_annual_objective_cannot_activate_without_breakthrough_alignment(self):
        Objective = self.env["strategic.objective"]
        self.assertIn("hoshin_level", Objective._fields)
        self.assertIn(
            "strategic.xmatrix.alignment",
            self.env.registry.models,
            "The explicit X-Matrix alignment model is required for Hoshin activation rules.",
        )

        annual = self._mk_org_obj(self._mk_plan())
        annual.write({"hoshin_level": "annual"})

        with self.assertRaises(ValidationError):
            annual.with_user(self.head_user).action_activate()


@tagged("post_install", "-at_install")
class TestHoshinXMatrix(StrategyFixture):
    """X-Matrix links use typed endpoints and keep primary/supporting distinct."""

    def _require_alignment_model(self):
        self.assertIn(
            "strategic.xmatrix.alignment",
            self.env.registry.models,
            "The explicit X-Matrix alignment model must be registered.",
        )
        return self.env["strategic.xmatrix.alignment"]

    def _hoshin_objective(self, plan, name, horizon):
        obj = self._mk_org_obj(plan)
        obj.write({"name": name, "hoshin_level": horizon})
        return obj

    def test_primary_and_supporting_breakthrough_to_annual_links(self):
        Alignment = self._require_alignment_model()
        plan = self._mk_plan()
        breakthrough = self._hoshin_objective(plan, "Breakthrough", "breakthrough")
        annual_a = self._hoshin_objective(plan, "Annual A", "annual")
        annual_b = self._hoshin_objective(plan, "Annual B", "annual")

        primary = Alignment.with_user(self.head_user).create(
            {
                "link_type": "B>A",
                "relationship_type": "primary",
                "breakthrough_objective_id": breakthrough.id,
                "annual_objective_id": annual_a.id,
            }
        )
        supporting = Alignment.with_user(self.head_user).create(
            {
                "link_type": "B>A",
                "relationship_type": "supporting",
                "breakthrough_objective_id": breakthrough.id,
                "annual_objective_id": annual_b.id,
            }
        )

        self.assertEqual(primary.relationship_type, "primary")
        self.assertEqual(supporting.relationship_type, "supporting")
        self.assertEqual(primary.link_type, "B>A")
        self.assertEqual(supporting.link_type, "B>A")

        with self.assertRaises(ValidationError):
            Alignment.with_user(self.head_user).create(
                {
                    "link_type": "B>A",
                    "relationship_type": "primary",
                    "breakthrough_objective_id": breakthrough.id,
                    "annual_objective_id": annual_b.id,
                }
            )

    def test_alignment_rejects_endpoint_mismatch(self):
        Alignment = self._require_alignment_model()
        plan = self._mk_plan()
        ordinary = self._mk_org_obj(plan)
        annual = self._hoshin_objective(plan, "Annual", "annual")

        # A B>A link cannot use an ordinary objective as the breakthrough end.
        with self.assertRaises(ValidationError):
            Alignment.with_user(self.head_user).create(
                {
                    "link_type": "B>A",
                    "relationship_type": "primary",
                    "breakthrough_objective_id": ordinary.id,
                    "annual_objective_id": annual.id,
                }
            )

    def test_initiative_cannot_activate_without_annual_kpi_and_owner_links(self):
        self.assertIn(
            "strategic.initiative",
            self.env.registry.models,
            "The improvement initiative model must be registered.",
        )
        initiative = self.env["strategic.initiative"].with_user(self.head_user).create(
            {
                "name": "Unaligned Initiative",
                "plan_id": self._mk_plan().id,
                "date_start": "2026-01-01",
                "date_end": "2026-12-31",
            }
        )

        with self.assertRaises(ValidationError):
            initiative.with_user(self.head_user).action_activate()

    def test_initiative_can_activate_after_annual_kpi_and_primary_owner_links_exist(self):
        self.assertIn("strategic.initiative", self.env.registry.models)
        Alignment = self._require_alignment_model()
        plan = self._mk_plan()
        annual = self._hoshin_objective(plan, "Annual", "annual")
        kpi = self._mk_kpi("HOSHIN-ACTIVATE")
        initiative = self.env["strategic.initiative"].with_user(self.head_user).create(
            {
                "name": "Reduce Validated Spoilage",
                "plan_id": plan.id,
                "date_start": "2026-01-01",
                "date_end": "2026-12-31",
            }
        )

        Alignment.with_user(self.head_user).create(
            {
                "link_type": "I>A",
                "relationship_type": "primary",
                "initiative_id": initiative.id,
                "annual_objective_id": annual.id,
            }
        )
        Alignment.with_user(self.head_user).create(
            {
                "link_type": "I>M",
                "relationship_type": "primary",
                "initiative_id": initiative.id,
                "kpi_id": kpi.id,
            }
        )
        Alignment.with_user(self.head_user).create(
            {
                "link_type": "I>O",
                "relationship_type": "primary",
                "initiative_id": initiative.id,
                "owner_id": self.head_user.id,
            }
        )

        initiative.with_user(self.head_user).action_activate()
        self.assertEqual(initiative.state, "active")
        self.assertEqual(initiative.owner_id, self.head_user)
