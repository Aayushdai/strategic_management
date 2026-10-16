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

@tagged("post_install", "-at_install")
class TestStrategicKpiDefinitionVersioning(TransactionCase):
    """TDD contracts for approved, effective-dated KPI formula versions."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.group_user = cls.env.ref("base.group_user")
        cls.group_head = cls.env.ref("strategic_management.group_strategic_head_office_manager")
        cls.group_branch = cls.env.ref("strategic_management.group_strategic_branch_manager")
        cls.head_user = cls._make_user("kpi_version_head", cls.group_head)
        cls.branch_user = cls._make_user("kpi_version_branch", cls.group_branch)
        cls.Kpi = cls.env["strategic.kpi.definition"]
        cls.Target = cls.env["strategic.kpi.target"]
        cls.Evaluation = cls.env["strategic.kpi.evaluation"]

    @classmethod
    def _make_user(cls, login, group):
        return cls.env["res.users"].with_context(no_reset_password=True).create(
            {
                "name": login,
                "login": login,
                "email": "%s@example.com" % login,
                "company_id": cls.company.id,
                "company_ids": [(6, 0, [cls.company.id])],
                "group_ids": [(6, 0, [cls.group_user.id, group.id])],
            }
        )

    def _create_kpi(self, code="VERSIONED"):
        return self.Kpi.with_user(self.head_user).create(
            {
                "name": "%s Test KPI" % code,
                "code": code,
                "unit": "%",
                "direction": "higher",
                "source_type": "manual",
                "description": "Initial approved contract.",
                "company_id": self.company.id,
            }
        )

    def _require_version_model(self):
        # During the red TDD stage, fail the explicit schema test while making
        # dependent behavior tests skip cleanly until the production model lands.
        if "strategic.kpi.definition.version" not in self.env.registry.models:
            self.skipTest("KPI definition version model is not implemented yet.")
        return self.env["strategic.kpi.definition.version"]

    def _month_start(self, offset=0):
        from datetime import date
        from dateutil.relativedelta import relativedelta

        today = date.today()
        return (today.replace(day=1) + relativedelta(months=offset)).isoformat()

    def _month_end(self, offset=0):
        from datetime import date, timedelta
        from dateutil.relativedelta import relativedelta

        today = date.today()
        start = today.replace(day=1) + relativedelta(months=offset + 1)
        return (start - timedelta(days=1)).isoformat()

    def _create_target(self, kpi, start, end, target_value):
        return self.Target.with_user(self.head_user).create(
            {
                "kpi_id": kpi.id,
                "date_start": start,
                "date_end": end,
                "scope": "organization",
                "target_value": target_value,
                "state": "approved",
            }
        )

    def _create_revision(self, kpi, effective_from, **overrides):
        values = {
            "kpi_id": kpi.id,
            "unit": "%",
            "direction": "lower",
            "source_type": "manual",
            "description": "Revised KPI calculation contract.",
            "effective_from": effective_from,
            "change_reason": "Approved change to the measurement contract.",
        }
        values.update(overrides)
        return self._require_version_model().with_user(self.head_user).create(values)

    def test_00_definition_version_model_is_registered(self):
        self.assertIn(
            "strategic.kpi.definition.version",
            self.env.registry.models,
            "KPI contract revision history must be represented by a dedicated model.",
        )

    def test_initial_definition_has_approved_version_and_evaluations_reference_it(self):
        Version = self._require_version_model()
        kpi = self._create_kpi("VINIT")
        versions = kpi.version_ids
        self.assertEqual(len(versions), 1)
        initial = versions[0]
        self.assertEqual(initial.version_number, 1)
        self.assertEqual(initial.state, "approved")
        self.assertEqual(initial.direction, "higher")
        self.assertEqual(initial.unit, "%")

        start = self._month_start(-1)
        end = self._month_end(-1)
        target = self._create_target(kpi, start, end, 100.0)
        evaluation = self.Evaluation.with_user(self.head_user).create(
            {"target_id": target.id, "actual_value": 80.0, "has_actual_value": True}
        )
        self.assertEqual(evaluation.definition_version_id, initial)
        self.assertIn(initial, Version.search([("kpi_id", "=", kpi.id)]))

    def test_approved_revision_applies_by_period_and_preserves_old_evaluation(self):
        self._require_version_model()
        kpi = self._create_kpi("VEFFECTIVE")
        initial = kpi.version_ids.filtered(lambda version: version.version_number == 1)
        old_target = self._create_target(
            kpi, self._month_start(-1), self._month_end(-1), 100.0
        )
        old_evaluation = self.Evaluation.with_user(self.head_user).create(
            {"target_id": old_target.id, "actual_value": 80.0, "has_actual_value": True}
        )
        self.assertEqual(old_evaluation.attainment, 80.0)
        self.assertEqual(old_evaluation.definition_version_id, initial)

        revision = self._create_revision(kpi, self._month_start(1))
        self.assertEqual(revision.state, "draft")
        revision.with_user(self.head_user).action_approve()
        revision.invalidate_recordset()
        initial.invalidate_recordset()
        self.assertEqual(revision.state, "approved")
        self.assertEqual(initial.effective_to, self._month_end(0))
        self.assertEqual(old_evaluation.definition_version_id, initial)
        self.assertEqual(old_evaluation.attainment, 80.0)

        new_target = self._create_target(
            kpi, self._month_start(1), self._month_end(1), 10.0
        )
        new_evaluation = self.Evaluation.with_user(self.head_user).create(
            {"target_id": new_target.id, "actual_value": 5.0, "has_actual_value": True}
        )
        self.assertEqual(new_evaluation.definition_version_id, revision)
        self.assertEqual(new_evaluation.attainment, 200.0)
        self.assertEqual(new_evaluation.attainment_status, "exceeded")

    def test_revision_approval_is_head_office_only(self):
        self._require_version_model()
        kpi = self._create_kpi("VAUTH")
        revision = self._create_revision(kpi, self._month_start(1))
        with self.assertRaises(AccessError):
            revision.with_user(self.branch_user).action_approve()
        self.assertEqual(revision.state, "draft")

    def test_versioned_definition_fields_cannot_be_changed_directly(self):
        self._require_version_model()
        kpi = self._create_kpi("VLOCK")
        with self.assertRaises(ValidationError):
            kpi.with_user(self.head_user).write({"direction": "lower"})
        with self.assertRaises(ValidationError):
            kpi.with_user(self.head_user).write({"unit": "items"})

    def test_approved_version_is_immutable(self):
        self._require_version_model()
        kpi = self._create_kpi("VIMMUTABLE")
        version = kpi.version_ids[0]
        with self.assertRaises(ValidationError):
            version.with_user(self.head_user).write({"direction": "lower"})

    def test_approval_rejects_duplicate_or_overlapping_effective_start(self):
        self._require_version_model()
        kpi = self._create_kpi("VOVERLAP")
        effective_from = self._month_start(1)
        first_revision = self._create_revision(kpi, effective_from)
        first_revision.with_user(self.head_user).action_approve()
        duplicate_revision = self._create_revision(
            kpi,
            effective_from,
            description="Conflicting second revision.",
            change_reason="Attempted conflicting effective date.",
        )
        with self.assertRaises(ValidationError):
            duplicate_revision.with_user(self.head_user).action_approve()
        self.assertEqual(duplicate_revision.state, "draft")

    def test_evaluation_formula_version_reference_is_immutable(self):
        self._require_version_model()
        kpi = self._create_kpi("VEVALLOCK")
        initial = kpi.version_ids[0]
        target = self._create_target(kpi, self._month_start(-1), self._month_end(-1), 100.0)
        evaluation = self.Evaluation.with_user(self.head_user).create(
            {"target_id": target.id, "actual_value": 75.0, "has_actual_value": True}
        )
        with self.assertRaises(ValidationError):
            evaluation.write({"definition_version_id": initial.id + 100000})

    def test_branch_manager_cannot_create_definition_revision(self):
        self._require_version_model()
        kpi = self._create_kpi("VBRANCH")
        with self.assertRaises(AccessError):
            self.env["strategic.kpi.definition.version"].with_user(self.branch_user).create(
                {
                    "kpi_id": kpi.id,
                    "unit": "%",
                    "direction": "lower",
                    "source_type": "manual",
                    "effective_from": self._month_start(1),
                    "change_reason": "Unauthorized branch revision.",
                }
            )

