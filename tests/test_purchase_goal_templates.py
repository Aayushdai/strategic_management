"""BDD scenarios for the Purchasing goal templates (tests/features/purchase_goal_templates.feature).

Real purchase orders, receipts, vendor bills and payments; the real collectors, range targets, approvals and
Key Result linking. Measured weeks are in January 2001 so no real company data falls inside them.
"""

from datetime import datetime

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

WEEK = ("2001-01-08", "2001-01-14")  # Monday to Sunday; the comparison week is 2001-01-01 .. 2001-01-07


def at(day, hour=10):
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=hour)


@tagged("post_install", "-at_install")
class TestPurchaseGoalTemplates(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        group_user = cls.env.ref("base.group_user")
        head_group = cls.env.ref("strategic_management.group_strategic_head_office_manager")
        cls.head = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "bg_tpl_head", "login": "bg_tpl_head", "email": "bg_tpl_head@example.com", "tz": "UTC",
            "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
            "group_ids": [(6, 0, [group_user.id, head_group.id, cls.env.ref("purchase.group_purchase_user").id])],
        })
        Warehouse = cls.env["stock.warehouse"]
        cls.branch = Warehouse.create({"name": "BG Template Branch", "code": "BGT1", "company_id": cls.company.id})
        cls.other_branch = Warehouse.create({"name": "BG Template Branch 2", "code": "BGT2", "company_id": cls.company.id})
        cls.kg = cls.env.ref("uom.product_uom_kgm")
        cls.beans = cls.env["product.template"].create({
            "name": "BG Template Coffee Beans", "purchase_ok": True, "uom_id": cls.kg.id, "is_storable": True,
        }).product_variant_id
        cls.milk = cls.env["product.template"].create({
            "name": "BG Template Milk", "purchase_ok": True, "uom_id": cls.kg.id, "is_storable": True,
        }).product_variant_id
        cls.vendor = cls.env["res.partner"].create({"name": "BG Template Roaster", "supplier_rank": 1})
        cls.vendor2 = cls.env["res.partner"].create({"name": "BG Template Dairy", "supplier_rank": 1})
        cls.plan = cls.env["strategic.plan"].with_user(cls.head).create({
            "name": "BG Template Plan", "company_id": cls.company.id, "date_start": "2000-01-01", "date_end": "2099-12-31",
        })
        cls.Setup = cls.env["strategic.goal.setup"].with_user(cls.head)
        cls.company_goal = cls.Setup.create_goal({
            "name": "Run purchasing well", "business_area": "purchasing", "deadline": "2099-12-31",
            "plan_id": cls.plan.id, "owner_id": cls.head.id,
        })["objective_id"]

    # ------------------------------------------------------------------ helpers
    def _po(self, lines, confirmed_at=None, vendor=None, warehouse=None, confirm=True):
        """Real PO; lines = [(product, qty, unit_price, promised_at or None, uom or None)]."""
        warehouse = warehouse or self.branch
        order = self.env["purchase.order"].create({
            "partner_id": (vendor or self.vendor).id, "company_id": self.company.id,
            "picking_type_id": warehouse.in_type_id.id,
            "order_line": [(0, 0, {
                "name": product.display_name, "product_id": product.id, "product_qty": qty, "price_unit": price,
                "uom_id": (uom or product.uom_id).id,
                "date_planned": promised or fields.Datetime.now(), "date_promised": promised or fields.Datetime.now(),
            }) for product, qty, price, promised, uom in [line + (None,) * (5 - len(line)) for line in lines]],
        })
        if confirm:
            order.button_confirm()
            if confirmed_at:
                order.write({"date_approve": confirmed_at})
        return order

    def _receive(self, order, quantity, when):
        """Receive part or all of the open receipt through the picking flow, dated ``when``."""
        picking = order.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))[:1]
        move = picking.move_ids[:1]
        move.quantity = quantity
        move.picked = True
        picking._action_done()
        move.write({"date": when})
        return move

    def _goal(self, template, target, product=None, period=WEEK, scope="branch", warehouse=None, name=None):
        values = {"name": name or template, "business_area": "purchasing", "deadline": period[1], "owner_id": self.head.id}
        if scope == "branch":
            values.update(scope="branch", warehouse_id=(warehouse or self.branch).id, parent_objective_id=self.company_goal)
        else:
            values.update(scope="organization", plan_id=self.plan.id)
        setup = self.Setup.browse(self.Setup.create_goal(values)["setup_id"])
        payload = setup.configure_measurement({
            "template_key": template, "product_id": product.id if product else False, "target_value": target,
            "period_start": period[0], "period_end": period[1],
        })
        self.assertEqual(setup.setup_state, "ready", payload["readiness"])
        setup.prepare_target()
        setup.approve_target()
        return setup

    def _final(self, setup):
        setup.evaluate_now()
        goal = next(g for g in self.Setup.get_goals("purchasing") if g["objective_id"] == setup.objective_id.id)
        return goal["result"], setup.key_result_id.evaluation_id

    # ------------------------------------------------------------------ catalogue
    def test_catalogue_is_complete_and_unavailable_options_are_refused(self):
        templates = [t for t in self.Setup.get_form_options()["templates"] if t["area"] == "purchasing"]
        self.assertEqual(len(templates), 21)
        statuses = {t["key"]: t["status"] for t in templates}
        self.assertEqual(sum(1 for s in statuses.values() if s == "available"), 13)
        self.assertEqual(statuses["purchase_monitor"], "available")
        self.assertEqual(statuses["restock_speed"], "setup")
        self.assertEqual(statuses["stockouts"], "unsupported")
        for template in templates:
            self.assertTrue(template["how"] if template["status"] == "available" else template["needs"], template["key"])
        setup = self.Setup.browse(self.Setup.create_goal({
            "name": "No stock-outs", "business_area": "purchasing", "deadline": WEEK[1], "plan_id": self.plan.id,
        })["setup_id"])
        with self.assertRaisesRegex(UserError, "cannot be measured yet"):
            setup.configure_measurement({"template_key": "stockouts", "target_value": 1, "period_start": WEEK[0], "period_end": WEEK[1]})
        self.assertFalse(setup.target_id)

    # ------------------------------------------------------------------ A3 quantity limit
    def test_quantity_limit_uses_base_unit_and_is_a_hard_ceiling(self):
        gram = self.env.ref("uom.product_uom_gram")
        self._po([(self.beans, 30.0, 10.0)], at("2001-01-09"))
        self._po([(self.beans, 500.0, 0.01, None, gram)], at("2001-01-10"))
        setup = self._goal("quantity_limit", 30.0, product=self.beans)
        result, evaluation = self._final(setup)
        self.assertAlmostEqual(evaluation.actual_value, 30.5)
        self.assertEqual(evaluation.attainment_status, "not_achieved")
        self.assertEqual(result["status"], "Not achieved: over the limit by 0.5 kg")
        empty = self._goal("quantity_limit", 30.0, product=self.beans, warehouse=self.other_branch, name="Branch 2 beans")
        _result, evaluation = self._final(empty)
        self.assertEqual((evaluation.status, evaluation.actual_value, evaluation.attainment_status), ("valid", 0.0, "achieved"))

    # ------------------------------------------------------------------ A2 / D2 price change
    def test_price_reduction_compares_with_the_previous_week(self):
        self._po([(self.beans, 10.0, 1000.0)], at("2001-01-03"))
        self._po([(self.beans, 10.0, 950.0)], at("2001-01-09"))
        five = self._goal("price_reduction", 5.0, product=self.beans)
        _result, evaluation = self._final(five)
        self.assertAlmostEqual(evaluation.actual_value, -5.0)
        self.assertEqual(evaluation.attainment_status, "achieved", "exactly 5% down meets 'at least 5%'")
        six = self._goal("price_reduction", 6.0, product=self.beans, name="six")
        self.assertEqual(self._final(six)[1].attainment_status, "not_achieved")

    def test_no_previous_purchases_gives_no_saving(self):
        self._po([(self.milk, 10.0, 80.0)], at("2001-01-09"))
        setup = self._goal("price_reduction", 5.0, product=self.milk)
        result, evaluation = self._final(setup)
        self.assertEqual(evaluation.status, "not_evaluated")
        self.assertIn("was not bought in the comparison period", evaluation.reason or evaluation.source_note)
        self.assertEqual(result["status"], "Not evaluated")

    def test_price_rise_alert_obeys_threshold(self):
        self._po([(self.beans, 10.0, 1000.0)], at("2001-01-03"))
        self._po([(self.beans, 10.0, 1060.0)], at("2001-01-09"))
        setup = self._goal("price_rise_alert", 5.0, product=self.beans)
        result, evaluation = self._final(setup)
        self.assertAlmostEqual(evaluation.actual_value, 6.0)
        self.assertEqual(evaluation.attainment_status, "not_achieved")
        self.assertEqual(result["status"], "Not achieved: price up 6.0%")

    # ------------------------------------------------------------------ D3 supplier concentration
    def test_supplier_share_reconciles_and_empty_period_has_no_share(self):
        self._po([(self.beans, 7.0, 100.0)], at("2001-01-09"), vendor=self.vendor)
        self._po([(self.milk, 3.0, 100.0)], at("2001-01-10"), vendor=self.vendor2)
        setup = self._goal("supplier_concentration", 60.0)
        result, evaluation = self._final(setup)
        self.assertAlmostEqual(evaluation.actual_value, 70.0)
        self.assertEqual(evaluation.attainment_status, "not_achieved")
        self.assertEqual([row["name"] for row in result["rows"]], ["BG Template Roaster", "BG Template Dairy"])
        empty = self._goal("supplier_concentration", 60.0, warehouse=self.other_branch, name="Branch 2 share")
        self.assertEqual(self._final(empty)[1].status, "not_evaluated")

    # ------------------------------------------------------------------ B4 awaiting delivery
    def test_value_awaiting_delivery_is_the_position_at_period_end(self):
        order = self._po([(self.beans, 10.0, 100.0)], at("2001-01-08"))
        self._receive(order, 4.0, at("2001-01-10"))
        self._receive(order, 6.0, at("2001-01-20"))
        self._po([(self.beans, 50.0, 100.0)], confirm=False)
        setup = self._goal("awaiting_delivery", 500.0)
        result, evaluation = self._final(setup)
        self.assertAlmostEqual(evaluation.actual_value, 600.0)
        self.assertEqual(evaluation.attainment_status, "not_achieved")
        self.assertEqual([row["name"] for row in result["rows"]], [order.name])

    # ------------------------------------------------------------------ B5 order-to-receipt time
    def test_receipt_lead_time_averages_completed_orders_only(self):
        done = self._po([(self.beans, 5.0, 10.0)], at("2001-01-08"))
        self._receive(done, 5.0, at("2001-01-10"))
        self._po([(self.milk, 5.0, 10.0)], at("2001-01-09"))
        setup = self._goal("receipt_lead_time", 3.0)
        _result, evaluation = self._final(setup)
        self.assertAlmostEqual(evaluation.actual_value, 2.0)
        self.assertEqual(evaluation.attainment_status, "achieved")
        self.assertIn("1 still waiting", evaluation.reason or evaluation.source_note)

    # ------------------------------------------------------------------ B1 / B3 deliveries
    def _three_deliveries(self):
        on_time = self._po([(self.beans, 4.0, 10.0, at("2001-01-09", 12))], at("2001-01-08"))
        self._receive(on_time, 4.0, at("2001-01-09", 10))
        late = self._po([(self.beans, 4.0, 10.0, at("2001-01-10", 12))], at("2001-01-08"))
        self._receive(late, 4.0, at("2001-01-12", 12))
        split = self._po([(self.milk, 4.0, 10.0, at("2001-01-11", 12))], at("2001-01-08"))
        self._receive(split, 2.0, at("2001-01-11", 10))
        self._receive(split, 2.0, at("2001-01-13", 12))

    def test_on_time_deliveries_count_only_complete_on_time_ones(self):
        self._three_deliveries()
        setup = self._goal("on_time_deliveries", 90.0)
        result, evaluation = self._final(setup)
        self.assertAlmostEqual(evaluation.actual_value, 100.0 / 3.0, places=3)
        self.assertEqual(evaluation.attainment_status, "not_achieved")
        self.assertIn("1 of 3 deliveries", evaluation.reason or evaluation.source_note)

    def test_average_delivery_delay_of_completed_deliveries(self):
        self._three_deliveries()
        setup = self._goal("delivery_delay", 1.0)
        _result, evaluation = self._final(setup)
        self.assertAlmostEqual(evaluation.actual_value, 4.0 / 3.0, places=3)
        self.assertEqual(evaluation.attainment_status, "not_achieved")

    # ------------------------------------------------------------------ F1 overdue bills
    def _bill(self, amount, invoice_date, due_date, post=True):
        bill = self.env["account.move"].create({
            "move_type": "in_invoice", "partner_id": self.vendor.id, "invoice_date": invoice_date,
            "invoice_date_due": due_date,
            "invoice_line_ids": [(0, 0, {"name": "Supplies", "quantity": 1.0, "price_unit": amount, "tax_ids": [(6, 0, [])]})],
        })
        if post:
            bill.action_post()
        return bill

    def _pay(self, bill, amount, when):
        self.env["account.payment.register"].with_context(active_model="account.move", active_ids=bill.ids).create({
            "payment_date": when, "amount": amount,
        })._create_payments()

    def test_overdue_bills_use_payments_recorded_by_period_end(self):
        bill = self._bill(1000.0, "2001-01-01", "2001-01-05")
        self._pay(bill, 400.0, "2001-01-10")
        self._pay(bill, 600.0, "2001-01-20")
        self._bill(5000.0, "2001-01-01", "2001-01-05", post=False)
        setup = self._goal("overdue_bills", 500.0, scope="organization")
        result, evaluation = self._final(setup)
        self.assertAlmostEqual(evaluation.actual_value, 600.0)
        self.assertEqual(evaluation.attainment_status, "not_achieved")
        self.assertEqual([row["name"] for row in result["rows"]], [bill.name])

    def test_overdue_bills_are_company_wide_only(self):
        setup = self.Setup.browse(self.Setup.create_goal({
            "name": "Branch bills", "business_area": "purchasing", "deadline": WEEK[1], "owner_id": self.head.id,
            "scope": "branch", "warehouse_id": self.branch.id, "parent_objective_id": self.company_goal,
        })["setup_id"])
        with self.assertRaisesRegex(UserError, "whole company only"):
            setup.configure_measurement({"template_key": "overdue_bills", "target_value": 500,
                                         "period_start": WEEK[0], "period_end": WEEK[1]})

    # ------------------------------------------------------------------ reuse and linking
    def test_same_option_reuses_one_measure_with_separate_targets(self):
        first = self._goal("supplier_concentration", 60.0)
        second = self._goal("supplier_concentration", 80.0, warehouse=self.other_branch, name="second")
        self.assertEqual(first.kpi_id, second.kpi_id)
        self.assertNotEqual(first.target_id, second.target_id)
        self.assertEqual((first.target_id.range_min, first.target_id.range_max), (0.0, 60.0))
        self.assertEqual(first.kpi_id.direction, "range")
        _result, evaluation = self._final(first)
        self.assertEqual(first.key_result_id.evaluation_id, evaluation)
        self.assertEqual(evaluation.target_id, first.target_id)
