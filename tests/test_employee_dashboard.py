"""TDD and regression tests for the Employee personal KPI dashboard."""

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import tagged

from .test_strategy import StrategyFixture


@tagged("post_install", "-at_install")
class TestEmployeeDashboard(StrategyFixture):
    def _make_user_target_evaluation(
        self, employee, code, actual=9.0, target_value=12.0,
        date_start="2026-10-01", date_end="2026-10-31",
    ):
        kpi = self._mk_kpi(code, individual=True)
        target = self.env["strategic.kpi.target"].with_user(self.head_user).create({
            "kpi_id": kpi.id,
            "date_start": date_start,
            "date_end": date_end,
            "scope": "user",
            "user_id": employee.id,
            "target_value": target_value,
            "state": "approved",
        })
        evaluation = self._mk_evaluation(target, actual=actual, has_actual=True)
        return target, evaluation

    def test_employee_dashboard_service_is_implemented(self):
        service = self.env["strategic.performance.dashboard"]
        self.assertTrue(
            hasattr(service, "get_employee_data"),
            "The Employee dashboard must expose a dedicated personal KPI data method.",
        )

    def test_employee_dashboard_rejects_management_roles(self):
        service = self.env["strategic.performance.dashboard"]
        for manager in (self.head_user, self.branch_user_a):
            with self.subTest(login=manager.login):
                with self.assertRaises(AccessError):
                    service.with_user(manager).get_employee_data(
                        "2026-10-01", "2026-10-31"
                    )

    def test_employee_dashboard_rejects_reversed_reporting_period(self):
        service = self.env["strategic.performance.dashboard"].with_user(self.emp_user)
        with self.assertRaisesRegex(ValidationError, "start date cannot be after"):
            service.get_employee_data("2026-10-31", "2026-10-01")

    def test_dashboard_exposes_employee_b_style_actual_target_and_attainment(self):
        _target, evaluation = self._make_user_target_evaluation(
            self.emp_user, "EMP_DASHBOARD_B_STYLE", actual=9.0, target_value=12.0
        )

        data = self.env["strategic.performance.dashboard"].with_user(
            self.emp_user
        ).get_employee_data("2026-10-01", "2026-10-31")

        self.assertEqual(data["user_id"], self.emp_user.id)
        self.assertEqual(data["user_name"], self.emp_user.name)
        self.assertEqual(data["period"], {
            "start": "2026-10-01", "end": "2026-10-31"
        })
        self.assertEqual(len(data["kpis"]), 1)
        row = data["kpis"][0]
        self.assertEqual(row["id"], evaluation.id)
        self.assertEqual(row["actual_value"], 9.0)
        self.assertEqual(row["target_value"], 12.0)
        self.assertAlmostEqual(row["attainment"], 75.0)
        self.assertEqual(row["status"], "valid")
        self.assertEqual(row["attainment_status"], "not_achieved")
        self.assertEqual(row["attainment_status_label"], "Not Achieved")
        self.assertAlmostEqual(data["kpi_summary"]["average_attainment"], 75.0)
        self.assertEqual(data["kpi_summary"]["valid_evaluation_count"], 1)
        self.assertEqual(data["kpi_summary"]["below_target_count"], 1)
        self.assertIsInstance(data["monthly_trend"], list)
        self.assertIn("measurement_submissions", data)
        self.assertIn("measurement_summary", data)

    def test_employee_dashboard_never_returns_another_employees_evaluation(self):
        _own_target, own_evaluation = self._make_user_target_evaluation(
            self.emp_user, "EMP_DASHBOARD_OWN", actual=9.0, target_value=12.0
        )
        other_employee = self._mk_user("p3_other_employee", self.g_emp, None)
        _other_target, other_evaluation = self._make_user_target_evaluation(
            other_employee, "EMP_DASHBOARD_OTHER", actual=4.0, target_value=20.0
        )

        data = self.env["strategic.performance.dashboard"].with_user(
            self.emp_user
        ).get_employee_data("2026-10-01", "2026-10-31")
        returned_ids = {row["id"] for row in data["kpis"]}
        self.assertIn(own_evaluation.id, returned_ids)
        self.assertNotIn(other_evaluation.id, returned_ids)
        self.assertTrue(all(row["scope"] == "user" for row in data["kpis"]))
        self.assertTrue(all(row["user_id"] == self.emp_user.id for row in data["kpis"]))

    def test_employee_dashboard_filters_records_to_selected_reporting_period(self):
        _october_target, october_evaluation = self._make_user_target_evaluation(
            self.emp_user, "EMP_DASHBOARD_OCTOBER", actual=9.0, target_value=12.0,
            date_start="2026-10-01", date_end="2026-10-31",
        )
        _november_target, november_evaluation = self._make_user_target_evaluation(
            self.emp_user, "EMP_DASHBOARD_NOVEMBER", actual=11.0, target_value=12.0,
            date_start="2026-11-01", date_end="2026-11-30",
        )

        data = self.env["strategic.performance.dashboard"].with_user(
            self.emp_user
        ).get_employee_data("2026-10-01", "2026-10-31")
        returned_ids = {row["id"] for row in data["kpis"]}
        self.assertIn(october_evaluation.id, returned_ids)
        self.assertNotIn(november_evaluation.id, returned_ids)

    def test_employee_dashboard_reports_own_manual_submission_status(self):
        # This fixture employee is assigned in this isolated test only. It does not
        # alter the real ui_test_employee_b record in strategic_management_test.
        self.emp_user.write({"strategic_branch_warehouse_id": self.wh1.id})
        kpi = self.env["strategic.kpi.definition"].with_user(self.head_user).create({
            "name": "Employee Dashboard Manual Submission KPI",
            "code": "EMP_DASH_MANUAL_SUBMIT",
            "unit": "%",
            "direction": "higher",
            "source_type": "manual",
            "description": "Employee dashboard submission-state test.",
            "individual_outcome": True,
            "company_id": self.company.id,
        })
        target = self.env["strategic.kpi.target"].with_user(self.head_user).create({
            "kpi_id": kpi.id,
            "date_start": "2026-10-01",
            "date_end": "2026-10-31",
            "scope": "user",
            "user_id": self.emp_user.id,
            "target_value": 100.0,
        })
        target.with_user(self.head_user).action_approve()
        measurement = self.env["strategic.kpi.measurement"].with_user(self.emp_user).create({
            "target_id": target.id,
            "actual_value": 82.0,
            "has_actual_value": True,
            "source_reference": "Employee dashboard TDD source.",
            "submitter_note": "Submitted from test fixture.",
        })
        measurement.with_user(self.emp_user).action_submit()

        data = self.env["strategic.performance.dashboard"].with_user(
            self.emp_user
        ).get_employee_data("2026-10-01", "2026-10-31")
        row = next(
            (item for item in data["measurement_submissions"] if item["id"] == measurement.id),
            None,
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["state"], "submitted")
        self.assertEqual(row["state_label"], "Submitted for Review")
        self.assertEqual(data["measurement_summary"]["submitted_count"], 1)

    def test_personal_kpi_menu_is_gone(self):
        self.assertFalse(self.env.ref("strategic_management.menu_strategic_kpi_dashboard_employee", raise_if_not_found=False))

    # ------------------------------------------------------------------ KPI ownership
    def _legacy_employee_target(self, code="OWN_LEGACY"):
        """As the real database has it: a business KPI given an employee target before the ownership rule existed."""
        kpi = self._mk_kpi(code, individual=True)
        target = self.env["strategic.kpi.target"].with_user(self.head_user).create({
            "kpi_id": kpi.id, "date_start": "2026-10-01", "date_end": "2026-10-31", "scope": "user", "user_id": self.emp_user.id, "target_value": 10.0, "state": "approved"})
        evaluation = self._mk_evaluation(target, actual=8.0, has_actual=True)
        self.env.cr.execute("UPDATE strategic_kpi_definition SET individual_outcome = false WHERE id = %s", (kpi.id,))
        kpi.invalidate_recordset(["individual_outcome"])
        self.env.transaction.invalidate_ormcache()
        return kpi, target, evaluation

    def test_business_kpi_cannot_get_an_employee_target(self):
        kpi = self._mk_kpi("OWN_BUSINESS")
        values = {"kpi_id": kpi.id, "date_start": "2026-10-01", "date_end": "2026-10-31", "target_value": 10.0}
        with self.assertRaisesRegex(ValidationError, "not an individual employee KPI"):
            self.env["strategic.kpi.target"].with_user(self.head_user).create(dict(values, scope="user", user_id=self.emp_user.id))
        organization = self.env["strategic.kpi.target"].with_user(self.head_user).create(dict(values, scope="organization", state="approved"))
        self.assertEqual(organization.scope, "organization", "the KPI stays usable at its own level")
        branch = self.env["strategic.kpi.target"].with_user(self.head_user).create(dict(values, scope="branch", warehouse_id=self.branch_user_a.strategic_branch_warehouse_id.id))
        self.assertEqual(branch.scope, "branch")

    def test_individual_ownership_needs_a_manual_kpi_and_no_stranded_targets(self):
        kpi = self._mk_kpi("OWN_FLAG", individual=True)
        self.env["strategic.kpi.target"].with_user(self.head_user).create({
            "kpi_id": kpi.id, "date_start": "2026-10-01", "date_end": "2026-10-31", "scope": "user", "user_id": self.emp_user.id, "target_value": 10.0, "state": "approved"})
        with self.assertRaisesRegex(ValidationError, "Archive the employee targets"):
            kpi.individual_outcome = False
        kpi.flush_recordset()

    def test_legacy_business_result_is_not_a_personal_result_anywhere(self):
        _kpi, target, evaluation = self._legacy_employee_target()
        Service = self.env["strategic.performance.dashboard"]
        self.assertEqual(Service.with_user(self.emp_user).get_employee_data("2026-10-01", "2026-10-31")["kpis"], [])
        self.assertEqual(self.env["strategic.kpi.evaluation"].with_user(self.emp_user).search_count([("id", "=", evaluation.id)]), 0, "not even by a direct request")
        self.assertEqual(self.env["strategic.kpi.target"].with_user(self.emp_user).search_count([("id", "=", target.id)]), 0)
        self.assertEqual(self.env["strategic.kpi.evaluation"].with_user(self.head_user).search_count([("id", "=", evaluation.id)]), 1, "kept for audit")

    def test_migration_archives_with_a_note_and_keeps_history(self):
        import importlib.util
        import os
        kpi, target, evaluation = self._legacy_employee_target("OWN_MIGRATE")
        _ik, kept = self._make_user_target_evaluation(self.emp_user, "OWN_REAL")[0].kpi_id, self._make_user_target_evaluation(self.emp_user, "OWN_REAL2")[0]
        path = os.path.join(os.path.dirname(__file__), "..", "migrations", "20.0.1.8.0", "post-migration.py")
        spec = importlib.util.spec_from_file_location("sm_ownership_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.migrate(self.env.cr, "20.0.1.7.0")
        self.assertEqual(target.state, "archived")
        self.assertTrue(evaluation.exists())
        self.assertIn("ownership correction", target.message_ids[0].body)
        self.assertEqual(kept.state, "approved", "a real individual KPI is untouched")

    def test_employee_reads_no_organization_baseline_or_definition(self):
        kpi = self._mk_kpi("OWN_ORG")
        self.env["strategic.kpi.target"].with_user(self.head_user).create({
            "kpi_id": kpi.id, "date_start": "2026-10-01", "date_end": "2026-10-31", "scope": "organization", "target_value": 10.0, "state": "approved"})
        self.assertEqual(self.env["strategic.kpi.definition"].with_user(self.emp_user).search_count([("id", "=", kpi.id)]), 0)
        self.assertEqual(self.env["strategic.kpi.target"].with_user(self.emp_user).search_count([("kpi_id", "=", kpi.id)]), 0)
        self.assertEqual(self.env["strategic.kpi.baseline"].with_user(self.emp_user).search_count([("scope", "=", "organization")]), 0)
