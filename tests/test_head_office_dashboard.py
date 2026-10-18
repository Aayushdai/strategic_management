"""Regression tests for the Head Office performance dashboard."""

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import tagged

from .test_strategy import StrategyFixture


@tagged("post_install", "-at_install")
class TestHeadOfficeDashboard(StrategyFixture):
    """Dashboard payloads use real strategy data and retain Head Office scope."""

    def test_dashboard_service_is_registered(self):
        self.assertIn(
            "strategic.performance.dashboard",
            self.env.registry.models,
            "The Head Office dashboard data service must be registered.",
        )

    def test_dashboard_service_rejects_non_head_office_users(self):
        service = self.env["strategic.performance.dashboard"].with_user(
            self.branch_user_a
        )
        with self.assertRaises(AccessError):
            service.get_head_office_data("2026-10-01", "2026-10-31")

    def test_dashboard_service_rejects_reversed_reporting_period(self):
        service = self.env["strategic.performance.dashboard"].with_user(
            self.head_user
        )
        with self.assertRaisesRegex(ValidationError, "start date cannot be after"):
            service.get_head_office_data("2026-10-31", "2026-10-01")

    def test_dashboard_payload_has_stable_sections_and_explicit_empty_state(self):
        data = self.env["strategic.performance.dashboard"].with_user(
            self.head_user
        ).get_head_office_data("2026-10-01", "2026-10-31")

        self.assertEqual(data["period"], {
            "start": "2026-10-01",
            "end": "2026-10-31",
        })
        self.assertIn("average_attainment", data["kpi_summary"])
        self.assertIsInstance(data["kpi_summary"]["valid_evaluation_count"], int)
        self.assertIsInstance(data["kpi_summary"]["exception_count"], int)
        self.assertIn("active_count", data["objective_summary"])
        self.assertIn("overdue_count", data["objective_summary"])
        self.assertIsInstance(data["branch_comparison"], list)
        self.assertIsInstance(data["monthly_trend"], list)
        self.assertIsInstance(data["objectives"], list)
        self.assertIn("kpi_exceptions", data["attention"])
        self.assertIn("overdue_objectives", data["attention"])
        self.assertIn("evaluations_truncated", data["limits"])

    def test_employee_kpi_results_sync_to_head_office_by_assigned_branch(self):
        """The same employee evaluation shown to a branch manager is visible at Head Office."""
        self.emp_user.write({"strategic_branch_warehouse_id": self.wh1.id})
        kpi = self._mk_kpi("HO_EMPLOYEE_SYNC")
        target = self.env["strategic.kpi.target"].create({
            "kpi_id": kpi.id,
            "date_start": "2026-10-01",
            "date_end": "2026-10-31",
            "scope": "user",
            "user_id": self.emp_user.id,
            "target_value": 10.0,
            "state": "approved",
        })
        evaluation = self._mk_evaluation(target, actual=8.0, has_actual=True)

        head_data = self.env["strategic.performance.dashboard"].with_user(
            self.head_user
        ).get_head_office_data("2026-10-01", "2026-10-31")
        branch_data = self.env["strategic.performance.dashboard"].with_user(
            self.branch_user_a
        ).get_branch_data("2026-10-01", "2026-10-31")

        self.assertIn(
            "employee_kpi_results", head_data,
            "Head Office must expose employee-scoped KPI results as well as branch-level aggregates.",
        )
        head_row = next(
            (row for row in head_data["employee_kpi_results"] if row["id"] == evaluation.id),
            None,
        )
        branch_row = next(
            (row for row in branch_data["kpis"] if row["id"] == evaluation.id),
            None,
        )
        self.assertIsNotNone(head_row, "The employee KPI evaluation is missing from Head Office results.")
        self.assertIsNotNone(branch_row, "The same evaluation should be present in the assigned branch dashboard.")
        self.assertEqual(head_row["branch_warehouse_id"], self.wh1.id)
        self.assertEqual(head_row["branch_name"], self.wh1.display_name)
        self.assertEqual(head_row["employee_id"], self.emp_user.id)
        self.assertEqual(head_row["employee_name"], self.emp_user.name)
        self.assertEqual(head_row["actual_value"], branch_row["actual_value"])
        self.assertEqual(head_row["target_value"], branch_row["target_value"])
        self.assertEqual(head_row["attainment"], branch_row["attainment"])
        self.assertEqual(head_row["status"], branch_row["status"])
        self.assertIn("employee_kpi_summary", head_data)
        self.assertGreaterEqual(head_data["employee_kpi_summary"]["evaluation_count"], 1)
        self.assertGreaterEqual(head_data["employee_kpi_summary"]["valid_evaluation_count"], 1)
    def test_head_office_dashboard_menu_uses_the_custom_client_action(self):
        action = self.env.ref(
            "strategic_management.action_strategic_head_office_dashboard"
        )
        menu = self.env.ref(
            "strategic_management.menu_strategic_kpi_dashboard_head_office"
        )
        self.assertEqual(action.tag, "strategic_management.head_office_dashboard")
        self.assertEqual(menu.action, action)
