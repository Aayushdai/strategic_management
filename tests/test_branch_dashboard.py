"""Regression tests for the Branch Manager performance dashboard."""
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import tagged

from .test_strategy import StrategyFixture


@tagged("post_install", "-at_install")
class TestBranchManagerDashboard(StrategyFixture):
    def test_branch_service_has_implemented_data_method(self):
        service = self.env["strategic.performance.dashboard"]
        self.assertTrue(
            hasattr(service, "get_branch_data"),
            "The Branch Manager dashboard data contract must be implemented.",
        )

    def test_dashboard_rejects_non_branch_manager(self):
        service = self.env["strategic.performance.dashboard"].with_user(self.head_user)
        with self.assertRaises(AccessError):
            service.get_branch_data("2026-10-01", "2026-10-31")

    def test_dashboard_rejects_branch_manager_without_assigned_warehouse(self):
        user = self._mk_user("p2_branch_dashboard_no_warehouse", self.g_branch, None)
        service = self.env["strategic.performance.dashboard"].with_user(user)
        with self.assertRaises(AccessError):
            service.get_branch_data("2026-10-01", "2026-10-31")

    def test_dashboard_rejects_reversed_reporting_period(self):
        service = self.env["strategic.performance.dashboard"].with_user(self.branch_user_a)
        with self.assertRaisesRegex(ValidationError, "start date cannot be after"):
            service.get_branch_data("2026-10-31", "2026-10-01")

    def test_payload_has_branch_context_and_stable_sections(self):
        data = self.env["strategic.performance.dashboard"].with_user(
            self.branch_user_a
        ).get_branch_data("2026-10-01", "2026-10-31")
        self.assertEqual(data["branch"]["warehouse_id"], self.wh1.id)
        self.assertEqual(data["branch"]["name"], self.wh1.display_name)
        self.assertEqual(data["period"], {"start": "2026-10-01", "end": "2026-10-31"})
        self.assertIn("average_attainment", data["kpi_summary"])
        self.assertIn("active_count", data["objective_summary"])
        self.assertIsInstance(data["monthly_trend"], list)
        self.assertIsInstance(data["objectives"], list)
        self.assertIsInstance(data["kpis"], list)
        self.assertIn("kpi_exceptions", data["attention"])

    def test_branch_objectives_are_filtered_to_assigned_warehouse(self):
        plan = self._mk_plan()
        parent = self._mk_org_obj(plan)
        parent.action_submit()
        parent.with_user(self.head_user).action_approve()
        objective_a = self._mk_branch_obj(plan, parent, warehouse=self.wh1)
        objective_a.with_user(self.branch_user_a).action_submit()
        objective_a.with_user(self.head_user).action_approve()
        objective_b = self._mk_branch_obj(plan, parent, warehouse=self.wh2)
        objective_b.with_user(self.branch_user_b).action_submit()
        objective_b.with_user(self.head_user).action_approve()

        data = self.env["strategic.performance.dashboard"].with_user(
            self.branch_user_a
        ).get_branch_data("2026-10-01", "2026-10-31")
        returned_ids = {row["id"] for row in data["objectives"]}
        self.assertIn(objective_a.id, returned_ids)
        self.assertNotIn(objective_b.id, returned_ids)

    def test_branch_dashboard_menu_uses_custom_client_action(self):
        action = self.env.ref("strategic_management.action_strategic_branch_dashboard")
        menu = self.env.ref("strategic_management.menu_strategic_kpi_dashboard_branch")
        self.assertEqual(action.tag, "strategic_management.branch_dashboard")
        self.assertEqual(menu.action, action)
