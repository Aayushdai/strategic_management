"""Tests for inventory balance reconstruction and movement KPIs."""

from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestStrategicKpiInventoryFlowAdapter(TransactionCase):
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
            "name": "Phase 5 Inventory Flow Test Product",
            "uom_id": unit.id,
            "purchase_ok": True,
        })
        cls.product = template.product_variant_id
        cls.warehouse = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.company.id)], limit=1
        )
        if not cls.warehouse:
            cls.warehouse = cls.env["stock.warehouse"].create({
                "name": "Phase 5 Flow Branch",
                "code": "P5FL",
                "company_id": cls.company.id,
            })
        cls.head = cls._make_user("phase5_flow_head", cls.GroupHead)
        cls.employee = cls._make_user("phase5_flow_employee", cls.GroupEmployee)
        cls.branch_manager = cls._make_user("phase5_flow_branch_manager", cls.GroupBranch)
        cls.branch_manager.sudo().write({"strategic_branch_warehouse_id": cls.warehouse.id})
        today = fields.Date.context_today(cls.env.user)
        cls.current_start = today.replace(day=1)
        cls.next_start = (cls.current_start + timedelta(days=32)).replace(day=1)
        cls.current_end = cls.next_start - timedelta(days=1)
        cls.previous_end = cls.current_start - timedelta(days=1)
        cls.previous_start = cls.previous_end.replace(day=1)

    @classmethod
    def _make_user(cls, login, role_group):
        return cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": login,
            "login": login,
            "email": "%s@example.com" % login,
            "company_id": cls.company.id,
            "company_ids": [(6, 0, [cls.company.id])],
            "group_ids": [(6, 0, [cls.GroupUser.id, role_group.id])],
        })

    def _make_target(self, code, calculation_type, *, scope="organization", past_period=False):
        kpi = self.env["strategic.kpi.definition"].with_user(self.head).create({
            "name": "%s KPI" % code,
            "code": code,
            "company_id": self.company.id,
            "unit": self.product.uom_id.name,
            "direction": "higher",
            "source_type": "inventory",
            "calculation_type": calculation_type,
            "source_product_id": self.product.id,
            "description": "Phase 5 inventory flow regression fixture.",
        })
        if past_period:
            start, end = self.previous_start, self.previous_end
        else:
            start, end = self.current_start, self.current_end
        vals = {
            "kpi_id": kpi.id,
            "date_start": start,
            "date_end": end,
            "scope": scope,
            "target_value": 100.0,
        }
        if scope == "branch":
            vals["warehouse_id"] = self.warehouse.id
        target = self.env["strategic.kpi.target"].with_user(self.head).create(vals)
        target.with_user(self.head).action_approve()
        return kpi, target

    def _evaluate(self, target, user=None):
        user = user or self.head
        action = target.with_user(user).action_evaluate_source()
        return self.env["strategic.kpi.evaluation"].browse(action["res_id"])

    def test_opening_stock_uses_historical_balance_contract(self):
        _kpi, target = self._make_target(
            "P5_OPENING_STOCK", "inventory_opening_stock_quantity", past_period=True
        )
        evaluation = self._evaluate(target)
        self.assertEqual(evaluation.status, "valid")
        self.assertTrue(evaluation.has_actual_value)
        self.assertAlmostEqual(evaluation.actual_value, 0.0, places=6)
        self.assertIn("Historical opening stock reconstructed", evaluation.source_note)
        self.assertIn("stock.quant+stock.move", evaluation.source_model)

    def test_closing_stock_is_not_evaluated_before_period_end(self):
        _kpi, target = self._make_target(
            "P5_CLOSING_OPEN_PERIOD", "inventory_closing_stock_quantity"
        )
        evaluation = self._evaluate(target)
        self.assertEqual(evaluation.status, "not_evaluated")
        self.assertFalse(evaluation.has_actual_value)
        self.assertIn("reporting period has not ended", evaluation.reason)

    def test_closing_stock_is_available_for_a_completed_period(self):
        _kpi, target = self._make_target(
            "P5_CLOSING_STOCK", "inventory_closing_stock_quantity", past_period=True
        )
        evaluation = self._evaluate(target)
        self.assertEqual(evaluation.status, "valid")
        self.assertTrue(evaluation.has_actual_value)
        self.assertAlmostEqual(evaluation.actual_value, 0.0, places=6)
        self.assertIn("Historical closing stock reconstructed", evaluation.source_note)

    def test_no_completed_production_moves_does_not_fabricate_actual_consumption(self):
        _kpi, target = self._make_target(
            "P5_ACTUAL_CONSUMPTION_NO_MOVES", "inventory_actual_consumption_quantity"
        )
        evaluation = self._evaluate(target)
        self.assertEqual(evaluation.status, "not_evaluated")
        self.assertFalse(evaluation.has_actual_value)
        self.assertIn("POS recipe estimates are not treated as actual stock consumption", evaluation.reason)

    def test_branch_transfer_in_and_out_can_be_evaluated(self):
        for calculation, code in (
            ("inventory_transfer_in_quantity", "P5_BRANCH_TRANSFER_IN"),
            ("inventory_transfer_out_quantity", "P5_BRANCH_TRANSFER_OUT"),
        ):
            _kpi, target = self._make_target(code, calculation, scope="branch")
            evaluation = self._evaluate(target, self.head)
            self.assertEqual(evaluation.status, "valid")
            self.assertTrue(evaluation.has_actual_value)
            self.assertAlmostEqual(evaluation.actual_value, 0.0, places=6)

    def test_no_recorded_inventory_adjustments_are_explicit_zero(self):
        _kpi, target = self._make_target(
            "P5_ADJUSTMENT_ZERO", "inventory_adjustment_quantity"
        )
        evaluation = self._evaluate(target)
        self.assertEqual(evaluation.status, "valid")
        self.assertTrue(evaluation.has_actual_value)
        self.assertAlmostEqual(evaluation.actual_value, 0.0, places=6)
        self.assertIn("zero recorded movement", evaluation.source_note)

    def test_employee_cannot_trigger_inventory_balance_evaluation(self):
        _kpi, target = self._make_target(
            "P5_EMPLOYEE_BALANCE_ACCESS", "inventory_opening_stock_quantity", past_period=True
        )
        with self.assertRaises(AccessError):
            target.with_user(self.employee).action_evaluate_source()


    def test_unexplained_variance_is_zero_when_no_unclassified_stock_flow_exists(self):
        _kpi, target = self._make_target(
            "P5_UNEXPLAINED_VARIANCE_ZERO", "inventory_unexplained_variance_quantity", past_period=True
        )
        evaluation = self._evaluate(target)
        self.assertEqual(evaluation.status, "valid")
        self.assertTrue(evaluation.has_actual_value)
        self.assertAlmostEqual(evaluation.actual_value, 0.0, places=6)
        self.assertIn("reconciliation residual", evaluation.source_note)

    def test_unexplained_variance_is_not_evaluated_before_period_end(self):
        _kpi, target = self._make_target(
            "P5_UNEXPLAINED_VARIANCE_OPEN", "inventory_unexplained_variance_quantity"
        )
        evaluation = self._evaluate(target)
        self.assertEqual(evaluation.status, "not_evaluated")
        self.assertFalse(evaluation.has_actual_value)
        self.assertIn("reporting period has not ended", evaluation.reason)

    def test_new_inventory_calculation_rejects_wrong_source_type(self):
        with self.assertRaises(ValidationError):
            self.env["strategic.kpi.definition"].with_user(self.head).create({
                "name": "Bad Inventory Flow Source",
                "code": "P5_BAD_FLOW_SOURCE",
                "company_id": self.company.id,
                "unit": self.product.uom_id.name,
                "direction": "higher",
                "source_type": "purchase",
                "calculation_type": "inventory_closing_stock_quantity",
                "source_product_id": self.product.id,
                "description": "Must reject source-type mismatch.",
            })
