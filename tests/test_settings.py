"""Settings: Head Office switches and numbers, audited; each switch really stops its feature and deletes nothing."""

from datetime import date

import pytz

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import tagged

from .test_strategy import StrategyFixture


@tagged("post_install", "-at_install")
class TestSettings(StrategyFixture):
    def _save(self, **values):
        return self.env["strategic.performance.dashboard"].with_user(self.head_user).save_strategic_settings(values)

    def test_head_office_only_validated_and_audited(self):
        Dashboard = self.env["strategic.performance.dashboard"]
        data = Dashboard.with_user(self.head_user).get_strategic_settings()
        self.assertEqual(data["values"]["sm_almost_met_pct"], 80.0, "defaults are the old fixed values")
        self.assertEqual((data["values"]["sm_due_soon_days"], data["values"]["sm_expiring_days"]), (3, 7))
        for user in (self.branch_user_a, self.emp_user):
            with self.assertRaises(AccessError):
                Dashboard.with_user(user).get_strategic_settings()
            with self.assertRaises(AccessError):
                Dashboard.with_user(user).save_strategic_settings({"sm_live_refresh": False})
        with self.assertRaises(UserError):
            self._save(sm_almost_met_pct=150)
        with self.assertRaises(UserError):
            self._save(sm_due_soon_days="soon")
        audit = self.env["strategic.admin.audit"].sudo()
        before = audit.search_count([("action", "=", "settings")])
        data = self._save(sm_expiring_days=10, sm_live_refresh=False, unknown_key=True)
        self.assertEqual((data["values"]["sm_expiring_days"], data["values"]["sm_live_refresh"]), (10, False))
        self.assertEqual(audit.search_count([("action", "=", "settings")]), before + 2, "one audit line per change")
        self.assertFalse(Dashboard.with_user(self.branch_user_a).get_navigation_context()["live_refresh"])

    def test_almost_met_threshold_and_nightly_hour(self):
        Evaluation = self.env["strategic.kpi.evaluation"]
        self.assertEqual(Evaluation._attainment_status(60.0), "not_achieved")
        self._save(sm_almost_met_pct=50)
        self.assertEqual(Evaluation._attainment_status(60.0), "almost_met")
        self.company.partner_id.sudo().tz = "Asia/Kathmandu"
        self._save(sm_nightly_hour=3)
        cron = self.env.ref("strategic_management.ir_cron_strategic_kpi_scheduled_jobs").sudo()
        local = pytz.UTC.localize(cron.nextcall).astimezone(pytz.timezone("Asia/Kathmandu"))
        self.assertEqual((local.hour, local.minute), (3, 0))

    def test_switches_stop_their_feature_and_delete_nothing(self):
        Periods = self.env["strategic.period.reports"]
        Report = self.env["strategic.branch.report"].sudo()
        june = [("warehouse_id", "=", self.wh1.id), ("date_start", "=", "2026-06-01")]
        Periods._open_monthly_branch_reports(date(2026, 7, 2))
        self.assertEqual(Report.search_count(june), 1)
        self._save(sm_monthly_reports=False)
        Periods._open_monthly_branch_reports(date(2026, 8, 2))
        self.assertFalse(Report.search_count([("warehouse_id", "=", self.wh1.id), ("date_start", "=", "2026-07-01")]))
        self.assertEqual(Report.search_count(june), 1, "turning it off deletes nothing")

        objective = self._mk_org_obj(self._mk_plan())
        objective.with_user(self.head_user).action_activate()  # due 2026-12-31
        self._save(sm_due_soon_days=10)
        Periods._objective_due_dates(date(2026, 12, 22))
        self.assertEqual(objective.due_soon_sent_for, objective.deadline, "reminded 9 days before, within the 10 set")
        self._save(sm_due_result_notes=False)
        messages = len(objective.message_ids)
        Periods._objective_due_dates(date(2027, 1, 2))
        self.assertEqual(len(objective.message_ids), messages, "no due-date note while it is off")

        self._save(sm_bm_can_propose=False)
        with self.assertRaisesRegex(UserError, "turned off"):
            self.env["strategic.goal.setup"].with_user(self.branch_user_a).create_goal(
                {"name": "More lattes", "business_area": "sales_finance", "deadline": "2026-12-31", "scope": "branch"})
        self.assertFalse(self.env["strategic.performance.dashboard"].with_user(self.branch_user_a).get_navigation_context()["can_propose"])

    def test_achievement_reports_switch(self):
        self._save(sm_achievement_reports=False)
        objective = self._mk_org_obj(self._mk_plan())
        self._add_valid_required_kr(objective, "NOREPORT")
        objective.action_submit()
        objective.with_user(self.head_user).action_approve()
        objective.with_user(self.head_user).action_complete()
        self.assertEqual(objective.state, "completed")
        self.assertFalse(self.env["strategic.achievement.report"].search([("objective_id", "=", objective.id)]))

    def test_company_profile(self):
        Dashboard = self.env["strategic.performance.dashboard"].with_user(self.head_user)
        with self.assertRaises(UserError):
            Dashboard.save_company_profile({"name": " "})
        with self.assertRaises(UserError):
            Dashboard.save_company_profile({"name": "Cafe Chain", "tz": "Mars/Base"})
        data = Dashboard.save_company_profile({"name": "Cafe Chain", "city": "Kathmandu", "tz": "Asia/Kathmandu"})
        self.assertEqual((data["company"]["name"], data["company"]["city"], data["company"]["tz"]), ("Cafe Chain", "Kathmandu", "Asia/Kathmandu"))

    def test_email_server_head_office_only_password_never_returned(self):
        Dashboard = self.env["strategic.performance.dashboard"]
        self.env["ir.mail_server"].sudo().search([]).active = False
        for user in (self.branch_user_a, self.emp_user):
            with self.assertRaises(AccessError):
                Dashboard.with_user(user).save_mail_server({"host": "smtp.example.com", "port": 587})
            with self.assertRaises(AccessError):
                Dashboard.with_user(user).send_test_email()
        head = Dashboard.with_user(self.head_user)
        with self.assertRaises(UserError):
            head.send_test_email()  # nothing saved yet
        with self.assertRaises(UserError):
            head.save_mail_server({"host": " ", "port": 587})
        with self.assertRaises(UserError):
            head.save_mail_server({"host": "smtp.example.com", "port": "abc"})
        with self.assertRaises(UserError):
            head.save_mail_server({"host": "smtp.example.com", "port": 587, "encryption": "rot13"})

        data = head.save_mail_server({"host": "smtp.example.com", "port": "587", "encryption": "starttls",
                                      "user": "cafe@example.com", "password": "app-secret"})
        server = self.env["ir.mail_server"].sudo().search([("active", "=", True)])
        self.assertEqual(len(server), 1)
        self.assertEqual((server.smtp_host, server.smtp_port, server.smtp_pass, server.from_filter),
                         ("smtp.example.com", 587, "app-secret", "cafe@example.com"))
        self.assertTrue(data["email"]["configured"] and data["email"]["ready"])
        self.assertIn("failed", data["email"])
        self.assertNotIn("app-secret", str(data), "the password never goes back to the browser")
        self.assertTrue(data["email"]["server"]["has_password"])

        head.save_mail_server({"host": "smtp.example.com", "port": 465, "encryption": "ssl", "user": "cafe@example.com", "password": ""})
        self.assertEqual((server.smtp_port, server.smtp_pass), (465, "app-secret"), "a blank password keeps the saved one")
        audit = self.env["strategic.admin.audit"].sudo().search([("action", "=", "settings")])
        self.assertFalse(any("app-secret" in (line.detail or "") for line in audit))

        self.head_user.email = False
        with self.assertRaises(UserError):
            head.send_test_email()
        self.head_user.email = "head@example.com"
        self.assertEqual(head.send_test_email(), {"sent_to": "head@example.com"})  # test mode: built and handed over, not sent

    def test_my_settings_own_account_only(self):
        Dashboard = self.env["strategic.performance.dashboard"]
        for user in (self.branch_user_a, self.emp_user):
            mine = Dashboard.with_user(user)
            data = mine.get_my_settings()
            self.assertEqual(data["login"], user.login)
            with self.assertRaises(UserError):
                mine.save_my_settings({"email": "not an email"})
            with self.assertRaises(UserError):
                mine.save_my_settings({"email": "a@example.com", "tz": "Mars/Base"})
            with self.assertRaises(UserError):
                mine.save_my_settings({"email": "", "notify_by_email": True})
            data = mine.save_my_settings({"email": "new.%s@example.com" % user.login, "phone": "9800000000",
                                          "tz": "Asia/Kathmandu", "notify_by_email": False, "login": "hijack", "group_ids": []})
            user.invalidate_recordset()
            self.assertEqual((user.email, user.phone, user.tz, user.notification_type),
                             ("new.%s@example.com" % user.login, "9800000000", "Asia/Kathmandu", "inbox"))
            self.assertNotEqual(user.login, "hijack", "only the account's own contact fields can change")
            self.assertTrue(user.has_group("base.group_user"))
            self.assertFalse(data["notify_by_email"])
        self.assertNotEqual(self.emp_user.email, self.branch_user_a.email)
