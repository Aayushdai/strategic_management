"""BDD scenarios for the Sales & Finance goal options (tests/features/sales_finance_goals.feature).

Real POS orders, order lines, refunds and posted journal entries through the real collectors, targets, approvals and
Key Result linking. Measured weeks are in January 2001 so no real company data falls inside them.
"""

from datetime import datetime, timedelta
from unittest import SkipTest

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged

WEEK = ("2001-01-08", "2001-01-14")      # Monday to Sunday
PREVIOUS = ("2001-01-01", "2001-01-07")  # the week just before
QUIET = ("2001-02-05", "2001-02-11")     # no sales at all


def at(day, hour=10):
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=hour)


@tagged("post_install", "-at_install")
class TestSalesFinanceGoals(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        ref = cls.env.ref
        cls.head = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "bg_sales_head", "login": "bg_sales_head", "email": "bg_sales_head@example.com", "tz": "UTC",
            "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
            "group_ids": [(6, 0, [ref("base.group_user").id, ref("stock.group_stock_user").id, ref("point_of_sale.group_pos_user").id,
                                  ref("strategic_management.group_strategic_head_office_manager").id])],
        })
        Warehouse = cls.env["stock.warehouse"]
        cls.b1 = Warehouse.create({"name": "BG Sales Branch 1", "code": "BGS1", "company_id": cls.company.id})
        cls.b2 = Warehouse.create({"name": "BG Sales Branch 2", "code": "BGS2", "company_id": cls.company.id})
        cls.b3 = Warehouse.create({"name": "BG Sales Branch 3", "code": "BGS3", "company_id": cls.company.id})
        cls.pos1 = cls.env["pos.config"].create({"name": "BG Sales POS 1", "picking_type_id": cls.b1.pos_type_id.id})
        cls.pos2 = cls.env["pos.config"].create({"name": "BG Sales POS 2", "picking_type_id": cls.b2.pos_type_id.id})
        cls.manager2 = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "bg_sales_branch2", "login": "bg_sales_branch2", "email": "bg_sales_branch2@example.com",
            "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])], "strategic_branch_warehouse_id": cls.b2.id,
            "group_ids": [(6, 0, [ref("base.group_user").id, ref("strategic_management.group_strategic_branch_manager").id])],
        })
        units = ref("uom.product_uom_unit")
        cls.latte = cls.env["product.template"].create({"name": "BG Sales Latte", "uom_id": units.id, "available_in_pos": True}).product_variant_id
        cls.muffin = cls.env["product.template"].create({"name": "BG Sales Muffin", "uom_id": units.id, "available_in_pos": True}).product_variant_id
        cls.plan = cls.env["strategic.plan"].with_user(cls.head).create({
            "name": "BG Sales Plan", "company_id": cls.company.id, "date_start": "2000-01-01", "date_end": "2099-12-31",
        })
        cls.Setup = cls.env["strategic.goal.setup"].with_user(cls.head)
        cls.company_goal = cls.Setup.create_goal({
            "name": "Sell more", "business_area": "sales_finance", "deadline": "2099-12-31",
            "plan_id": cls.plan.id, "owner_id": cls.head.id,
        })["objective_id"]

    # ------------------------------------------------------------------ helpers
    def _order(self, config, day, lines, refund=False, state="paid"):
        """POS order; lines = [(product, qty, subtotal excluding tax, tax)]; refunds carry negative amounts."""
        session = self.env["pos.session"].search([("config_id", "=", config.id), ("state", "!=", "closed")], limit=1) \
            or self.env["pos.session"].create({"config_id": config.id, "user_id": self.env.uid})
        tax = sum(line[3] for line in lines)
        total = sum(line[2] + line[3] for line in lines)
        return self.env["pos.order"].create({
            "session_id": session.id, "company_id": self.company.id, "state": state, "date_order": at(day), "is_refund": refund,
            "amount_tax": tax, "amount_total": total, "amount_paid": total, "amount_return": 0.0,
            "lines": [(0, 0, {"product_id": product.id, "qty": qty, "price_unit": subtotal / qty, "price_subtotal": subtotal,
                              "price_subtotal_incl": subtotal + line_tax, "full_product_name": product.name})
                      for product, qty, subtotal, line_tax in lines],
        })

    def _week_of_sales(self):
        """Branch 1: 10.00 + 4.00 - 5.00 refund = 9.00 net over 2 customer orders. Branch 2: 20.00."""
        self._order(self.pos1, "2001-01-09", [(self.latte, 2, 10.0, 1.5)])
        self._order(self.pos1, "2001-01-14", [(self.muffin, 1, 4.0, 0.6)])                     # last day counts
        self._order(self.pos1, "2001-01-12", [(self.latte, -1, -5.0, -0.75)], refund=True)
        self._order(self.pos1, "2001-01-10", [(self.latte, 3, 15.0, 2.25)], state="draft")    # not a sale
        self._order(self.pos1, "2001-01-15", [(self.latte, 4, 20.0, 3.0)])                    # after the week
        self._order(self.pos2, "2001-01-10", [(self.muffin, 5, 20.0, 3.0)])

    def _goal(self, template, target, product=None, warehouse=None, period=WEEK, ready=True):
        values = {"name": template, "business_area": "sales_finance", "deadline": period[1], "owner_id": self.head.id}
        if warehouse:
            values.update(scope="branch", warehouse_id=warehouse.id, parent_objective_id=self.company_goal)
        else:
            values.update(scope="organization", plan_id=self.plan.id)
        setup = self.Setup.browse(self.Setup.create_goal(values)["setup_id"])
        payload = setup.configure_measurement({
            "template_key": template, "product_id": product.id if product else False, "target_value": target,
            "period_start": period[0], "period_end": period[1],
        })
        if not ready:
            return setup, payload
        self.assertEqual(setup.setup_state, "ready", payload["readiness"])
        setup.prepare_target()
        setup.approve_target()
        return setup

    def _final(self, setup):
        setup.evaluate_now()
        goal = next(g for g in self.Setup.get_goals("sales_finance") if g["objective_id"] == setup.objective_id.id)
        return goal, setup.key_result_id.evaluation_id

    def _data_check(self, payload):
        return next(c for c in payload["readiness"] if c["key"] == "data")

    # ------------------------------------------------------------------ net sales
    def test_net_sales_exclude_tax_subtract_refunds_and_match_the_dashboard(self):
        self._week_of_sales()
        branch = self._goal("branch_sales", 8.0, warehouse=self.b1)
        goal, evaluation = self._final(branch)
        self.assertEqual(evaluation.status, "valid", evaluation.reason)
        self.assertAlmostEqual(evaluation.actual_value, 9.0)
        self.assertIn(evaluation.attainment_status, ("achieved", "exceeded"))
        self.assertEqual(branch.kpi_id.direction, "higher")
        self.assertEqual(branch.target_id.target_value, 8.0)
        self.assertTrue(goal["result"]["status"].startswith("Achieved"))
        self.assertEqual(goal["current"], goal["result"]["value_display"])

        company = self._goal("net_sales", 50.0)
        _goal, evaluation = self._final(company)
        self.assertAlmostEqual(evaluation.actual_value, 29.0)
        self.assertEqual(evaluation.attainment_status, "not_achieved")
        self.assertEqual(company.kpi_id, branch.kpi_id, "one approved net-sales measure serves both goals")

        Dashboard = self.env["strategic.performance.dashboard"].with_user(self.head)
        self.assertAlmostEqual(Dashboard.get_business_overview_data(WEEK[0], WEEK[1], self.b1.id)["sales"], 9.0)
        self.assertAlmostEqual(Dashboard.get_business_overview_data(WEEK[0], WEEK[1], False)["sales"], 29.0)

    def test_no_orders_or_no_linked_pos_is_not_zero_sales(self):
        setup = self._goal("net_sales", 10.0, period=QUIET)
        _goal, evaluation = self._final(setup)
        self.assertEqual(evaluation.status, "not_evaluated")
        self.assertIn("zero sales have not been assumed", evaluation.reason)
        self.assertEqual(setup.objective_id.scoring_status, "not_evaluated")
        _setup, payload = self._goal("branch_sales", 10.0, warehouse=self.b3, ready=False)
        self.assertFalse(self._data_check(payload)["ok"])
        self.assertIn("No Point of Sale is linked to BG Sales Branch 3", self._data_check(payload)["message"])

    # ------------------------------------------------------------------ average order value and growth
    def test_average_order_value_counts_customer_orders_not_refunds(self):
        self._week_of_sales()
        setup = self._goal("average_order_value", 5.0, warehouse=self.b1)
        _goal, evaluation = self._final(setup)
        self.assertAlmostEqual(evaluation.actual_value, 4.5)
        self.assertIn("2 customer orders", evaluation.source_note or evaluation.reason)
        self.assertEqual(setup.kpi_id.unit, self.company.currency_id.symbol)

    def test_average_order_value_with_only_refunds_has_no_result(self):
        self._order(self.pos1, "2001-02-06", [(self.latte, -1, -5.0, 0.0)], refund=True)
        setup = self._goal("average_order_value", 5.0, warehouse=self.b1, period=QUIET)
        _goal, evaluation = self._final(setup)
        self.assertEqual(evaluation.status, "not_evaluated")
        self.assertIn("no customer orders", evaluation.reason)

    def test_sales_growth_compares_the_preceding_period_of_equal_length(self):
        self._week_of_sales()
        self._order(self.pos1, "2001-01-03", [(self.latte, 1, 6.0, 0.9)])       # previous week: 6.00
        self._order(self.pos1, "2000-12-31", [(self.latte, 9, 45.0, 6.75)])     # before the previous week
        setup = self._goal("sales_growth", 40.0, warehouse=self.b1)
        goal, evaluation = self._final(setup)
        self.assertEqual(evaluation.status, "valid", evaluation.reason)
        self.assertAlmostEqual(evaluation.actual_value, 50.0)
        self.assertIn("2001-01-01 to 2001-01-07", evaluation.source_note or evaluation.reason)
        self.assertEqual(goal["result"]["value_display"], "+50.0%")
        self.assertEqual(goal["result"]["target_display"], "Growth of at least 40.0%")
        self.assertEqual(setup.kpi_id.unit, "%")

    def test_sales_growth_without_previous_sales_or_before_the_period_ends_has_no_result(self):
        self._week_of_sales()
        branch2 = self._goal("sales_growth", 10.0, warehouse=self.b2)
        _goal, evaluation = self._final(branch2)
        self.assertEqual(evaluation.status, "not_evaluated")
        self.assertIn("no positive net sales", evaluation.reason)
        today = fields.Date.context_today(self.env.user)
        open_week = (fields.Date.to_string(today - timedelta(days=1)), fields.Date.to_string(today + timedelta(days=5)))
        live = self._goal("sales_growth", 10.0, warehouse=self.b1, period=open_week)
        with self.assertRaisesRegex(ValidationError, "judged on the whole period"):
            live.evaluate_now()
        goal = next(g for g in self.Setup.get_goals("sales_finance") if g["objective_id"] == live.objective_id.id)
        self.assertIn("once the period has ended", goal["result"]["note"])
        self.assertFalse(goal["result"]["value_display"])

    # ------------------------------------------------------------------ products
    def test_product_sales_and_quantity_filter_the_product_and_subtract_refunds(self):
        self._week_of_sales()
        revenue = self._goal("product_revenue", 5.0, product=self.latte, warehouse=self.b1)
        _goal, evaluation = self._final(revenue)
        self.assertAlmostEqual(evaluation.actual_value, 5.0)      # 10.00 sold - 5.00 refunded
        self.assertEqual((revenue.kpi_id.unit, revenue.kpi_id.source_product_id), (self.company.currency_id.symbol, self.latte))
        quantity = self._goal("product_quantity", 1.0, product=self.latte, warehouse=self.b1)
        goal, evaluation = self._final(quantity)
        self.assertAlmostEqual(evaluation.actual_value, 1.0)      # 2 sold - 1 refunded
        self.assertEqual(goal["result"]["value_display"], "1 %s" % self.latte.uom_id.name)
        muffin = self._goal("product_revenue", 1.0, product=self.muffin)
        _goal, evaluation = self._final(muffin)
        self.assertAlmostEqual(evaluation.actual_value, 24.0)     # 4.00 at branch 1 + 20.00 at branch 2

    def test_existing_quantity_measure_is_reused_not_duplicated(self):
        existing = self.env["strategic.kpi.definition"].with_user(self.head).create({
            "name": "Latte sold", "code": "BG_SALES_LATTE", "company_id": self.company.id, "unit": self.latte.uom_id.name,
            "direction": "higher", "source_type": "pos", "calculation_type": "pos_sold_quantity", "source_product_id": self.latte.id,
        })
        setup = self._goal("product_quantity", 1.0, product=self.latte, warehouse=self.b1)
        self.assertEqual(setup.kpi_id, existing)
        setup.evaluate_now()
        first = setup.key_result_id.evaluation_id
        setup.evaluate_now()
        self.assertEqual(setup.key_result_id.evaluation_id, first)
        self.assertEqual(len(setup.objective_id.okr_objective_ids.key_result_ids), 1)
        with self.assertRaises(UserError):
            setup.approve_target()

    # ------------------------------------------------------------------ profitability and costs
    def _accounts(self):
        Account = self.env["account.account"].sudo()
        accounts = Account.search([("account_type", "in", ["income", "expense", "expense_direct_cost", "asset_current"])])
        if "company_ids" in Account._fields:
            accounts = accounts.filtered(lambda account: self.company in account.company_ids)
        pick = {kind: accounts.filtered(lambda a, kind=kind: a.account_type == kind)[:1]
                for kind in ("income", "expense", "expense_direct_cost", "asset_current")}
        journal = self.env["account.journal"].sudo().search([("company_id", "=", self.company.id), ("type", "=", "general")], limit=1)
        if not all(pick.values()) or not journal:
            raise SkipTest("Needs income, expense, direct cost and current asset accounts and a general journal.")
        return pick, journal

    def _entry(self, day, account, debit=0.0, credit=0.0, post=True):
        pick, journal = self._accounts()
        move = self.env["account.move"].sudo().create({
            "move_type": "entry", "company_id": self.company.id, "journal_id": journal.id, "date": day,
            "line_ids": [(0, 0, {"name": "BG P&L", "account_id": account.id, "debit": debit, "credit": credit}),
                         (0, 0, {"name": "BG offset", "account_id": pick["asset_current"].id, "debit": credit, "credit": debit})],
        })
        if post:
            move.action_post()
        return move

    def test_expenses_profit_and_ratios_use_posted_pnl_entries(self):
        pick, _journal = self._accounts()
        self._entry("2001-01-09", pick["income"], credit=1000.0)
        self._entry("2001-01-10", pick["expense"], debit=300.0)
        self._entry("2001-01-11", pick["expense_direct_cost"], debit=200.0)
        self._entry("2001-01-11", pick["expense"], debit=999.0, post=False)    # draft: not counted
        self._entry("2001-01-20", pick["expense"], debit=50.0)                 # after the week
        expenses = self._goal("total_expenses", 600.0)
        goal, evaluation = self._final(expenses)
        self.assertAlmostEqual(evaluation.actual_value, 500.0)
        self.assertEqual(evaluation.attainment_status, "achieved")
        self.assertIn("not operating expenses only", evaluation.source_note or evaluation.reason)
        self.assertEqual(goal["result"]["status"], "Achieved: within the limit")
        ratio = self._goal("expense_ratio", 40.0)
        _goal, evaluation = self._final(ratio)
        self.assertAlmostEqual(evaluation.actual_value, 50.0)
        self.assertEqual(evaluation.attainment_status, "not_achieved")
        profit = self._goal("net_profit", 400.0)
        _goal, evaluation = self._final(profit)
        self.assertAlmostEqual(evaluation.actual_value, 500.0)
        margin = self._goal("profit_margin", 50.0)
        _goal, evaluation = self._final(margin)
        self.assertAlmostEqual(evaluation.actual_value, 50.0)
        self.assertEqual(evaluation.attainment_status, "achieved")

    def test_ratio_without_income_and_totals_without_expenses_have_no_result(self):
        pick, _journal = self._accounts()
        self._entry("2001-01-10", pick["expense"], debit=300.0)
        ratio = self._goal("expense_ratio", 40.0)
        _goal, evaluation = self._final(ratio)
        self.assertEqual(evaluation.status, "not_evaluated")
        expenses = self._goal("total_expenses", 600.0)
        _goal, evaluation = self._final(expenses)
        self.assertAlmostEqual(evaluation.actual_value, 300.0, msg="expenses alone are measurable")
        quiet = self._goal("total_expenses", 600.0, period=QUIET)
        _goal, evaluation = self._final(quiet)
        self.assertEqual(evaluation.status, "not_evaluated")
        self.assertIn("missing expenses are not assumed to be zero", evaluation.reason)

    # ------------------------------------------------------------------ scope, access and unsupported options
    def test_company_only_branch_only_and_unsupported_options_are_refused(self):
        branch_goal = self.Setup.browse(self.Setup.create_goal({
            "name": "Branch profit", "business_area": "sales_finance", "deadline": WEEK[1], "owner_id": self.head.id,
            "scope": "branch", "warehouse_id": self.b1.id, "parent_objective_id": self.company_goal,
        })["setup_id"])
        values = {"target_value": 10.0, "period_start": WEEK[0], "period_end": WEEK[1]}
        with self.assertRaisesRegex(UserError, "whole company only"):
            branch_goal.configure_measurement(dict(values, template_key="net_profit"))
        with self.assertRaisesRegex(UserError, "cannot be measured yet"):
            branch_goal.configure_measurement(dict(values, template_key="branch_profit"))
        with self.assertRaisesRegex(UserError, "cannot be measured yet"):
            branch_goal.configure_measurement(dict(values, template_key="product_margin", product_id=self.latte.id))
        company_goal = self.Setup.browse(self.Setup.create_goal({
            "name": "All branches", "business_area": "sales_finance", "deadline": WEEK[1], "plan_id": self.plan.id,
        })["setup_id"])
        with self.assertRaisesRegex(UserError, "measured for one branch"):
            company_goal.configure_measurement(dict(values, template_key="branch_sales"))
        with self.assertRaisesRegex(UserError, "Choose one of the Sales & Finance goal options"):
            company_goal.configure_measurement(dict(values, template_key="quantity_limit"))
        self.assertFalse(branch_goal.target_id or company_goal.target_id)

    def test_branch_manager_evaluates_only_their_own_branch(self):
        self._week_of_sales()
        branch1 = self._goal("branch_sales", 8.0, warehouse=self.b1)
        with self.assertRaises(AccessError):
            branch1.target_id.with_user(self.manager2).action_evaluate_source()
        branch2 = self._goal("branch_sales", 8.0, warehouse=self.b2)
        branch2.target_id.with_user(self.manager2).action_evaluate_source()
        self.assertAlmostEqual(branch2.key_result_id.evaluation_id.actual_value, 20.0)
        with self.assertRaises(AccessError):
            self._goal("branch_sales", 8.0, warehouse=self.b2, ready=False)[0].with_user(self.manager2).prepare_target()

    def test_goal_is_submitted_for_approval_not_activated(self):
        self._week_of_sales()
        setup = self._goal("branch_sales", 8.0, warehouse=self.b1)
        setup.submit_goal()
        self.assertEqual(setup.objective_id.state, "pending_approval")

    # ------------------------------------------------------------------ progress with and without data
    def _open_week(self):
        today = fields.Date.context_today(self.env.user)
        return (fields.Date.to_string(today - timedelta(days=1)), fields.Date.to_string(today + timedelta(days=5)))

    def _row(self, setup):
        return next(g for g in self.Setup.get_goals("sales_finance") if g["objective_id"] == setup.objective_id.id)

    def test_configured_goal_without_orders_shows_waiting_for_data_not_zero(self):
        week = self._open_week()
        setup, payload = self._goal("product_quantity", 100.0, product=self.latte, warehouse=self.b1, period=week, ready=False)
        data = self._data_check(payload)
        self.assertEqual(data["label"], "Data source checked")
        self.assertIn("no matching records found", data["message"])
        self.assertIn("No paid or completed POS orders", data["message"])
        self.assertEqual(setup.setup_state, "ready", "no records yet does not block the setup")
        setup.prepare_target()
        setup.approve_target()

        goal = self._row(setup)
        result = goal["result"]
        self.assertEqual((result["status"], result["value"], result["value_display"]), ("Waiting for data", False, False))
        self.assertIn("No paid or completed POS orders", result["note"])
        self.assertEqual(result["target_display"], "At least 100 %s" % self.latte.uom_id.name)
        self.assertEqual(result["bar"], {"kind": "waiting", "pct": False, "bar": 0, "tone": "neutral", "margin": False,
                                         "label": "Waiting for data"})
        self.assertEqual(goal["bar"]["kind"], "waiting", "the dashboard row shows the same placeholder")
        self.assertEqual((goal["category"], goal["margin"]), ("setup", False), "never ranked as 0%")
        self.assertFalse(setup.key_result_id.evaluation_id)
        self.assertEqual((setup.target_id.state, setup.target_id.target_value), ("approved", 100.0))

    def test_configured_goal_with_orders_shows_value_target_and_percentage(self):
        week = self._open_week()
        today = fields.Date.to_string(fields.Date.context_today(self.env.user))
        self._order(self.pos1, today, [(self.latte, 40, 200.0, 0.0)])
        setup = self._goal("product_quantity", 100.0, product=self.latte, warehouse=self.b1, period=week)
        goal = self._row(setup)
        result = goal["result"]
        self.assertEqual(result["value_display"], "40 %s" % self.latte.uom_id.name)
        self.assertEqual(result["value_label"], "So far")
        self.assertEqual((result["bar"]["kind"], result["bar"]["pct"], result["bar"]["bar"]), ("attainment", 40.0, 40.0))
        self.assertEqual(goal["bar"], result["bar"])
        self.assertEqual(goal["current"], result["value_display"])
        self.assertEqual(goal["category"], "measured")
