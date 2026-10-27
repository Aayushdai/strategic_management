"""BDD scenarios: quantity, price per unit and total money stay separate (tests/features/measurement_units.feature).

One product, coffee beans in kg, is bought at 200 per kg, sold at 300 per kg and costed at 200 per kg in stock.
Real purchase orders, POS orders, stock moves and Odoo valuation; measured weeks are in January 2001.
"""

from datetime import datetime

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import format_amount

from odoo.addons.strategic_management.models.kpi import _validate_calculation_contract
from odoo.addons.strategic_management.services.kpi_sources.common import quantity_to_product_uom

WEEK = ("2001-01-08", "2001-01-14")


def at(day, hour=10):
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=hour)


@tagged("post_install", "-at_install")
class TestMeasurementUnits(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        ref = cls.env.ref
        cls.head = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "bg_units_head", "login": "bg_units_head", "email": "bg_units_head@example.com", "tz": "UTC",
            "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
            "group_ids": [(6, 0, [ref("base.group_user").id, ref("stock.group_stock_user").id, ref("purchase.group_purchase_user").id,
                                  ref("strategic_management.group_strategic_head_office_manager").id])],
        })
        cls.branch = cls.env["stock.warehouse"].create({"name": "BG Units Branch", "code": "BGU1", "company_id": cls.company.id})
        cls.pos = cls.env["pos.config"].create({"name": "BG Units POS", "picking_type_id": cls.branch.pos_type_id.id})
        cls.kg, cls.gram, cls.litre = ref("uom.product_uom_kgm"), ref("uom.product_uom_gram"), ref("uom.product_uom_litre")
        cls.beans = cls.env["product.template"].create({
            "name": "BG Units Beans", "uom_id": cls.kg.id, "is_storable": True, "purchase_ok": True, "available_in_pos": True,
            "standard_price": 200.0, "list_price": 300.0,
        }).product_variant_id
        cls.no_cost = cls.env["product.template"].create({
            "name": "BG Units No Cost", "uom_id": cls.kg.id, "is_storable": True, "standard_price": 0.0,
        }).product_variant_id
        # Moves are back-dated to 2001; give the cost a history that starts before then.
        cls.env["product.value"].sudo().search([("product_id", "=", cls.beans.id)]).write({"date": "2000-01-01 00:00:00"})
        cls.vendor = cls.env["res.partner"].create({"name": "BG Units Roaster", "supplier_rank": 1})
        cls.plan = cls.env["strategic.plan"].with_user(cls.head).create({
            "name": "BG Units Plan", "company_id": cls.company.id, "date_start": "2000-01-01", "date_end": "2099-12-31",
        })
        cls.Setup = cls.env["strategic.goal.setup"].with_user(cls.head)
        cls.symbol = cls.company.currency_id.symbol

    # ------------------------------------------------------------------ helpers
    def _money(self, amount):
        return format_amount(self.env, amount, self.company.currency_id)

    def _buy(self, lines, day):
        """Confirmed PO; lines = [(qty, unit price, uom)]."""
        order = self.env["purchase.order"].create({
            "partner_id": self.vendor.id, "company_id": self.company.id, "picking_type_id": self.branch.in_type_id.id,
            "order_line": [(0, 0, {"name": "Beans", "product_id": self.beans.id, "product_qty": qty, "price_unit": price,
                                   "uom_id": uom.id, "date_planned": at(day)}) for qty, price, uom in lines],
        })
        order.button_confirm()
        order.write({"date_approve": at(day)})
        return order

    def _sell(self, lines, day, refund=False):
        """POS order of beans; lines = [(qty in kg, subtotal excluding tax)]."""
        session = self.env["pos.session"].search([("config_id", "=", self.pos.id), ("state", "!=", "closed")], limit=1) \
            or self.env["pos.session"].create({"config_id": self.pos.id, "user_id": self.env.uid})
        total = sum(subtotal for _qty, subtotal in lines)
        return self.env["pos.order"].create({
            "session_id": session.id, "company_id": self.company.id, "state": "paid", "date_order": at(day), "is_refund": refund,
            "amount_tax": 0.0, "amount_total": total, "amount_paid": total, "amount_return": 0.0,
            "lines": [(0, 0, {"product_id": self.beans.id, "qty": qty, "price_unit": subtotal / qty, "price_subtotal": subtotal,
                              "price_subtotal_incl": subtotal, "full_product_name": "Beans"}) for qty, subtotal in lines],
        })

    def _stock(self, product, quantity, day):
        move = self.env["stock.move"].create({
            "product_id": product.id, "product_uom_qty": quantity, "quantity": quantity, "uom_id": product.uom_id.id,
            "location_id": self.env.ref("stock.stock_location_suppliers").id, "location_dest_id": self.branch.lot_stock_id.id,
        })
        move._action_confirm()
        move.picked = True
        move._action_done()
        move.write({"date": at(day)})

    def _goal(self, area, template, target, product, ready=True):
        setup = self.Setup.browse(self.Setup.create_goal({
            "name": template, "business_area": area, "deadline": WEEK[1], "plan_id": self.plan.id, "owner_id": self.head.id,
        })["setup_id"])
        payload = setup.configure_measurement({
            "template_key": template, "product_id": product.id, "target_value": target, "period_start": WEEK[0], "period_end": WEEK[1],
        })
        if not ready:
            return setup, payload
        self.assertEqual(setup.setup_state, "ready", payload["readiness"])
        setup.prepare_target()
        setup.approve_target()
        return setup

    def _final(self, area, setup):
        setup.evaluate_now()
        goal = next(g for g in self.Setup.get_goals(area) if g["objective_id"] == setup.objective_id.id)
        return goal, setup.key_result_id.evaluation_id

    # ------------------------------------------------------------------ price per unit
    def test_purchase_price_per_unit_is_currency_per_product_unit(self):
        self._buy([(10.0, 200.0, self.kg), (1000.0, 0.2, self.gram)], "2001-01-09")   # 2,000 for 10 kg + 200 for 1,000 g
        setup = self._goal("purchasing", "unit_purchase_price", 220.0, self.beans)
        goal, evaluation = self._final("purchasing", setup)
        self.assertEqual(evaluation.status, "valid", evaluation.reason)
        self.assertAlmostEqual(evaluation.actual_value, 200.0, msg="grams converted to kg: 2,200 / 11 kg")
        self.assertEqual(setup.kpi_id.unit, "%s/kg" % self.symbol)
        self.assertEqual(goal["result"]["value_display"], "%s/kg" % self._money(200.0))
        self.assertEqual(goal["result"]["target_display"], "At most %s/kg" % self._money(220.0))
        self.assertEqual(goal["current"], goal["result"]["value_display"])
        self.assertEqual(goal["result"]["bar"]["pct"], 90.9)
        self.assertEqual(evaluation.attainment_status, "achieved")

    def test_purchase_price_and_selling_price_are_not_interchanged(self):
        self._buy([(10.0, 200.0, self.kg)], "2001-01-09")
        self._sell([(2.0, 600.0)], "2001-01-10")
        self._sell([(1.0, 300.0)], "2001-01-11")
        self._sell([(-1.0, -300.0)], "2001-01-12", refund=True)
        selling = self._goal("sales_finance", "unit_selling_price", 250.0, self.beans)
        purchase = self._goal("purchasing", "unit_purchase_price", 220.0, self.beans)
        goal, evaluation = self._final("sales_finance", selling)
        self.assertAlmostEqual(evaluation.actual_value, 300.0, msg="(600 + 300 - 300) / (2 + 1 - 1) kg")
        self.assertEqual(goal["result"]["value_display"], "%s/kg" % self._money(300.0))
        self.assertEqual(goal["result"]["target_display"], "At least %s/kg" % self._money(250.0))
        self.assertIn("not a purchase cost", evaluation.source_note or evaluation.reason)
        _goal, purchase_evaluation = self._final("purchasing", purchase)
        self.assertAlmostEqual(purchase_evaluation.actual_value, 200.0)
        self.assertNotEqual(selling.kpi_id, purchase.kpi_id)
        self.assertEqual((selling.kpi_id.calculation_type, purchase.kpi_id.calculation_type),
                         ("pos_average_unit_price", "purchase_average_unit_price"))

    def test_no_purchase_or_sale_is_no_price_not_zero(self):
        purchase = self._goal("purchasing", "unit_purchase_price", 220.0, self.beans)
        _goal, evaluation = self._final("purchasing", purchase)
        self.assertEqual(evaluation.status, "not_evaluated")
        self.assertIn("no price to measure", evaluation.reason)

    # ------------------------------------------------------------------ total money and quantity
    def test_stock_value_comes_from_valuation_and_stock_quantity_from_the_product_unit(self):
        self._stock(self.beans, 10.0, "2001-01-09")
        self._stock(self.beans, 5.0, "2001-01-20")                     # after the period
        value = self._goal("inventory_waste", "stock_value", 2500.0, self.beans)
        goal, evaluation = self._final("inventory_waste", value)
        self.assertEqual(evaluation.status, "valid", evaluation.reason)
        self.assertAlmostEqual(evaluation.actual_value, 2000.0, msg="10 kg at the 200/kg cost, not the 300/kg sales price")
        self.assertEqual(value.kpi_id.unit, self.symbol)
        self.assertEqual(goal["result"]["value_display"], self._money(2000.0))
        self.assertEqual(goal["result"]["bar"]["pct"], 80.0)
        quantity = self._goal("inventory_waste", "minimum_stock", 8.0, self.beans)
        goal, evaluation = self._final("inventory_waste", quantity)
        self.assertAlmostEqual(evaluation.actual_value, 10.0)
        self.assertEqual(quantity.kpi_id.unit, "kg")
        self.assertEqual(goal["result"]["value_display"], "10 kg")
        self.assertEqual(goal["result"]["target_display"], "At least 8 kg")
        self.assertIn(evaluation.attainment_status, ("achieved", "exceeded"))
        self.assertNotEqual(value.kpi_id, quantity.kpi_id)

    def test_stock_without_cost_is_not_a_zero_value(self):
        self._stock(self.no_cost, 4.0, "2001-01-09")
        _setup, payload = self._goal("inventory_waste", "stock_value", 100.0, self.no_cost, ready=False)
        data = next(c for c in payload["readiness"] if c["key"] == "data")
        # Readiness probes the goal's own (closed) period: the missing cost is reported, not shown as 0.
        self.assertFalse(data["ok"], data["message"])
        self.assertIn("no cost", data["message"])

    # ------------------------------------------------------------------ unit rules
    def test_units_must_match_the_kind_of_measurement(self):
        company = self.company.id
        _validate_calculation_contract(self.env, "purchase", "purchase_average_unit_price", self.beans.id, "%s/kg" % self.symbol, company)
        _validate_calculation_contract(self.env, "inventory", "inventory_closing_stock_value", self.beans.id, self.symbol, company)
        _validate_calculation_contract(self.env, "inventory", "inventory_closing_stock_quantity", self.beans.id, "kg", company)
        for source, calc, unit in [
            ("purchase", "purchase_average_unit_price", self.symbol),          # money is not a price per unit
            ("purchase", "purchase_average_unit_price", "%s/L" % self.symbol), # wrong unit basis
            ("pos", "pos_average_unit_price", "kg"),
            ("inventory", "inventory_closing_stock_value", "kg"),             # a quantity is not money
            ("inventory", "inventory_closing_stock_quantity", self.symbol),   # money is not a quantity
            ("inventory", "inventory_closing_stock_quantity", "%s/kg" % self.symbol),
        ]:
            with self.assertRaises(ValidationError, msg="%s accepted %s" % (calc, unit)):
                _validate_calculation_contract(self.env, source, calc, self.beans.id, unit, company)

    def test_units_convert_only_within_a_compatible_kind(self):
        self.assertAlmostEqual(quantity_to_product_uom(1000.0, self.gram, self.beans), 1.0)
        with self.assertRaisesRegex(ValidationError, "measure different things"):
            quantity_to_product_uom(1.0, self.litre, self.beans)
