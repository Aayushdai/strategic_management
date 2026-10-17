"""Idempotent scheduled KPI evaluations and in-app review notifications."""

import logging

from markupsafe import escape

from odoo import api, fields, models, _

from .kpi_source_adapter import (
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


class StrategicKpiScheduledJobs(models.Model):
    _inherit = "strategic.kpi.target"

    @api.model
    def _cron_process_scheduled_kpis(self):
        """Evaluate operational targets after their reporting period has closed.

        Manual-value KPIs are deliberately excluded. A stable source snapshot key
        makes retries idempotent; changed source snapshots are retained as a new
        evaluation by the existing immutable evaluation workflow.
        """
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
                    if version.calculation_type == "supplier_on_time_delivery_pct":
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

            if evaluation.status in ("data_quality_issue", "not_evaluated"):
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
        self, record, summary, note, warehouse=None, scope="organization", company=None
    ):
        """Create an in-app activity only when the same live reminder is absent."""
        company = company or record.company_id
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
                    "date_deadline": fields.Date.today(),
                }
            )
            created += 1
        return created
