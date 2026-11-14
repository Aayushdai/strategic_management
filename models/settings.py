"""Settings: the switches and numbers that change how the app behaves, kept per company. Head Office only.

Each setting replaces a value that used to be fixed in the code; its default is that old value. Every change is
written to the admin audit log. Turning a feature off never deletes what it already created.
"""

from datetime import datetime, time, timedelta

import pytz

from odoo import api, fields, models
from odoo.addons.base.models.ir_mail_server import MailDeliveryException
from odoo.exceptions import AccessError, UserError
from odoo.tools import email_normalize

# key: (kind, label, minimum, maximum). Booleans have no range.
SETTINGS = {
    "sm_almost_met_pct": ("float", '"Almost met" starts at (% of target)', 1.0, 99.0),
    "sm_monthly_reports": ("bool", "Monthly branch reports", None, None),
    "sm_report_submit_days": ("int", "Days to submit a monthly report", 1, 28),
    "sm_due_soon_reminders": ("bool", "Objective due-soon reminders", None, None),
    "sm_due_soon_days": ("int", "Due-soon warning (days before)", 1, 60),
    "sm_due_result_notes": ("bool", "Due-date result notes", None, None),
    "sm_achievement_reports": ("bool", "Achievement reports", None, None),
    "sm_company_reports": ("bool", "Monthly company report", None, None),
    "sm_nightly_hour": ("int", "Nightly update hour", 0, 23),
    "sm_expiring_days": ("int", "Expiring stock window (days)", 1, 90),
    "sm_late_critical_days": ("int", "Late delivery is critical after (days)", 1, 60),
    "sm_bm_can_propose": ("bool", "Branch Managers can propose objectives", None, None),
    "sm_live_refresh": ("bool", "Live page refresh", None, None),
}
_HEAD = "strategic_management.group_strategic_head_office_manager"
_CRON = "strategic_management.ir_cron_strategic_kpi_scheduled_jobs"
_MAIL_SERVER_NAME = "Outgoing email (Settings)"
_ENCRYPTIONS = {"none": "None", "starttls": "STARTTLS", "ssl": "SSL/TLS"}


class ResCompanyStrategicSettings(models.Model):
    _inherit = "res.company"

    sm_almost_met_pct = fields.Float('"Almost met" starts at (%)', default=80.0)
    sm_monthly_reports = fields.Boolean("Monthly branch reports", default=True)
    sm_report_submit_days = fields.Integer("Days to submit a monthly report", default=7)
    sm_due_soon_reminders = fields.Boolean("Objective due-soon reminders", default=True)
    sm_due_soon_days = fields.Integer("Due-soon warning (days)", default=3)
    sm_due_result_notes = fields.Boolean("Due-date result notes", default=True)
    sm_achievement_reports = fields.Boolean("Achievement reports", default=True)
    sm_company_reports = fields.Boolean("Monthly company report", default=True)
    sm_nightly_hour = fields.Integer("Nightly update hour", default=22)
    sm_expiring_days = fields.Integer("Expiring stock window (days)", default=7)
    sm_late_critical_days = fields.Integer("Late delivery critical after (days)", default=7)
    sm_bm_can_propose = fields.Boolean("Branch Managers can propose objectives", default=True)
    sm_live_refresh = fields.Boolean("Live page refresh", default=True)


class StrategicPerformanceDashboardSettings(models.AbstractModel):
    _inherit = "strategic.performance.dashboard"

    @api.model
    def _require_settings_access(self):
        if not self.env.user.has_group(_HEAD):
            raise AccessError("Settings are changed by Head Office.")

    @api.model
    def get_strategic_settings(self):
        self._require_settings_access()
        company = self.env.company
        servers = self.env["ir.mail_server"].sudo().search([("active", "=", True)])
        server = servers[:1]
        cron = self.env.ref(_CRON, raise_if_not_found=False)
        return {
            "values": {key: company[key] for key in SETTINGS},
            "company": {
                "name": company.name or "", "street": company.street or "", "city": company.city or "", "zip": company.zip or "",
                "country_id": company.country_id.id or False, "phone": company.phone or "", "email": company.email or "",
                "currency": company.currency_id.name, "tz": company.partner_id.tz or "", "has_logo": bool(company.logo),
            },
            "countries": [{"id": country.id, "name": country.name} for country in self.env["res.country"].sudo().search([], order="name")],
            "timezones": [tz for tz in pytz.common_timezones],
            # Ready: a server that can sign in (a username without a password is refused by providers such as Gmail).
            "email": {"configured": bool(servers), "ready": bool(server) and (bool(server.smtp_pass) or not server.smtp_user),
                      "failed": self.env["mail.mail"].sudo().search_count([("state", "=", "exception")]),
                      "servers": servers.mapped("name"), "company_email": company.email or "",
                      # The password never goes back to the browser; only whether one is saved.
                      "server": {"host": server.smtp_host or "", "port": server.smtp_port or 587,
                                 "encryption": server.smtp_encryption or "starttls", "user": server.smtp_user or "",
                                 "has_password": bool(server.smtp_pass)}},
            "next_run": fields.Datetime.to_string(cron.sudo().nextcall) if cron else False,
        }

    @api.model
    def save_strategic_settings(self, values):
        """Save the switches and numbers; only known keys, each checked against its range."""
        self._require_settings_access()
        company = self.env.company
        updates, changes = {}, []
        for key, raw in (values or {}).items():
            if key not in SETTINGS:
                continue
            kind, label, low, high = SETTINGS[key]
            if kind == "bool":
                value = bool(raw)
            else:
                try:
                    value = int(raw) if kind == "int" else float(raw)
                except (TypeError, ValueError):
                    raise UserError("%s: enter a number." % label)
                if not low <= value <= high:
                    raise UserError("%s: enter a number from %s to %s." % (label, low, high))
            if value != company[key]:
                updates[key] = value
                shown = ("On" if value else "Off") if kind == "bool" else value
                changes.append("%s: %s" % (label, shown))
        if updates:
            company.sudo().write(updates)
            if "sm_nightly_hour" in updates:
                self._reschedule_nightly_job(company)
            for change in changes:
                self.env["strategic.admin.console"]._audit("settings", detail=change)
        return self.get_strategic_settings()

    @api.model
    def save_company_profile(self, values):
        """Name, address, contact, time zone and logo: what printed reports show."""
        self._require_settings_access()
        company = self.env.company
        name = (values.get("name") or "").strip()
        if not name:
            raise UserError("Enter the company name.")
        updates = {"name": name, "street": (values.get("street") or "").strip() or False, "city": (values.get("city") or "").strip() or False,
                   "zip": (values.get("zip") or "").strip() or False, "phone": (values.get("phone") or "").strip() or False,
                   "email": (values.get("email") or "").strip() or False}
        country = self.env["res.country"].browse(int(values.get("country_id") or 0)).exists()
        updates["country_id"] = country.id or False
        if "logo" in values:
            updates["logo"] = values["logo"] or False
        tz = values.get("tz") or False
        if tz and tz not in pytz.all_timezones:
            raise UserError("Choose a time zone from the list.")
        company.sudo().write(updates)
        company.partner_id.sudo().write({"tz": tz})
        self.env["strategic.admin.console"]._audit("settings", detail="Company details updated")
        if tz:
            self._reschedule_nightly_job(company)
        return self.get_strategic_settings()

    @api.model
    def get_my_settings(self):
        """Branch Managers and Employees: their own account (Settings shows this instead of the data setup)."""
        user = self.env.user
        server = self.env["ir.mail_server"].sudo().search([("active", "=", True)], limit=1)
        return {
            "name": user.name, "login": user.login, "email": user.email or "", "phone": user.phone or "", "tz": user.tz or "",
            "notify_by_email": user.notification_type == "email",
            "email_ready": bool(server) and (bool(server.smtp_pass) or not server.smtp_user),
            "timezones": list(pytz.common_timezones),
        }

    @api.model
    def save_my_settings(self, values):
        """Only fields Odoo lets every user change on their own account; written as the user, not as superuser."""
        values = values or {}
        email = (values.get("email") or "").strip()
        if email and not email_normalize(email):
            raise UserError("Enter a valid email address.")
        tz = values.get("tz") or False
        if tz and tz not in pytz.all_timezones:
            raise UserError("Choose a time zone from the list.")
        notify_by_email = bool(values.get("notify_by_email"))
        if notify_by_email and not email:
            raise UserError("Add your email address to get notifications by email.")
        self.env.user.write({"email": email or False, "phone": (values.get("phone") or "").strip() or False, "tz": tz,
                             "notification_type": "email" if notify_by_email else "inbox"})
        return self.get_my_settings()

    @api.model
    def save_mail_server(self, values):
        """The outgoing mail server every notification and reminder is emailed through. A blank password keeps the saved one."""
        self._require_settings_access()
        values = values or {}
        host = (values.get("host") or "").strip()
        user = (values.get("user") or "").strip()
        encryption = values.get("encryption") or "starttls"
        try:
            port = int(values.get("port"))
        except (TypeError, ValueError):
            raise UserError("Port: enter a number, such as 587.")
        if not host:
            raise UserError("Enter the mail server, such as smtp.gmail.com.")
        if not 1 <= port <= 65535:
            raise UserError("Port: enter a number from 1 to 65535.")
        if encryption not in _ENCRYPTIONS:
            raise UserError("Choose the security type from the list.")
        Server = self.env["ir.mail_server"].sudo()
        server = Server.search([("active", "=", True)], limit=1)
        updates = {"smtp_host": host, "smtp_port": port, "smtp_encryption": encryption, "smtp_user": user or False,
                   "smtp_authentication": "login"}
        password = values.get("password") or ""
        if password:
            updates["smtp_pass"] = password
        if user and "@" in user:
            # Providers such as Gmail only send as the signed-in address, so mail goes out from it.
            updates["from_filter"] = user
        if server:
            server.write(updates)
        else:
            Server.create({"name": _MAIL_SERVER_NAME, **updates})
        self.env["strategic.admin.console"]._audit(
            "settings", detail="Email server set: %s:%s (%s)%s" % (host, port, _ENCRYPTIONS[encryption], ", new password" if password else ""))
        return self.get_strategic_settings()

    @api.model
    def send_test_email(self):
        """Send a real email to the signed-in Head Office user through the saved server; a failure says why."""
        self._require_settings_access()
        server = self.env["ir.mail_server"].sudo().search([("active", "=", True)], limit=1)
        if not server:
            raise UserError("Save the email server first.")
        to = self.env.user.email
        if not to:
            raise UserError("Your user has no email address to send the test to.")
        sender = server.from_filter if server.from_filter and "@" in server.from_filter else (self.env.company.email or server.smtp_user)
        mail = self.env["mail.mail"].sudo().create({
            "subject": "Test email from %s" % self.env.company.name, "email_from": sender, "email_to": to,
            "body_html": "<p>Email is working. Notifications, reminders and reports from Strategic Management will arrive here.</p>",
            "mail_server_id": server.id, "auto_delete": True,
        })
        try:
            mail.send(raise_exception=True)
        except (MailDeliveryException, OSError) as error:  # carries the provider's reason (bad password, blocked port)
            raise UserError("The test email could not be sent: %s" % error) from error
        return {"sent_to": to}

    @api.model
    def _reschedule_nightly_job(self, company):
        """Move the nightly job to the chosen hour in the company's time zone (its next occurrence)."""
        cron = self.env.ref(_CRON, raise_if_not_found=False)
        if not cron:
            return
        zone = pytz.timezone(company.partner_id.tz or "UTC")
        now = datetime.now(zone)
        run = zone.localize(datetime.combine(now.date(), time(hour=company.sm_nightly_hour)))
        if run <= now:
            run = zone.localize(datetime.combine(now.date() + timedelta(days=1), time(hour=company.sm_nightly_hour)))
        cron.sudo().write({"nextcall": run.astimezone(pytz.UTC).replace(tzinfo=None)})
