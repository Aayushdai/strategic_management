"""Idempotent scheduled KPI evaluations and in-app review notifications."""

import logging

from dateutil.relativedelta import relativedelta
from markupsafe import escape

from odoo import api, fields, models, _

from .kpi_source_adapter import (
    DELIVERY_CALCULATIONS,
    _SOURCE_ADAPTERS,
    _SOURCE_COLLECTORS,
    collect_supplier_delivery_result,
)

_logger = logging.getLogger(__name__)

_GROUP_HEAD = "strategic_management.group_strategic_head_office_manager"
_GROUP_BRANCH = "strategic_management.group_strategic_branch_manager"

_EXCEPTION_SUMMARY = "Operational KPI exception requires review"
_SCHEDULE_FAILURE_SUMMARY = "Scheduled KPI evaluation failed"
_PENDING_REVIEW_SUMMARY = "KPI measurement pending review"


def reporting_periods(date_start, date_end, frequency):
    """Calendar periods of the frequency that touch the range, clipped to it."""
    periods, current = [], date_start
    while current <= date_end:
        if frequency == "daily":
            end = current
        elif frequency == "weekly":
            end = current + relativedelta(days=6 - current.weekday())
        elif frequency == "monthly":
            end = current + relativedelta(day=31)
        elif frequency == "quarterly":
            end = current + relativedelta(month=(current.month - 1) // 3 * 3 + 3, day=31)
        else:
            end = current + relativedelta(month=12, day=31)
        periods.append((current, min(end, date_end)))
        current = end + relativedelta(days=1)
    return periods


class StrategicKpiReportingSchedule(models.Model):
    """One KPI at one scope, reported every period of its frequency. Each closed period gets its own approved target,
    which the daily job then evaluates, so a daily KPI has one evaluation per day. The target applies to each period."""
    _name = "strategic.kpi.reporting.schedule"
    _description = "KPI Reporting Schedule"
    _order = "kpi_id, id"

    kpi_id = fields.Many2one("strategic.kpi.definition", required=True, ondelete="restrict", index=True)
    company_id = fields.Many2one(related="kpi_id.company_id", store=True, readonly=True)
    scope = fields.Selection([("organization", "Organization"), ("branch", "Branch")], required=True, default="organization")
    warehouse_id = fields.Many2one("stock.warehouse", string="Branch Warehouse", index=True)
    date_start = fields.Date(required=True)
    date_end = fields.Date(required=True)
    target_value = fields.Float(required=True)
    target_ids = fields.One2many("strategic.kpi.target", "schedule_id")

    def _generate_due_targets(self):
        """Idempotent: one target per closed period. An open period is never measured, so no partial result is stored."""
        created = self.env["strategic.kpi.target"].sudo()
        for schedule in self.sudo():
            today = fields.Date.context_today(schedule.with_context(tz=schedule.company_id.partner_id.tz or "UTC"))
            existing = set(schedule.target_ids.mapped("date_start"))
            for start, end in reporting_periods(schedule.date_start, schedule.date_end, schedule.kpi_id.reporting_frequency or "monthly"):
                if end >= today or start in existing:
                    continue
                created |= created.create({"kpi_id": schedule.kpi_id.id, "schedule_id": schedule.id, "date_start": start, "date_end": end, "scope": schedule.scope,
                                           "warehouse_id": schedule.warehouse_id.id or False, "target_basis": "absolute", "target_value": schedule.target_value, "state": "approved"})
        return created


class StrategicKpiScheduledJobs(models.Model):
    _inherit = "strategic.kpi.target"

    schedule_id = fields.Many2one("strategic.kpi.reporting.schedule", readonly=True, index=True, copy=False, ondelete="restrict")

    _one_per_scheduled_period = models.UniqueIndex("(schedule_id, date_start) WHERE schedule_id IS NOT NULL", "This reporting period already has a target.")

    @api.model
    def _cron_process_scheduled_kpis(self):
        """Evaluate operational targets after their reporting period has closed.

        Manual-value KPIs are deliberately excluded. A stable source snapshot key
        makes retries idempotent; changed source snapshots are retained as a new
        evaluation by the existing immutable evaluation workflow.
        """
        for schedule in self.env["strategic.kpi.reporting.schedule"].sudo().search([]):
            try:
                with self.env.cr.savepoint():
                    schedule._generate_due_targets()
            except Exception:
                _logger.exception("Could not prepare the reporting periods of KPI schedule %s", schedule.id)
        targets = self.sudo().search(
            [("state", "=", "approved"), ("date_end", "!=", False)],
            order="company_id, date_end, id",
        )
        processed = 0
        notified = 0

        for target in targets:
            timezone = target.company_id.partner_id.tz or "UTC"
            company_today = fields.Date.context_today(target.with_context(tz=timezone))
            if not target.date_end or target.date_end >= company_today:
                continue

            try:
                with self.env.cr.savepoint():
                    company_target = target.with_company(target.company_id)
                    version = company_target.kpi_id._get_definition_version_for_period(
                        company_target.date_start, company_target.date_end
                    )
                    if version.calculation_type in DELIVERY_CALCULATIONS:
                        collector = collect_supplier_delivery_result
                    else:
                        collector = _SOURCE_COLLECTORS.get(version.source_type)
                    supported = _SOURCE_ADAPTERS.get(version.source_type, set())
                    if not collector or version.calculation_type not in supported:
                        continue

                    result = collector(company_target, version)
                    evaluation = self.env["strategic.kpi.evaluation"].sudo()._create_from_source_adapter(
                        company_target, version, result
                    )
                    processed += 1
            except Exception as exc:
                # Isolate a bad target so one source/data issue cannot stop the
                # daily job for every other branch or KPI.
                _logger.exception(
                    "Scheduled KPI evaluation failed for target %s (id=%s)",
                    target.display_name,
                    target.id,
                )
                try:
                    with self.env.cr.savepoint():
                        notified += self._create_deduplicated_activity(
                            target,
                            _SCHEDULE_FAILURE_SUMMARY,
                            _("Scheduled evaluation could not complete: %s") % str(exc),
                        )
                except Exception:
                    _logger.exception(
                        "Could not create a follow-up activity for KPI target %s", target.id
                    )
                continue

            # A scheduled period with no sales is an ordinary result, not an exception; a daily KPI would otherwise ask for review every day.
            if evaluation.status == "data_quality_issue" or (evaluation.status == "not_evaluated" and not target.schedule_id):
                reason = evaluation.reason or evaluation.source_note or _(
                    "The operational source did not produce a valid measurement."
                )
                try:
                    with self.env.cr.savepoint():
                        notified += self._create_deduplicated_activity(
                            target,
                            _EXCEPTION_SUMMARY,
                            _("KPI %s for %s to %s needs review: %s")
                            % (target.kpi_id.display_name, target.date_start, target.date_end, reason),
                        )
                except Exception:
                    _logger.exception(
                        "Could not create an exception follow-up for KPI target %s", target.id
                    )

        notified += self._cron_notify_pending_measurement_reviews()
        _logger.info(
            "Strategic KPI scheduler completed: %s operational evaluations processed; %s review/exception activities created",
            processed,
            notified,
        )
        # After the results are in: open last month's branch reports and send objective due-date reminders.
        try:
            with self.env.cr.savepoint():
                self.env["strategic.period.reports"]._run_daily()
        except Exception:
            _logger.exception("Could not open period-end reports or send objective due-date reminders")
        return {"evaluations_processed": processed, "activities_created": notified}

    @api.model
    def _cron_notify_pending_measurement_reviews(self):
        """Create one pending activity per reviewer and submitted measurement."""
        Measurement = self.env["strategic.kpi.measurement"].sudo()
        measurements = Measurement.search([("state", "=", "submitted")], order="submitted_at, id")
        created = 0
        for measurement in measurements:
            if measurement.scope == "branch":
                warehouse = measurement.warehouse_id
            elif measurement.scope == "user":
                warehouse = measurement.user_id.strategic_branch_warehouse_id
            else:
                # Organization-level manual submissions are not part of the
                # approved submitter workflow in this release.
                continue

            try:
                with self.env.cr.savepoint():
                    created += self._create_deduplicated_activity(
                        measurement,
                        _PENDING_REVIEW_SUMMARY,
                        _("KPI measurement %s for %s to %s was submitted by %s and is awaiting review.")
                        % (
                            measurement.kpi_id.display_name,
                            measurement.date_start,
                            measurement.date_end,
                            measurement.submitted_by_id.display_name,
                        ),
                        warehouse=warehouse,
                        scope=measurement.scope,
                        company=measurement.company_id,
                    )
            except Exception:
                _logger.exception(
                    "Could not create a pending-review activity for KPI measurement %s",
                    measurement.id,
                )
        return created

    @api.model
    def _get_schedule_notification_users(self, scope, company, warehouse=None):
        """Route branch/employee work to its branch manager; fall back to Head Office."""
        User = self.env["res.users"].sudo()
        domain = [
            ("active", "=", True),
            ("share", "=", False),
            ("company_ids", "in", [company.id]),
        ]
        users = User.search(domain)
        if scope in ("branch", "user") and warehouse:
            branch_users = users.filtered(
                lambda user: user.strategic_branch_warehouse_id == warehouse
                and user.group_ids.filtered_domain([("id", "=", self.env.ref(_GROUP_BRANCH).id)])
            )
            if branch_users:
                return branch_users
        return users.filtered(
            lambda user: user.group_ids.filtered_domain([("id", "=", self.env.ref(_GROUP_HEAD).id)])
        )

    @api.model
    def _create_deduplicated_activity(
        self, record, summary, note, warehouse=None, scope="organization", company=None,
        recipients=None, date_deadline=None,
    ):
        """Create an in-app activity only when the same live reminder is absent."""
        company = company or record.company_id
        if recipients is None:
            recipients = self._get_schedule_notification_users(
                scope, company, warehouse=warehouse
            )
        if not recipients:
            _logger.warning(
                "No configured reviewer found for scheduled KPI notification on %s,%s",
                record._name,
                record.id,
            )
            return 0

        model_id = self.env["ir.model"]._get_id(record._name)
        activity_type = self.env.ref("mail.mail_activity_data_todo", raise_if_not_found=False)
        if not model_id or not activity_type:
            _logger.warning("Odoo To-Do activity type or model metadata is unavailable; skipping notification")
            return 0

        Activity = self.env["mail.activity"].sudo()
        created = 0
        safe_note = "<p>%s</p>" % escape(str(note or ""))
        for recipient in recipients:
            domain = [
                ("res_model_id", "=", model_id),
                ("res_id", "=", record.id),
                ("user_id", "=", recipient.id),
                ("summary", "=", summary),
            ]
            if Activity.search_count(domain):
                continue
            Activity.create(
                {
                    "res_model_id": model_id,
                    "res_id": record.id,
                    "activity_type_id": activity_type.id,
                    "summary": summary,
                    "note": safe_note,
                    "user_id": recipient.id,
                    "date_deadline": date_deadline or fields.Date.today(),
                }
            )
            created += 1
        return created
