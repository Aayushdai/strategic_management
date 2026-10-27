"""BDD scenarios for progress bars, colours and ranking (tests/features/goal_progress_semantics.feature)."""

from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.strategic_management.models.goal_templates import attainment_tone, measure_bar, usage_tone


@tagged("post_install", "-at_install")
class TestGoalProgressRules(TransactionCase):
    """Pure calculation rules: no records needed."""

    def test_colour_band_boundaries_are_exact(self):
        self.assertEqual([usage_tone(p) for p in (69.9, 70.0, 89.9, 90.0, 100.0, 100.1)],
                         ["good", "warn", "warn", "bad", "bad", "exceeded"])
        self.assertEqual([attainment_tone(p) for p in (89.9, 90.0, 99.9, 100.0, 103.0)],
                         ["bad", "warn", "warn", "good", "good"])

    def test_limit_usage_and_margin(self):
        """A-D: 50, 95, 100 and 110 against a 100 limit."""
        self.assertEqual(measure_bar("ceiling", 50.0, 0.0, 100.0),
                         {"kind": "usage", "pct": 50.0, "bar": 50.0, "tone": "good", "margin": 50.0})
        self.assertEqual(measure_bar("ceiling", 95.0, 0.0, 100.0)["tone"], "bad")
        self.assertEqual(measure_bar("ceiling", 100.0, 0.0, 100.0)["margin"], 0.0)
        over = measure_bar("ceiling", 110.0, 0.0, 100.0)
        self.assertEqual((over["pct"], over["bar"], over["tone"], over["margin"]), (110.0, 100.0, "exceeded", -10.0))

    def test_higher_is_better_uses_attainment(self):
        """E: at least 95% on time."""
        low = measure_bar("floor", 80.0, 95.0, 100.0)
        self.assertEqual((low["kind"], low["pct"], low["tone"]), ("attainment", 84.2, "bad"))
        self.assertLess(low["margin"], 0)
        exact = measure_bar("floor", 95.0, 95.0, 100.0)
        self.assertEqual((exact["pct"], exact["tone"], exact["margin"]), (100.0, "good", 0.0))

    def test_ranking_margin_orders_by_business_performance(self):
        """G: 20% usage outranks 95%, which outranks 110%; comparable to a minimum goal on the same scale."""
        margins = {usage: measure_bar("ceiling", usage, 0.0, 100.0)["margin"] for usage in (20.0, 95.0, 110.0)}
        self.assertEqual(sorted(margins, key=lambda usage: -margins[usage]), [20.0, 95.0, 110.0])
        self.assertLess(measure_bar("floor", 80.0, 95.0, 100.0)["margin"], margins[95.0], "below a minimum is failing")

    def test_price_rules(self):
        cut = measure_bar("reduce_by", -3.0, -100.0, -5.0)
        self.assertEqual((cut["kind"], cut["pct"], cut["tone"]), ("attainment", 60.0, "bad"))
        rise_instead = measure_bar("reduce_by", 2.0, -100.0, -5.0)
        self.assertEqual((rise_instead["pct"], rise_instead["bar"]), (-40.0, 0.0))
        allowed = measure_bar("rise_at_most", 3.0, -100.0, 5.0)
        self.assertEqual((allowed["kind"], allowed["pct"], allowed["tone"]), ("usage", 60.0, "good"))
        self.assertEqual(measure_bar("rise_at_most", 6.0, -100.0, 5.0)["tone"], "exceeded")
        self.assertEqual(measure_bar("rise_at_most", -2.0, -100.0, 5.0)["pct"], 0.0)


@tagged("post_install", "-at_install")
class TestGoalProgressOnGoals(TransactionCase):
    """End to end on real goals: open-week quantity limits with real confirmed purchase orders."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        cls.head = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "bg_bar_head", "login": "bg_bar_head", "email": "bg_bar_head@example.com", "tz": "UTC",
            "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
            "group_ids": [(6, 0, [cls.env.ref("base.group_user").id,
                                  cls.env.ref("strategic_management.group_strategic_head_office_manager").id])],
        })
        cls.branch = cls.env["stock.warehouse"].create({"name": "BG Bar Branch", "code": "BGBR", "company_id": cls.company.id})
        cls.vendor = cls.env["res.partner"].create({"name": "BG Bar Dairy", "supplier_rank": 1})
        cls.plan = cls.env["strategic.plan"].with_user(cls.head).create({
            "name": "BG Bar Plan", "company_id": cls.company.id, "date_start": "2000-01-01", "date_end": "2099-12-31",
        })
        cls.Setup = cls.env["strategic.goal.setup"].with_user(cls.head)
        cls.company_goal = cls.Setup.create_goal({
            "name": "Purchase sensibly", "business_area": "purchasing", "deadline": "2099-12-31", "plan_id": cls.plan.id,
        })["objective_id"]
        today = fields.Date.context_today(cls.env["strategic.goal.setup"].with_context(tz="UTC"))
        start = today - timedelta(days=today.weekday())
        cls.week = (str(start), str(start + timedelta(days=6)))

    def _goal_with(self, litres, name):
        """A 100 L limit goal for its own product, with ``litres`` confirmed this week."""
        product = self.env["product.template"].create({
            "name": name, "purchase_ok": True, "uom_id": self.env.ref("uom.product_uom_litre").id,
        }).product_variant_id
        order = self.env["purchase.order"].create({
            "partner_id": self.vendor.id, "company_id": self.company.id, "picking_type_id": self.branch.in_type_id.id,
            "order_line": [(0, 0, {"name": name, "product_id": product.id, "product_qty": litres, "price_unit": 1.0,
                                   "date_planned": fields.Datetime.now()})],
        })
        order.button_confirm()
        setup = self.Setup.browse(self.Setup.create_goal({
            "name": name, "business_area": "purchasing", "deadline": self.week[1], "owner_id": self.head.id,
            "scope": "branch", "warehouse_id": self.branch.id, "parent_objective_id": self.company_goal,
        })["setup_id"])
        setup.configure_measurement({"template_key": "quantity_limit", "product_id": product.id, "target_value": 100.0,
                                     "period_start": self.week[0], "period_end": self.week[1]})
        setup.prepare_target()
        setup.approve_target()
        setup.submit_goal()
        setup.approve_goal()  # the dashboard shows result statuses for approved (active) goals
        return next(g for g in self.Setup.get_goals("purchasing") if g["objective_id"] == setup.objective_id.id)

    def test_usage_status_and_consistency_across_dashboard_and_detail(self):
        """A, C, D, F, I: what the owner sees at 50, 100 and 110 L of a 100 L limit, during the open week."""
        half = self._goal_with(50.0, "BG Bar Milk 50")
        self.assertEqual((half["bar"]["pct"], half["bar"]["tone"]), (50.0, "good"))
        self.assertEqual(half["bar"]["label"], "50% of the limit used")
        self.assertEqual(half["result"]["remaining_display"], "50 L")
        self.assertEqual((half["status"], half["result"]["final"]), ("Within the limit so far", False))
        self.assertEqual(half["bar"], half["result"]["bar"], "dashboard and detail panel share one bar")
        self.assertEqual(half["result"]["used_pct"], 50.0)

        full = self._goal_with(100.0, "BG Bar Milk 100")
        self.assertEqual((full["bar"]["pct"], full["result"]["remaining_display"], full["result"]["over_display"]), (100.0, "0 L", False))
        self.assertEqual(full["status"], "Near the limit so far", "equal to the limit is still within it")

        over = self._goal_with(110.0, "BG Bar Milk 110")
        self.assertEqual((over["bar"]["pct"], over["bar"]["bar"], over["bar"]["tone"]), (110.0, 100.0, "exceeded"))
        self.assertIn("10 L over the limit", over["bar"]["label"])
        self.assertEqual((over["status"], over["category"]), ("Over the limit by 10 L", "measured"))

    def test_near_the_limit_and_ranking(self):
        """B, G: 95 L is red and ranks below 20 L; 110 L ranks last."""
        low = self._goal_with(20.0, "BG Bar Milk 20")
        near = self._goal_with(95.0, "BG Bar Milk 95")
        over = self._goal_with(110.0, "BG Bar Milk 110b")
        self.assertEqual((near["bar"]["tone"], near["status"]), ("bad", "Near the limit so far"))
        ranked = sorted([near, over, low], key=lambda goal: -goal["margin"])
        self.assertEqual([goal["title"] for goal in ranked], ["BG Bar Milk 20", "BG Bar Milk 95", "BG Bar Milk 110b"])
