"""Regression tests for POS calculation contracts and source evaluation governance."""

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestStrategicKpiPosSourceAdapter(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        cls.GroupUser = cls.env.ref("base.group_user")
        cls.GroupHead = cls.env.ref("strategic_management.group_strategic_head_office_manager")
        cls.GroupBranch = cls.env.ref("strategic_management.group_strategic_branch_manager")
        cls.GroupEmployee = cls.env.ref("strategic_management.group_strategic_employee")
        cls.Kpi = cls.env["strategic.kpi.definition"]
        cls.Target = cls.env["strategic.kpi.target"]
        cls.Version = cls.env["strategic.kpi.definition.version"]
        cls.Evaluation = cls.env["strategic.kpi.evaluation"]

        unit = cls.env.ref("uom.product_uom_unit")
        template = cls.env["product.template"].create({
            "name": "Phase 5 POS adapter test product",
            "uom_id": unit.id,
        })
        cls.product = template.product_variant_id
        cls.head = cls._make_user("phase5_pos_head", cls.GroupHead)
        cls.employee = cls._make_user("phase5_pos_employee", cls.GroupEmployee)
        warehouse = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.company.id)], limit=1
        )
        if not warehouse:
            warehouse = cls.env["stock.warehouse"].create({
                "name": "Phase 5 POS Branch",
                "code": "P5PB",
                "company_id": cls.company.id,
            })
        cls.warehouse = warehouse
        cls.manager = cls._make_user("phase5_pos_manager", cls.GroupBranch, warehouse)

    @classmethod
    def _make_user(cls, login, role_group, warehouse=False):
        values = {
            "name": login,
            "login": login,
            "email": "%s@example.com" % login,
            "company_id": cls.company.id,
            "company_ids": [(6, 0, [cls.company.id])],
            "group_ids": [(6, 0, [cls.GroupUser.id, role_group.id])],
        }
        if warehouse:
            values["strategic_branch_warehouse_id"] = warehouse.id
        return cls.env["res.users"].with_context(no_reset_password=True).create(values)

    def _make_target(self, code, calculation_type="pos_sold_quantity", scope="organization", user=None):
        user = user or self.head
        kpi = self.Kpi.with_user(user).create({
            "name": "%s KPI" % code,
            "code": code,
            "company_id": self.company.id,
            "unit": self.product.uom_id.name,
            "direction": "higher",
            "source_type": "pos",
            "calculation_type": calculation_type,
            "source_product_id": self.product.id,
            "description": "POS adapter regression fixture.",
        })
        values = {
            "kpi_id": kpi.id,
            "date_start": "2098-01-01",
            "date_end": "2098-01-31",
            "scope": scope,
            "target_value": 100.0,
        }
        if scope == "branch":
            values["warehouse_id"] = self.warehouse.id
        target = self.Target.with_user(user).create(values)
        target.with_user(user).action_approve()
        return kpi, target

    def test_initial_definition_version_snapshots_pos_calculation_contract(self):
        kpi, _target = self._make_target("P5_POS_VERSION_SNAPSHOT")
        version = kpi._get_definition_version_for_period("2098-01-01", "2098-01-31")
        self.assertEqual(version.source_type, "pos")
        self.assertEqual(version.calculation_type, "pos_sold_quantity")
        self.assertEqual(version.source_product_id, self.product)

    def test_pos_calculation_version_cannot_be_approved_without_tracked_product(self):
        kpi, _target = self._make_target("P5_POS_MISSING_PRODUCT")
        draft = self.Version.with_user(self.head).create({
            "kpi_id": kpi.id,
            "unit": self.product.uom_id.name,
            "direction": "higher",
            "source_type": "pos",
            "calculation_type": "pos_recipe_consumption_estimate",
            "effective_from": "2098-02-01",
            "change_reason": "Test validation for a missing tracked recipe ingredient.",
        })
        with self.assertRaises(ValidationError):
            draft.with_user(self.head).action_approve()

    def test_no_completed_pos_orders_creates_not_evaluated_and_is_idempotent(self):
        _kpi, target = self._make_target("P5_POS_NO_ORDERS")
        first_action = target.with_user(self.head).action_evaluate_source()
        first = self.Evaluation.browse(first_action["res_id"])
        self.assertEqual(first.status, "not_evaluated")
        self.assertFalse(first.has_actual_value)
        self.assertIn("No paid or completed POS orders", first.reason)
        self.assertTrue(first.source_key)

        second_action = target.with_user(self.head).action_evaluate_source()
        self.assertEqual(second_action["res_id"], first.id)
        self.assertEqual(
            self.Evaluation.search_count([("target_id", "=", target.id), ("source_key", "!=", False)]),
            1,
        )

    def test_employee_cannot_trigger_operational_source_evaluation(self):
        _kpi, target = self._make_target("P5_POS_EMPLOYEE_BLOCK")
        with self.assertRaises(AccessError):
            target.with_user(self.employee).action_evaluate_source()

    def test_branch_manager_cannot_evaluate_an_organization_target(self):
        _kpi, target = self._make_target("P5_POS_BRANCH_SCOPE_BLOCK")
        with self.assertRaises(AccessError):
            target.with_user(self.manager).action_evaluate_source()
