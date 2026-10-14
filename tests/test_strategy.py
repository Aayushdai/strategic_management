"""
Phase 2 test suite — Strategic Objectives, BSC, OKRs, Key Results.
All 16 required scenarios from the Phase 2 spec are covered.
Uses isolated transactions; no dependency on fixed database IDs or
pre-existing business records.
"""
from datetime import date, timedelta

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixture mixin — creates two warehouses, three users, one KPI+target
# ─────────────────────────────────────────────────────────────────────────────

class StrategyFixture(TransactionCase):
    """Shared setup used by every Phase 2 test class."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        # Warehouses ─ branches
        cls.wh1 = cls.env["stock.warehouse"].create(
            {"name": "P2 Branch Alpha", "code": "P2A", "company_id": cls.company.id}
        )
        cls.wh2 = cls.env["stock.warehouse"].create(
            {"name": "P2 Branch Beta", "code": "P2B", "company_id": cls.company.id}
        )
        # Security groups
        cls.g_head = cls.env.ref("strategic_management.group_strategic_head_office_manager")
        cls.g_branch = cls.env.ref("strategic_management.group_strategic_branch_manager")
        cls.g_emp = cls.env.ref("strategic_management.group_strategic_employee")
        cls.g_user = cls.env.ref("base.group_user")
        # Users
        cls.head_user = cls._mk_user("p2_head", cls.g_head, None)
        cls.branch_user_a = cls._mk_user("p2_branch_a", cls.g_branch, cls.wh1)
        cls.branch_user_b = cls._mk_user("p2_branch_b", cls.g_branch, cls.wh2)
        cls.emp_user = cls._mk_user("p2_emp", cls.g_emp, None)

    @classmethod
    def _mk_user(cls, login, group, warehouse):
        return cls.env["res.users"].with_context(no_reset_password=True).create(
            {
                "name": login,
                "login": login,
                "email": "%s@example.com" % login,
                "company_id": cls.company.id,
                "company_ids": [(6, 0, [cls.company.id])],
                "group_ids": [(6, 0, [cls.g_user.id, group.id])],
                "strategic_branch_warehouse_id": warehouse.id if warehouse else False,
            }
        )

    def _mk_plan(self, start="2026-01-01", end="2026-12-31"):
        return self.env["strategic.plan"].create(
            {
                "name": "Test Plan",
                "date_start": start,
                "date_end": end,
                "company_id": self.company.id,
            }
        )

    def _mk_org_obj(self, plan, state="draft", deadline="2026-12-31"):
        return (
            self.env["strategic.objective"]
            .with_user(self.head_user)
            .create(
                {
                    "name": "Org Objective",
                    "plan_id": plan.id,
                    "perspective": "financial",
                    "scope": "organization",
                    "owner_id": self.head_user.id,
                    "deadline": deadline,
                    "state": state,
                }
            )
        )

    def _mk_branch_obj(self, plan, parent, warehouse=None, state="draft"):
        wh = warehouse or self.wh1
        owner = self.branch_user_b if wh == self.wh2 else self.branch_user_a
        # Create as Head Office so tests can explicitly build either branch's
        # records without the current user's branch being substituted.
        return self.env["strategic.objective"].with_user(self.head_user).create(
            {
                "name": "Branch Objective",
                "plan_id": plan.id,
                "perspective": "financial",
                "scope": "branch",
                "warehouse_id": wh.id,
                "parent_objective_id": parent.id,
                "owner_id": owner.id,
                "deadline": "2026-12-31",
                "state": state,
            }
        )

    def _mk_kpi(self, code, direction="higher"):
        return self.env["strategic.kpi.definition"].create(
            {
                "name": "KPI %s" % code,
                "code": code,
                "unit": "%",
                "direction": direction,
                "company_id": self.company.id,
            }
        )

    def _mk_target(self, kpi, scope="organization", warehouse=None, start="2026-01-01", end="2026-03-31", value=100.0):
        vals = {
            "kpi_id": kpi.id,
            "date_start": start,
            "date_end": end,
            "scope": scope,
            "target_value": value,
            "state": "approved",
        }
        if warehouse:
            vals["warehouse_id"] = warehouse.id
        return self.env["strategic.kpi.target"].create(vals)

    def _mk_evaluation(self, target, actual, has_actual=True):
        return self.env["strategic.kpi.evaluation"].create(
            {
                "target_id": target.id,
                "actual_value": actual,
                "has_actual_value": has_actual,
            }
        )

    def _mk_okr(self, strategic_obj, start="2026-01-01", end="2026-03-31"):
        return self.env["strategic.okr.objective"].create(
            {
                "name": "Test OKR",
                "strategic_objective_id": strategic_obj.id,
                "owner_id": self.head_user.id,
                "date_start": start,
                "date_end": end,
            }
        )

    def _mk_kr(self, okr, kpi, target, evaluation=None, required=True):
        vals = {
            "name": "Key Result",
            "okr_objective_id": okr.id,
            "primary_kpi_id": kpi.id,
            "target_id": target.id,
            "required": required,
        }
        if evaluation:
            vals["evaluation_id"] = evaluation.id
        return self.env["strategic.key.result"].create(vals)


# ─────────────────────────────────────────────────────────────────────────────
# Test 1 — Valid and invalid objective lifecycle transitions
# ─────────────────────────────────────────────────────────────────────────────

@tagged("post_install", "-at_install")
class TestObjectiveLifecycle(StrategyFixture):

    def test_full_valid_lifecycle(self):
        """draft → pending_approval → active → completed → closed → archived"""
        plan = self._mk_plan()
        obj = self._mk_org_obj(plan)

        self.assertEqual(obj.state, "draft")
        obj.action_submit()
        self.assertEqual(obj.state, "pending_approval")

        obj.with_user(self.head_user).action_approve()
        self.assertEqual(obj.state, "active")

        obj.with_user(self.head_user).action_complete()
        self.assertEqual(obj.state, "completed")

        obj.with_user(self.head_user).action_close()
        self.assertEqual(obj.state, "closed")

        obj.with_user(self.head_user).action_archive()
        self.assertEqual(obj.state, "archived")

    def test_direct_activation_skips_proposal(self):
        plan = self._mk_plan()
        obj = self._mk_org_obj(plan)
        obj.with_user(self.head_user).action_activate()
        self.assertEqual(obj.state, "active")

    def test_invalid_transition_pending_to_completed(self):
        plan = self._mk_plan()
        obj = self._mk_org_obj(plan)
        obj.action_submit()
        self.assertEqual(obj.state, "pending_approval")
        with self.assertRaises(ValidationError):
            obj.with_user(self.head_user).action_complete()

    def test_invalid_transition_draft_to_closed(self):
        plan = self._mk_plan()
        obj = self._mk_org_obj(plan)
        with self.assertRaises(ValidationError):
            obj.with_user(self.head_user).action_close()

    def test_closed_objective_cannot_be_reopened(self):
        plan = self._mk_plan()
        obj = self._mk_org_obj(plan)
        obj.action_submit()
        obj.with_user(self.head_user).action_approve()
        obj.with_user(self.head_user).action_complete()
        obj.with_user(self.head_user).action_close()
        # No action exists to go back; write attempt on state should fail
        with self.assertRaises(ValidationError):
            obj.write({"state": "active"})


# ─────────────────────────────────────────────────────────────────────────────
# Test 2 — Branch objective alignment
# ─────────────────────────────────────────────────────────────────────────────

@tagged("post_install", "-at_install")
class TestBranchAlignment(StrategyFixture):

    def test_branch_obj_requires_org_parent_in_same_plan(self):
        plan = self._mk_plan()
        org_obj = self._mk_org_obj(plan)
        # Valid: branch obj aligned to org obj in same plan
        branch_obj = self._mk_branch_obj(plan, org_obj)
        self.assertEqual(branch_obj.parent_objective_id, org_obj)

    def test_branch_obj_without_parent_raises(self):
        plan = self._mk_plan()
        with self.assertRaises((ValidationError, Exception)):
            self.env["strategic.objective"].with_user(self.head_user).create(
                {
                    "name": "Bad Branch",
                    "plan_id": plan.id,
                    "perspective": "financial",
                    "scope": "branch",
                    "warehouse_id": self.wh1.id,
                    "owner_id": self.head_user.id,
                    "deadline": "2026-12-31",
                    # parent_objective_id intentionally omitted
                }
            )

    def test_org_obj_cannot_have_parent(self):
        plan = self._mk_plan()
        org1 = self._mk_org_obj(plan)
        with self.assertRaises(ValidationError):
            self.env["strategic.objective"].with_user(self.head_user).create(
                {
                    "name": "Nested Org",
                    "plan_id": plan.id,
                    "perspective": "financial",
                    "scope": "organization",
                    "parent_objective_id": org1.id,
                    "owner_id": self.head_user.id,
                    "deadline": "2026-12-31",
                }
            )

    def test_branch_obj_parent_must_be_in_same_plan(self):
        plan_a = self._mk_plan()
        plan_b = self.env["strategic.plan"].create(
            {"name": "Plan B", "date_start": "2026-01-01", "date_end": "2026-12-31", "company_id": self.company.id}
        )
        org_a = self._mk_org_obj(plan_a)
        with self.assertRaises(ValidationError):
            self.env["strategic.objective"].with_user(self.head_user).create(
                {
                    "name": "Cross-Plan Branch",
                    "plan_id": plan_b.id,
                    "perspective": "financial",
                    "scope": "branch",
                    "warehouse_id": self.wh1.id,
                    "parent_objective_id": org_a.id,
                    "owner_id": self.head_user.id,
                    "deadline": "2026-12-31",
                }
            )


# ─────────────────────────────────────────────────────────────────────────────
# Test 3 — Head Office approval of a branch objective proposal
# ─────────────────────────────────────────────────────────────────────────────

@tagged("post_install", "-at_install")
class TestHeadOfficeApproval(StrategyFixture):

    def test_head_office_approves_branch_proposal(self):
        plan = self._mk_plan()
        org_obj = self._mk_org_obj(plan)
        branch_obj = self._mk_branch_obj(plan, org_obj)
        branch_obj.with_user(self.branch_user_a).action_submit()
        self.assertEqual(branch_obj.state, "pending_approval")
        self.assertEqual(branch_obj.proposer_id, self.branch_user_a)

        branch_obj.with_user(self.head_user).action_approve()
        self.assertEqual(branch_obj.state, "active")
        self.assertEqual(branch_obj.approved_by_id, self.head_user)


# ─────────────────────────────────────────────────────────────────────────────
# Test 4 — Branch Manager cannot approve their own proposal
# ─────────────────────────────────────────────────────────────────────────────

@tagged("post_install", "-at_install")
class TestBranchCannotSelfApprove(StrategyFixture):

    def test_branch_manager_cannot_approve(self):
        plan = self._mk_plan()
        org_obj = self._mk_org_obj(plan)
        branch_obj = self._mk_branch_obj(plan, org_obj)
        branch_obj.with_user(self.branch_user_a).action_submit()
        with self.assertRaises(AccessError):
            branch_obj.with_user(self.branch_user_a).action_approve()


# ─────────────────────────────────────────────────────────────────────────────
# Test 5 — Branch Manager cannot access another branch's records
# ─────────────────────────────────────────────────────────────────────────────

@tagged("post_install", "-at_install")
class TestBranchIsolation(StrategyFixture):

    def test_branch_manager_cannot_see_other_branch_objective(self):
        plan = self._mk_plan()
        org_obj = self._mk_org_obj(plan)
        # branch_obj_b belongs to wh2; branch_user_a is assigned to wh1
        branch_obj_b = self._mk_branch_obj(plan, org_obj, warehouse=self.wh2)

        visible = self.env["strategic.objective"].with_user(self.branch_user_a).search([])
        self.assertNotIn(branch_obj_b, visible)

    def test_branch_manager_cannot_modify_other_branch_objective(self):
        plan = self._mk_plan()
        org_obj = self._mk_org_obj(plan)
        branch_obj_b = self._mk_branch_obj(plan, org_obj, warehouse=self.wh2)
        with self.assertRaises((AccessError, Exception)):
            branch_obj_b.with_user(self.branch_user_a).write({"name": "Hacked"})


# ─────────────────────────────────────────────────────────────────────────────
# Test 6 — Branch Manager cannot modify global KPI definitions or targets
# ─────────────────────────────────────────────────────────────────────────────

@tagged("post_install", "-at_install")
class TestBranchCannotEditKpi(StrategyFixture):

    def test_branch_cannot_create_kpi_definition(self):
        with self.assertRaises(AccessError):
            self.env["strategic.kpi.definition"].with_user(self.branch_user_a).create(
                {"name": "Forbidden KPI", "code": "FORBID", "unit": "%", "direction": "higher",
                 "company_id": self.company.id}
            )

    def test_branch_cannot_create_kpi_target(self):
        kpi = self._mk_kpi("BRTGT")
        with self.assertRaises(AccessError):
            self.env["strategic.kpi.target"].with_user(self.branch_user_a).create(
                {
                    "kpi_id": kpi.id,
                    "date_start": "2026-01-01",
                    "date_end": "2026-03-31",
                    "scope": "organization",
                    "target_value": 50.0,
                }
            )


# ─────────────────────────────────────────────────────────────────────────────
# Test 7 — Active objective core fields cannot be silently edited
# ─────────────────────────────────────────────────────────────────────────────

@tagged("post_install", "-at_install")
class TestActiveCoreImmutability(StrategyFixture):

    def test_active_core_fields_locked(self):
        plan = self._mk_plan()
        obj = self._mk_org_obj(plan)
        obj.action_submit()
        obj.with_user(self.head_user).action_approve()
        self.assertEqual(obj.state, "active")

        core_fields = [
            {"name": "Renamed"},
            {"description": "Changed"},
            {"deadline": "2027-01-01"},
            {"perspective": "customer"},
        ]
        for vals in core_fields:
            with self.assertRaises(ValidationError, msg="Expected lock for: %s" % vals):
                obj.write(vals)


# ─────────────────────────────────────────────────────────────────────────────
# Test 8 — Controlled revisions retain their history
# ─────────────────────────────────────────────────────────────────────────────

@tagged("post_install", "-at_install")
class TestRevisionHistory(StrategyFixture):

    def test_deadline_extension_creates_revision_record(self):
        plan = self._mk_plan()
        obj = self._mk_org_obj(plan, deadline="2026-06-30")
        obj.action_submit()
        obj.with_user(self.head_user).action_approve()

        obj.with_user(self.head_user).action_extend_deadline("2026-12-31", "Budget extended")
        revisions = obj.revision_ids
        self.assertTrue(revisions, "Expected at least one revision record.")
        rev = revisions[0]
        self.assertEqual(rev.field_name, "deadline")
        self.assertEqual(rev.old_value, "2026-06-30")
        self.assertEqual(rev.new_value, "2026-12-31")
        self.assertIn("Budget extended", rev.reason)
        self.assertEqual(rev.revised_by_id, self.head_user)


# ─────────────────────────────────────────────────────────────────────────────
# Test 9 — Parent deadline extension propagates to branch objectives
# ─────────────────────────────────────────────────────────────────────────────

@tagged("post_install", "-at_install")
class TestDeadlinePropagation(StrategyFixture):

    def test_parent_deadline_propagates_to_shorter_children(self):
        plan = self._mk_plan(end="2027-06-30")
        org_obj = self._mk_org_obj(plan, deadline="2026-06-30")
        branch_obj = self._mk_branch_obj(plan, org_obj)
        # Both set to 2026-12-31; branch has shorter deadline if we test:
        branch_obj.with_context(allow_objective_core_write=True).write({"deadline": "2026-03-31"})

        org_obj.action_submit()
        org_obj.with_user(self.head_user).action_approve()
        branch_obj.action_submit()
        branch_obj.with_user(self.head_user).action_approve()

        org_obj.with_user(self.head_user).action_extend_deadline("2026-12-31", "Q4 extension")

        # Branch objective's shorter deadline (2026-03-31) should be extended to 2026-12-31
        self.assertEqual(str(branch_obj.deadline), "2026-12-31")
        # A revision should exist on the branch objective too
        self.assertTrue(branch_obj.revision_ids)

    def test_branch_with_longer_deadline_not_affected(self):
        """Branch objectives already at or beyond the new deadline are not touched."""
        plan = self._mk_plan(end="2027-06-30")
        org_obj = self._mk_org_obj(plan, deadline="2026-06-30")
        branch_obj = self._mk_branch_obj(plan, org_obj)
        # branch_obj.deadline stays at 2026-12-31 — ALREADY longer than 2026-09-30 extension
        branch_obj.with_context(allow_objective_core_write=True).write({"deadline": "2026-12-31"})

        org_obj.action_submit()
        org_obj.with_user(self.head_user).action_approve()
        branch_obj.action_submit()
        branch_obj.with_user(self.head_user).action_approve()

        # Extend parent from 2026-06-30 to 2026-09-30 — branch stays at 2026-12-31
        org_obj.with_user(self.head_user).action_extend_deadline("2026-09-30", "Mid-year extension")
        self.assertEqual(str(branch_obj.deadline), "2026-12-31")


# ─────────────────────────────────────────────────────────────────────────────
# Test 10 — Parent closure does NOT auto-close branch objectives
# ─────────────────────────────────────────────────────────────────────────────

@tagged("post_install", "-at_install")
class TestParentClosureIndependence(StrategyFixture):

    def test_closing_parent_does_not_close_branch(self):
        plan = self._mk_plan()
        org_obj = self._mk_org_obj(plan)
        branch_obj = self._mk_branch_obj(plan, org_obj)

        org_obj.action_submit()
        org_obj.with_user(self.head_user).action_approve()
        org_obj.with_user(self.head_user).action_complete()
        org_obj.with_user(self.head_user).action_close()

        self.assertEqual(org_obj.state, "closed")
        # branch_obj must remain in its current state — untouched
        self.assertNotEqual(branch_obj.state, "closed")


# ─────────────────────────────────────────────────────────────────────────────
# Test 11 — Key Result must have exactly one primary KPI
# ─────────────────────────────────────────────────────────────────────────────

@tagged("post_install", "-at_install")
class TestKeyResultSinglePrimaryKpi(StrategyFixture):

    def test_primary_kpi_required(self):
        plan = self._mk_plan()
        org_obj = self._mk_org_obj(plan)
        okr = self._mk_okr(org_obj)
        kpi = self._mk_kpi("KR11")
        target = self._mk_target(kpi)
        # primary_kpi_id is a required field — ORM should refuse creation without it
        with self.assertRaises(Exception):
            self.env["strategic.key.result"].create(
                {
                    "name": "KR without KPI",
                    "okr_objective_id": okr.id,
                    "target_id": target.id,
                    # primary_kpi_id intentionally omitted
                }
            )

    def test_supporting_kpi_cannot_be_primary(self):
        plan = self._mk_plan()
        org_obj = self._mk_org_obj(plan)
        okr = self._mk_okr(org_obj)
        kpi = self._mk_kpi("KR11B")
        target = self._mk_target(kpi)
        with self.assertRaises(ValidationError):
            self.env["strategic.key.result"].create(
                {
                    "name": "Duplicate KPI",
                    "okr_objective_id": okr.id,
                    "primary_kpi_id": kpi.id,
                    "supporting_kpi_ids": [(4, kpi.id)],
                    "target_id": target.id,
                }
            )


# ─────────────────────────────────────────────────────────────────────────────
# Test 12 — Supporting KPIs do not affect scoring
# ─────────────────────────────────────────────────────────────────────────────

@tagged("post_install", "-at_install")
class TestSupportingKpiNoEffect(StrategyFixture):

    def test_supporting_kpi_does_not_affect_attainment(self):
        plan = self._mk_plan()
        org_obj = self._mk_org_obj(plan)
        okr = self._mk_okr(org_obj)

        kpi_primary = self._mk_kpi("KR12P")
        kpi_support = self._mk_kpi("KR12S")

        target = self._mk_target(kpi_primary)
        evaluation = self._mk_evaluation(target, actual=90.0)  # 90% attainment

        kr = self.env["strategic.key.result"].create(
            {
                "name": "KR with supporting",
                "okr_objective_id": okr.id,
                "primary_kpi_id": kpi_primary.id,
                "supporting_kpi_ids": [(4, kpi_support.id)],
                "target_id": target.id,
                "evaluation_id": evaluation.id,
            }
        )
        # Attainment should be driven purely by the primary KPI evaluation
        self.assertAlmostEqual(kr.attainment, 90.0)
        self.assertEqual(kr.attainment_status, "almost_met")


# ─────────────────────────────────────────────────────────────────────────────
# Test 13 — Objective score is arithmetic mean of required KR attainments
# ─────────────────────────────────────────────────────────────────────────────

@tagged("post_install", "-at_install")
class TestObjectiveScoring(StrategyFixture):

    def test_objective_score_is_mean_of_required_krs(self):
        plan = self._mk_plan()
        org_obj = self._mk_org_obj(plan)
        okr = self._mk_okr(org_obj)

        kpi1 = self._mk_kpi("KR13A")
        kpi2 = self._mk_kpi("KR13B")

        target1 = self._mk_target(kpi1)
        target2 = self._mk_target(kpi2)

        eval1 = self._mk_evaluation(target1, actual=80.0)   # 80%
        eval2 = self._mk_evaluation(target2, actual=100.0)  # 100%

        self._mk_kr(okr, kpi1, target1, evaluation=eval1, required=True)
        self._mk_kr(okr, kpi2, target2, evaluation=eval2, required=True)

        # Trigger compute
        okr.invalidate_recordset()
        org_obj.invalidate_recordset()

        expected_score = (80.0 + 100.0) / 2  # = 90.0
        self.assertAlmostEqual(okr.score, expected_score, places=2)
        self.assertEqual(okr.scoring_status, "almost_met")

    def test_optional_kr_excluded_from_mean(self):
        plan = self._mk_plan()
        org_obj = self._mk_org_obj(plan)
        okr = self._mk_okr(org_obj)

        kpi_req = self._mk_kpi("KR13C")
        kpi_opt = self._mk_kpi("KR13D")

        target_req = self._mk_target(kpi_req)
        target_opt = self._mk_target(kpi_opt)

        eval_req = self._mk_evaluation(target_req, actual=100.0)   # 100%
        eval_opt = self._mk_evaluation(target_opt, actual=10.0)    # 10%

        self._mk_kr(okr, kpi_req, target_req, evaluation=eval_req, required=True)
        self._mk_kr(okr, kpi_opt, target_opt, evaluation=eval_opt, required=False)

        okr.invalidate_recordset()
        # Only the required KR counts — score should be 100%
        self.assertAlmostEqual(okr.score, 100.0, places=2)
        self.assertEqual(okr.scoring_status, "achieved")


# ─────────────────────────────────────────────────────────────────────────────
# Test 14 — Missing required KR data produces Not Evaluated
# ─────────────────────────────────────────────────────────────────────────────

@tagged("post_install", "-at_install")
class TestMissingKrDataNotEvaluated(StrategyFixture):

    def test_no_key_results_gives_not_evaluated(self):
        plan = self._mk_plan()
        org_obj = self._mk_org_obj(plan)
        okr = self._mk_okr(org_obj)
        okr.invalidate_recordset()
        self.assertEqual(okr.scoring_status, "not_evaluated")
        self.assertEqual(okr.score, 0.0)

    def test_required_kr_without_evaluation_gives_not_evaluated(self):
        plan = self._mk_plan()
        org_obj = self._mk_org_obj(plan)
        okr = self._mk_okr(org_obj)

        kpi = self._mk_kpi("KR14A")
        target = self._mk_target(kpi)
        # No evaluation linked
        self._mk_kr(okr, kpi, target, evaluation=None, required=True)

        okr.invalidate_recordset()
        self.assertEqual(okr.scoring_status, "not_evaluated")

    def test_data_quality_issue_propagates_not_evaluated(self):
        plan = self._mk_plan()
        org_obj = self._mk_org_obj(plan)
        okr = self._mk_okr(org_obj)

        kpi = self._mk_kpi("KR14B")
        # zero target → will produce not_evaluated evaluation
        target = self._mk_target(kpi, value=0.0)
        evaluation = self._mk_evaluation(target, actual=50.0, has_actual=True)
        # evaluation status should be not_evaluated (division by zero guard)
        self.assertEqual(evaluation.status, "not_evaluated")

        self._mk_kr(okr, kpi, target, evaluation=evaluation, required=True)
        okr.invalidate_recordset()
        self.assertEqual(okr.scoring_status, "not_evaluated")


# ─────────────────────────────────────────────────────────────────────────────
# Test 15 — Boundary attainment bands
# ─────────────────────────────────────────────────────────────────────────────

@tagged("post_install", "-at_install")
class TestBoundaryScoring(StrategyFixture):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.kpi = cls._mk_kpi(cls, "BOUND")
        cls.target = cls._mk_target(cls, cls.kpi, value=100.0)

    def _check_band(self, actual, expected_status):
        evaluation = self._mk_evaluation(self.target, actual=actual)
        self.assertEqual(evaluation.status, "valid")
        self.assertEqual(evaluation.attainment_status, expected_status,
                         "Actual %s expected %s got %s" % (actual, expected_status, evaluation.attainment_status))

    def test_below_80_not_achieved(self):
        self._check_band(79.99, "not_achieved")

    def test_exactly_80_almost_met(self):
        self._check_band(80.0, "almost_met")

    def test_below_100_almost_met(self):
        self._check_band(99.99, "almost_met")

    def test_exactly_100_achieved(self):
        self._check_band(100.0, "achieved")

    def test_above_100_exceeded(self):
        self._check_band(100.01, "exceeded")


# ─────────────────────────────────────────────────────────────────────────────
# Test 16 — Phase 1 regression: calc engine & security continue to pass
# ─────────────────────────────────────────────────────────────────────────────

@tagged("post_install", "-at_install")
class TestPhase1Regression(StrategyFixture):

    def test_higher_is_better_calculation_unchanged(self):
        kpi = self._mk_kpi("REG1H")
        target = self._mk_target(kpi, value=100.0)
        ev = self._mk_evaluation(target, actual=80.0)
        self.assertEqual(ev.status, "valid")
        self.assertAlmostEqual(ev.attainment, 80.0)
        self.assertEqual(ev.attainment_status, "almost_met")

    def test_lower_is_better_calculation_unchanged(self):
        kpi = self._mk_kpi("REG1L", direction="lower")
        target = self._mk_target(kpi, value=10.0)
        ev = self._mk_evaluation(target, actual=5.0)
        self.assertEqual(ev.status, "valid")
        self.assertAlmostEqual(ev.attainment, 200.0)
        self.assertEqual(ev.attainment_status, "exceeded")

    def test_range_target_unchanged(self):
        kpi = self._mk_kpi("REG1R", direction="range")
        target = self.env["strategic.kpi.target"].create(
            {
                "kpi_id": kpi.id,
                "date_start": "2026-01-01",
                "date_end": "2026-03-31",
                "scope": "organization",
                "target_value": 0.0,
                "range_min": 90.0,
                "range_max": 110.0,
                "state": "approved",
            }
        )
        ev_in = self._mk_evaluation(target, actual=100.0)
        self.assertEqual(ev_in.attainment_status, "achieved")
        ev_out = self._mk_evaluation(target, actual=120.0)
        self.assertEqual(ev_out.attainment_status, "not_achieved")

    def test_evaluation_immutability_unchanged(self):
        kpi = self._mk_kpi("REG1I")
        target = self._mk_target(kpi, value=100.0)
        ev = self._mk_evaluation(target, actual=90.0)
        with self.assertRaises(ValidationError):
            ev.write({"actual_value": 95.0})

    def test_branch_kpi_isolation_unchanged(self):
        kpi = self._mk_kpi("REG1B")
        target_wh1 = self._mk_target(kpi, scope="branch", warehouse=self.wh1)
        target_wh2 = self._mk_target(kpi, scope="branch", warehouse=self.wh2)
        visible = self.env["strategic.kpi.target"].with_user(self.branch_user_a).search([])
        self.assertIn(target_wh1, visible)
        self.assertNotIn(target_wh2, visible)

