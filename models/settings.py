"""Settings: the switches and numbers that change how the app behaves, kept per company. Head Office only.

Each setting replaces a value that used to be fixed in the code; its default is that old value. Every change is
written to the admin audit log. Turning a feature off never deletes what it already created.
"""

from datetime import datetime, time, timedelta

import pytz

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

# key: (kind, label, minimum, maximum). Booleans have no range.
SETTINGS = {
    "sm_almost_met_pct": ("float", '"Almost met" starts at (% of target)', 1.0, 99.0),
    "sm_monthly_reports": ("bool", "Monthly branch reports", None, None),
    "sm_report_submit_days": ("int", "Days to submit a monthly report", 1, 28),
    "sm_due_soon_reminders": ("bool", "Objective due-soon reminders", None, None),
    "sm_due_soon_days": ("int", "Due-soon warning (days before)", 1, 60),
    "sm_due_result_notes": ("bool", "Due-date result notes", None, None),
    "sm_achievement_reports": ("bool", "Achievement reports", None, None),
    "sm_nightly_hour": ("int", "Nightly update hour", 0, 23),
    "sm_expiring_days": ("int", "Expiring stock window (days)", 1, 90),
    "sm_late_critical_days": ("int", "Late delivery is critical after (days)", 1, 60),
    "sm_bm_can_propose": ("bool", "Branch Managers can propose objectives", None, None),
    "sm_live_refresh": ("bool", "Live page refresh", None, None),
}
_HEAD = "strategic_management.group_strategic_head_office_manager"
_CRON = "strategic_management.ir_cron_strategic_kpi_scheduled_jobs"


class ResCompanyStrategicSettings(models.Model):
    _inherit = "res.company"

    sm_almost_met_pct = fields.Float('"Almost met" starts at (%)', default=80.0)
    sm_monthly_reports = fields.Boolean("Monthly branch reports", default=True)
    sm_report_submit_days = fields.Integer("Days to submit a monthly report", default=7)
    sm_due_soon_reminders = fields.Boolean("Objective due-soon reminders", default=True)
    sm_due_soon_days = fields.Integer("Due-soon warning (days)", default=3)
    sm_due_result_notes = fields.Boolean("Due-date result notes", default=True)
    sm_achievement_reports = fields.Boolean("Achievement reports", default=True)
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
            "email": {"configured": bool(servers), "servers": servers.mapped("name"), "company_email": company.email or ""},
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
