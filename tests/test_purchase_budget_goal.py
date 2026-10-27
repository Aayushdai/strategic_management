"""BDD scenarios for the weekly purchasing budget goal (tests/features/purchase_budget_goal.feature).

Real purchase orders, the real collector, real KPI evaluation, approvals and record rules; nothing is stubbed.
Closed-week scenarios use the week of Mon 2001-01-01 so no real company data falls inside it.
"""

from datetime import datetime, timedelta

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged

CLOSED_START, CLOSED_END = "2001-01-01", "2001-01-07"


@tagged("post_install", "-at_install")
class TestPurchaseBudgetGoal(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        cls.currency = cls.company.currency_id
        group_user = cls.env.ref("base.group_user")
        purchase_user = cls.env.ref("purchase.group_purchase_user")
        head_group = cls.env.ref("strategic_management.group_strategic_head_office_manager")
        branch_group = cls.env.ref("strategic_management.group_strategic_branch_manager")
        Warehouse = cls.env["stock.warehouse"]
        cls.branch1 = Warehouse.create({"name": "BG Budget Branch 1", "code": "BGB1", "company_id": cls.company.id})
        cls.branch2 = Warehouse.create({"name": "BG Budget Branch 2", "code": "BGB2", "company_id": cls.company.id})
        cls.head = cls._make_user("bg_budget_head", [group_user, head_group, purchase_user])
        cls.manager2 = cls._make_user("bg_budget_manager2", [group_user, branch_group, purchase_user], cls.branch2)
        cls.vendor = cls.env["res.partner"].create({"name": "BG Budget Supplier", "supplier_rank": 1})
        cls.item = cls.env["product.template"].create({
            "name": "BG Budget Milk Crate", "purchase_ok": True, "uom_id": cls.env.ref("uom.product_uom_unit").id,
        }).product_variant_id
        cls.plan = cls.env["strategic.plan"].with_user(cls.head).create({
            "name": "BG Budget Plan", "company_id": cls.company.id, "date_start": "2000-01-01", "date_end": "2099-12-31",
        })
        cls.Setup = cls.env["strategic.goal.setup"].with_user(cls.head)
        cls.company_goal = cls.Setup.create_goal({
            "name": "Control purchasing costs", "business_area": "purchasing", "deadline": "2099-12-31",
            "plan_id": cls.plan.id, "owner_id": cls.head.id,
        })["objective_id"]

    @classmethod
    def _make_user(cls, login, groups, warehouse=False):
        values = {
            "name": login, "login": login, "email": "%s@example.com" % login, "tz": "UTC",
            "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
            "group_ids": [(6, 0, [group.id for group in groups])],
        }
        if warehouse:
            values["strategic_branch_warehouse_id"] = warehouse.id
        return cls.env["res.users"].with_context(no_reset_password=True).create(values)

    # ------------------------------------------------------------------ helpers
    def _po(self, amount, confirmed_at=None, warehouse=None, currency=None, taxes=None, confirm=True):
        """A real purchase order for one line; confirmed_at back-dates its confirmation (date_approve)."""
        warehouse = warehouse or self.branch1
        order = self.env["purchase.order"].create({
            "partner_id": self.vendor.id,
            "company_id": self.company.id,
            "picking_type_id": warehouse.in_type_id.id,
            "currency_id": (currency or self.currency).id,
            "order_line": [(0, 0, {
                "name": self.item.display_name, "product_id": self.item.id, "product_qty": 1.0,
                "price_unit": amount, "date_planned": fields.Datetime.now(),
                "tax_ids": [(6, 0, taxes.ids if taxes else [])],
            })],
        })
        if confirm:
            order.button_confirm()
            if confirmed_at:
                order.write({"date_approve": confirmed_at})
        return order

    def _goal(self, start=CLOSED_START, end=CLOSED_END, limit=10000.0, warehouse="branch1", name="Keep weekly purchasing within budget"):
        values = {
            "name": name, "business_area": "purchasing", "deadline": end, "owner_id": self.head.id,
            "desired_result": "Keep confirmed purchase orders within the weekly budget",
        }
        if warehouse:
            values.update({"scope": "branch", "warehouse_id": getattr(self, warehouse).id, "parent_objective_id": self.company_goal})
        else:
            values.update({"scope": "organization", "plan_id": self.plan.id})
        setup = self.Setup.browse(self.Setup.create_goal(values)["setup_id"])
        payload = setup.configure_measurement({
            "template_key": "budget", "target_value": limit, "period_start": start, "period_end": end,
        })
        self.assertEqual(setup.setup_state, "ready", payload["readiness"])
        setup.prepare_target()
        setup.approve_target()
        return setup

    def _view(self, setup):
        return next(goal for goal in self.Setup.get_goals("purchasing") if goal["objective_id"] == setup.objective_id.id)

    def _final(self, setup):
        setup.evaluate_now()
        return self._view(setup)

    def _at(self, day, hh=10):
        return datetime.strptime(day, "%Y-%m-%d").replace(hour=hh)

    # ------------------------------------------------------------------ scenarios
    def test_a_create_goal_saves_linked_records_and_reopens(self):
        """Given a Head Office Manager, when they choose Purchasing, 10,000, one week and a branch,
        then goal, target and Key Result are saved without a hand-made formula, and reopening shows the same goal."""
        setup = self._goal()
        target, key_result = setup.target_id, setup.key_result_id
        self.assertEqual(setup.objective_id.business_area, "purchasing")
        self.assertEqual(setup.objective_id.scope, "branch")
        self.assertEqual(setup.objective_id.warehouse_id, self.branch1)
        self.assertEqual(setup.kpi_id.calculation_type, "purchase_confirmed_amount")
        self.assertEqual((target.state, target.range_min, target.range_max), ("approved", 0.0, 10000.0))
        self.assertEqual((str(target.date_start), str(target.date_end)), (CLOSED_START, CLOSED_END))
        self.assertEqual(key_result.target_id, target)
        self.assertEqual(key_result.okr_objective_id.date_start, target.date_start)
        first, second = self._view(setup), self._view(setup)
        self.assertEqual(first, second)
        self.assertEqual(first["target"], "Up to %s" % setup._money(10000.0))

    def test_b_below_limit_is_achieved_with_remaining_budget(self):
        """Given 8,500 confirmed in the closed week, then 8,500 committed, 1,500 remaining, Achieved."""
        self._po(5000.0, self._at("2001-01-02"))
        self._po(3500.0, self._at("2001-01-05"))
        setup = self._goal()
        budget = self._final(setup)["result"]
        self.assertTrue(budget["final"])
        self.assertEqual(budget["value"], 8500.0)
        self.assertEqual(budget["remaining_display"], setup._money(1500.0))
        self.assertEqual(budget["status"], "Achieved: within budget")
        self.assertEqual(setup.key_result_id.evaluation_id.attainment_status, "achieved")

    def test_c_equal_to_limit_is_achieved(self):
        """Given exactly 10,000 confirmed, then the goal is Achieved (the limit is inclusive)."""
        self._po(10000.0, self._at("2001-01-03"))
        setup = self._goal()
        budget = self._final(setup)["result"]
        self.assertEqual(budget["value"], 10000.0)
        self.assertEqual(budget["status"], "Achieved: within budget")
        self.assertEqual(setup.key_result_id.evaluation_id.attainment_status, "achieved")

    def test_d_over_limit_is_not_achieved_never_almost_met(self):
        """Given 10,001 confirmed, then Not achieved, over budget by 1, and never Almost Met."""
        self._po(10001.0, self._at("2001-01-03"))
        setup = self._goal()
        budget = self._final(setup)["result"]
        self.assertEqual(budget["status"], "Not achieved: over budget by %s" % setup._money(1.0))
        evaluation = setup.key_result_id.evaluation_id
        self.assertEqual(evaluation.attainment_status, "not_achieved")
        self.assertNotEqual(evaluation.attainment_status, "almost_met")

    def test_e_open_week_shows_spending_so_far_and_no_final_result(self):
        """Given 6,000 confirmed in the current week, then 6,000 so far, 4,000 remaining, 'Within budget so far',
        and no final result can be recorded before the week ends."""
        today = fields.Date.context_today(self.env["strategic.goal.setup"].with_context(tz="UTC"))
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
        self._po(6000.0)
        setup = self._goal(start=str(start), end=str(end))
        view = self._view(setup)
        budget = view["result"]
        self.assertEqual(budget["phase"], "open")
        self.assertFalse(budget["final"])
        self.assertEqual(budget["value"], 6000.0)
        self.assertEqual(budget["remaining_display"], setup._money(4000.0))
        self.assertEqual(budget["status"], "Within budget so far")
        self.assertFalse(view["can_evaluate"])
        with self.assertRaises(ValidationError):
            setup.target_id.with_user(self.head).action_evaluate_source()
        self.assertFalse(self.env["strategic.kpi.evaluation"].search([("target_id", "=", setup.target_id.id)]))
        self._po(4500.0)
        self.assertEqual(self._view(setup)["result"]["status"], "Over budget by %s" % setup._money(500.0))

    def test_f_rfq_excluded_unreceived_included_and_receipt_not_double_counted(self):
        """Given a draft RFQ and a confirmed unreceived 4,000 order, then only the 4,000 counts, also after receipt."""
        self._po(9000.0, confirm=False)
        order = self._po(4000.0, self._at("2001-01-02"))
        setup = self._goal()
        self.assertEqual(self._view(setup)["result"]["value"], 4000.0)
        for picking in order.picking_ids:
            for move in picking.move_ids:
                move.quantity = move.product_uom_qty
                move.picked = True
            picking._action_done()
        self.assertEqual(order.state, "purchase")
        self.assertEqual(self._view(setup)["result"]["value"], 4000.0)

    def test_g_period_boundaries_follow_company_day_bounds(self):
        """Given confirmations just outside and exactly on the week's edges, only the inside ones count."""
        self._po(1.0, datetime(2000, 12, 31, 23, 59, 59))
        self._po(10.0, datetime(2001, 1, 1, 0, 0, 0))
        self._po(100.0, datetime(2001, 1, 7, 23, 59, 59))
        self._po(1000.0, datetime(2001, 1, 8, 0, 0, 0))
        setup = self._goal()
        self.assertEqual(self._view(setup)["result"]["value"], 110.0)

    def test_h_branch_scope_access_and_reconciliation(self):
        """Given a Branch 1 goal, Branch 2 orders are excluded, Branch 2's manager cannot see or evaluate it,
        and branch totals reconcile to the company total."""
        self._po(700.0, self._at("2001-01-02"), warehouse=self.branch1)
        self._po(300.0, self._at("2001-01-02"), warehouse=self.branch2)
        branch1_goal = self._goal()
        branch2_goal = self._goal(warehouse="branch2", name="Branch 2 budget")
        company_total_goal = self._goal(warehouse=False, name="Company budget")
        self.assertEqual(self._view(branch1_goal)["result"]["value"], 700.0)
        self.assertEqual(self._view(branch2_goal)["result"]["value"], 300.0)
        self.assertEqual(self._view(company_total_goal)["result"]["value"], 1000.0)
        manager_goals = self.env["strategic.goal.setup"].with_user(self.manager2).get_goals("purchasing")
        self.assertNotIn(branch1_goal.objective_id.id, [goal["objective_id"] for goal in manager_goals])
        self.assertIn(branch2_goal.objective_id.id, [goal["objective_id"] for goal in manager_goals])
        with self.assertRaises(AccessError):
            branch1_goal.target_id.with_user(self.manager2).action_evaluate_source()

    def test_i_no_orders_is_zero_and_missing_rate_is_unavailable(self):
        """Given no orders, committed is 0 and valid; given a currency with no exchange rate, Data unavailable."""
        setup = self._goal()
        budget = self._final(setup)["result"]
        self.assertEqual(budget["value"], 0.0)
        self.assertEqual(budget["status"], "Achieved: within budget")
        # Bad data arriving after setup: the goal shows Data unavailable and the evaluation is not a result.
        broken = self._goal(warehouse="branch2", name="Branch 2 budget")
        no_rate = self.env["res.currency"].create({"name": "BGZ", "symbol": "Z", "active": True})
        self._po(50.0, self._at("2001-01-03"), warehouse=self.branch2, currency=no_rate)
        # Bad data already present: readiness checks the goal's own period and stops the setup.
        late = self.Setup.browse(self.Setup.create_goal({
            "name": "Late budget", "business_area": "purchasing", "deadline": CLOSED_END, "owner_id": self.head.id,
            "scope": "branch", "warehouse_id": self.branch2.id, "parent_objective_id": self.company_goal,
        })["setup_id"])
        payload = late.configure_measurement({"template_key": "budget", "target_value": 10000.0,
                                              "period_start": CLOSED_START, "period_end": CLOSED_END})
        data_check = next(check for check in payload["readiness"] if check["key"] == "data")
        self.assertFalse(data_check["ok"])
        self.assertIn("no exchange rate", data_check["message"])
        self.assertEqual(late.setup_state, "needs_setup")
        view = self._view(broken)
        self.assertEqual(view["result"]["status"], "Data unavailable")
        broken.evaluate_now()
        evaluation = broken.key_result_id.evaluation_id
        self.assertEqual(evaluation.status, "data_quality_issue")
        self.assertNotEqual(self._view(broken)["status"], "Achieved: within budget")

    def test_j_repeated_evaluation_creates_no_duplicates(self):
        """When refreshed and evaluated again, no duplicate goal, KPI, target, Key Result or evaluation appears."""
        self._po(2000.0, self._at("2001-01-04"))
        setup = self._goal()
        counts = lambda: tuple(self.env[model].search_count([]) for model in (
            "strategic.objective", "strategic.kpi.definition", "strategic.kpi.target", "strategic.key.result", "strategic.kpi.evaluation"))
        setup.evaluate_now()
        before = counts()
        setup.evaluate_now()
        self.Setup.get_goals()
        self.assertEqual(counts(), before)
        other = self._goal(warehouse="branch2", name="Second budget goal")
        self.assertEqual(other.kpi_id, setup.kpi_id, "the same approved budget KPI is reused")

    def test_k_foreign_currency_converted_and_untaxed_amount_used(self):
        """Given a foreign-currency order and a taxed order, the converted untaxed amounts are counted."""
        foreign = self.env["res.currency"].with_context(active_test=False).search(
            [("name", "in", ("EUR", "GBP", "INR")), ("id", "!=", self.currency.id)], limit=1)
        foreign.active = True
        self.env["res.currency.rate"].create({"currency_id": foreign.id, "name": "2001-01-01", "rate": 2.0, "company_id": self.company.id})
        tax = self.env["account.tax"].create({
            "name": "BG 13% purchase", "amount": 13.0, "amount_type": "percent", "type_tax_use": "purchase", "company_id": self.company.id,
        })
        foreign_order = self._po(200.0, self._at("2001-01-03"), currency=foreign)
        taxed_order = self._po(1000.0, self._at("2001-01-03"), taxes=tax)
        self.assertGreater(taxed_order.amount_total, taxed_order.amount_untaxed)
        setup = self._goal()
        expected = foreign._convert(200.0, self.currency, self.company, fields.Date.to_date("2001-01-03")) + 1000.0
        self.assertAlmostEqual(self._view(setup)["result"]["value"], self.currency.round(expected))
        self.assertAlmostEqual(foreign._convert(200.0, self.currency, self.company, fields.Date.to_date("2001-01-03")), 100.0)
        self.assertIn(foreign_order.name, [row["name"] for row in self._view(setup)["result"]["rows"]])

    def test_l_approval_required_and_evaluation_linked_to_key_result(self):
        """Given an unapproved target, the goal cannot be evaluated and a Branch Manager cannot approve it;
        the final evaluation is linked to the goal's Key Result."""
        setup = self.Setup.browse(self.Setup.create_goal({
            "name": "Approval budget", "business_area": "purchasing", "deadline": CLOSED_END, "owner_id": self.head.id,
            "scope": "branch", "warehouse_id": self.branch1.id, "parent_objective_id": self.company_goal,
        })["setup_id"])
        setup.configure_measurement({"template_key": "budget", "target_value": 10000.0,
                                     "period_start": CLOSED_START, "period_end": CLOSED_END})
        setup.prepare_target()
        with self.assertRaises(UserError):
            setup.evaluate_now()
        with self.assertRaises(AccessError):
            setup.with_user(self.manager2).approve_target()
        setup.approve_target()
        setup.evaluate_now()
        evaluation = setup.key_result_id.evaluation_id
        self.assertTrue(evaluation)
        self.assertEqual(evaluation.target_id, setup.target_id)
        self.assertEqual(setup.key_result_id.scoring_status, "valid")

    # ------------------------------------------------------------------ the "budget control" goal set up with a quantity option
    def test_m_budget_goal_switched_from_quantity_and_deadline_extended(self):
        """Given "budget control" created with a deadline on the first day and the quantity option chosen,
        when the owner switches to the budget option and Head Office extends the deadline to the period end,
        then the goal measures money against Up to 10,000 and reports within budget from real orders."""
        setup = self.Setup.browse(self.Setup.create_goal({
            "name": "budget control", "business_area": "purchasing", "deadline": CLOSED_START, "owner_id": self.head.id,
            "scope": "organization", "plan_id": self.plan.id,
        })["setup_id"])
        objective = setup.objective_id
        # As found: "Limit how much of a product we buy", 200 of a product, period past the deadline.
        payload = setup.configure_measurement({"template_key": "quantity_limit", "product_id": self.item.id, "target_value": 200.0,
                                               "period_start": CLOSED_START, "period_end": CLOSED_END})
        check = next(c for c in payload["readiness"] if c["key"] == "setup")
        self.assertFalse(check["ok"])
        self.assertIn("goal deadline (01 Jan 2001): choose an earlier 'Measure until' date, or extend the goal deadline", check["message"])
        self.assertIn("At most 200", check["message"], "the quantity option is reported as a quantity target")

        # Switching to the budget option drops the product and measures money.
        setup.configure_measurement({"template_key": "budget", "product_id": self.item.id, "target_value": 10000.0,
                                     "period_start": CLOSED_START, "period_end": CLOSED_END})
        self.assertEqual((setup.template_key, setup.measure_key, setup.product_id.id), ("budget", "purchase_confirmed_amount", False))
        self.assertEqual(setup.setup_state, "needs_setup", "the deadline still blocks it")

        # Only Head Office extends, never past the plan, and the change is recorded as a revision.
        with self.assertRaises(AccessError):
            setup.with_user(self.manager2).extend_deadline(CLOSED_END)
        with self.assertRaisesRegex(UserError, "after the current one"):
            setup.extend_deadline(CLOSED_START)
        with self.assertRaisesRegex(UserError, "ends on 31 Dec 2099"):
            setup.extend_deadline("2100-01-31")
        payload = setup.extend_deadline(CLOSED_END)
        self.assertEqual(str(objective.deadline), CLOSED_END)
        revision = self.env["strategic.objective.revision"].search([("objective_id", "=", objective.id), ("field_name", "=", "deadline")])
        self.assertEqual((revision.old_value, revision.new_value), (CLOSED_START, CLOSED_END))
        self.assertEqual(setup.setup_state, "ready", payload["readiness"])

        setup.prepare_target()
        setup.approve_target()
        self.assertEqual((setup.kpi_id.calculation_type, setup.kpi_id.unit, setup.kpi_id.source_product_id.id),
                         ("purchase_confirmed_amount", self.currency.symbol, False))
        self.assertEqual((setup.target_id.range_min, setup.target_id.range_max), (0.0, 10000.0))
        self.assertEqual(setup.key_result_id.primary_kpi_id, setup.kpi_id)

        self._po(6000.0, self._at("2001-01-03"))
        self._po(9000.0, self._at("2001-01-03"), confirm=False)            # RFQ: not spending
        view = self._final(setup)
        result = view["result"]
        self.assertEqual(setup.key_result_id.evaluation_id.actual_value, 6000.0)
        self.assertEqual(result["value_display"], setup._money(6000.0))
        self.assertEqual(result["target_display"], "Up to %s" % setup._money(10000.0))
        self.assertEqual(result["remaining_display"], setup._money(4000.0))
        self.assertEqual(result["status"], "Achieved: within budget")
        self.assertEqual((result["bar"]["kind"], result["bar"]["pct"]), ("usage", 60.0))
        self.assertEqual(view["current"], result["value_display"])

    def test_purchasing_page_and_goal_agree(self):
        """Given the same branch and week, the Purchasing overview's confirmed value equals the goal's committed amount."""
        self._po(1234.0, self._at("2001-01-02"))
        self._po(766.0, self._at("2001-01-06"))
        setup = self._goal()
        overview = self.env["strategic.performance.dashboard"].with_user(self.head).get_purchasing_overview_data(
            CLOSED_START, CLOSED_END, self.branch1.id)
        self.assertEqual(overview["purchase_order_value"], 2000.0)
        self.assertEqual(self._view(setup)["result"]["value"], overview["purchase_order_value"])
