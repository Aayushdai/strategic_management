"""Inventory & Waste dashboard: movements, waste valuation and live refresh."""

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestInventoryDashboard(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        cls.warehouse = cls.env["stock.warehouse"].create({"name": "Inventory Test Branch", "code": "ITB", "company_id": cls.company.id})
        cls.product = cls.env["product.product"].create({"name": "Test Milk", "is_storable": True, "standard_price": 2.0})
        cls.tag = cls.env["stock.scrap.reason.tag"].search([("name", "=", "Damaged Goods")], limit=1) or cls.env["stock.scrap.reason.tag"].create({"name": "Damaged Goods"})
        cls.head = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Inventory Head", "login": "inventory_dashboard_head", "company_id": cls.company.id,
            "company_ids": [(6, 0, [cls.company.id])],
            "group_ids": [(6, 0, [cls.env.ref("base.group_user").id, cls.env.ref("stock.group_stock_user").id,
                                  cls.env.ref("strategic_management.group_strategic_head_office_manager").id])],
        })

    def _movement(self):
        today = fields.Date.to_string(fields.Date.context_today(self.env.user))
        data = self.env["strategic.performance.dashboard"].with_user(self.head).get_inventory_waste_data(
            today, today, self.warehouse.id, self.product.id)
        return data, data["movement"]

    def _move(self, source, destination, quantity, **values):
        move = self.env["stock.move"].create({
            "product_id": self.product.id, "product_uom_qty": quantity, "quantity": quantity, "uom_id": self.product.uom_id.id,
            "location_id": source.id, "location_dest_id": destination.id, **values,
        })
        return move

    def test_movement_reconciles_receipt_adjustment_and_valued_waste(self):
        stock = self.warehouse.lot_stock_id
        receipt = self._move(self.env.ref("stock.stock_location_suppliers"), stock, 10.0)
        receipt._action_confirm()
        receipt.picked = True
        receipt._action_done()

        quant = self.env["stock.quant"].search([("product_id", "=", self.product.id), ("location_id", "=", stock.id)])
        quant.inventory_quantity = 8.5
        data, movement = self._movement()
        self.assertEqual(data["summary"]["discrepancy_count"], 1, "A pending physical count is a discrepancy.")
        self.assertAlmostEqual(movement["variance"], -1.5)

        quant.action_apply_inventory()
        loss = self.env["stock.location"].search([("usage", "=", "inventory"), ("company_id", "in", [False, self.company.id])], limit=1)
        scrap = self._move(stock, loss, 0.5, scrap_reason_tag_ids=[(6, 0, self.tag.ids)])
        scrap.action_scrap()

        data, movement = self._movement()
        self.assertAlmostEqual(movement["received_quantity"], 10.0, msg="Moves done in the current second must be included.")
        self.assertAlmostEqual(movement["adjustment_quantity"], -1.5)
        self.assertAlmostEqual(movement["waste_quantity"], 0.5)
        self.assertAlmostEqual(movement["other_net_quantity"], 0.0)
        self.assertAlmostEqual(movement["expected_closing_stock"], 8.0)
        self.assertEqual(data["summary"]["discrepancy_count"], 0)
        # Odoo 20 stores valuation on the move; the cost must not be "Not Available".
        if "value" in self.env["stock.move"]._fields:
            self.assertAlmostEqual(data["summary"]["waste_cost"], 1.0)
            self.assertEqual([(row["product"], row["cost"]) for row in data["waste_by_product"]], [("Test Milk", 1.0)])

    def test_inventory_configuration_changes_queue_live_refresh(self):
        def queued_areas():
            payload = self.env.cr.precommit.data.get("strategic_management.data_changed")
            return payload["areas"] if payload else []

        self.env.cr.precommit.data.pop("strategic_management.data_changed", None)
        self.env["stock.warehouse.orderpoint"].create({
            "product_id": self.product.id, "warehouse_id": self.warehouse.id, "location_id": self.warehouse.lot_stock_id.id,
            "product_min_qty": 5, "product_max_qty": 10,
        })
        self.assertIn("inventory", queued_areas())

        self.env.cr.precommit.data.pop("strategic_management.data_changed", None)
        self.env["mrp.bom"].create({"product_tmpl_id": self.product.product_tmpl_id.id, "product_qty": 1})
        self.assertIn("inventory", queued_areas())

    def test_manufacturing_recipe_for_pos_product_is_flagged_until_kit(self):
        """POS sales only consume ingredients through Kit BoMs; anything else is called out."""
        latte = self.env["product.product"].create({"name": "Test Flat White", "available_in_pos": True})
        bom = self.env["mrp.bom"].create({
            "product_tmpl_id": latte.product_tmpl_id.id, "product_qty": 1, "type": "normal",
            "bom_line_ids": [(0, 0, {"product_id": self.product.id, "product_qty": 0.2})],
        })
        Dashboard = self.env["strategic.performance.dashboard"]
        self.assertIn("Test Flat White", Dashboard._overview_pos_recipes_not_kits(self.company))
        bom.type = "phantom"
        self.assertNotIn("Test Flat White", Dashboard._overview_pos_recipes_not_kits(self.company))
