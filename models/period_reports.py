"""Automatic period-end work, run once a day by the KPI job (kpi_scheduler.py).

- When a month ends, each branch with a Branch Manager gets that month's branch report opened as a draft, and its
  managers a To-Do to add their comment and submit it. Submitting takes the snapshot and goes to Head Office review
  (branch_report.py), as for a report a manager opens themselves.
- An active objective gets a "due soon" To-Do for its owner a few days before its deadline, and once the deadline has
  passed its final result is written in its history and Head Office gets a To-Do to complete or close it.

Each step happens once: a report exists for the period, and an objective remembers which deadline it was reminded
of and reported on (an extended deadline is reminded of again). To-Dos are e-mailed by Odoo when mail is configured.
"""

import logging
from datetime import timedelta

from markupsafe import Markup

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

DUE_SOON_DAYS = 3  # default of Settings > Objective due-soon reminders (res.company.sm_due_soon_days)
# Opened only in the first days of the month, so a deploy mid-month does not open last month's reports late.
REPORT_WINDOW_DAYS = 7
_GROUP_BRANCH = "strategic_management.group_strategic_branch_manager"
_REPORT_TODO = "Submit branch report: "
_DUE_SOON_TODO = "Objective due soon: "
_DUE_PASSED_TODO = "Objective reached its due date: "


class StrategicObjectiveDueDates(models.Model):
    _inherit = "strategic.objective"

    due_soon_sent_for = fields.Date("Due-soon reminder sent for", readonly=True, copy=False)
    due_result_sent_for = fields.Date("Due-date result sent for", readonly=True, copy=False)


class StrategicPeriodReports(models.AbstractModel):
    _name = "strategic.period.reports"
    _description = "Automatic period-end reports and due-date reminders"

    @api.model
    def _run_daily(self, today=None):
        today = today or fields.Date.context_today(self)
        return {"reports": self._open_monthly_branch_reports(today), "objectives": self._objective_due_dates(today)}

    @api.model
    def _open_monthly_branch_reports(self, today):
        if today.day > REPORT_WINDOW_DAYS:
            return 0
        end = today.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
        Report = self.env["strategic.branch.report"].sudo()
        Notify = self.env["strategic.kpi.target"].sudo()
        manager_group = self.env.ref(_GROUP_BRANCH)
        opened = 0
        for branch in self.env["stock.warehouse"].sudo().search([]):
            if not branch.company_id.sm_monthly_reports:  # Settings: monthly branch reports off
                continue
            managers = self.env["res.users"].sudo().search([
                ("active", "=", True), ("share", "=", False), ("strategic_branch_warehouse_id", "=", branch.id),
                ("group_ids", "in", manager_group.ids)])
            if not managers or Report.search_count([("warehouse_id", "=", branch.id), ("date_start", "=", start), ("date_end", "=", end)]):
                continue
            report = Report.create({"warehouse_id": branch.id, "company_id": branch.company_id.id, "date_start": start, "date_end": end})
            Notify._create_deduplicated_activity(
                report, _REPORT_TODO + report.name,
                "%s has ended. The report is ready: add your comment and submit it to Head Office." % start.strftime("%B %Y"),
                recipients=managers, company=branch.company_id, date_deadline=today + timedelta(days=branch.company_id.sm_report_submit_days - 1))
            opened += 1
        return opened

    @api.model
    def _objective_due_dates(self, today):
        # Only the two bookkeeping dates are written; the objective's own write guard is for people, not this job.
        Objective = self.env["strategic.objective"].sudo().with_context(allow_objective_core_write=True)
        Notify = self.env["strategic.kpi.target"].sudo()
        done = 0
        for objective in Objective.search([("state", "=", "active"), ("deadline", ">=", today),
                                           ("deadline", "<=", today + timedelta(days=60))]):
            settings = objective.company_id
            if (not settings.sm_due_soon_reminders or objective.deadline > today + timedelta(days=settings.sm_due_soon_days)
                    or objective.due_soon_sent_for == objective.deadline or not objective.owner_id):
                continue
            Notify._create_deduplicated_activity(
                objective, _DUE_SOON_TODO + objective.name, "Due on %s." % objective.deadline,
                recipients=objective.owner_id, company=objective.company_id, date_deadline=objective.deadline)
            objective.due_soon_sent_for = objective.deadline
            done += 1
        statuses = dict(Objective._fields["scoring_status"].selection)
        for objective in Objective.search([("state", "=", "active"), ("deadline", "<", today)]):
            if objective.due_result_sent_for == objective.deadline or not objective.company_id.sm_due_result_notes:
                continue
            result = statuses.get(objective.scoring_status, "Not Evaluated")
            if objective.scoring_status not in ("not_evaluated", "data_quality_issue"):
                result = "%s (score %.0f%%)" % (result, objective.score)
            objective.message_post(
                body=Markup("<p><b>Due date reached</b> (%s).</p><p>Result: %s</p>") % (objective.deadline, result),
                partner_ids=objective.owner_id.partner_id.ids, subtype_xmlid="mail.mt_note")
            Notify._create_deduplicated_activity(
                objective, _DUE_PASSED_TODO + objective.name,
                "Due on %s. Result: %s. Mark it completed, or close it." % (objective.deadline, result),
                scope="organization", company=objective.company_id, date_deadline=today + timedelta(days=3))
            objective.due_result_sent_for = objective.deadline
            done += 1
        return done
