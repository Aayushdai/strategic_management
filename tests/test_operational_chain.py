"""BDD scenarios: Purchasing, Inventory and POS as one data chain (tests/features/operational_chain.feature).

Real purchase orders and receipts, real POS orders whose stock moves Odoo creates itself (kit recipes exploded by
Odoo), real scrap; every figure comes from the KPI collectors through approved targets. Measured weeks are in 2001.
"""

from datetime import datetime

from odoo.tests.common import TransactionCase, tagged

WEEK = ("2001-01-08", "2001-01-14")
OTHER_WEEK = ("2001-02-05", "2001-02-11")


def at(day, hour=10):
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=hour)


@tagged("post_install", "-at_install")
class TestOperationalChain(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        cls.company.sudo().point_of_sale_update_stock_quantities = "real"
        ref = cls.env.ref
        cls.head = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "bg_chain_head", "login": "bg_chain_head", "email": "bg_chain_head@example.com", "tz": "UTC",
            "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
            "group_ids": [(6, 0, [ref("base.group_user").id, ref("stock.group_stock_user").id, ref("purchase.group_purchase_user").id,
                                  ref("strategic_management.group_strategic_head_office_manager").id])],
        })
        cls.branch = cls.env["stock.warehouse"].create({"name": "BG Chain Branch", "code": "BGC1", "company_id": cls.company.id})
        cls.pos = cls.env["pos.config"].create({"name": "BG Chain POS", "picking_type_id": cls.branch.pos_type_id.id})
        cls.kg, cls.gram, cls.units = ref("uom.product_uom_kgm"), ref("uom.product_uom_gram"), ref("uom.product_uom_unit")
        cls.coffee = cls.env["product.template"].create({
            "name": "BG Chain Coffee Beans", "uom_id": cls.kg.id, "is_storable": True, "purchase_ok": True, "standard_price": 200.0,
        }).product_variant_id
        cls.latte = cls.env["product.template"].create({
            "name": "BG Chain Latte", "uom_id": cls.units.id, "type": "consu", "available_in_pos": True, "list_price": 4.0,
        }).product_variant_id
        cls.muffin = cls.env["product.template"].create({
            "name": "BG Chain Muffin", "uom_id": cls.units.id, "type": "consu", "available_in_pos": True,
        }).product_variant_id
        # Approved recipe: a latte is a kit of 18 g of coffee, in force since 2000 (Odoo 20 dates recipes by their record dates).
        cls.recipe = cls.env["mrp.bom"].create({
            "product_tmpl_id": cls.latte.product_tmpl_id.id, "product_qty": 1.0, "type": "phantom",
            "bom_line_ids": [(0, 0, {"product_id": cls.coffee.id, "product_qty": 18.0, "uom_id": cls.gram.id})],
        })
        cls.env.cr.execute("UPDATE mrp_bom SET create_date = '2000-01-01', write_date = '2000-01-01' WHERE id = %s", [cls.recipe.id])
        cls.recipe.invalidate_recordset()
        cls.env["product.value"].sudo().search([("product_id", "=", cls.coffee.id)]).write({"date": "2000-01-01 00:00:00"})
        Tag = cls.env["stock.scrap.reason.tag"]
        cls.spoiled = Tag.search([("name", "=", "Spoilage/Expiry")], limit=1) or Tag.create({"name": "Spoilage/Expiry"})
        cls.vendor = cls.env["res.partner"].create({"name": "BG Chain Roaster", "supplier_rank": 1})

    # ------------------------------------------------------------------ helpers
    def _measure(self, calculation, source, unit, product=None, period=WEEK):
        """One approved KPI target for the branch, evaluated by its real collector."""
        kpi = self.env["strategic.kpi.definition"].with_user(self.head).create({
            "name": calculation, "code": "BGC-%s-%s" % (calculation, period[0]), "company_id": self.company.id, "unit": unit,
            "direction": "higher", "source_type": source, "calculation_type": calculation,
            "source_product_id": product.id if product else False,
        })
        target = self.env["strategic.kpi.target"].with_user(self.head).create({
            "kpi_id": kpi.id, "date_start": period[0], "date_end": period[1], "scope": "branch",
            "warehouse_id": self.branch.id, "target_basis": "absolute", "target_value": 1.0,
        })
        target.action_approve()
        action = target.action_evaluate_source()
        return self.env["strategic.kpi.evaluation"].browse(action["res_id"])

    def _redate(self, moves, day):
        moves.write({"date": at(day)})

    def _receive_stock(self, quantity, day):
        move = self.env["stock.move"].create({
            "product_id": self.coffee.id, "product_uom_qty": quantity, "quantity": quantity, "uom_id": self.kg.id,
            "location_id": self.env.ref("stock.stock_location_suppliers").id, "location_dest_id": self.branch.lot_stock_id.id,
        })
        move._action_confirm()
        move.picked = True
        move._action_done()
        self._redate(move, day)

    def _purchase(self, ordered, received, day):
        order = self.env["purchase.order"].create({
            "partner_id": self.vendor.id, "company_id": self.company.id, "picking_type_id": self.branch.in_type_id.id,
            "order_line": [(0, 0, {"name": "Coffee", "product_id": self.coffee.id, "product_qty": ordered, "price_unit": 200.0,
                                   "uom_id": self.kg.id, "date_planned": at(day)})],
        })
        order.button_confirm()
        order.write({"date_approve": at(day)})
        picking = order.picking_ids
        move = picking.move_ids
        move.quantity = received
        move.picked = True
        picking._action_done()
        self._redate(picking.move_ids.filtered(lambda m: m.state == "done"), day)
        return order

    def _sell(self, product, quantity, day, state="paid"):
        session = self.env["pos.session"].search([("config_id", "=", self.pos.id), ("state", "!=", "closed")], limit=1) \
            or self.env["pos.session"].create({"config_id": self.pos.id, "user_id": self.env.uid})
        total = 4.0 * quantity
        order = self.env["pos.order"].create({
            "session_id": session.id, "company_id": self.company.id, "state": state, "date_order": at(day),
            "amount_tax": 0.0, "amount_total": total, "amount_paid": total, "amount_return": 0.0,
            "lines": [(0, 0, {"product_id": product.id, "qty": quantity, "price_unit": 4.0, "price_subtotal": total,
                              "price_subtotal_incl": total, "full_product_name": product.name})],
        })
        if state == "paid":
            order._create_order_picking()          # Odoo's own POS stock deduction, real-time mode
            self._redate(order.picking_ids.move_ids, day)
        return order

    def _scrap(self, quantity, day):
        move = self.env["stock.move"].create({
            "product_id": self.coffee.id, "product_uom_qty": quantity, "quantity": quantity, "uom_id": self.kg.id,
            "location_id": self.branch.lot_stock_id.id, "scrap_reason_tag_ids": [(6, 0, self.spoiled.ids)],
            "location_dest_id": self.env["stock.location"].search([("usage", "=", "inventory"), ("company_id", "in", [False, self.company.id])], limit=1).id,
        })
        move.action_scrap()
        self._redate(move, day)

    # ------------------------------------------------------------------ purchase order -> receipt -> inventory
    def test_ordered_received_and_stock_stay_separate(self):
        """Given 10 kg in stock, when 20 kg are ordered at 200/kg and only 15 kg are received,
        then ordered 20 kg, received 15 kg, commitment 4,000 and stock rises from 10 to 25 kg."""
        self._receive_stock(10.0, "2001-01-02")
        self._purchase(20.0, 15.0, "2001-01-09")
        symbol = self.company.currency_id.symbol
        results = {
            "ordered": self._measure("purchase_ordered_quantity", "purchase", "kg", self.coffee),
            "received": self._measure("purchase_received_quantity", "purchase", "kg", self.coffee),
            "opening": self._measure("inventory_opening_stock_quantity", "inventory", "kg", self.coffee),
            "closing": self._measure("inventory_closing_stock_quantity", "inventory", "kg", self.coffee),
            "commitment": self._measure("purchase_confirmed_amount", "purchase", symbol),
            "price": self._measure("purchase_average_unit_price", "purchase", "%s/kg" % symbol, self.coffee),
            "variance": self._measure("inventory_unexplained_variance_quantity", "inventory", "kg", self.coffee),
        }
        for name, evaluation in results.items():
            self.assertEqual(evaluation.status, "valid", "%s: %s" % (name, evaluation.reason))
        values = {name: evaluation.actual_value for name, evaluation in results.items()}
        self.assertEqual(values, {"ordered": 20.0, "received": 15.0, "opening": 10.0, "closing": 25.0,
                                  "commitment": 4000.0, "price": 200.0, "variance": 0.0})

    def test_unvalidated_receipt_does_not_increase_stock(self):
        self._receive_stock(10.0, "2001-01-02")
        order = self.env["purchase.order"].create({
            "partner_id": self.vendor.id, "company_id": self.company.id, "picking_type_id": self.branch.in_type_id.id,
            "order_line": [(0, 0, {"name": "Coffee", "product_id": self.coffee.id, "product_qty": 20.0, "price_unit": 200.0,
                                   "uom_id": self.kg.id, "date_planned": at("2001-01-09")})],
        })
        order.button_confirm()
        order.write({"date_approve": at("2001-01-09")})
        self.assertEqual(self._measure("purchase_ordered_quantity", "purchase", "kg", self.coffee).actual_value, 20.0)
        received = self._measure("purchase_received_quantity", "purchase", "kg", self.coffee)
        self.assertEqual(received.status, "not_evaluated", "nothing received is not shown as a received quantity")
        self.assertEqual(self._measure("inventory_closing_stock_quantity", "inventory", "kg", self.coffee).actual_value, 10.0)

    # ------------------------------------------------------------------ POS sale -> recipe -> ingredient use
    def test_pos_sales_recipe_estimate_actual_moves_waste_and_stock_flow(self):
        """Given 10 kg of coffee, 100 lattes sold (18 g each), a cancelled order of 50 lattes and 0.5 kg spoiled,
        then expected use is 1.8 kg, Odoo deducted 1.8 kg through the POS kit, waste is 0.5 kg on its own,
        closing stock is 7.7 kg and the stock-flow check finds no unexplained difference."""
        self._receive_stock(10.0, "2001-01-02")
        self._sell(self.latte, 100, "2001-01-09")
        self._sell(self.latte, 50, "2001-01-10", state="cancel")
        self._scrap(0.5, "2001-01-11")

        pos_moves = self.env["stock.move"].search([("product_id", "=", self.coffee.id), ("state", "=", "done"),
                                                   ("location_dest_id.usage", "=", "customer")])
        self.assertAlmostEqual(sum(pos_moves.mapped("quantity_product_uom")), 1.8, msg="Odoo exploded the kit into 1.8 kg of coffee")

        expected = self._measure("pos_recipe_consumption_estimate", "pos", "kg", self.coffee)
        self.assertEqual(expected.status, "valid", expected.reason)
        self.assertAlmostEqual(expected.actual_value, 1.8, msg="100 x 18 g, the cancelled order excluded, grams to kg")
        waste = self._measure("inventory_validated_waste_quantity", "inventory", "kg", self.coffee)
        self.assertAlmostEqual(waste.actual_value, 0.5, msg="waste is not part of recipe use")
        closing = self._measure("inventory_closing_stock_quantity", "inventory", "kg", self.coffee)
        self.assertAlmostEqual(closing.actual_value, 7.7)
        variance = self._measure("inventory_unexplained_variance_quantity", "inventory", "kg", self.coffee)
        self.assertEqual(variance.status, "valid", variance.reason)
        self.assertAlmostEqual(variance.actual_value, 0.0, msg="POS deductions are explained, not left in the residual")
        # Actual recorded use is the 1.8 kg Odoo deducted once; it is reported apart from the estimate, never added to it.
        actual = self._measure("inventory_actual_consumption_quantity", "inventory", "kg", self.coffee)
        self.assertEqual(actual.status, "valid", actual.reason)
        self.assertAlmostEqual(actual.actual_value, 1.8)
        self.assertIn("never added to these moves", actual.source_note or actual.reason)

    def test_sales_without_a_recipe_give_no_estimate(self):
        self._sell(self.muffin, 10, OTHER_WEEK[0])
        expected = self._measure("pos_recipe_consumption_estimate", "pos", "kg", self.coffee, period=OTHER_WEEK)
        self.assertEqual(expected.status, "data_quality_issue")
        self.assertIn("No effective recipe", expected.reason)

    def test_no_sales_or_history_is_not_zero(self):
        sold = self._measure("pos_sold_quantity", "pos", self.latte.uom_id.name, self.latte, period=OTHER_WEEK)
        self.assertEqual(sold.status, "not_evaluated")
        self.assertFalse(sold.has_actual_value)


    # ------------------------------------------------------------------ Phase 3: units, stuck POS deliveries, returns, money vs quantity
    def test_gram_recipe_on_a_product_kept_in_unrelated_units_is_refused_not_converted(self):
        """Given coffee kept in Odoo's "Units" unit (as in the live database, where that unit was renamed "Kg")
        and a recipe of 18 g per latte, then the estimate is unavailable instead of 1,000 times too large."""
        beans = self.env["product.template"].create({
            "name": "BG Chain Beans In Units", "uom_id": self.units.id, "is_storable": True}).product_variant_id
        latte = self.env["product.template"].create({
            "name": "BG Chain Unit Latte", "uom_id": self.units.id, "type": "consu", "available_in_pos": True}).product_variant_id
        recipe = self.env["mrp.bom"].create({
            "product_tmpl_id": latte.product_tmpl_id.id, "product_qty": 1.0, "type": "normal",
            "bom_line_ids": [(0, 0, {"product_id": beans.id, "product_qty": 18.0, "uom_id": self.gram.id})]})
        self.env.cr.execute("UPDATE mrp_bom SET create_date = '2000-01-01', write_date = '2000-01-01' WHERE id = %s", [recipe.id])
        recipe.invalidate_recordset()
        self._sell(latte, 100, OTHER_WEEK[0])
        expected = self._measure("pos_recipe_consumption_estimate", "pos", self.units.name, beans, period=OTHER_WEEK)
        self.assertEqual(expected.status, "data_quality_issue")
        self.assertIn("g cannot be converted to %s" % self.units.name, expected.reason)
        self.assertFalse(expected.has_actual_value)

    def test_pos_delivery_left_incomplete_makes_actual_use_unavailable(self):
        """Given a latte kit that also needs lot-tracked milk, when POS cannot complete the delivery,
        then no stock is taken, the expected use stays 1.8 kg, and actual use and stock flow say why they are unavailable."""
        litre = self.env.ref("uom.product_uom_litre")
        milk = self.env["product.template"].create({
            "name": "BG Chain Lot Milk", "uom_id": litre.id, "is_storable": True, "tracking": "lot"}).product_variant_id
        self.recipe.write({"bom_line_ids": [(0, 0, {"product_id": milk.id, "product_qty": 0.2, "uom_id": litre.id})]})
        self.env.flush_all()          # save the change first, or the flush after the SQL below re-stamps today's date
        self.env.cr.execute("UPDATE mrp_bom SET write_date = '2000-01-01' WHERE id = %s", [self.recipe.id])
        self.recipe.invalidate_recordset()
        self._receive_stock(10.0, "2001-01-02")
        order = self._sell(self.latte, 100, "2001-01-09")
        self.assertTrue(order.picking_ids and all(p.state != "done" for p in order.picking_ids), "Odoo left the POS delivery open")

        expected = self._measure("pos_recipe_consumption_estimate", "pos", "kg", self.coffee)
        self.assertAlmostEqual(expected.actual_value, 1.8, msg="%s: %s" % (expected.status, expected.source_note))
        self.assertAlmostEqual(self._measure("inventory_closing_stock_quantity", "inventory", "kg", self.coffee).actual_value, 10.0)
        for calc in ("inventory_actual_consumption_quantity", "inventory_unexplained_variance_quantity"):
            result = self._measure(calc, "inventory", "kg", self.coffee)
            self.assertEqual(result.status, "data_quality_issue", calc)
            self.assertIn("POS deliveries", result.reason)
            self.assertIn(order.picking_ids[:1].name, result.reason)

    def test_received_quantity_is_net_of_goods_returned_to_the_supplier(self):
        self._purchase(20.0, 15.0, "2001-01-09")
        returned = self.env["stock.move"].create({
            "product_id": self.coffee.id, "product_uom_qty": 3.0, "quantity": 3.0, "uom_id": self.kg.id,
            "location_id": self.branch.lot_stock_id.id, "location_dest_id": self.env.ref("stock.stock_location_suppliers").id,
            "picking_type_id": self.branch.out_type_id.id})
        returned._action_confirm()
        returned.picked = True
        returned._action_done()
        self._redate(returned, "2001-01-11")
        received = self._measure("purchase_received_quantity", "purchase", "kg", self.coffee)
        self.assertAlmostEqual(received.actual_value, 12.0)
        self.assertIn("minus goods returned to the supplier (3)", received.source_note or received.reason)
        self.assertAlmostEqual(self._measure("purchase_ordered_quantity", "purchase", "kg", self.coffee).actual_value, 20.0)

    def test_quantity_limit_and_budget_keep_their_own_units_and_tax_basis(self):
        """Given 20 kg ordered at 500 per kg with 15% tax, the quantity goal sees 20 kg and the budget sees 10,000 before tax."""
        tax = self.env["account.tax"].create({"name": "BG Chain 15%", "amount": 15.0, "type_tax_use": "purchase",
                                              "company_id": self.company.id})
        order = self.env["purchase.order"].create({
            "partner_id": self.vendor.id, "company_id": self.company.id, "picking_type_id": self.branch.in_type_id.id,
            "order_line": [(0, 0, {"name": "Coffee", "product_id": self.coffee.id, "product_qty": 20.0, "price_unit": 500.0,
                                   "uom_id": self.kg.id, "date_planned": at("2001-01-09"), "tax_ids": [(6, 0, tax.ids)]})]})
        order.button_confirm()
        order.write({"date_approve": at("2001-01-09")})
        self.assertAlmostEqual(order.amount_total, 11500.0)
        symbol = self.company.currency_id.symbol
        quantity = self._measure("purchase_product_quantity_limit", "purchase", "kg", self.coffee)
        budget = self._measure("purchase_confirmed_amount", "purchase", symbol)
        price = self._measure("purchase_average_unit_price", "purchase", "%s/kg" % symbol, self.coffee)
        self.assertEqual((quantity.actual_value, quantity.unit), (20.0, "kg"))
        self.assertEqual((budget.actual_value, budget.unit), (10000.0, symbol))
        self.assertEqual((price.actual_value, price.unit), (500.0, "%s/kg" % symbol))

