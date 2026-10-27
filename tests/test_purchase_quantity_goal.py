"""BDD scenarios for the product quantity goal (tests/features/purchase_quantity_goal.feature).

Real purchase orders and the real collector; closed periods use January 2001 so no real company data falls inside.
"""

from datetime import datetime, timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

CLOSED = ("2001-01-08", "2001-01-14")


def at(day, hour=10):
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=hour)


@tagged("post_install", "-at_install")
class TestPurchaseQuantityGoal(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        cls.head = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "bg_qty_head", "login": "bg_qty_head", "email": "bg_qty_head@example.com", "tz": "UTC",
            "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
            "group_ids": [(6, 0, [cls.env.ref("base.group_user").id,
                                  cls.env.ref("strategic_management.group_strategic_head_office_manager").id,
                                  cls.env.ref("purchase.group_purchase_user").id])],
        })
        Warehouse = cls.env["stock.warehouse"]
        cls.branch = Warehouse.create({"name": "BG Qty Branch 1", "code": "BGQ1", "company_id": cls.company.id})
        cls.branch2 = Warehouse.create({"name": "BG Qty Branch 2", "code": "BGQ2", "company_id": cls.company.id})
        cls.litre = cls.env.ref("uom.product_uom_litre")
        cls.milk = cls.env["product.template"].create({
            "name": "BG Qty Milk", "purchase_ok": True, "uom_id": cls.litre.id, "is_storable": True,
        }).product_variant_id
        cls.beans = cls.env["product.template"].create({
            "name": "BG Qty Coffee Beans", "purchase_ok": True, "uom_id": cls.env.ref("uom.product_uom_kgm").id,
        }).product_variant_id
        cls.vendor = cls.env["res.partner"].create({"name": "BG Qty Dairy", "supplier_rank": 1})
        cls.plan = cls.env["strategic.plan"].with_user(cls.head).create({
            "name": "BG Qty Plan", "company_id": cls.company.id, "date_start": "2000-01-01", "date_end": "2099-12-31",
        })
        cls.Setup = cls.env["strategic.goal.setup"].with_user(cls.head)
        cls.company_goal = cls.Setup.create_goal({
            "name": "Buy sensibly", "business_area": "purchasing", "deadline": "2099-12-31", "plan_id": cls.plan.id,
        })["objective_id"]

    def _po(self, product, qty, confirmed_at=None, warehouse=None, uom=None, confirm=True):
        order = self.env["purchase.order"].create({
            "partner_id": self.vendor.id, "company_id": self.company.id,
            "picking_type_id": (warehouse or self.branch).in_type_id.id,
            "order_line": [(0, 0, {"name": product.display_name, "product_id": product.id, "product_qty": qty,
                                   "uom_id": (uom or product.uom_id).id, "price_unit": 1.0, "date_planned": fields.Datetime.now()})],
        })
        if confirm:
            order.button_confirm()
            if confirmed_at:
                order.write({"date_approve": confirmed_at})
        return order

    def _setup(self, period=CLOSED, deadline=None, limit=200.0, product=None):
        setup = self.Setup.browse(self.Setup.create_goal({
            "name": "Milk limit", "business_area": "purchasing", "deadline": deadline or period[1], "owner_id": self.head.id,
            "scope": "branch", "warehouse_id": self.branch.id, "parent_objective_id": self.company_goal,
        })["setup_id"])
        payload = setup.configure_measurement({
            "template_key": "quantity_limit", "product_id": (product or self.milk).id, "target_value": limit,
            "period_start": period[0], "period_end": period[1],
        })
        return setup, payload

    def _goal(self, **kwargs):
        setup, payload = self._setup(**kwargs)
        self.assertEqual(setup.setup_state, "ready", payload["readiness"])
        setup.prepare_target()
        setup.approve_target()
        return setup

    def _final(self, setup):
        setup.evaluate_now()
        return setup.key_result_id.evaluation_id

    def _view(self, setup):
        return next(g for g in self.Setup.get_goals("purchasing") if g["objective_id"] == setup.objective_id.id)

    def test_only_the_selected_product_counts(self):
        """Given a Milk goal and confirmed 20 L Milk and 50 kg Coffee Beans, then 20 L is measured."""
        self._po(self.milk, 20.0, at("2001-01-09"))
        self._po(self.beans, 50.0, at("2001-01-09"))
        evaluation = self._final(self._goal())
        self.assertEqual((evaluation.status, evaluation.actual_value, evaluation.unit), ("valid", 20.0, "L"))

    def test_only_purchases_confirmed_inside_the_period_count(self):
        """Given Milk confirmed just before, inside and just after the period, then only the inside one counts."""
        self._po(self.milk, 1.0, datetime(2001, 1, 7, 23, 59, 59))
        self._po(self.milk, 20.0, datetime(2001, 1, 8, 0, 0, 0))
        self._po(self.milk, 300.0, datetime(2001, 1, 15, 0, 0, 0))
        self.assertEqual(self._final(self._goal()).actual_value, 20.0)

    def test_unconfirmed_orders_and_other_branches_do_not_count(self):
        """Given an unconfirmed Milk RFQ and Milk for another branch, then neither counts for Branch 1."""
        self._po(self.milk, 20.0, at("2001-01-09"))
        self._po(self.milk, 99.0, confirm=False)
        self._po(self.milk, 77.0, at("2001-01-09"), warehouse=self.branch2)
        self.assertEqual(self._final(self._goal()).actual_value, 20.0)

    def test_purchase_units_convert_to_the_base_unit(self):
        """Given Milk bought as 5,000 mL, then it counts as 5 L."""
        millilitre = self.env["uom.uom"].create({"name": "BG mL", "relative_uom_id": self.litre.id, "relative_factor": 0.001})
        self._po(self.milk, 5000.0, at("2001-01-09"), uom=millilitre)
        self.assertAlmostEqual(self._final(self._goal()).actual_value, 5.0)

    def test_period_must_end_by_the_goal_deadline(self):
        """Given a goal due on the 24th with a period ending on the 31st, then readiness explains it and no target is prepared."""
        setup, payload = self._setup(period=("2001-01-08", "2001-01-31"), deadline="2001-01-24")
        self.assertEqual(setup.setup_state, "needs_setup")
        self.assertIn("must end on or before the goal deadline", payload["readiness"][0]["message"])
        with self.assertRaises(UserError):
            setup.prepare_target()
        self.assertFalse(setup.target_id)
        goal = self._view(setup)
        self.assertEqual(goal["category"], "setup")
        self.assertIn("must end on or before the goal deadline", goal["reason"])

    def test_open_period_shows_progress_so_far_not_a_final_result(self):
        """Given a 200 L limit for the current week and 20 L confirmed now, then 20 L so far, 10% used, not final."""
        today = fields.Date.context_today(self.env["strategic.goal.setup"].with_context(tz="UTC"))
        start = today - timedelta(days=today.weekday())
        period = (str(start), str(start + timedelta(days=6)))
        self._po(self.milk, 20.0)
        setup, payload = self._setup(period=period)
        data_check = next(check for check in payload["readiness"] if check["key"] == "data")
        self.assertIn("So far in this goal's period", data_check["message"])
        self.assertIn("20 L", data_check["message"])
        setup.prepare_target()
        setup.approve_target()
        result = self._view(setup)["result"]
        self.assertEqual((result["phase"], result["final"]), ("open", False))
        self.assertEqual(result["value_display"], "20 L")
        self.assertEqual(result["target_display"], "At most 200 L")
        self.assertEqual(result["used_pct"], 10.0)
        self.assertEqual(result["status"], "Within the limit so far")
        goal = self._view(setup)
        self.assertEqual(goal["category"], "measured")
        self.assertEqual((goal["bar"]["kind"], goal["bar"]["pct"], goal["bar"]["tone"]), ("usage", 10.0, "good"), "20 of 200 L = 10% used")
        self.assertEqual(goal["bar"], result["bar"], "dashboard and detail use the same bar")

    def test_re_evaluation_creates_no_duplicates(self):
        """When a closed goal is evaluated twice, then there is one evaluation, linked to its Key Result."""
        self._po(self.milk, 20.0, at("2001-01-09"))
        setup = self._goal()
        first = self._final(setup)
        second = self._final(setup)
        self.assertEqual(first, second)
        self.assertEqual(self.env["strategic.kpi.evaluation"].search_count([("target_id", "=", setup.target_id.id)]), 1)
        self.assertEqual(setup.key_result_id.evaluation_id, first)

    def test_zero_is_a_result_but_a_broken_source_is_not(self):
        """Given no Milk orders the result is a valid 0 L; given no company timezone, Data unavailable, not 0."""
        evaluation = self._final(self._goal())
        self.assertEqual((evaluation.status, evaluation.actual_value), ("valid", 0.0))
        broken = self._goal()
        self.company.partner_id.sudo().write({"tz": False})
        self.assertEqual(self._view(broken)["result"]["status"], "Data unavailable")
        evaluation = self._final(broken)
        self.assertEqual(evaluation.status, "data_quality_issue")
        self.assertFalse(evaluation.has_actual_value)

    def test_final_result_over_the_limit_is_a_missed_target(self):
        """Given 20 L bought against a 10 L limit in a closed period, then it is listed as a missed target at 50%."""
        self._po(self.milk, 20.0, at("2001-01-09"))
        setup = self._goal(limit=10.0)
        self._final(setup)
        goal = self._view(setup)
        self.assertEqual(goal["category"], "missed")
        self.assertEqual((goal["bar"]["pct"], goal["bar"]["bar"], goal["bar"]["tone"]), (200.0, 100.0, "exceeded"))
        self.assertTrue(goal["result"]["final"])

    def test_missing_data_is_never_ranked_or_counted_as_missed(self):
        """Given a goal whose data source fails, then it is listed under setup with the reason, not ranked at 0%."""
        broken = self._goal()
        self.company.partner_id.sudo().write({"tz": False})
        goal = self._view(broken)
        self.assertEqual(goal["category"], "setup")
        self.assertIn("timezone", goal["reason"])
