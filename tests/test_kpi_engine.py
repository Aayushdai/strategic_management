from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestStrategicKpiEngine(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Kpi = cls.env["strategic.kpi.definition"]
        cls.Target = cls.env["strategic.kpi.target"]
        cls.Evaluation = cls.env["strategic.kpi.evaluation"]
        cls.company = cls.env.company
        cls.warehouse_1 = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        if not cls.warehouse_1:
            cls.warehouse_1 = cls.env["stock.warehouse"].create(
                {"name": "Strategic Branch 1", "code": "SB1", "company_id": cls.company.id}
            )
        cls.warehouse_2 = cls.env["stock.warehouse"].create(
            {"name": "Strategic Branch 2", "code": "SB2", "company_id": cls.company.id}
        )

    def _create_kpi(self, direction="higher", code="KPI"):
        return self.Kpi.create(
            {
                "name": "%s Test KPI" % code,
                "code": code,
                "unit": "%",
                "direction": direction,
                "company_id": self.company.id,
            }
        )

    def _create_target(self, kpi, **values):
        target_values = {
            "kpi_id": kpi.id,
            "date_start": "2026-01-01",
            "date_end": "2026-01-31",
            "scope": "organization",
            "target_value": 100.0,
            "state": "approved",
        }
        target_values.update(values)
        return self.Target.create(target_values)

    def test_higher_is_better_attainment_bands(self):
        kpi = self._create_kpi("higher", "HIGH")
        target = self._create_target(kpi)
        cases = [
            (79.99, "not_achieved"),
            (80.0, "almost_met"),
            (100.0, "achieved"),
            (100.01, "exceeded"),
        ]
        for actual, status in cases:
            evaluation = self.Evaluation.create(
                {
                    "target_id": target.id,
                    "actual_value": actual,
                    "has_actual_value": True,
                }
            )
            self.assertEqual(evaluation.status, "valid")
            self.assertEqual(evaluation.attainment_status, status)

    def test_lower_is_better_and_zero_actual(self):
        kpi = self._create_kpi("lower", "LOW")
        target = self._create_target(kpi, target_value=10.0)
        evaluation = self.Evaluation.create(
            {
                "target_id": target.id,
                "actual_value": 5.0,
                "has_actual_value": True,
            }
        )
        self.assertEqual(evaluation.attainment, 200.0)
        self.assertEqual(evaluation.attainment_status, "exceeded")

        zero_actual = self.Evaluation.create(
            {
                "target_id": target.id,
                "actual_value": 0.0,
                "has_actual_value": True,
            }
        )
        self.assertEqual(zero_actual.status, "valid")
        self.assertEqual(zero_actual.attainment, 100.0)

    def test_range_target_binary_result(self):
        kpi = self._create_kpi("range", "RANGE")
        target = self._create_target(kpi, target_value=0.0, range_min=90.0, range_max=110.0)
        in_range = self.Evaluation.create(
            {
                "target_id": target.id,
                "actual_value": 100.0,
                "has_actual_value": True,
            }
        )
        out_of_range = self.Evaluation.create(
            {
                "target_id": target.id,
                "actual_value": 120.0,
                "has_actual_value": True,
            }
        )
        self.assertEqual(in_range.attainment_status, "achieved")
        self.assertEqual(in_range.attainment, 100.0)
        self.assertEqual(out_of_range.attainment_status, "not_achieved")
        self.assertEqual(out_of_range.attainment, 0.0)

    def test_missing_and_invalid_data_do_not_fabricate_results(self):
        kpi = self._create_kpi("higher", "SAFE")
        zero_target = self._create_target(kpi, target_value=0.0)
        missing_actual = self.Evaluation.create({"target_id": zero_target.id})
        self.assertEqual(missing_actual.status, "not_evaluated")

        unsafe_division = self.Evaluation.create(
            {
                "target_id": zero_target.id,
                "actual_value": 10.0,
                "has_actual_value": True,
            }
        )
        self.assertEqual(unsafe_division.status, "not_evaluated")
        self.assertEqual(unsafe_division.attainment_status, "not_evaluated")

        with self.assertRaises(ValidationError):
            self._create_target(kpi, target_value=-1.0)

    def test_evaluation_history_is_immutable_and_preserves_target_snapshot(self):
        kpi = self._create_kpi("higher", "HIST")
        target = self._create_target(kpi, target_value=100.0)
        evaluation = self.Evaluation.create(
            {
                "target_id": target.id,
                "actual_value": 90.0,
                "has_actual_value": True,
            }
        )
        target.write({"target_value": 120.0})
        self.assertEqual(evaluation.target_value, 100.0)
        with self.assertRaises(ValidationError):
            evaluation.write({"actual_value": 95.0})


@tagged("post_install", "-at_install")
class TestStrategicKpiSecurity(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.warehouse_1 = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        if not cls.warehouse_1:
            cls.warehouse_1 = cls.env["stock.warehouse"].create(
                {"name": "Security Branch 1", "code": "SC1", "company_id": cls.company.id}
            )
        cls.warehouse_2 = cls.env["stock.warehouse"].create(
            {"name": "Security Branch 2", "code": "SC2", "company_id": cls.company.id}
        )
        cls.group_user = cls.env.ref("base.group_user")
        cls.group_head = cls.env.ref("strategic_management.group_strategic_head_office_manager")
        cls.group_branch = cls.env.ref("strategic_management.group_strategic_branch_manager")
        cls.group_employee = cls.env.ref("strategic_management.group_strategic_employee")
        cls.head_user = cls._make_user("strategic_head", cls.group_head)
        cls.branch_user = cls._make_user("strategic_branch", cls.group_branch, cls.warehouse_1)
        cls.employee_user = cls._make_user("strategic_employee", cls.group_employee)

    @classmethod
    def _make_user(cls, login, group, warehouse=False):
        return cls.env["res.users"].with_context(no_reset_password=True).create(
            {
                "name": login,
                "login": login,
                "email": "%s@example.com" % login,
                "company_id": cls.company.id,
                "company_ids": [(6, 0, [cls.company.id])],
                "group_ids": [(6, 0, [cls.group_user.id, group.id])],
                "strategic_branch_warehouse_id": warehouse.id if warehouse else False,
            }
        )

    def test_role_boundaries_for_definitions_targets_and_evaluations(self):
        kpi = self.env["strategic.kpi.definition"].with_user(self.head_user).create(
            {
                "name": "Security KPI",
                "code": "SEC",
                "unit": "%",
                "direction": "higher",
                "company_id": self.company.id,
            }
        )
        branch_target = self.env["strategic.kpi.target"].with_user(self.head_user).create(
            {
                "kpi_id": kpi.id,
                "date_start": "2026-01-01",
                "date_end": "2026-01-31",
                "scope": "branch",
                "warehouse_id": self.warehouse_1.id,
                "target_value": 100.0,
                "state": "approved",
            }
        )
        other_branch_target = self.env["strategic.kpi.target"].with_user(self.head_user).create(
            {
                "kpi_id": kpi.id,
                "date_start": "2026-01-01",
                "date_end": "2026-01-31",
                "scope": "branch",
                "warehouse_id": self.warehouse_2.id,
                "target_value": 100.0,
                "state": "approved",
            }
        )
        self.env["strategic.kpi.evaluation"].with_user(self.head_user).create(
            {
                "target_id": branch_target.id,
                "actual_value": 90.0,
                "has_actual_value": True,
            }
        )
        self.env["strategic.kpi.evaluation"].with_user(self.head_user).create(
            {
                "target_id": other_branch_target.id,
                "actual_value": 90.0,
                "has_actual_value": True,
            }
        )

        with self.assertRaises(AccessError):
            self.env["strategic.kpi.definition"].with_user(self.branch_user).create(
                {"name": "Blocked", "code": "BLOCK", "unit": "%", "direction": "higher"}
            )

        visible_targets = self.env["strategic.kpi.target"].with_user(self.branch_user).search([])
        self.assertIn(branch_target, visible_targets)
        self.assertNotIn(other_branch_target, visible_targets)

        employee_evaluations = self.env["strategic.kpi.evaluation"].with_user(self.employee_user).search([])
        self.assertFalse(employee_evaluations)
