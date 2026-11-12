"""Automatic period-end reports and objective due-date reminders (models/period_reports.py), each sent once."""

from datetime import date

from odoo.tests.common import tagged

from .test_strategy import StrategyFixture


@tagged("post_install", "-at_install")
class TestPeriodReports(StrategyFixture):
    def _todos(self, record, prefix):
        return self.env["mail.activity"].sudo().search([("res_model", "=", record._name), ("res_id", "=", record.id), ("summary", "=like", prefix + "%")])

    def test_monthly_branch_report_opens_once_for_the_branch_manager(self):
        Periods = self.env["strategic.period.reports"]
        Report = self.env["strategic.branch.report"].sudo()
        june = [("warehouse_id", "=", self.wh1.id), ("date_start", "=", "2026-06-01"), ("date_end", "=", "2026-06-30")]
        Periods._open_monthly_branch_reports(date(2026, 7, 2))
        Periods._open_monthly_branch_reports(date(2026, 7, 3))
        report = Report.search(june)
        self.assertEqual((len(report), report.state), (1, "draft"), "one draft per branch and month, however often the job runs")
        self.assertEqual(self._todos(report, "Submit branch report").user_id, self.branch_user_a)
        Periods._open_monthly_branch_reports(date(2026, 8, 20))
        self.assertFalse(Report.search_count([("warehouse_id", "=", self.wh1.id), ("date_start", "=", "2026-07-01")]), "not opened late, after the first week")
        # The manager submits it as any other report: the snapshot is taken then.
        report.with_user(self.branch_user_a).action_submit("Good month.")
        self.assertEqual(report.state, "submitted")

    def test_objective_due_soon_and_due_date_result_once(self):
        Periods = self.env["strategic.period.reports"]
        objective = self._mk_org_obj(self._mk_plan())
        objective.with_user(self.head_user).action_activate()  # deadline 2026-12-31, owner Head Office
        Periods._objective_due_dates(date(2026, 12, 20))
        self.assertFalse(self._todos(objective, "Objective due soon"), "nothing until three days before")
        Periods._objective_due_dates(date(2026, 12, 29))
        Periods._objective_due_dates(date(2026, 12, 30))
        soon = self._todos(objective, "Objective due soon")
        self.assertEqual((len(soon), soon.user_id), (1, objective.owner_id))
        soon.action_done()
        Periods._objective_due_dates(date(2026, 12, 31))
        self.assertFalse(self._todos(objective, "Objective due soon"), "a reminder marked done does not come back")

        messages = len(objective.message_ids)
        Periods._objective_due_dates(date(2027, 1, 1))
        Periods._objective_due_dates(date(2027, 1, 2))
        self.assertEqual(len(objective.message_ids), messages + 1, "the result is written once")
        self.assertIn("Due date reached", objective.message_ids[0].body)
        self.assertIn("Not Evaluated", objective.message_ids[0].body)
        self.assertTrue(self._todos(objective, "Objective reached its due date").user_id & self.head_user)
        self.assertEqual(objective.state, "active", "Head Office decides; nothing is completed automatically")
