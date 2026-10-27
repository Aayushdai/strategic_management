"""Business Goals workflow: draft goal -> measurement -> approved target -> Key Result -> approval."""

from datetime import date
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase, tagged

_COLLECTOR = "odoo.addons.strategic_management.models.goal_setup.source_collector_for"


def _valid_collector(value):
    """Stand-in data source returning a valid value; scoring, linking and approvals still run for real."""
    def collector(target, version):
        return {"status": "valid", "has_value": True, "value": value, "unit": version.unit, "reason": False,
                "source_model": "pos.order.line", "source_record_ref": False, "source_key": "probe"}
    return lambda version: collector


@tagged("post_install", "-at_install")
class TestBusinessGoals(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        group_user = cls.env.ref("base.group_user")
        cls.head = cls._make_user("bg_head", [group_user, cls.env.ref("strategic_management.group_strategic_head_office_manager")])
        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.manager = cls._make_user("bg_manager", [group_user, cls.env.ref("strategic_management.group_strategic_branch_manager")], cls.warehouse)
        cls.product = cls.env["product.template"].create({
            "name": "Business Goals test latte", "uom_id": cls.env.ref("uom.product_uom_unit").id,
        }).product_variant_id
        cls.plan = cls.env["strategic.plan"].with_user(cls.head).create({
            "name": "Business Goals test plan", "company_id": cls.company.id,
            "date_start": "2098-01-01", "date_end": "2098-12-31",
        })
        cls.Setup = cls.env["strategic.goal.setup"]

    @classmethod
    def _make_user(cls, login, groups, warehouse=False):
        values = {
            "name": login, "login": login, "email": "%s@example.com" % login,
            "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
            "group_ids": [(6, 0, [group.id for group in groups])],
        }
        if warehouse:
            values["strategic_branch_warehouse_id"] = warehouse.id
        return cls.env["res.users"].with_context(no_reset_password=True).create(values)

    def _create_goal(self, area="sales_finance", name="Sell more lattes"):
        payload = self.Setup.with_user(self.head).create_goal({
            "name": name, "business_area": area, "desired_result": "Sell 20 lattes",
            "deadline": "2098-12-31", "owner_id": self.head.id, "reason": "Latte is our best margin drink.",
            "plan_id": self.plan.id,
        })
        return self.Setup.browse(payload["setup_id"]).with_user(self.head), payload

    def _configure_latte(self, setup, target=20.0):
        return setup.configure_measurement({
            "measure_key": "pos_sold_quantity", "product_id": self.product.id, "target_value": target,
            "period_start": "2098-01-01", "period_end": "2098-01-31",
        })

    def _existing_latte_kpi(self, code="BG_TEST_LATTE"):
        return self.env["strategic.kpi.definition"].with_user(self.head).create({
            "name": "Latte sold", "code": code, "company_id": self.company.id, "unit": self.product.uom_id.name,
            "direction": "higher", "source_type": "pos", "calculation_type": "pos_sold_quantity",
            "source_product_id": self.product.id,
        })

    def _measured_goal(self):
        """Goal with an approved target and linked Key Result (data source stubbed as valid)."""
        setup, _payload = self._create_goal()
        with patch(_COLLECTOR, _valid_collector(12.0)):
            self._configure_latte(setup)
            setup.prepare_target()
        return setup

    # 1, 4, 10 -------------------------------------------------------------
    def test_create_goal_saves_persistent_draft_not_active(self):
        setup, payload = self._create_goal()
        objective = setup.objective_id
        self.assertEqual(objective.state, "draft")
        self.assertEqual(objective.business_area, "sales_finance")
        self.assertEqual(objective.perspective, "financial", "perspective stays a Balanced Scorecard value")
        self.assertEqual(objective.plan_id, self.plan)
        self.assertEqual(payload["status"], "Needs setup")
        self.assertEqual(payload["current"], "Not measured")
        with self.assertRaises(UserError):
            setup.submit_goal()
        self.assertEqual(objective.state, "draft")

    # Plan scenarios A-E: tests/features/business_goal_plan.feature ----------
    def test_plan_a_no_covering_plan_saves_nothing_and_explains(self):
        """Given no active plan covers the deadline, when the draft is saved, then nothing is created and the
        message says so, gives the date that works and where Head Office fixes it."""
        objectives = self.env["strategic.objective"].search_count([])
        with self.assertRaises(UserError) as caught:
            self.Setup.with_user(self.head).create_goal({
                "name": "Too late", "business_area": "purchasing", "deadline": "2199-01-31",
            })
        message = str(caught.exception)
        self.assertIn("has not been saved", message)
        self.assertIn("31 Jan 2199", message)
        self.assertIn("31 Dec 2098", message, "names the latest date a plan covers")
        self.assertIn("Strategy & KPIs > Strategy > Strategic Plans", message)
        self.assertEqual(self.env["strategic.objective"].search_count([]), objectives)

    def _ready_to_submit(self):
        setup = self._measured_goal()
        setup.approve_target()
        return setup

    def test_plan_b_submit_refused_when_plan_archived_or_shortened(self):
        """Given a measured draft whose plan is archived or shortened, when submitted, then it is refused and stays draft."""
        setup = self._ready_to_submit()
        self.plan.with_user(self.head).write({"active": False})
        with self.assertRaisesRegex(UserError, "no longer covers"):
            setup.submit_goal()
        self.assertEqual(setup.objective_id.state, "draft")
        self.plan.with_user(self.head).write({"active": True, "date_end": "2098-06-30"})
        with self.assertRaisesRegex(UserError, "no longer covers"):
            setup.submit_goal()
        self.assertEqual(setup.objective_id.state, "draft")

    def test_plan_b_approval_refused_when_plan_archived_after_submission(self):
        """Given a submitted goal whose plan is then archived, when approved, then approval is refused."""
        setup = self._ready_to_submit()
        setup.submit_goal()
        self.plan.with_user(self.head).write({"active": False})
        with self.assertRaisesRegex(UserError, "no longer covers"):
            setup.approve_goal()
        self.assertEqual(setup.objective_id.state, "pending_approval")

    def test_plan_c_valid_plan_is_linked(self):
        """Given an active plan covers the deadline, then the goal is linked to it and stays a draft."""
        setup, _payload = self._create_goal()
        self.assertEqual(setup.objective_id.plan_id, self.plan)
        self.assertEqual(setup.objective_id.state, "draft")

    def test_plan_d_archived_or_non_covering_plan_rejected(self):
        """Given a chosen plan that is archived or does not cover the deadline, then saving is rejected clearly."""
        short = self.env["strategic.plan"].with_user(self.head).create({
            "name": "Short plan", "company_id": self.company.id, "date_start": "2098-01-01", "date_end": "2098-03-31",
        })
        archived = self.env["strategic.plan"].with_user(self.head).create({
            "name": "Old plan", "company_id": self.company.id, "date_start": "2098-01-01", "date_end": "2098-12-31", "active": False,
        })
        for plan in (short, archived):
            with self.assertRaisesRegex(UserError, "cannot hold this goal"):
                self.Setup.with_user(self.head).create_goal({
                    "name": "Wrong plan", "business_area": "sales_finance", "deadline": "2098-12-31", "plan_id": plan.id,
                })

    def test_plan_e_form_shows_latest_allowed_deadline(self):
        """Given an owner opening the form, then it knows the latest deadline any active plan covers."""
        options = self.Setup.with_user(self.head).get_form_options()
        self.assertTrue(options["has_plan"])
        self.assertGreaterEqual(options["latest_deadline"], "2098-12-31")

    # 2, 3, 14 -------------------------------------------------------------
    def test_same_record_in_business_goals_and_area_pages_without_duplicates(self):
        setup, _payload = self._create_goal()
        objective_count = self.env["strategic.objective"].search_count([])
        everywhere = [g for g in self.Setup.with_user(self.head).get_goals() if g["objective_id"] == setup.objective_id.id]
        in_area = [g for g in self.Setup.with_user(self.head).get_goals("sales_finance") if g["objective_id"] == setup.objective_id.id]
        elsewhere = [g for g in self.Setup.with_user(self.head).get_goals("purchasing") if g["objective_id"] == setup.objective_id.id]
        self.assertEqual(len(everywhere), 1)
        self.assertEqual(everywhere, in_area, "area page shows the identical record and values")
        self.assertFalse(elsewhere)
        self.assertEqual(self.env["strategic.objective"].search_count([]), objective_count)

    def test_measure_from_another_area_is_rejected(self):
        setup, _payload = self._create_goal(area="purchasing")
        with self.assertRaisesRegex(UserError, "Choose one of the Purchasing goal options"):
            self._configure_latte(setup)

    # 6, 12 ----------------------------------------------------------------
    def test_source_failure_blocks_readiness_and_target(self):
        """Real POS collector: without a company timezone it cannot bound the period, so the goal stays in setup."""
        setup, _payload = self._create_goal()
        self.company.partner_id.sudo().write({"tz": False})
        payload = self._configure_latte(setup)
        data_check = next(check for check in payload["readiness"] if check["key"] == "data")
        self.assertFalse(data_check["ok"])
        self.assertIn("timezone", data_check["message"])
        self.assertEqual(setup.setup_state, "needs_setup")
        with self.assertRaises(UserError):
            setup.prepare_target()
        self.assertFalse(setup.target_id)

    def test_unsupported_area_explains_missing_measure(self):
        setup, _payload = self._create_goal(area="people")
        payload = setup.check_readiness()
        self.assertEqual(setup.setup_state, "needs_setup")
        self.assertIn("No People measures", payload["readiness"][0]["message"])

    # 5, 7, 4, 10, 11 ------------------------------------------------------
    def test_full_flow_reuses_kpi_links_key_result_and_respects_approvals(self):
        kpi = self._existing_latte_kpi()
        kpi_count = self.env["strategic.kpi.definition"].search_count([])
        setup = self._measured_goal()
        self.assertEqual(setup.kpi_id, kpi, "the compatible existing KPI is reused")
        self.assertEqual(self.env["strategic.kpi.definition"].search_count([]), kpi_count)
        self.assertEqual(setup.target_id.state, "draft")
        self.assertEqual(setup.setup_state, "target_pending")

        with self.assertRaises(AccessError):
            setup.with_user(self.manager).approve_target()
        payload = setup.approve_target()
        key_result = setup.key_result_id
        self.assertEqual(setup.target_id.state, "approved")
        self.assertEqual(key_result.primary_kpi_id, kpi)
        self.assertEqual(key_result.target_id, setup.target_id)
        self.assertEqual(key_result.okr_objective_id.strategic_objective_id, setup.objective_id)
        self.assertEqual(payload["status"], "Ready to submit")
        self.assertEqual(setup.objective_id.state, "draft")

        payload = setup.submit_goal()
        self.assertEqual(setup.objective_id.state, "pending_approval")
        self.assertEqual(payload["status"], "Pending approval", "never shown as Active before approval")
        with self.assertRaises(AccessError):
            setup.with_user(self.manager).approve_goal()
        payload = setup.approve_goal()
        self.assertEqual(setup.objective_id.state, "active")
        self.assertEqual(payload["status"], "Active · not evaluated yet")
        self.assertEqual(setup.objective_id.scoring_status, "not_evaluated", "missing evaluation stays Not Evaluated")

    def test_branch_manager_cannot_prepare_target(self):
        setup, _payload = self._create_goal()
        with patch(_COLLECTOR, _valid_collector(12.0)):
            self._configure_latte(setup)
        with self.assertRaises(AccessError):
            setup.with_user(self.manager).prepare_target()

    # 8, 9 -----------------------------------------------------------------
    def test_existing_evaluation_links_to_new_key_result_and_scores_objective(self):
        setup = self._measured_goal()
        # The target can be evaluated as soon as it is approved, before its Key Result exists.
        setup.target_id.action_approve()
        evaluation = self.env["strategic.kpi.evaluation"].with_user(self.head).create({
            "target_id": setup.target_id.id, "actual_value": 15.0, "has_actual_value": True,
        })
        setup.approve_target()
        self.assertEqual(setup.key_result_id.evaluation_id, evaluation)
        self.assertEqual(setup.key_result_id.scoring_status, "valid")
        self.assertAlmostEqual(setup.objective_id.score, 75.0)
        payload = self.Setup.with_user(self.head).get_goals("sales_finance")
        goal = next(g for g in payload if g["objective_id"] == setup.objective_id.id)
        self.assertEqual(goal["current"], "15 %s" % self.product.uom_id.name)

    def test_evaluation_of_another_target_is_not_linked(self):
        setup = self._measured_goal()
        other = self.env["strategic.kpi.target"].with_user(self.head).create({
            "kpi_id": setup.kpi_id.id, "date_start": "2098-03-01", "date_end": "2098-03-31",
            "scope": "organization", "target_value": 5.0,
        })
        other.action_approve()
        self.env["strategic.kpi.evaluation"].with_user(self.head).create({
            "target_id": other.id, "actual_value": 9.0, "has_actual_value": True,
        })
        setup.approve_target()
        self.assertFalse(setup.key_result_id.evaluation_id)

    # 12, 13 ---------------------------------------------------------------
    def _manual_kpi(self, code, direction):
        return self.env["strategic.kpi.definition"].with_user(self.head).create({
            "name": code, "code": code, "company_id": self.company.id, "unit": "L",
            "direction": direction, "source_type": "manual", "calculation_type": "manual_value",
        })

    def _evaluate(self, target, actual):
        """Manual-value KPIs are evaluated only through the governed measurement approval
        (Branch Manager submits for their branch, Head Office approves; company-wide manual submissions are deferred)."""
        measurement = self.env["strategic.kpi.measurement"].with_user(self.manager).create({
            "target_id": target.id, "actual_value": actual, "has_actual_value": True,
            "source_reference": "Reconciled stock count.", "submitter_note": "Test submission.",
        })
        measurement.with_user(self.manager).action_submit()
        measurement.with_user(self.head).action_approve()
        return measurement.evaluation_ids[:1]

    def _baseline_target(self, baseline_value, target_value, code="BG_BASELINE"):
        kpi = self._manual_kpi(code, "lower")
        today = date.today()
        baseline = self.env["strategic.kpi.baseline"].with_user(self.head).create({
            "kpi_id": kpi.id, "scope": "branch", "warehouse_id": self.warehouse.id, "baseline_value": baseline_value,
            "source_period_start": "2097-01-01", "source_period_end": "2097-03-01",
            "effective_from": today.isoformat(), "source_note": "Test baseline.", "change_reason": "Test.",
        })
        baseline.action_approve()
        target = self.env["strategic.kpi.target"].with_user(self.head).create({
            "kpi_id": kpi.id, "date_start": "2098-01-01", "date_end": "2098-03-01", "scope": "branch",
            "warehouse_id": self.warehouse.id, "target_basis": "baseline_relative", "baseline_id": baseline.id, "target_value": target_value,
        })
        target.action_approve()
        return target

    def test_baseline_relative_progress_100_80_90_is_50_percent(self):
        target = self._baseline_target(100.0, 80.0)
        evaluation = self._evaluate(target, 90.0)
        self.assertTrue(evaluation.has_progress_to_target)
        self.assertAlmostEqual(evaluation.progress_to_target, 50.0)
        self.assertAlmostEqual(evaluation.attainment, 80.0 / 90.0 * 100.0, places=4, msg="attainment rule unchanged")

    def test_baseline_relative_progress_edge_cases(self):
        worse = self._evaluate(self._baseline_target(100.0, 80.0, code="BG_WORSE"), 110.0)
        self.assertAlmostEqual(worse.progress_to_target, -50.0, msg="worse than baseline")
        beyond = self._evaluate(self._baseline_target(100.0, 80.0, code="BG_BEYOND"), 70.0)
        self.assertAlmostEqual(beyond.progress_to_target, 150.0, msg="beyond target")
        flat = self._evaluate(self._baseline_target(80.0, 80.0, code="BG_FLAT"), 75.0)
        self.assertFalse(flat.has_progress_to_target, "baseline equal to target has no progress")
        # A measurement always carries a value, so the missing-actual rule is checked on the calculation itself.
        missing = self.env["strategic.kpi.evaluation"]._calculate_baseline_progress("lower", {
            "target_basis": "baseline_relative", "status": "not_evaluated", "has_actual_value": False,
            "baseline_value": 100.0, "target_value": 80.0, "actual_value": 0.0,
        })
        self.assertFalse(missing["has_progress_to_target"])

    def test_absolute_target_evaluation_behaviour_unchanged(self):
        kpi = self._manual_kpi("BG_ABSOLUTE", "higher")
        target = self.env["strategic.kpi.target"].with_user(self.head).create({
            "kpi_id": kpi.id, "date_start": "2098-01-01", "date_end": "2098-01-31",
            "scope": "branch", "warehouse_id": self.warehouse.id, "target_value": 100.0,
        })
        target.action_approve()
        evaluation = self._evaluate(target, 80.0)
        self.assertAlmostEqual(evaluation.attainment, 80.0)
        self.assertEqual(evaluation.status, "valid")
        self.assertFalse(evaluation.has_progress_to_target)
