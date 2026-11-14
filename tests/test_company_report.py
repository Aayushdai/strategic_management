"""Monthly company report: made once per month for Head Office, read as Head Office, sent with its PDF, never shown to others."""

from datetime import date

from odoo.exceptions import AccessError
from odoo.tests.common import tagged

from .test_strategy import StrategyFixture


@tagged("post_install", "-at_install")
class TestCompanyReport(StrategyFixture):
    def _reports(self, start="2026-06-01"):
        return self.env["strategic.company.report"].sudo().search([("company_id", "=", self.company.id), ("date_start", "=", start)])

    def test_made_once_in_the_first_days_and_sent_to_head_office(self):
        Periods = self.env["strategic.period.reports"]
        self.assertEqual(Periods._open_monthly_company_reports(date(2026, 7, 9)), 0, "only in the first days of the month")
        self.assertFalse(self._reports())
        self.assertEqual(Periods._open_monthly_company_reports(date(2026, 7, 2)), 1)
        self.assertEqual(Periods._open_monthly_company_reports(date(2026, 7, 3)), 0, "once per month")
        report = self._reports()
        self.assertEqual(len(report), 1)
        self.assertEqual((report.date_start, report.date_end), (date(2026, 6, 1), date(2026, 6, 30)))

        snap = report.snapshot
        names = [row["name"] for row in snap["branches"]]
        self.assertIn(self.wh1.display_name, names)
        self.assertIn(self.wh2.display_name, names)
        for key in ("net_sales", "orders", "waste_cost", "purchases", "overdue_deliveries"):
            self.assertIn(key, snap["totals"])
        self.assertIn(self.wh1.display_name, report.summary_html)
        self.assertLessEqual({row["status"] for row in snap["objectives"]["by_status"]}, {"on track", "close", "off track", "not measured yet"})

        note = report.message_ids.filtered(lambda message: "Monthly company report" in (message.body or ""))
        self.assertEqual(len(note), 1)
        self.assertIn(self.head_user.partner_id, note.partner_ids, "every Head Office user is told")
        self.assertNotIn(self.branch_user_a.partner_id, note.partner_ids)
        self.assertTrue(note.attachment_ids, "the printable report goes with it")

    def test_switch_off_makes_none(self):
        self.env["strategic.performance.dashboard"].with_user(self.head_user).save_strategic_settings({"sm_company_reports": False})
        self.assertEqual(self.env["strategic.period.reports"]._open_monthly_company_reports(date(2026, 7, 2)), 0)
        self.assertFalse(self._reports())

    def test_head_office_only(self):
        self.env["strategic.period.reports"]._open_monthly_company_reports(date(2026, 7, 2))
        report = self._reports()
        Report = self.env["strategic.company.report"]
        self.assertEqual(Report.with_user(self.head_user).search([("id", "=", report.id)]), report.with_user(self.head_user))
        for user in (self.branch_user_a, self.emp_user):
            with self.assertRaises(AccessError):
                Report.with_user(user).browse(report.id).read(["name"])
            with self.assertRaises(AccessError):
                Report.with_user(user).action_create_last_month()

    def test_head_office_creates_last_month_now(self):
        action = self.env["strategic.company.report"].with_user(self.head_user).action_create_last_month()
        report = self.env["strategic.company.report"].sudo().browse(action["res_id"])
        self.assertTrue(report.snapshot)
        again = self.env["strategic.company.report"].with_user(self.head_user).action_create_last_month()
        self.assertEqual(again["res_id"], report.id, "a second click opens the same report")
