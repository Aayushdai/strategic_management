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

    def test_target_form_exposes_source_calculation_type_for_button_visibility(self):
        product = self.env["product.product"].search([("uom_id", "!=", False)], limit=1)
        self.assertTrue(product, "The test database must contain a product with a unit of measure.")
        kpi = self.Kpi.create({
            "name": "POS View Modifier Test KPI",
            "code": "POS_VIEW_MODIFIER",
            "unit": product.uom_id.name,
            "direction": "higher",
            "source_type": "pos",
            "calculation_type": "pos_sold_quantity",
            "source_product_id": product.id,
            "company_id": self.company.id,
        })
        target = self._create_target(kpi, target_value=4.0)
        self.assertEqual(target.calculation_type, "pos_sold_quantity")

        view = self.env.ref("strategic_management.view_strategic_kpi_target_form")
        architecture = view.arch_db
        self.assertIn('field name="calculation_type" invisible="1"', architecture)
        self.assertIn("calculation_type not in", architecture)
        self.assertNotIn("kpi_id.calculation_type not in", architecture)

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
                # This test exercises target/evaluation access boundaries, not
                # manual-submission governance. Manual KPIs must use the
                # approval workflow tested in test_kpi_measurement.py.
                "source_type": "other",
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
                # These versioning tests create evaluations directly to test
                # version/target snapshots. Manual-source evaluations are
                # intentionally restricted to the approved measurement flow.
                "source_type": "other",
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
            "source_type": "other",
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

    def test_revision_creation_recovers_readonly_kpi_from_context(self):
        Version = self._require_version_model()
        kpi = self._create_kpi("VCONTEXT")

        # Mirrors the web form: kpi_id is readonly and can be omitted from
        # the create payload even though default_kpi_id populated the form.
        revision = Version.with_user(self.head_user).with_context(
            default_kpi_id=kpi.id
        ).create({
            "unit": "Units",
            "direction": "higher",
            "source_type": "pos",
            "effective_from": self._month_start(1),
            "change_reason": "Regression test for readonly KPI context default.",
        })

        self.assertEqual(revision.kpi_id, kpi)
        self.assertEqual(revision.version_number, 2)
        self.assertEqual(revision.state, "draft")

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
        self.assertEqual(str(initial.effective_to), self._month_end(0))
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
@tagged("post_install", "-at_install")
class TestStrategicKpiBaseline(TransactionCase):
    """TDD contracts for approved, scoped, historically traceable KPI baselines."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.group_user = cls.env.ref("base.group_user")
        cls.group_head = cls.env.ref("strategic_management.group_strategic_head_office_manager")
        cls.group_branch = cls.env.ref("strategic_management.group_strategic_branch_manager")
        cls.head_user = cls._make_user("kpi_baseline_head", cls.group_head)
        cls.branch_user = cls._make_user("kpi_baseline_branch", cls.group_branch)
        cls.Kpi = cls.env["strategic.kpi.definition"]
        cls.Warehouse = cls.env["stock.warehouse"]
        cls.warehouse = cls.Warehouse.search(
            [("company_id", "=", cls.company.id)], limit=1
        )
        if not cls.warehouse:
            cls.warehouse = cls.Warehouse.create(
                {"name": "Baseline Test Branch", "code": "BTB", "company_id": cls.company.id}
            )

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

    def _require_baseline_model(self):
        # Red-stage behavior: fail the registration test explicitly and let
        # dependent contract tests skip until the baseline model is implemented.
        if "strategic.kpi.baseline" not in self.env.registry.models:
            self.skipTest("Approved KPI baseline model is not implemented yet.")
        return self.env["strategic.kpi.baseline"]

    def _create_kpi(self, code="BASELINE_KPI"):
        return self.Kpi.with_user(self.head_user).create(
            {
                "name": "%s Test KPI" % code,
                "code": code,
                "unit": "%",
                "direction": "higher",
                # Baseline/target-resolution tests create evaluations
                # directly; manual submission behavior has its own tests.
                "source_type": "other",
                "description": "KPI used to test baseline management.",
                "company_id": self.company.id,
            }
        )

    def _baseline_values(self, kpi, **overrides):
        from datetime import date, timedelta

        today = date.today()
        source_period_end = today.replace(day=1) - timedelta(days=1)
        source_period_start = source_period_end.replace(day=1)
        values = {
            "kpi_id": kpi.id,
            "definition_version_id": kpi.version_ids[:1].id,
            "scope": "organization",
            "baseline_value": 72.5,
            "source_period_start": source_period_start.isoformat(),
            "source_period_end": source_period_end.isoformat(),
            "effective_from": today.isoformat(),
            "source_note": "Reconciled prior-month finance report; approved for comparison.",
            "change_reason": "Initial approved baseline for the KPI.",
        }
        values.update(overrides)
        return values

    def _create_baseline(self, code="BASELINE_KPI", **overrides):
        kpi = self._create_kpi(code)
        baseline = self._require_baseline_model().with_user(self.head_user).create(
            self._baseline_values(kpi, **overrides)
        )
        return kpi, baseline

    def test_00_baseline_model_is_registered(self):
        self.assertIn(
            "strategic.kpi.baseline",
            self.env.registry.models,
            "Approved baselines must be represented by a dedicated model, separate from KPI targets.",
        )

    def test_create_derives_readonly_definition_version_when_payload_omits_it(self):
        Baseline = self._require_baseline_model()
        kpi = self._create_kpi("BASELINE_DERIVE_VERSION")
        values = self._baseline_values(kpi)
        values.pop("definition_version_id", None)

        # Mirrors the web form: readonly onchange fields may not be included
        # in the create payload, even though the version appears on-screen.
        baseline = Baseline.with_user(self.head_user).create(values)

        self.assertEqual(baseline.definition_version_id, kpi.version_ids[:1])

    def test_baseline_starts_as_draft_and_retains_provenance(self):
        self._require_baseline_model()
        kpi, baseline = self._create_baseline("BASELINE_DRAFT")
        self.assertEqual(baseline.state, "draft")
        self.assertEqual(baseline.kpi_id, kpi)
        self.assertEqual(baseline.definition_version_id, kpi.version_ids[:1])
        self.assertEqual(baseline.scope, "organization")
        self.assertAlmostEqual(baseline.baseline_value, 72.5)
        self.assertTrue(baseline.source_period_start)
        self.assertTrue(baseline.source_period_end)
        self.assertTrue(baseline.effective_from)
        self.assertIn("Reconciled prior-month", baseline.source_note)
        self.assertFalse(baseline.approved_by_id)
        self.assertFalse(baseline.approved_at)

    def test_head_office_approval_records_actor_and_timestamp(self):
        self._require_baseline_model()
        _kpi, baseline = self._create_baseline("BASELINE_APPROVE")
        baseline.with_user(self.head_user).action_approve()
        baseline.invalidate_recordset()
        self.assertEqual(baseline.state, "approved")
        self.assertEqual(baseline.approved_by_id, self.head_user)
        self.assertTrue(baseline.approved_at)

    def test_branch_manager_cannot_create_baseline(self):
        Baseline = self._require_baseline_model()
        kpi = self._create_kpi("BASELINE_BRANCH_CREATE")
        with self.assertRaises(AccessError):
            Baseline.with_user(self.branch_user).create(
                self._baseline_values(kpi, change_reason="Unauthorized baseline creation.")
            )

    def test_branch_manager_cannot_approve_baseline(self):
        self._require_baseline_model()
        _kpi, baseline = self._create_baseline("BASELINE_BRANCH_APPROVE")
        with self.assertRaises(AccessError):
            baseline.with_user(self.branch_user).action_approve()
        self.assertEqual(baseline.state, "draft")

    def test_approved_baseline_is_immutable(self):
        self._require_baseline_model()
        _kpi, baseline = self._create_baseline("BASELINE_IMMUTABLE")
        baseline.with_user(self.head_user).action_approve()
        baseline.invalidate_recordset()
        with self.assertRaises(ValidationError):
            baseline.with_user(self.head_user).write({"baseline_value": 99.0})
        with self.assertRaises(ValidationError):
            baseline.with_user(self.head_user).write(
                {"source_note": "Replace the evidence for an approved baseline."}
            )

    def test_approved_baseline_cannot_be_deleted(self):
        self._require_baseline_model()
        _kpi, baseline = self._create_baseline("BASELINE_NO_DELETE")
        baseline.with_user(self.head_user).action_approve()
        with self.assertRaises(ValidationError):
            baseline.with_user(self.head_user).unlink()

    def test_branch_baseline_requires_warehouse(self):
        self._require_baseline_model()
        kpi = self._create_kpi("BASELINE_BRANCH_SCOPE")
        with self.assertRaises(ValidationError):
            self._require_baseline_model().with_user(self.head_user).create(
                self._baseline_values(kpi, scope="branch")
            )

    def test_employee_baseline_requires_employee(self):
        self._require_baseline_model()
        kpi = self._create_kpi("BASELINE_EMPLOYEE_SCOPE")
        with self.assertRaises(ValidationError):
            self._require_baseline_model().with_user(self.head_user).create(
                self._baseline_values(kpi, scope="user")
            )

    def test_approved_baseline_revision_closes_prior_effective_interval(self):
        from datetime import date, timedelta

        Baseline = self._require_baseline_model()
        kpi = self._create_kpi("BASELINE_REVISION_INTERVAL")
        today = date.today()
        first = Baseline.with_user(self.head_user).create(
            self._baseline_values(kpi, effective_from=today.isoformat())
        )
        first.with_user(self.head_user).action_approve()

        next_start = today + timedelta(days=30)
        revised = Baseline.with_user(self.head_user).create(
            self._baseline_values(
                kpi,
                baseline_value=75.0,
                effective_from=next_start.isoformat(),
                change_reason="Approved replacement baseline for the next period.",
            )
        )
        revised.with_user(self.head_user).action_approve()
        first.invalidate_recordset()
        self.assertEqual(first.effective_to, next_start - timedelta(days=1))
        self.assertFalse(revised.effective_to)

    def test_baseline_relative_target_requires_explicit_approved_baseline(self):
        from datetime import date, timedelta

        kpi = self._create_kpi("BASELINE_TARGET_REQUIRED")
        today = date.today()
        month_end = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        with self.assertRaises(ValidationError):
            self.env["strategic.kpi.target"].with_user(self.head_user).create({
                "kpi_id": kpi.id,
                "date_start": today.isoformat(),
                "date_end": month_end.isoformat(),
                "scope": "organization",
                "target_basis": "baseline_relative",
                "target_value": 80.0,
                "state": "approved",
            })

    def test_absolute_target_cannot_reference_baseline(self):
        from datetime import date, timedelta

        Baseline = self._require_baseline_model()
        kpi, baseline = self._create_baseline("BASELINE_ABSOLUTE_TARGET")
        baseline.with_user(self.head_user).action_approve()
        today = date.today()
        month_end = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        with self.assertRaises(ValidationError):
            self.env["strategic.kpi.target"].with_user(self.head_user).create({
                "kpi_id": kpi.id,
                "date_start": today.isoformat(),
                "date_end": month_end.isoformat(),
                "scope": "organization",
                "target_basis": "absolute",
                "baseline_id": baseline.id,
                "target_value": 80.0,
                "state": "approved",
            })

    def test_baseline_relative_target_requires_matching_scope(self):
        from datetime import date, timedelta

        kpi, baseline = self._create_baseline("BASELINE_TARGET_SCOPE")
        baseline.with_user(self.head_user).action_approve()
        today = date.today()
        month_end = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        with self.assertRaises(ValidationError):
            self.env["strategic.kpi.target"].with_user(self.head_user).create({
                "kpi_id": kpi.id,
                "date_start": today.isoformat(),
                "date_end": month_end.isoformat(),
                "scope": "branch",
                "warehouse_id": self.warehouse.id,
                "target_basis": "baseline_relative",
                "baseline_id": baseline.id,
                "target_value": 80.0,
                "state": "approved",
            })

    def test_baseline_relative_target_snapshots_baseline_on_evaluation(self):
        from datetime import date, timedelta

        kpi, baseline = self._create_baseline("BASELINE_TARGET_SNAPSHOT")
        baseline.with_user(self.head_user).action_approve()
        today = date.today()
        month_end = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        target = self.env["strategic.kpi.target"].with_user(self.head_user).create({
            "kpi_id": kpi.id,
            "date_start": today.isoformat(),
            "date_end": month_end.isoformat(),
            "scope": "organization",
            "target_basis": "baseline_relative",
            "baseline_id": baseline.id,
            "target_value": 80.0,
            "state": "approved",
        })
        evaluation = self.env["strategic.kpi.evaluation"].with_user(self.head_user).create({
            "target_id": target.id,
            "actual_value": 82.0,
            "has_actual_value": True,
        })
        self.assertEqual(evaluation.target_basis, "baseline_relative")
        self.assertEqual(evaluation.baseline_id, baseline)
        self.assertAlmostEqual(evaluation.baseline_value, baseline.baseline_value)
        with self.assertRaises(ValidationError):
            evaluation.with_user(self.head_user).write({"baseline_value": 99.0})

    def test_baseline_source_period_must_be_valid(self):
        self._require_baseline_model()
        kpi = self._create_kpi("BASELINE_INVALID_PERIOD")
        with self.assertRaises(ValidationError):
            self._require_baseline_model().with_user(self.head_user).create(
                self._baseline_values(
                    kpi,
                    source_period_start="2026-10-31",
                    source_period_end="2026-10-01",
                )
            )


    # Target-resolution contracts. The production resolver is intentionally
    # checked before exercising the cases so the initial TDD run reports one
    # clear missing-capability failure rather than a cascade of AttributeErrors.
    def _require_target_resolver(self):
        Target = self.env["strategic.kpi.target"]
        if not hasattr(Target, "_resolve_for_period"):
            self.skipTest("Deterministic KPI target resolver is not implemented yet.")
        return Target.with_user(self.head_user)

    def _create_resolution_target(self, kpi, **overrides):
        values = {
            "kpi_id": kpi.id,
            "date_start": "2026-01-01",
            "date_end": "2026-01-31",
            "scope": "organization",
            "target_value": 10.0,
            "state": "approved",
        }
        values.update(overrides)
        return self.env["strategic.kpi.target"].with_user(self.head_user).create(values)

    def test_00_target_resolver_is_registered(self):
        Target = self.env["strategic.kpi.target"]
        self.assertTrue(
            hasattr(Target, "_resolve_for_period"),
            "KPI target selection must use a deterministic resolver that fails closed when the match is missing or ambiguous.",
        )

    def test_resolver_returns_the_unique_approved_target_covering_full_period(self):
        Target = self._require_target_resolver()
        kpi = self._create_kpi("TARGET_RESOLVE_UNIQUE")
        target = self._create_resolution_target(kpi)

        resolved = Target._resolve_for_period(
            kpi,
            "2026-01-01",
            "2026-01-31",
            scope="organization",
            required=True,
        )
        self.assertEqual(resolved, target)

    def test_resolver_fails_closed_when_no_target_matches(self):
        Target = self._require_target_resolver()
        kpi = self._create_kpi("TARGET_RESOLVE_MISSING")

        with self.assertRaises(ValidationError):
            Target._resolve_for_period(
                kpi,
                "2026-01-01",
                "2026-01-31",
                scope="organization",
                required=True,
            )

    def test_resolver_allows_no_target_for_optional_generic_monitoring(self):
        Target = self._require_target_resolver()
        kpi = self._create_kpi("TARGET_RESOLVE_OPTIONAL")

        resolved = Target._resolve_for_period(
            kpi,
            "2026-01-01",
            "2026-01-31",
            scope="organization",
            required=False,
        )
        self.assertFalse(resolved)

    def test_resolver_fails_closed_when_multiple_approved_targets_match(self):
        Target = self._require_target_resolver()
        kpi = self._create_kpi("TARGET_RESOLVE_AMBIGUOUS")
        self._create_resolution_target(kpi, target_value=10.0)
        self._create_resolution_target(kpi, target_value=12.0)

        with self.assertRaises(ValidationError):
            Target._resolve_for_period(
                kpi,
                "2026-01-01",
                "2026-01-31",
                scope="organization",
                required=True,
            )

    def test_resolver_does_not_use_a_draft_target(self):
        Target = self._require_target_resolver()
        kpi = self._create_kpi("TARGET_RESOLVE_DRAFT")
        self._create_resolution_target(kpi, state="draft")

        with self.assertRaises(ValidationError):
            Target._resolve_for_period(
                kpi,
                "2026-01-01",
                "2026-01-31",
                scope="organization",
                required=True,
            )

    def test_resolver_does_not_fallback_across_scope(self):
        Target = self._require_target_resolver()
        kpi = self._create_kpi("TARGET_RESOLVE_SCOPE")
        self._create_resolution_target(
            kpi,
            scope="branch",
            warehouse_id=self.warehouse.id,
        )

        with self.assertRaises(ValidationError):
            Target._resolve_for_period(
                kpi,
                "2026-01-01",
                "2026-01-31",
                scope="organization",
                required=True,
            )

    def test_resolver_rejects_target_that_covers_only_part_of_period(self):
        Target = self._require_target_resolver()
        kpi = self._create_kpi("TARGET_RESOLVE_PARTIAL_PERIOD")
        self._create_resolution_target(
            kpi,
            date_start="2026-01-10",
            date_end="2026-01-31",
        )

        with self.assertRaises(ValidationError):
            Target._resolve_for_period(
                kpi,
                "2026-01-01",
                "2026-01-31",
                scope="organization",
                required=True,
            )

    def _create_resolution_key_result(self, kpi, target, code):
        """Create an organization-scoped KR with an explicit approved target."""
        Plan = self.env["strategic.plan"].with_user(self.head_user)
        plan = Plan.create(
            {
                "name": "Target Resolution Plan %s" % code,
                "company_id": self.company.id,
                "date_start": "2026-01-01",
                "date_end": "2026-12-31",
            }
        )
        objective = self.env["strategic.objective"].with_user(self.head_user).create(
            {
                "name": "Target Resolution Objective %s" % code,
                "plan_id": plan.id,
                "perspective": "financial",
                "scope": "organization",
                "owner_id": self.head_user.id,
                "deadline": "2026-12-31",
                "state": "draft",
            }
        )
        okr = self.env["strategic.okr.objective"].with_user(self.head_user).create(
            {
                "name": "Target Resolution OKR %s" % code,
                "strategic_objective_id": objective.id,
                "owner_id": self.head_user.id,
                "date_start": target.date_start.isoformat(),
                "date_end": target.date_end.isoformat(),
            }
        )
        return self.env["strategic.key.result"].with_user(self.head_user).create(
            {
                "name": "Target Resolution KR %s" % code,
                "okr_objective_id": okr.id,
                "primary_kpi_id": kpi.id,
                "target_id": target.id,
                "required": True,
            }
        )

    def test_key_result_resolver_returns_its_explicit_target_even_if_another_matches(self):
        """KR resolution must use its own target, not a generic search result."""
        Target = self._require_target_resolver()
        kpi = self._create_kpi("TARGET_RESOLVE_KR_EXPLICIT")
        explicit_target = self._create_resolution_target(kpi, target_value=10.0)
        # A second matching record makes generic lookup ambiguous, but must not
        # override or invalidate the target explicitly linked to this KR.
        self._create_resolution_target(kpi, target_value=12.0)
        key_result = self._create_resolution_key_result(
            kpi, explicit_target, "EXPLICIT_TARGET"
        )

        resolved = Target._resolve_for_period(
            kpi,
            "2026-01-01",
            "2026-01-31",
            scope="organization",
            required=True,
            key_result=key_result,
        )
        self.assertEqual(resolved, explicit_target)

    def test_evaluation_creation_uses_kr_explicit_target_instead_of_generic_fallback(self):
        """A second generic match must not make an explicit KR target unusable."""
        kpi = self._create_kpi("TARGET_RESOLVE_KR_EVALUATION")
        explicit_target = self._create_resolution_target(kpi, target_value=10.0)
        self._create_resolution_target(kpi, target_value=12.0)
        key_result = self._create_resolution_key_result(
            kpi, explicit_target, "EVALUATION_TARGET"
        )

        # Evaluation creation should recognize that the selected target is
        # explicitly assigned to a KR and resolve in that KR context, rather
        # than running an ambiguous generic-monitoring lookup.
        evaluation = self.env["strategic.kpi.evaluation"].with_user(self.head_user).create(
            {
                "target_id": explicit_target.id,
                "actual_value": 8.0,
                "has_actual_value": True,
            }
        )
        key_result.with_user(self.head_user).write({"evaluation_id": evaluation.id})
        self.assertEqual(evaluation.target_id, explicit_target)
        self.assertEqual(key_result.evaluation_id, evaluation)
        self.assertEqual(key_result.scoring_status, "valid")
        self.assertAlmostEqual(key_result.attainment, 80.0)

    def test_key_result_resolver_rejects_a_different_reporting_period(self):
        Target = self._require_target_resolver()
        kpi = self._create_kpi("TARGET_RESOLVE_KR_PERIOD")
        target = self._create_resolution_target(kpi)
        key_result = self._create_resolution_key_result(kpi, target, "PERIOD_MISMATCH")

        with self.assertRaises(ValidationError):
            Target._resolve_for_period(
                kpi,
                "2026-01-02",
                "2026-01-31",
                scope="organization",
                required=True,
                key_result=key_result,
            )

@tagged("post_install", "-at_install")
class TestStrategicKpiReportingViews(TransactionCase):
    """Phase 7 native reporting actions keep valid results separate from exceptions."""

    def test_kpi_analysis_uses_valid_evaluations_and_native_pivot_graph_views(self):
        action = self.env.ref("strategic_management.action_strategic_kpi_analysis")
        self.assertEqual(action.res_model, "strategic.kpi.evaluation")
        self.assertIn("pivot", action.view_mode)
        self.assertIn("graph", action.view_mode)
        self.assertIn("valid", action.domain)
        self.assertEqual(
            self.env["strategic.kpi.evaluation"]._fields["attainment"].aggregator,
            "avg",
        )
        self.assertTrue(self.env.ref("strategic_management.view_strategic_kpi_evaluation_graph"))
        self.assertTrue(self.env.ref("strategic_management.view_strategic_kpi_evaluation_pivot"))
        self.assertTrue(self.env.ref("strategic_management.view_strategic_kpi_evaluation_search"))

    def test_kpi_exceptions_action_only_shows_nonvalid_evaluation_statuses(self):
        action = self.env.ref("strategic_management.action_strategic_kpi_exceptions")
        self.assertEqual(action.res_model, "strategic.kpi.evaluation")
        self.assertIn("not_evaluated", action.domain)
        self.assertIn("data_quality_issue", action.domain)
        self.assertIn("pending_review", action.domain)
        self.assertTrue(self.env.ref("strategic_management.view_strategic_kpi_evaluation_exception_list"))

@tagged("post_install", "-at_install")
class TestStrategicKpiRoleDashboards(TransactionCase):
    """Phase 7 dashboards open valid KPI attainment using role-scoped access."""

    def test_role_dashboard_actions_use_valid_evaluations_and_role_pivots(self):
        cases = (
            ("head_office", "view_strategic_kpi_dashboard_head_office_pivot"),
            ("branch", "view_strategic_kpi_dashboard_branch_pivot"),
            ("employee", "view_strategic_kpi_dashboard_employee_pivot"),
        )
        for role, view_xmlid in cases:
            with self.subTest(role=role):
                action = self.env.ref(
                    "strategic_management.action_strategic_kpi_dashboard_%s" % role
                )
                self.assertEqual(action.res_model, "strategic.kpi.evaluation")
                self.assertIn("pivot", action.view_mode)
                self.assertIn("graph", action.view_mode)
                self.assertIn("valid", action.domain)
                self.assertEqual(
                    action.view_id,
                    self.env.ref("strategic_management.%s" % view_xmlid),
                )
                self.assertEqual(
                    action.search_view_id,
                    self.env.ref("strategic_management.view_strategic_kpi_evaluation_search"),
                )

    def test_role_dashboard_defaults_show_relevant_kpi_and_scope_breakdowns(self):
        head_office = self.env.ref(
            "strategic_management.action_strategic_kpi_dashboard_head_office"
        )
        branch = self.env.ref(
            "strategic_management.action_strategic_kpi_dashboard_branch"
        )
        employee = self.env.ref(
            "strategic_management.action_strategic_kpi_dashboard_employee"
        )

        self.assertIn("search_default_group_by_kpi", head_office.context)
        self.assertIn("search_default_group_by_scope", head_office.context)
        self.assertIn("search_default_group_by_warehouse", head_office.context)

        self.assertIn("search_default_group_by_kpi", branch.context)
        self.assertIn("search_default_group_by_scope", branch.context)
        self.assertIn("search_default_group_by_warehouse", branch.context)
        self.assertIn("search_default_group_by_period", branch.context)
        self.assertIn(
            'name="warehouse_id"',
            branch.view_id.arch_db,
            "Branch analysis must expose the assigned warehouse dimension; record rules still restrict rows.",
        )

        self.assertIn("search_default_group_by_kpi", employee.context)
        self.assertIn("search_default_group_by_period", employee.context)

    def test_dashboard_menus_are_restricted_to_the_matching_role(self):
        role_menu_pairs = (
            ("head_office", "group_strategic_head_office_manager"),
            ("branch", "group_strategic_branch_manager"),
            ("employee", "group_strategic_employee"),
        )
        for role, group_name in role_menu_pairs:
            with self.subTest(role=role):
                menu = self.env.ref(
                    "strategic_management.menu_strategic_kpi_dashboard_%s" % role
                )
                group = self.env.ref("strategic_management.%s" % group_name)
                self.assertEqual(menu.group_ids, group)
