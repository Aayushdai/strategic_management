"""TDD acceptance tests for purchase-to-KPI-dashboard integration.

The first two tests intentionally describe the desired behavior and are expected
not to pass until purchase events refresh approved KPI snapshots and the dashboard
presents actual values with their units. Run against strategic_management_test.
"""

from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestPurchaseDashboardAcceptance(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        cls.GroupUser = cls.env.ref("base.group_user")
        cls.GroupHead = cls.env.ref("strategic_management.group_strategic_head_office_manager")
        cls.GroupBranch = cls.env.ref("strategic_management.group_strategic_branch_manager")
        cls.GroupEmployee = cls.env.ref("strategic_management.group_strategic_employee")
        cls.PurchaseUser = cls.env.ref("purchase.group_purchase_user")
        cls.Kg = cls.env.ref("uom.product_uom_kgm")

        cls.WarehouseA = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.company.id)], limit=1
        )
        if not cls.WarehouseA:
            cls.WarehouseA = cls.env["stock.warehouse"].create({
                "name": "TDD Coffee Branch A", "code": "TCA", "company_id": cls.company.id,
            })

        cls.WarehouseB = cls.env["stock.warehouse"].search([
            ("company_id", "=", cls.company.id), ("id", "!=", cls.WarehouseA.id),
        ], limit=1)
        if not cls.WarehouseB:
            cls.WarehouseB = cls.env["stock.warehouse"].create({
                "name": "TDD Coffee Branch B", "code": "TCB", "company_id": cls.company.id,
            })

        cls.IncomingA = cls.env["stock.picking.type"].search([
            ("code", "=", "incoming"), ("warehouse_id", "=", cls.WarehouseA.id),
        ], limit=1)

        coffee_template = cls.env["product.template"].create({
            "name": "TDD Coffee Beans",
            "uom_id": cls.Kg.id,
            "purchase_ok": True,
        })
        cls.Product = coffee_template.product_variant_id
        cls.Vendor = cls.env["res.partner"].create({
            "name": "TDD Coffee Supplier", "supplier_rank": 1,
        })

        cls.Head = cls._make_user(
            "tdd_purchase_dashboard_head",
            cls.GroupHead,
            extra_groups=[cls.PurchaseUser],
        )
        cls.BranchA = cls._make_user(
            "tdd_purchase_dashboard_branch_a", cls.GroupBranch, cls.WarehouseA
        )
        cls.BranchB = cls._make_user(
            "tdd_purchase_dashboard_branch_b", cls.GroupBranch, cls.WarehouseB
        )
        cls.Employee = cls._make_user(
            "tdd_purchase_dashboard_employee", cls.GroupEmployee, cls.WarehouseA
        )

        today = fields.Date.context_today(cls.env.user)
        cls.PeriodStart = today.replace(day=1)
        next_month = (cls.PeriodStart + timedelta(days=32)).replace(day=1)
        cls.PeriodEnd = next_month - timedelta(days=1)

    @classmethod
    def _make_user(cls, login, role_group, warehouse=False, extra_groups=()):
        group_ids = [cls.GroupUser.id, role_group.id] + [group.id for group in extra_groups]
        values = {
            "name": login,
            "login": login,
            "email": "%s@example.com" % login,
            "company_id": cls.company.id,
            "company_ids": [(6, 0, [cls.company.id])],
            "group_ids": [(6, 0, group_ids)],
        }
        if warehouse:
            values["strategic_branch_warehouse_id"] = warehouse.id
        return cls.env["res.users"].with_context(no_reset_password=True).create(values)

    def _make_target(
        self, code, scope, warehouse=False,
        calculation_type="purchase_ordered_quantity",
    ):
        quantity_label = (
            "Received" if calculation_type == "purchase_received_quantity" else "Ordered"
        )
        kpi = self.env["strategic.kpi.definition"].with_user(self.Head).create({
            "name": "%s Coffee Beans %s" % (code, quantity_label),
            "code": code,
            "company_id": self.company.id,
            "unit": self.Kg.name,
            "direction": "higher",
            "source_type": "purchase",
            "calculation_type": calculation_type,
            "source_product_id": self.Product.id,
            "description": "TDD: purchased coffee beans quantity must be visible on the role-appropriate dashboard.",
        })
        target_values = {
            "kpi_id": kpi.id,
            "date_start": self.PeriodStart,
            "date_end": self.PeriodEnd,
            "scope": scope,
            "target_value": 20.0,
        }
        if scope == "branch":
            target_values["warehouse_id"] = warehouse.id
        target = self.env["strategic.kpi.target"].with_user(self.Head).create(target_values)
        target.with_user(self.Head).action_approve()
        return target

    def _confirm_purchase_order(self, quantity=20.0):
        if not self.IncomingA:
            self.skipTest("The test company has no incoming operation type for Branch A.")
        order = self.env["purchase.order"].with_user(self.Head).create({
            "partner_id": self.Vendor.id,
            "company_id": self.company.id,
            "picking_type_id": self.IncomingA.id,
            "order_line": [(0, 0, {
                "name": self.Product.display_name,
                "product_id": self.Product.id,
                "product_qty": quantity,
                "uom_id": self.Kg.id,
                "price_unit": 12.50,
                "date_planned": fields.Datetime.now(),
            })],
        })
        order.with_user(self.Head).button_confirm()
        return order

    def _current_valid_evaluation(self, target):
        return self.env["strategic.kpi.evaluation"].sudo().search([
            ("target_id", "=", target.id),
            ("status", "=", "valid"),
            ("superseded_by_id", "=", False),
        ], order="evaluated_at desc, id desc", limit=1)

    def test_confirming_branch_a_order_does_not_refresh_branch_b_target(self):
        """An order routed to Branch A must never refresh Branch B's target."""
        branch_b_target = self._make_target(
            "TDD_COFFEE_BRANCH_B_ISOLATION", "branch", self.WarehouseB
        )

        order = self._confirm_purchase_order(20.0)

        self.assertIn(order.state, ("purchase", "done"))
        self.assertFalse(
            self._current_valid_evaluation(branch_b_target),
            "Confirming a Branch A purchase order must not create a valid evaluation for Branch B.",
        )

    def test_purchase_confirmation_succeeds_when_kpi_refresh_fails(self):
        """A KPI collector failure must not roll back purchase confirmation."""
        target = self._make_target("TDD_COFFEE_ORDER_REFRESH_FAILURE", "organization")

        with patch(
            "odoo.addons.strategic_management.models.purchase_order.collect_purchase_result",
            side_effect=RuntimeError("Simulated ordered-quantity KPI collector failure"),
        ):
            order = self._confirm_purchase_order(20.0)

        self.assertIn(order.state, ("purchase", "done"))
        self.assertFalse(
            self._current_valid_evaluation(target),
            "A failed KPI refresh must not fabricate a valid evaluation.",
        )

    def test_receipt_validation_succeeds_when_received_kpi_refresh_fails(self):
        """A KPI collector failure must not roll back a completed supplier receipt."""
        target = self._make_target(
            "TDD_COFFEE_RECEIPT_REFRESH_FAILURE",
            "organization",
            calculation_type="purchase_received_quantity",
        )
        order = self._confirm_purchase_order(20.0)
        self.assertFalse(self._current_valid_evaluation(target))

        picking = order.picking_ids.filtered(
            lambda item: item.state not in ("done", "cancel")
        )[:1]
        self.assertTrue(picking, "The confirmed coffee PO should create an incoming receipt.")

        with patch(
            "odoo.addons.strategic_management.models.kpi_receipt_refresh.collect_purchase_result",
            side_effect=RuntimeError("Simulated received-quantity KPI collector failure"),
        ):
            picking.sudo().button_validate()

        self.assertEqual(picking.state, "done")
        self.assertFalse(
            self._current_valid_evaluation(target),
            "A failed receipt KPI refresh must not fabricate a valid evaluation.",
        )

    def test_confirming_20kg_order_refreshes_organization_and_branch_dashboards(self):
        """No manual KPI button should be needed after the PO is confirmed."""
        organization_target = self._make_target(
            "TDD_COFFEE_ORG_ORDERED", "organization"
        )
        branch_target = self._make_target(
            "TDD_COFFEE_BRANCH_ORDERED", "branch", self.WarehouseA
        )

        self._confirm_purchase_order(20.0)

        organization_eval = self._current_valid_evaluation(organization_target)
        branch_eval = self._current_valid_evaluation(branch_target)
        self.assertTrue(
            organization_eval,
            "Confirming a 20 kg coffee PO should refresh the organization-level ordered-quantity KPI without a manual evaluation action.",
        )
        self.assertTrue(
            branch_eval,
            "Confirming a 20 kg coffee PO for Branch A should refresh Branch A's ordered-quantity KPI without a manual evaluation action.",
        )
        self.assertAlmostEqual(organization_eval.actual_value, 20.0, places=6)
        self.assertAlmostEqual(branch_eval.actual_value, 20.0, places=6)
        self.assertEqual(organization_eval.unit, self.Kg.name)
        self.assertEqual(branch_eval.unit, self.Kg.name)

    def test_dashboard_landing_shows_raw_quantity_and_unit_before_attainment_pivot(self):
        """A user should be able to read '20 kg', not only '100% attainment'."""
        action_ids = [
            "action_strategic_kpi_dashboard_head_office",
            "action_strategic_kpi_dashboard_branch",
        ]
        for action_id in action_ids:
            action = self.env.ref("strategic_management.%s" % action_id).sudo()
            self.assertEqual(
                action.view_mode.split(",")[0],
                "list",
                "The dashboard should open on a unit-aware actual-value list; pivot/graph analysis can remain available as alternate views.",
            )

        evaluation_list = self.env.ref(
            "strategic_management.view_strategic_kpi_evaluation_list"
        ).sudo()
        self.assertIn('name="actual_value"', evaluation_list.arch_db)
        self.assertIn(
            'name="unit"', evaluation_list.arch_db,
            "The dashboard's quantity row must display its unit so 20 cannot be mistaken for 20 percent or 20 items.",
        )

    def test_dashboard_data_remains_scoped_by_role(self):
        """Head Office sees all scopes; a manager sees own branch; employee sees no branch aggregate."""
        target = self._make_target(
            "TDD_COFFEE_BRANCH_SECURITY", "branch", self.WarehouseA
        )
        self._confirm_purchase_order(20.0)
        action = target.with_user(self.Head).action_evaluate_source()
        evaluation = self.env["strategic.kpi.evaluation"].sudo().browse(action["res_id"])
        Evaluation = self.env["strategic.kpi.evaluation"]

        self.assertEqual(Evaluation.with_user(self.Head).search_count([("id", "=", evaluation.id)]), 1)
        self.assertEqual(Evaluation.with_user(self.BranchA).search_count([("id", "=", evaluation.id)]), 1)
        self.assertEqual(Evaluation.with_user(self.BranchB).search_count([("id", "=", evaluation.id)]), 0)
        self.assertEqual(Evaluation.with_user(self.Employee).search_count([("id", "=", evaluation.id)]), 0)

    def test_validating_20kg_supplier_receipt_refreshes_received_quantity_targets(self):
        """Only a completed inbound receipt may refresh received quantity KPIs."""
        organization_target = self._make_target(
            "TDD_COFFEE_ORG_RECEIVED", "organization",
            calculation_type="purchase_received_quantity",
        )
        branch_target = self._make_target(
            "TDD_COFFEE_BRANCH_RECEIVED", "branch", self.WarehouseA,
            calculation_type="purchase_received_quantity",
        )

        order = self._confirm_purchase_order(20.0)
        self.assertFalse(
            self._current_valid_evaluation(organization_target),
            "Confirming the PO must not claim coffee beans have been received.",
        )
        self.assertFalse(
            self._current_valid_evaluation(branch_target),
            "Branch received quantity must remain unevaluated until its receipt is completed.",
        )

        picking = order.picking_ids.filtered(
            lambda item: item.state not in ("done", "cancel")
        )[:1]
        self.assertTrue(picking, "The confirmed coffee PO should create an incoming receipt.")
        picking.sudo().button_validate()
        self.assertEqual(picking.state, "done", "The test must complete the incoming receipt.")

        organization_eval = self._current_valid_evaluation(organization_target)
        branch_eval = self._current_valid_evaluation(branch_target)
        self.assertTrue(
            organization_eval,
            "Validating a 20 kg supplier receipt should automatically refresh the organization received-quantity KPI.",
        )
        self.assertTrue(
            branch_eval,
            "Validating Branch A's 20 kg supplier receipt should automatically refresh Branch A's received-quantity KPI.",
        )
        self.assertAlmostEqual(organization_eval.actual_value, 20.0, places=6)
        self.assertAlmostEqual(branch_eval.actual_value, 20.0, places=6)
        self.assertEqual(organization_eval.unit, self.Kg.name)
        self.assertEqual(branch_eval.unit, self.Kg.name)
