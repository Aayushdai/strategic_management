"""Achievement reports: completing an objective freezes what was achieved, tells its owner, and can be printed."""

from odoo.tests.common import tagged
from odoo.tools import format_amount

from .test_strategy import StrategyFixture


@tagged("post_install", "-at_install")
class TestAchievementReports(StrategyFixture):
    def test_completing_an_objective_freezes_and_reports_its_results(self):
        objective = self._mk_org_obj(self._mk_plan())
        objective.write({"name": "Cut coffee waste"})
        kr = self._add_valid_required_kr(objective, "ACHIEVE")  # target 100, actual 100
        objective.action_submit()
        objective.with_user(self.head_user).action_approve()
        Report = self.env["strategic.achievement.report"]
        self.assertFalse(Report.search([("objective_id", "=", objective.id)]), "nothing until it is completed")

        objective.with_user(self.head_user).action_complete()
        report = Report.search([("objective_id", "=", objective.id)])
        self.assertEqual(len(report), 1)
        row = report.snapshot["key_results"][0]
        self.assertEqual((row["name"], row["target"], row["actual"], row["attainment"]), (kr.name, 100.0, 100.0, 100.0))
        self.assertEqual(report.completed_by_id, self.head_user)
        self.assertIn("%", report.result)
        self.assertIn(kr.name, report.summary_html)
        notice = report.message_ids.filtered(lambda message: "was completed" in (message.body or ""))
        self.assertIn(objective.owner_id.partner_id, notice.partner_ids, "the owner is told")


        # Head Office and the owner read it; another branch's manager does not.
        self.assertTrue(Report.with_user(self.head_user).search_count([("id", "=", report.id)]))
        self.assertFalse(Report.with_user(self.branch_user_a).search_count([("id", "=", report.id)]))

        html = self.env["ir.actions.report"]._render_qweb_html("strategic_management.report_achievement_document", report.ids)[0]
        self.assertIn(b"Cut coffee waste", html)

    def test_submitted_branch_report_prints(self):
        report = self.env["strategic.branch.report"].with_user(self.branch_user_a).create({"date_start": "2026-06-01", "date_end": "2026-06-30"})
        report.with_user(self.branch_user_a).action_submit("A good month.")
        html = self.env["ir.actions.report"]._render_qweb_html("strategic_management.report_branch_report_document", report.ids)[0]
        self.assertIn(b"A good month.", html)
        # Money is shown in the company currency; counts stay plain numbers.
        self.assertEqual(report._summary_value("net_sales", 34.0), format_amount(self.env, 34.0, report.company_id.currency_id))
        self.assertEqual((report._summary_value("orders", 5), report._summary_value("net_sales", False)), (5, "Not available"))
        self.assertIn(report.warehouse_id.display_name.encode(), html)
