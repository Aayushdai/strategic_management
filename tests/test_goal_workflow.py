"""BDD scenarios for the goal-to-KPI workflow (tests/features/goal_workflow.feature).

Business goal -> option -> target and period -> KPI definition/version -> OKR objective -> Key Result -> collector ->
evaluation -> progress, through the real records and roles. Measured days are in January 2001.
"""

from datetime import datetime

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase, tagged

START, END = "2001-01-08", "2001-01-10"


def at(day, hour=10):
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=hour)


@tagged("post_install", "-at_install")
class TestGoalWorkflow(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        ref = cls.env.ref
        Warehouse = cls.env["stock.warehouse"]
        cls.branch1 = Warehouse.create({"name": "BG Flow Branch 1", "code": "BGF1", "company_id": cls.company.id})
        cls.branch2 = Warehouse.create({"name": "BG Flow Branch 2", "code": "BGF2", "company_id": cls.company.id})

        def user(login, group, warehouse=False):
            values = {"name": login, "login": login, "email": "%s@example.com" % login, "tz": "UTC",
                      "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
                      "group_ids": [(6, 0, [ref("base.group_user").id, ref("purchase.group_purchase_user").id, ref(group).id])]}
            if warehouse:
                values["strategic_branch_warehouse_id"] = warehouse.id
            return cls.env["res.users"].with_context(no_reset_password=True).create(values)

        cls.head = user("bg_flow_head", "strategic_management.group_strategic_head_office_manager")
        cls.manager1 = user("bg_flow_manager1", "strategic_management.group_strategic_branch_manager", cls.branch1)
        cls.kg = ref("uom.product_uom_kgm")
        cls.beans = cls.env["product.template"].create({
            "name": "BG Flow Coffee Beans", "uom_id": cls.kg.id, "is_storable": True, "purchase_ok": True}).product_variant_id
        cls.vendor = cls.env["res.partner"].create({"name": "BG Flow Roaster", "supplier_rank": 1})
        cls.plan = cls.env["strategic.plan"].with_user(cls.head).create({
            "name": "BG Flow Plan", "company_id": cls.company.id, "date_start": "2000-01-01", "date_end": "2099-12-31"})
        cls.Setup = cls.env["strategic.goal.setup"]
        cls.company_goal = cls.Setup.with_user(cls.head).create_goal({
            "name": "Control ingredient purchasing", "business_area": "purchasing", "deadline": "2099-12-31",
            "plan_id": cls.plan.id, "owner_id": cls.head.id})["objective_id"]

    # ------------------------------------------------------------------ helpers
    def _goal(self, user=None, name="Keep purchasing within budget", deadline=END, branch=None):
        user = user or self.head
        values = {"name": name, "business_area": "purchasing", "deadline": deadline, "owner_id": user.id}
        if branch:
            values.update(scope="branch", warehouse_id=branch.id, parent_objective_id=self.company_goal)
        else:
            values.update(scope="organization", plan_id=self.plan.id)
        return self.Setup.with_user(user).browse(self.Setup.with_user(user).create_goal(values)["setup_id"])

    def _save(self, setup, template, target, product=None, end=END):
        return setup.configure_measurement({"template_key": template, "product_id": product.id if product else False,
                                            "target_value": target, "period_start": START, "period_end": end})

    def _link(self, setup):
        setup.with_user(self.head).prepare_target()
        setup.with_user(self.head).approve_target()

    def _buy(self, quantity, price, day="2001-01-09", warehouse=None):
        order = self.env["purchase.order"].create({
            "partner_id": self.vendor.id, "company_id": self.company.id, "picking_type_id": (warehouse or self.branch1).in_type_id.id,
            "order_line": [(0, 0, {"name": "Beans", "product_id": self.beans.id, "product_qty": quantity, "price_unit": price,
                                   "uom_id": self.kg.id, "date_planned": at(day)})]})
        order.button_confirm()
        order.write({"date_approve": at(day)})

    def _reloaded(self, setup):
        """The goal as the page shows it after a reload: fresh caches, read back through get_goals."""
        self.env.invalidate_all()
        return next(g for g in self.Setup.with_user(self.head).get_goals("purchasing") if g["objective_id"] == setup.objective_id.id)

    def _counts(self):
        return {model: self.env[model].search_count([]) for model in (
            "strategic.objective", "strategic.goal.setup", "strategic.kpi.definition", "strategic.kpi.target",
            "strategic.okr.objective", "strategic.key.result", "strategic.kpi.evaluation")}

    def _step(self, goal, key):
        return next(c for c in goal["readiness"] if c["key"] == key)

    # ------------------------------------------------------------------ A budget, F progress with data
    def test_a_budget_goal_stores_money_links_records_and_shows_limit_used(self):
        self._buy(8.0, 500.0)                                   # 4,000 committed
        setup = self._goal()
        self._save(setup, "budget", 10000.0)
        goal = self._reloaded(setup)
        self.assertEqual((goal["template_key"], goal["measure_key"], goal["product"], goal["target_value"]),
                         ("budget", "purchase_confirmed_amount", False, 10000.0))
        self.assertEqual(self._step(goal, "linked")["state"], "pending")
        self._link(setup)
        kpi, target, kr = setup.kpi_id, setup.target_id, setup.key_result_id
        self.assertEqual((kpi.calculation_type, kpi.unit, kpi.direction), ("purchase_confirmed_amount", self.company.currency_id.symbol, "range"))
        self.assertEqual((target.range_min, target.range_max, str(target.date_start), str(target.date_end)), (0.0, 10000.0, START, END))
        self.assertEqual((kr.primary_kpi_id, kr.target_id, kr.okr_objective_id.strategic_objective_id), (kpi, target, setup.objective_id))
        self.assertFalse(self.env["strategic.kpi.definition"].search_count([
            ("calculation_type", "=", "purchase_product_quantity_limit"), ("source_product_id", "=", self.beans.id)]),
            "no quantity measure is created for a budget")
        setup.with_user(self.head).evaluate_now()
        goal = self._reloaded(setup)
        self.assertEqual(goal["current"], setup._money(4000.0))
        self.assertEqual((goal["bar"]["kind"], goal["bar"]["pct"]), ("usage", 40.0), "limit used, not target achieved")
        self.assertEqual(goal["result"]["status"], "Achieved: within budget")
        self.assertEqual([self._step(goal, key)["state"] for key in ("linked", "approval", "result")], ["ok", "pending", "ok"])

    # ------------------------------------------------------------------ B quantity, reload persistence
    def test_b_quantity_goal_keeps_product_unit_dates_and_uses_ordered_quantity(self):
        self._buy(12.0, 500.0)
        setup = self._goal(name="Limit coffee bean purchases")
        self._save(setup, "quantity_limit", 20.0, product=self.beans)
        goal = self._reloaded(setup)
        self.assertEqual((goal["template_key"], goal["product"]["id"], goal["product"]["uom"], goal["target_value"],
                          goal["period_start"], goal["period_end"]), ("quantity_limit", self.beans.id, "kg", 20.0, START, END))
        self._link(setup)
        self.assertEqual((setup.kpi_id.calculation_type, setup.kpi_id.unit), ("purchase_product_quantity_limit", "kg"))
        setup.with_user(self.head).evaluate_now()
        goal = self._reloaded(setup)
        self.assertEqual(goal["current"], "12 kg")
        self.assertEqual(goal["result"]["target_display"], "At most 20 kg")

    # ------------------------------------------------------------------ C deadline
    def test_c_period_after_the_deadline_is_refused_and_a_same_day_end_is_accepted(self):
        setup = self._goal(deadline="2001-01-09")
        payload = self._save(setup, "budget", 10000.0, end="2001-01-10")
        check = self._step(payload, "setup")
        self.assertFalse(check["ok"])
        self.assertIn("goal deadline (09 Jan 2001)", check["message"])
        self.assertEqual(setup.setup_state, "needs_setup")
        self.assertFalse(setup.target_id)
        with self.assertRaises(UserError):
            setup.with_user(self.head).prepare_target()
        self._save(setup, "budget", 10000.0, end="2001-01-09")
        self.assertEqual(setup.setup_state, "ready")
        self.assertEqual(str(setup.objective_id.deadline), "2001-01-09", "the deadline is never changed silently")

    # ------------------------------------------------------------------ D repeated save
    def test_d_repeated_saves_create_no_duplicates_and_linked_setups_are_locked(self):
        setup = self._goal()
        before = self._counts()
        for _ in range(3):
            self._save(setup, "budget", 10000.0)
        self.assertEqual(self._counts(), before, "saving creates no KPI, target, OKR or Key Result")
        self._link(setup)
        linked = self._counts()
        with self.assertRaisesRegex(UserError, "locked"):
            self._save(setup, "budget", 9000.0)
        with self.assertRaises(UserError):
            setup.with_user(self.head).prepare_target()
        with self.assertRaises(UserError):
            setup.with_user(self.head).approve_target()
        setup.with_user(self.head).evaluate_now()
        setup.with_user(self.head).evaluate_now()
        after = self._counts()
        self.assertEqual({k: after[k] - linked[k] for k in after}, {
            "strategic.objective": 0, "strategic.goal.setup": 0, "strategic.kpi.definition": 0, "strategic.kpi.target": 0,
            "strategic.okr.objective": 0, "strategic.key.result": 0, "strategic.kpi.evaluation": 1})
        self.assertEqual(setup.target_id.range_max, 10000.0)

    # ------------------------------------------------------------------ E missing records, F placeholder
    def test_e_no_purchases_gives_a_checked_source_and_waiting_for_data(self):
        setup = self._goal(name="Track the coffee price")
        payload = self._save(setup, "unit_purchase_price", 520.0, product=self.beans)
        data = self._step(payload, "data")
        self.assertEqual((data["label"], data["ok"]), ("Data source checked", True))
        self.assertIn("no matching records found", data["message"])
        self._link(setup)
        goal = self._reloaded(setup)
        self.assertEqual(goal["result"]["status"], "Waiting for data")
        self.assertEqual((goal["bar"]["kind"], goal["bar"]["pct"]), ("waiting", False))
        self.assertEqual((goal["category"], goal["margin"]), ("setup", False))
        self.assertEqual(self._step(goal, "result")["state"], "pending")
        setup.with_user(self.head).evaluate_now()
        evaluation = setup.key_result_id.evaluation_id
        self.assertEqual((evaluation.status, evaluation.has_actual_value), ("not_evaluated", False))
        goal = self._reloaded(setup)
        self.assertEqual(goal["result"]["status"], "Not evaluated")
        self.assertEqual((goal["bar"]["kind"], goal["bar"]["pct"]), ("waiting", False), "no 0% after a final 'no data'")

    # ------------------------------------------------------------------ G branch manager proposal and approval
    def test_g_branch_manager_proposes_own_branch_goal_and_head_office_approves(self):
        setup = self._goal(user=self.manager1, name="Branch 1 buys within budget", branch=self.branch2)
        objective = setup.objective_id
        self.assertEqual((objective.scope, objective.warehouse_id, objective.state, objective.parent_objective_id.id),
                         ("branch", self.branch1, "draft", self.company_goal), "the proposal is forced to the manager's own branch")
        self._save(setup, "budget", 5000.0)
        self.assertEqual(setup.setup_state, "ready")
        with self.assertRaises(AccessError):
            setup.prepare_target()
        with self.assertRaises(AccessError):
            self.env["strategic.kpi.definition"].with_user(self.manager1).create({
                "name": "Own formula", "code": "BG-FLOW-OWN", "company_id": self.company.id, "unit": "%", "direction": "higher"})
        with self.assertRaises(AccessError):
            self._goal(user=self.manager1, name="Company strategy")
        other = self._goal(name="Branch 2 goal", branch=self.branch2)
        with self.assertRaises(AccessError):
            other.with_user(self.manager1).configure_measurement({"template_key": "budget", "target_value": 1.0,
                                                                  "period_start": START, "period_end": END})
        company_setup = self.Setup.search([("objective_id", "=", self.company_goal)])
        with self.assertRaises(AccessError):
            company_setup.with_user(self.manager1).configure_measurement({"template_key": "budget", "target_value": 1.0,
                                                                          "period_start": START, "period_end": END})

        self._link(setup)
        setup.submit_goal()
        self.assertEqual(objective.state, "pending_approval")
        self.assertEqual(self._step(setup._goal_payload(objective), "approval")["message"], "Waiting for Head Office approval.")
        with self.assertRaises(AccessError):
            setup.approve_goal()
        setup.with_user(self.head).approve_goal()
        self.assertEqual(objective.state, "active")
