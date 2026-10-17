"""Regression tests for Purchase quantity and Inventory waste source adapters."""

from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestStrategicKpiPurchaseInventorySourceAdapter(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        cls.GroupUser = cls.env.ref("base.group_user")
        cls.GroupHead = cls.env.ref("strategic_management.group_strategic_head_office_manager")
        cls.GroupBranch = cls.env.ref("strategic_management.group_strategic_branch_manager")
        cls.GroupEmployee = cls.env.ref("strategic_management.group_strategic_employee")
        unit = cls.env.ref("uom.product_uom_unit")
        template = cls.env["product.template"].create({
            "name": "Phase 5 Purchase and Waste Test Product",
            "uom_id": unit.id,
            "purchase_ok": True,
        })
        cls.product = template.product_variant_id
        cls.warehouse = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.company.id)], limit=1
        )
        if not cls.warehouse:
            cls.warehouse = cls.env["stock.warehouse"].create({
                "name": "Phase 5 Source Branch",
                "code": "P5SB",
                "company_id": cls.company.id,
            })
        cls.incoming_type = cls.env["stock.picking.type"].search([
            ("code", "=", "incoming"),
            ("warehouse_id", "=", cls.warehouse.id),
        ], limit=1)
        cls.vendor = cls.env["res.partner"].create({
            "name": "Phase 5 Source Supplier",
            "supplier_rank": 1,
            "company_id": False,
        })
        cls.head = cls._make_user(
            "phase5_source_head",
            cls.GroupHead,
            extra_groups=[cls.env.ref("purchase.group_purchase_user")],
        )
        cls.employee = cls._make_user("phase5_source_employee", cls.GroupEmployee)

        today = fields.Date.context_today(cls.env.user)
        cls.period_start = today.replace(day=1)
        next_month = (cls.period_start + timedelta(days=32)).replace(day=1)
        cls.period_end = next_month - timedelta(days=1)

    @classmethod
    def _make_user(cls, login, role_group, extra_groups=()):
        group_ids = [cls.GroupUser.id, role_group.id] + [group.id for group in extra_groups]
        return cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": login,
            "login": login,
            "email": "%s@example.com" % login,
            "company_id": cls.company.id,
            "company_ids": [(6, 0, [cls.company.id])],
            "group_ids": [(6, 0, group_ids)],
        })

    def _make_target(self, code, calculation_type, source_type, user=None, scope="organization"):
        user = user or self.head
        kpi = self.env["strategic.kpi.definition"].with_user(user).create({
            "name": "%s KPI" % code,
            "code": code,
            "company_id": self.company.id,
            "unit": self.product.uom_id.name,
            "direction": "higher",
            "source_type": source_type,
            "calculation_type": calculation_type,
            "source_product_id": self.product.id,
            "description": "Phase 5 source adapter regression fixture.",
        })
        target_values = {
            "kpi_id": kpi.id,
            "date_start": self.period_start,
            "date_end": self.period_end,
            "scope": scope,
            "target_value": 100.0,
        }
        if scope == "branch":
            target_values["warehouse_id"] = self.warehouse.id
        target = self.env["strategic.kpi.target"].with_user(user).create(target_values)
        target.with_user(user).action_approve()
        return kpi, target

    def _create_confirmed_purchase_order(self, quantity):
        if not self.incoming_type:
            self.skipTest("The test company has no incoming operation type for its warehouse.")
        order = self.env["purchase.order"].with_user(self.head).create({
            "partner_id": self.vendor.id,
            "company_id": self.company.id,
            "picking_type_id": self.incoming_type.id,
            "order_line": [(0, 0, {
                "name": self.product.display_name,
                "product_id": self.product.id,
                "product_qty": quantity,
                "uom_id": self.product.uom_id.id,
                "price_unit": 1.0,
                "date_planned": fields.Datetime.now(),
            })],
        })
        order.with_user(self.head).button_confirm()
        return order

    def test_purchase_calculation_requires_matching_source_and_product_unit(self):
        with self.assertRaises(ValidationError):
            self.env["strategic.kpi.definition"].with_user(self.head).create({
                "name": "Bad Purchase Source Contract",
                "code": "P5_BAD_PURCHASE_SOURCE_CONTRACT",
                "company_id": self.company.id,
                "unit": self.product.uom_id.name,
                "direction": "higher",
                "source_type": "inventory",
                "calculation_type": "purchase_ordered_quantity",
                "source_product_id": self.product.id,
                "description": "Must reject a mismatched source type.",
            })
        with self.assertRaises(ValidationError):
            self.env["strategic.kpi.definition"].with_user(self.head).create({
                "name": "Bad Purchase Source Unit",
                "code": "P5_BAD_PURCHASE_SOURCE_UNIT",
                "company_id": self.company.id,
                "unit": "__invalid_unit_for_phase5_test__",
                "direction": "higher",
                "source_type": "purchase",
                "calculation_type": "purchase_ordered_quantity",
                "source_product_id": self.product.id,
                "description": "Must reject a unit inconsistent with the tracked product.",
            })

    def test_confirmed_purchase_order_quantity_uses_confirmed_line_quantity(self):
        _kpi, target = self._make_target(
            "P5_PURCHASE_ORDERED_QTY", "purchase_ordered_quantity", "purchase"
        )
        order = self._create_confirmed_purchase_order(3.0)
        self.assertIn(order.state, ("purchase", "done"))

        action = target.with_user(self.head).action_evaluate_source()
        evaluation = self.env["strategic.kpi.evaluation"].browse(action["res_id"])
        self.assertEqual(evaluation.status, "valid")
        self.assertTrue(evaluation.has_actual_value)
        self.assertAlmostEqual(evaluation.actual_value, 3.0, places=6)
        self.assertIn("purchase.order.line", evaluation.source_model)

    def test_purchase_receipt_requires_completed_inbound_move(self):
        _kpi, target = self._make_target(
            "P5_PURCHASE_RECEIVED_QTY", "purchase_received_quantity", "purchase"
        )
        self._create_confirmed_purchase_order(4.0)
        action = target.with_user(self.head).action_evaluate_source()
        evaluation = self.env["strategic.kpi.evaluation"].browse(action["res_id"])
        self.assertEqual(evaluation.status, "not_evaluated")
        self.assertFalse(evaluation.has_actual_value)
        self.assertIn("No completed supplier-to-internal stock receipts", evaluation.reason)

    def test_no_waste_records_means_zero_recorded_waste_not_zero_physical_waste(self):
        _kpi, target = self._make_target(
            "P5_RECORDED_WASTE_ZERO", "inventory_recorded_waste_quantity", "inventory"
        )
        action = target.with_user(self.head).action_evaluate_source()
        evaluation = self.env["strategic.kpi.evaluation"].browse(action["res_id"])
        self.assertEqual(evaluation.status, "valid")
        self.assertTrue(evaluation.has_actual_value)
        self.assertAlmostEqual(evaluation.actual_value, 0.0, places=6)
        self.assertIn("not proof that no physical waste occurred", evaluation.source_note)


    def _create_scrap_move(self, quantity, tag_names=()):
        tag_model = self.env["stock.scrap.reason.tag"].sudo()
        tags = tag_model.search([("name", "in", list(tag_names))]) if tag_names else tag_model.browse()
        existing_names = set(tags.mapped("name"))
        for name in tag_names:
            if name not in existing_names:
                tags |= tag_model.create({"name": name})
        scrap_location = self.company.scrap_location_id
        if not scrap_location:
            scrap_location = self.env["stock.location"].search([
                ("usage", "=", "inventory"),
                "|", ("company_id", "=", False), ("company_id", "=", self.company.id),
            ], limit=1)
        self.assertTrue(self.warehouse.lot_stock_id, "The test warehouse must have a stock location.")
        self.assertTrue(scrap_location, "The test company must have a scrap/inventory location.")
        return self.env["stock.move"].sudo().create({
            "company_id": self.company.id,
            "product_id": self.product.id,
            "product_uom_qty": quantity,
            "uom_id": self.product.uom_id.id,
            "location_id": self.warehouse.lot_stock_id.id,
            "location_dest_id": scrap_location.id,
            "is_scrap": True,
            "scrap_reason_tag_ids": [(6, 0, tags.ids)],
        })

    def test_pending_scrap_with_allowed_category_counts_as_recorded_waste(self):
        _kpi, target = self._make_target(
            "P5_RECORDED_WASTE_PENDING", "inventory_recorded_waste_quantity", "inventory"
        )
        self._create_scrap_move(2.5, ("Spoilage/Expiry",))
        action = target.with_user(self.head).action_evaluate_source()
        evaluation = self.env["strategic.kpi.evaluation"].browse(action["res_id"])
        self.assertEqual(evaluation.status, "valid")
        self.assertTrue(evaluation.has_actual_value)
        self.assertAlmostEqual(evaluation.actual_value, 2.5, places=6)
        self.assertIn("recorded scrap quantity", evaluation.source_note)

    def test_untagged_scrap_fails_closed_as_data_quality_issue(self):
        _kpi, target = self._make_target(
            "P5_RECORDED_WASTE_UNTAGGED", "inventory_recorded_waste_quantity", "inventory"
        )
        self._create_scrap_move(1.0)
        action = target.with_user(self.head).action_evaluate_source()
        evaluation = self.env["strategic.kpi.evaluation"].browse(action["res_id"])
        self.assertEqual(evaluation.status, "data_quality_issue")
        self.assertFalse(evaluation.has_actual_value)
        self.assertIn("no waste category tag", evaluation.reason)

    def test_employee_cannot_trigger_purchase_or_inventory_source_evaluation(self):
        for source, calculation, code in (
            ("purchase", "purchase_ordered_quantity", "P5_PURCHASE_EMPLOYEE_BLOCK"),
            ("inventory", "inventory_validated_waste_quantity", "P5_WASTE_EMPLOYEE_BLOCK"),
        ):
            _kpi, target = self._make_target(code, calculation, source)
            with self.assertRaises(AccessError):
                target.with_user(self.employee).action_evaluate_source()
