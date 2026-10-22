"""TDD acceptance tests for purchase-to-KPI-dashboard integration.

The first two tests intentionally describe the desired behavior and are expected
not to pass until purchase events refresh approved KPI snapshots and the dashboard
presents actual values with their units. Run against strategic_management_test.
"""

from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError
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

    def _purchasing_overview(self, warehouse=False):
        today = fields.Date.context_today(self.env.user)
        return self.env["strategic.performance.dashboard"].with_user(self.Head).get_purchasing_overview_data(
            fields.Date.to_string(today.replace(day=1)), fields.Date.to_string(today), warehouse.id if warehouse else False,
        )

    def test_confirmed_order_with_future_deadline_appears_on_purchasing_dashboard(self):
        """A PO confirmed today counts today, even when its Order Deadline is later."""
        before = self._purchasing_overview()
        order = self._confirm_purchase_order(20.0)
        order.with_user(self.Head).write({"date_order": fields.Datetime.now() + timedelta(days=11)})

        after = self._purchasing_overview()
        self.assertIn(order.name, [row["name"] for row in after["recent_orders"]])
        self.assertEqual(after["recent_orders"][0]["name"], order.name)
        self.assertEqual(after["purchase_order_count"], before["purchase_order_count"] + 1)
        # Purchasing spend is reported excluding tax, like the line-based delivery values.
        self.assertAlmostEqual(after["purchase_order_value"] - before["purchase_order_value"], order.amount_untaxed, places=2)

        if self.WarehouseB != self.WarehouseA:
            branch_b = self._purchasing_overview(self.WarehouseB)
            self.assertNotIn(order.name, [row["name"] for row in branch_b["recent_orders"]])

    def _queued_dashboard_notifications(self):
        return [
            (channel, payload) for channel, notification_type, payload
            in self.env.cr.precommit.data.get("bus.bus.values", [])
            if notification_type == "strategic_management.data_changed"
        ]

    def _reset_dashboard_notifications(self):
        self.env.cr.precommit.data.pop("bus.bus.values", None)
        self.env.cr.precommit.data.pop("strategic_management.data_changed", None)

    def test_purchase_changes_queue_one_dashboard_refresh_notification(self):
        """Open dashboards are told to re-fetch; no figures are pushed over the bus."""
        self._reset_dashboard_notifications()
        self._confirm_purchase_order(20.0)
        self._make_target("TDD_LIVE_REFRESH_AREAS", "branch", self.WarehouseA)

        queued = self._queued_dashboard_notifications()
        self.assertEqual(len(queued), 1, "All changes in one transaction share one notification.")
        self.assertEqual(queued[0][0][1:], ("res.groups", self.GroupEmployee.id))
        self.assertEqual(set(queued[0][1]), {"areas"}, "The payload must not carry business figures.")
        self.assertIn("purchase", queued[0][1]["areas"])
        self.assertIn("strategic", queued[0][1]["areas"])

    def _branches(self, user, warehouse=False):
        today = fields.Date.context_today(self.env.user)
        return self.env["strategic.performance.dashboard"].with_user(user).get_branches_overview_data(
            fields.Date.to_string(today.replace(day=1)), fields.Date.to_string(today), warehouse.id if warehouse else False,
        )

    def test_branches_dashboard_respects_roles(self):
        """Head Office compares branches; a manager sees only their branch; employees see none."""
        head = self._branches(self.Head)
        self.assertEqual(head["role"], "head_office")
        self.assertIn(self.WarehouseA.id, [row["warehouse_id"] for row in head["branches"]])
        self.assertIn(self.WarehouseB.id, [row["warehouse_id"] for row in head["branches"]])

        branch_a = self._branches(self.BranchA, self.WarehouseB)
        self.assertEqual(branch_a["role"], "branch_manager")
        self.assertEqual([row["warehouse_id"] for row in branch_a["branches"]], [self.WarehouseA.id])
        self.assertEqual([row["id"] for row in branch_a["branch_options"]], [self.WarehouseA.id])
        self.assertTrue(all(item["branch"] == self.WarehouseA.display_name for item in branch_a["attention"]))

        employee = self._branches(self.Employee)
        self.assertEqual(employee["role"], "employee")
        self.assertFalse(employee["branches"])
        self.assertFalse(employee["attention"])

        with self.assertRaises(AccessError):
            self.env["strategic.performance.dashboard"].with_user(self.Head).get_branches_overview_data(False, False, 999999999)

    def test_branch_without_target_is_not_evaluated(self):
        """Missing targets never become a fabricated 0% or 100% attainment."""
        row = next(row for row in self._branches(self.Head)["branches"] if row["warehouse_id"] == self.WarehouseB.id)
        if row["kpi_total"]:
            self.skipTest("Branch B already has approved KPI targets in this database.")
        self.assertEqual(row["status"], "not_evaluated")
        self.assertEqual(row["status_label"], "Not Evaluated")
        self.assertIs(row["attainment"], False)

    def test_branch_status_follows_live_operational_data_without_manual_evaluation(self):
        """Confirming purchase orders moves Branch A from below target to meeting target."""
        target = self._make_target("TDD_BRANCH_LIVE_STATUS", "branch", self.WarehouseA)
        self._confirm_purchase_order(5.0)

        def branch_a_kpi(data):
            row = next(row for row in data["branches"] if row["warehouse_id"] == self.WarehouseA.id)
            items = [item for item in data["attention"] if target.kpi_id.display_name in item["title"]]
            return row, items

        row, items = branch_a_kpi(self._branches(self.Head))
        self.assertEqual(row["status"], "below_target")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["severity"], "critical")
        self.assertIn("Actual 5", items[0]["evidence"])
        self.assertIn("target 20", items[0]["evidence"])
        self.assertTrue(items[0]["deadline"])
        self.assertTrue(items[0]["responsible"])

        self._confirm_purchase_order(20.0)
        row, items = branch_a_kpi(self._branches(self.Head))
        self.assertFalse(items, "A target met by live data must leave the attention list.")
        self.assertGreaterEqual(row["kpi_met"], 1)
