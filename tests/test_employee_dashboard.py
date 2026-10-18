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
        kpi = self._mk_kpi(code)
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

    def test_employee_dashboard_menu_uses_custom_client_action(self):
        action = self.env.ref("strategic_management.action_strategic_employee_dashboard")
        menu = self.env.ref("strategic_management.menu_strategic_kpi_dashboard_employee")
        self.assertEqual(action.tag, "strategic_management.employee_dashboard")
        self.assertEqual(menu.action, action)
