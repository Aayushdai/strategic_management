"""BDD/TDD acceptance tests for Hoshin Kanri and X-Matrix behavior.

These tests intentionally define the expected model contract before the
Hoshin models are implemented. Run on the isolated strategic_management_test
 database only.
"""
from odoo.exceptions import AccessError, ValidationError
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

    def test_annual_objective_cannot_be_created_active_without_alignment(self):
        with self.assertRaises(ValidationError):
            self.env["strategic.objective"].with_user(self.head_user).create(
                {
                    "name": "Directly Active Annual Objective",
                    "plan_id": self._mk_plan().id,
                    "perspective": "financial",
                    "scope": "organization",
                    "owner_id": self.head_user.id,
                    "deadline": "2026-12-31",
                    "hoshin_level": "annual",
                    "state": "active",
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

    def _mk_metric_target(self, kpi, start="2026-01-01", end="2026-12-31", state="approved", scope="organization", warehouse=None):
        values = {
            "kpi_id": kpi.id,
            "date_start": start,
            "date_end": end,
            "scope": scope,
            "target_value": 100.0,
            "state": state,
        }
        if warehouse:
            values["warehouse_id"] = warehouse.id
        return self.env["strategic.kpi.target"].with_user(self.head_user).create(values)

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

    def test_new_alignment_can_link_active_breakthrough_objective(self):
        """Creating a new link is allowed even when its B endpoint is active."""
        Alignment = self._require_alignment_model()
        plan = self._mk_plan()
        breakthrough = self._hoshin_objective(plan, "Active Breakthrough", "breakthrough")
        breakthrough.with_user(self.head_user).action_activate()
        annual = self._hoshin_objective(plan, "Draft Annual", "annual")

        alignment = Alignment.with_user(self.head_user).create(
            {
                "link_type": "B>A",
                "relationship_type": "primary",
                "breakthrough_objective_id": breakthrough.id,
                "annual_objective_id": annual.id,
                "change_reason": "Initial alignment of annual objective",
            }
        )

        self.assertEqual(alignment.state, "active")
        self.assertEqual(alignment.breakthrough_objective_id, breakthrough)
        self.assertEqual(alignment.annual_objective_id, annual)

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

    def test_initiative_cannot_be_created_active_to_bypass_activation(self):
        with self.assertRaises(ValidationError):
            self.env["strategic.initiative"].with_user(self.head_user).create(
                {
                    "name": "Directly Active Initiative",
                    "plan_id": self._mk_plan().id,
                    "date_start": "2026-01-01",
                    "date_end": "2026-12-31",
                    "state": "active",
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
        target = self._mk_metric_target(kpi)
        Alignment.with_user(self.head_user).create(
            {
                "link_type": "I>M",
                "relationship_type": "primary",
                "initiative_id": initiative.id,
                "kpi_id": kpi.id,
                "target_id": target.id,
                "effective_date": "2026-06-15",
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


    def test_breakthrough_metric_link_uses_explicit_approved_target(self):
        Alignment = self._require_alignment_model()
        plan = self._mk_plan()
        breakthrough = self._hoshin_objective(plan, "Breakthrough Metric", "breakthrough")
        kpi = self._mk_kpi("XMAT-BREAKTHROUGH-METRIC")
        target = self._mk_metric_target(kpi)
        link = Alignment.with_user(self.head_user).create({
            "link_type": "B>M",
            "relationship_type": "primary",
            "breakthrough_objective_id": breakthrough.id,
            "kpi_id": kpi.id,
            "target_id": target.id,
            "effective_date": "2026-06-15",
        })
        self.assertEqual(link.kpi_id, kpi)
        self.assertEqual(link.target_id, target)

    def test_metric_alignment_requires_explicit_approved_target(self):
        Alignment = self._require_alignment_model()
        plan = self._mk_plan()
        kpi = self._mk_kpi("XMAT-TARGET-REQUIRED")
        initiative = self.env["strategic.initiative"].with_user(self.head_user).create({
            "name": "Metric Target Required Initiative",
            "plan_id": plan.id,
            "date_start": "2026-01-01",
            "date_end": "2026-12-31",
        })
        with self.assertRaisesRegex(ValidationError, "explicit approved KPI target"):
            Alignment.with_user(self.head_user).create({
                "link_type": "I>M",
                "relationship_type": "primary",
                "initiative_id": initiative.id,
                "kpi_id": kpi.id,
                "effective_date": "2026-06-15",
            })

    def test_metric_alignment_rejects_mismatched_unapproved_or_out_of_period_target(self):
        Alignment = self._require_alignment_model()
        plan = self._mk_plan()
        kpi = self._mk_kpi("XMAT-TARGET-KPI")
        other_kpi = self._mk_kpi("XMAT-TARGET-OTHER")
        initiative = self.env["strategic.initiative"].with_user(self.head_user).create({
            "name": "Metric Target Contract Initiative",
            "plan_id": plan.id,
            "date_start": "2026-01-01",
            "date_end": "2026-12-31",
        })
        wrong_kpi_target = self._mk_metric_target(other_kpi)
        draft_target = self._mk_metric_target(kpi, state="draft")
        out_of_period_target = self._mk_metric_target(
            kpi, start="2026-01-01", end="2026-03-31"
        )
        with self.assertRaisesRegex(ValidationError, "belong to the linked KPI"):
            Alignment.with_user(self.head_user).create({
                "link_type": "I>M", "relationship_type": "primary",
                "initiative_id": initiative.id, "kpi_id": kpi.id,
                "target_id": wrong_kpi_target.id,
                "effective_date": "2026-06-15",
            })
        with self.assertRaisesRegex(ValidationError, "only approved"):
            Alignment.with_user(self.head_user).create({
                "link_type": "I>M", "relationship_type": "primary",
                "initiative_id": initiative.id, "kpi_id": kpi.id,
                "target_id": draft_target.id,
                "effective_date": "2026-06-15",
            })
        with self.assertRaisesRegex(ValidationError, "effective date"):
            Alignment.with_user(self.head_user).create({
                "link_type": "I>M", "relationship_type": "primary",
                "initiative_id": initiative.id, "kpi_id": kpi.id,
                "target_id": out_of_period_target.id,
                "effective_date": "2026-06-15",
            })

    def test_metric_alignment_requires_organization_target(self):
        Alignment = self._require_alignment_model()
        plan = self._mk_plan()
        kpi = self._mk_kpi("XMAT-BRANCH-TARGET")
        initiative = self.env["strategic.initiative"].with_user(self.head_user).create({
            "name": "Metric Branch Target Initiative",
            "plan_id": plan.id,
            "date_start": "2026-01-01",
            "date_end": "2026-12-31",
        })
        branch_target = self._mk_metric_target(
            kpi, scope="branch", warehouse=self.wh1
        )
        with self.assertRaisesRegex(ValidationError, "organization-wide"):
            Alignment.with_user(self.head_user).create({
                "link_type": "I>M", "relationship_type": "primary",
                "initiative_id": initiative.id, "kpi_id": kpi.id,
                "target_id": branch_target.id, "effective_date": "2026-06-15",
            })

    def test_metric_target_is_preserved_by_controlled_revision(self):
        Alignment = self._require_alignment_model()
        plan = self._mk_plan()
        kpi = self._mk_kpi("XMAT-TARGET-REVISION")
        initiative = self.env["strategic.initiative"].with_user(self.head_user).create({
            "name": "Metric Target Revision Initiative",
            "plan_id": plan.id,
            "date_start": "2026-01-01",
            "date_end": "2026-12-31",
        })
        target = self._mk_metric_target(kpi)
        link = Alignment.with_user(self.head_user).create({
            "link_type": "I>M", "relationship_type": "primary",
            "initiative_id": initiative.id, "kpi_id": kpi.id,
            "target_id": target.id, "effective_date": "2026-06-15",
        })
        action = link.with_user(self.head_user).action_open_revision_wizard()
        self.assertEqual(action["context"]["default_target_id"], target.id)
        replacement = link.with_user(self.head_user).action_revise(
            {"target_id": target.id}, "Keep this approved metric target explicitly linked."
        )
        self.assertEqual(replacement.target_id, target)
        history = self.env["strategic.xmatrix.alignment.history"].search([
            ("source_alignment_id", "=", link.id)
        ], limit=1)
        self.assertIn('"target_id": %s' % target.id, history.old_snapshot)
        self.assertIn('"target_id": %s' % target.id, history.new_snapshot)


@tagged("post_install", "-at_install")
class TestHoshinSecurityAndHistory(StrategyFixture):
    """Hoshin permissions and controlled changes are enforced server-side."""

    def test_context_cannot_bypass_active_hoshin_horizon_lock(self):
        plan = self._mk_plan()
        objective = self._mk_org_obj(plan)
        objective.write({"hoshin_level": "breakthrough"})
        objective.with_user(self.head_user).action_activate()
        with self.assertRaisesRegex(ValidationError, "controlled revision"):
            objective.with_user(self.head_user).with_context(
                allow_hoshin_revision=True
            ).write({"hoshin_level": "none"})

    def test_context_cannot_bypass_alignment_state_guard(self):
        Alignment = self.env["strategic.xmatrix.alignment"]
        plan = self._mk_plan()
        breakthrough = self._mk_org_obj(plan)
        breakthrough.write({"hoshin_level": "breakthrough"})
        annual = self._mk_org_obj(plan)
        annual.write({"hoshin_level": "annual"})
        link = Alignment.with_user(self.head_user).create({
            "link_type": "B>A", "relationship_type": "primary",
            "breakthrough_objective_id": breakthrough.id,
            "annual_objective_id": annual.id,
        })
        with self.assertRaisesRegex(ValidationError, "controlled revision workflow"):
            link.with_user(self.head_user).with_context(
                allow_hoshin_revision=True
            ).write({"state": "superseded"})

    def test_target_cannot_be_archived_while_an_active_xmatrix_link_uses_it(self):
        Alignment = self.env["strategic.xmatrix.alignment"]
        plan = self._mk_plan()
        kpi = self._mk_kpi("XMAT-TARGET-ARCHIVE")
        initiative = self.env["strategic.initiative"].with_user(self.head_user).create({
            "name": "Metric Target Archive Initiative",
            "plan_id": plan.id,
            "date_start": "2026-01-01",
            "date_end": "2026-12-31",
        })
        target = self.env["strategic.kpi.target"].with_user(self.head_user).create({
            "kpi_id": kpi.id,
            "date_start": "2026-01-01",
            "date_end": "2026-12-31",
            "scope": "organization",
            "target_value": 100.0,
            "state": "approved",
        })
        Alignment.with_user(self.head_user).create({
            "link_type": "I>M", "relationship_type": "primary",
            "initiative_id": initiative.id, "kpi_id": kpi.id,
            "target_id": target.id, "effective_date": "2026-06-15",
        })
        with self.assertRaisesRegex(ValidationError, "active X-Matrix alignment"):
            target.with_user(self.head_user).action_archive()

    def test_saved_alignment_change_reason_cannot_be_rewritten(self):
        Alignment = self.env["strategic.xmatrix.alignment"]
        plan = self._mk_plan()
        breakthrough = self._mk_org_obj(plan)
        breakthrough.write({"hoshin_level": "breakthrough"})
        annual = self._mk_org_obj(plan)
        annual.write({"hoshin_level": "annual"})
        link = Alignment.with_user(self.head_user).create({
            "link_type": "B>A", "relationship_type": "primary",
            "breakthrough_objective_id": breakthrough.id,
            "annual_objective_id": annual.id,
            "change_reason": "Initial approved alignment rationale.",
        })
        with self.assertRaisesRegex(ValidationError, "reason .*immutable"):
            link.with_user(self.head_user).write({"change_reason": "Tampered history"})

    def test_context_cannot_forge_xmatrix_revision_history(self):
        History = self.env["strategic.xmatrix.alignment.history"]
        with self.assertRaises(AccessError):
            History.with_user(self.head_user).with_context(
                allow_hoshin_history_create=True
            ).create({
                "source_alignment_id": 1,
                "replacement_alignment_id": 1,
                "old_snapshot": "{}",
                "new_snapshot": "{}",
                "reason": "Forged revision",
            })

    def test_branch_and_employee_cannot_access_hoshin_management_models(self):
        Initiative = self.env["strategic.initiative"]
        Alignment = self.env["strategic.xmatrix.alignment"]
        History = self.env["strategic.xmatrix.alignment.history"]

        for user in (self.branch_user_a, self.emp_user):
            with self.assertRaises(AccessError):
                Initiative.with_user(user).search([])
            with self.assertRaises(AccessError):
                Alignment.with_user(user).create({
                    "link_type": "I>O",
                    "relationship_type": "primary",
                    "initiative_id": 1,
                    "owner_id": user.id,
                })
            with self.assertRaises(AccessError):
                History.with_user(user).search([])

    def test_revision_preserves_old_alignment_and_immutable_history(self):
        Alignment = self.env["strategic.xmatrix.alignment"]
        History = self.env["strategic.xmatrix.alignment.history"]
        plan = self._mk_plan()
        breakthrough = self._mk_org_obj(plan)
        breakthrough.write({"hoshin_level": "breakthrough"})
        annual_a = self._mk_org_obj(plan)
        annual_a.write({"name": "Annual A", "hoshin_level": "annual"})
        annual_b = self._mk_org_obj(plan)
        annual_b.write({"name": "Annual B", "hoshin_level": "annual"})
        link = Alignment.with_user(self.head_user).create({
            "link_type": "B>A",
            "relationship_type": "primary",
            "breakthrough_objective_id": breakthrough.id,
            "annual_objective_id": annual_a.id,
        })

        breakthrough.with_user(self.head_user).action_activate()
        annual_a.with_user(self.head_user).action_activate()

        replacement = link.with_user(self.head_user).action_revise(
            {"annual_objective_id": annual_b.id}, "Annual priorities were realigned."
        )
        self.assertEqual(link.state, "superseded")
        self.assertEqual(link.annual_objective_id, annual_a)
        self.assertEqual(replacement.state, "active")
        self.assertEqual(replacement.annual_objective_id, annual_b)

        history = History.search([("source_alignment_id", "=", link.id)])
        self.assertEqual(len(history), 1)
        self.assertEqual(history.reason, "Annual priorities were realigned.")
        self.assertEqual(history.changed_by_id, self.head_user)
        self.assertIn('"annual_objective_id": %s' % annual_a.id, history.old_snapshot)
        self.assertIn('"annual_objective_id": %s' % annual_b.id, history.new_snapshot)

        with self.assertRaises(ValidationError):
            history.write({"reason": "tampered"})
        with self.assertRaises(ValidationError):
            history.unlink()

    def test_open_revision_wizard_returns_required_defaults(self):
        Alignment = self.env["strategic.xmatrix.alignment"]
        plan = self._mk_plan()
        breakthrough = self._mk_org_obj(plan)
        breakthrough.write({"hoshin_level": "breakthrough"})
        annual = self._mk_org_obj(plan)
        annual.write({"hoshin_level": "annual"})
        link = Alignment.with_user(self.head_user).create({
            "link_type": "B>A",
            "relationship_type": "primary",
            "breakthrough_objective_id": breakthrough.id,
            "annual_objective_id": annual.id,
        })

        action = link.with_user(self.head_user).action_open_revision_wizard()
        self.assertEqual(action["res_model"], "strategic.xmatrix.alignment.revision.wizard")
        self.assertEqual(action["context"]["default_alignment_id"], link.id)
        self.assertEqual(action["context"]["default_link_type"], "B>A")
        self.assertEqual(action["context"]["default_relationship_type"], "primary")
        self.assertEqual(action["context"]["default_breakthrough_objective_id"], breakthrough.id)
        self.assertEqual(action["context"]["default_annual_objective_id"], annual.id)

    def test_revision_wizard_applies_audited_revision(self):
        Alignment = self.env["strategic.xmatrix.alignment"]
        History = self.env["strategic.xmatrix.alignment.history"]
        plan = self._mk_plan()
        Objective = self.env["strategic.objective"].with_user(self.head_user)
        breakthrough = Objective.create({
            "name": "Breakthrough for Wizard",
            "plan_id": plan.id,
            "perspective": "financial",
            "scope": "organization",
            "owner_id": self.head_user.id,
            "deadline": "2026-12-31",
            "hoshin_level": "breakthrough",
        })
        annual_a = Objective.create({
            "name": "Annual A for Wizard",
            "plan_id": plan.id,
            "perspective": "financial",
            "scope": "organization",
            "owner_id": self.head_user.id,
            "deadline": "2026-12-31",
            "hoshin_level": "annual",
        })
        annual_b = Objective.create({
            "name": "Annual B for Wizard",
            "plan_id": plan.id,
            "perspective": "financial",
            "scope": "organization",
            "owner_id": self.head_user.id,
            "deadline": "2026-12-31",
            "hoshin_level": "annual",
        })
        link = Alignment.with_user(self.head_user).create({
            "link_type": "B>A",
            "relationship_type": "primary",
            "breakthrough_objective_id": breakthrough.id,
            "annual_objective_id": annual_a.id,
        })
        breakthrough.action_activate()
        annual_a.action_activate()

        wizard = self.env["strategic.xmatrix.alignment.revision.wizard"].with_user(
            self.head_user
        ).with_context(default_alignment_id=link.id).create({
            "annual_objective_id": annual_b.id,
            "reason": "Annual priority changed after review.",
        })
        action = wizard.action_apply_revision()
        replacement = Alignment.browse(action["res_id"])

        self.assertEqual(link.state, "superseded")
        self.assertEqual(replacement.state, "active")
        self.assertEqual(replacement.annual_objective_id, annual_b)
        history = History.search([("source_alignment_id", "=", link.id)])
        self.assertEqual(len(history), 1)
        self.assertEqual(history.reason, "Annual priority changed after review.")

    def test_alignment_revision_requires_head_office_and_reason(self):
        Alignment = self.env["strategic.xmatrix.alignment"]
        plan = self._mk_plan()
        breakthrough = self._mk_org_obj(plan)
        breakthrough.write({"hoshin_level": "breakthrough"})
        annual = self._mk_org_obj(plan)
        annual.write({"hoshin_level": "annual"})
        link = Alignment.with_user(self.head_user).create({
            "link_type": "B>A",
            "breakthrough_objective_id": breakthrough.id,
            "annual_objective_id": annual.id,
        })

        with self.assertRaises(AccessError):
            link.with_user(self.branch_user_a).action_revise(
                {"relationship_type": "supporting"}, "Not allowed"
            )
        with self.assertRaises(ValidationError):
            link.with_user(self.head_user).action_revise(
                {"relationship_type": "supporting"}, "   "
            )
