"""Governed manual KPI measurement submissions and immutable review history."""

import math

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


_GROUP_HEAD = "strategic_management.group_strategic_head_office_manager"
_GROUP_BRANCH = "strategic_management.group_strategic_branch_manager"
_GROUP_EMPLOYEE = "strategic_management.group_strategic_employee"


class StrategicKpiMeasurement(models.Model):
    _name = "strategic.kpi.measurement"
    _description = "Strategic KPI Manual Measurement"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "date_start desc, id desc"

    name = fields.Char(compute="_compute_name", store=True)
    kpi_id = fields.Many2one("strategic.kpi.definition", required=True, readonly=True, ondelete="restrict", index=True)
    target_id = fields.Many2one("strategic.kpi.target", required=True, readonly=True, ondelete="restrict", index=True)
    definition_version_id = fields.Many2one(
        "strategic.kpi.definition.version", required=True, readonly=True, ondelete="restrict", index=True
    )
    company_id = fields.Many2one("res.company", required=True, readonly=True, index=True)
    date_start = fields.Date(required=True, readonly=True, index=True)
    date_end = fields.Date(required=True, readonly=True, index=True)
    scope = fields.Selection(
        [("organization", "Organization"), ("branch", "Branch"), ("user", "Employee")],
        required=True, readonly=True, index=True,
    )
    warehouse_id = fields.Many2one("stock.warehouse", readonly=True, index=True)
    user_id = fields.Many2one("res.users", string="Employee", readonly=True, index=True)
    unit = fields.Char(readonly=True)
    actual_value = fields.Float(required=True, tracking=True)
    has_actual_value = fields.Boolean(default=False, required=True)
    source_reference = fields.Char(
        string="Source Reference", tracking=True,
        help="Reference to the source report, worksheet, count sheet or other evidence for this value.",
    )
    evidence_attachment_ids = fields.Many2many(
        "ir.attachment", "strategic_kpi_measurement_attachment_rel",
        "measurement_id", "attachment_id", string="Evidence Attachments",
    )
    submitter_note = fields.Text()
    correction_note = fields.Text(
        help="Explain the changes made after a reviewer returned this submission for correction."
    )
    correction_reason = fields.Text(
        help="Reason supplied when this record is created as a correction of an already approved measurement.",
    )
    corrects_measurement_id = fields.Many2one(
        "strategic.kpi.measurement", string="Corrects Measurement", readonly=True,
        ondelete="restrict", index=True, copy=False,
    )
    correction_ids = fields.One2many("strategic.kpi.measurement", "corrects_measurement_id", readonly=True)
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("submitted", "Submitted for Review"),
            ("returned", "Returned for Correction"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
        ], default="draft", required=True, tracking=True, index=True, copy=False,
    )
    submitted_by_id = fields.Many2one("res.users", required=True, readonly=True, index=True, copy=False)
    submitted_at = fields.Datetime(readonly=True, copy=False)
    reviewed_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    reviewed_at = fields.Datetime(readonly=True, copy=False)
    review_note = fields.Text(
        tracking=True,
        help="Reviewer explanation. Required before a return or rejection decision.",
    )
    submission_version = fields.Integer(default=0, readonly=True, copy=False)
    revision_ids = fields.One2many("strategic.kpi.measurement.revision", "measurement_id", readonly=True)
    review_history_ids = fields.One2many("strategic.kpi.measurement.review", "measurement_id", readonly=True)
    evaluation_ids = fields.One2many("strategic.kpi.evaluation", "measurement_id", readonly=True)

    @api.model
    def default_get(self, fields_list):
        """Populate readonly contract fields from the target selected on the target form."""
        values = super().default_get(fields_list)
        target_id = values.get("target_id") or self.env.context.get("default_target_id")
        target = self.env["strategic.kpi.target"].browse(target_id).exists() if target_id else False
        if target:
            version = target.kpi_id._get_definition_version_for_period(target.date_start, target.date_end)
            derived = {
                "kpi_id": target.kpi_id.id,
                "target_id": target.id,
                "definition_version_id": version.id,
                "company_id": target.company_id.id,
                "date_start": target.date_start,
                "date_end": target.date_end,
                "scope": target.scope,
                "warehouse_id": target.warehouse_id.id or False,
                "user_id": target.user_id.id or False,
                "unit": version.unit,
            }
            values.update({key: value for key, value in derived.items() if key in fields_list})
        return values

    @api.depends("kpi_id.name", "date_start", "date_end", "submission_version")
    def _compute_name(self):
        for rec in self:
            suffix = " — submission %s" % rec.submission_version if rec.submission_version else ""
            rec.name = "%s measurement (%s to %s)%s" % (
                rec.kpi_id.name or "KPI", rec.date_start or "", rec.date_end or "", suffix
            )

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for incoming in vals_list:
            vals = dict(incoming)
            if vals.get("state") not in (None, False, "draft"):
                raise ValidationError("Manual measurements must be created as drafts and submitted through the workflow.")
            target = self.env["strategic.kpi.target"].browse(vals.get("target_id")).exists()
            if not target:
                raise ValidationError("Select an existing KPI target before recording a manual measurement.")
            self._check_submitter_scope(target)
            if target.state != "approved":
                raise ValidationError("Manual measurements require an approved KPI target.")
            if target.kpi_id.source_type != "manual":
                raise ValidationError("Manual submission is only available for KPIs configured with a manual source.")

            corrects = self.env["strategic.kpi.measurement"].browse(vals.get("corrects_measurement_id")).exists()
            reason = (vals.get("correction_reason") or "").strip()
            if vals.get("corrects_measurement_id"):
                if not corrects or corrects.state != "approved":
                    raise ValidationError("A correction must reference an approved measurement.")
                if corrects.submitted_by_id != self.env.user:
                    raise AccessError("Only the original submitter may create a correction for this measurement.")
                if corrects.target_id != target:
                    raise ValidationError("A correction must retain the original KPI target and reporting scope.")
                if not reason:
                    raise ValidationError("A reason is required when correcting an approved measurement.")
            elif reason:
                raise ValidationError("A correction reason must reference the approved measurement being corrected.")

            version = target.kpi_id._get_definition_version_for_period(target.date_start, target.date_end)
            vals.update({
                "kpi_id": target.kpi_id.id,
                "target_id": target.id,
                "definition_version_id": version.id,
                "company_id": target.company_id.id,
                "date_start": target.date_start,
                "date_end": target.date_end,
                "scope": target.scope,
                "warehouse_id": target.warehouse_id.id or False,
                "user_id": target.user_id.id or False,
                "unit": version.unit,
                "submitted_by_id": self.env.user.id,
                "state": "draft",
                "submission_version": 0,
            })
            for protected in (
                "submitted_at", "reviewed_by_id", "reviewed_at", "review_note",
                "review_history_ids", "revision_ids", "evaluation_ids", "correction_ids",
            ):
                vals.pop(protected, None)
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        vals = dict(vals)
        protected = {
            "kpi_id", "target_id", "definition_version_id", "company_id", "date_start", "date_end",
            "scope", "warehouse_id", "user_id", "unit", "submitted_by_id", "submitted_at",
            "reviewed_by_id", "reviewed_at", "submission_version", "state", "corrects_measurement_id",
            "correction_reason", "revision_ids", "review_history_ids", "evaluation_ids",
        }
        if protected.intersection(vals):
            raise ValidationError("Measurement workflow and provenance fields can only be changed by controlled actions.")
        if set(vals) == {"review_note"}:
            for rec in self:
                if rec.state != "submitted":
                    raise ValidationError("Reviewer notes can only be changed while a submission awaits review.")
                rec._check_reviewer()
            return super().write(vals)

        editable = {
            "actual_value", "has_actual_value", "source_reference", "evidence_attachment_ids",
            "submitter_note", "correction_note",
        }
        if not vals or set(vals) - editable:
            raise ValidationError("Only measurement data, evidence and correction notes can be edited before review.")
        for rec in self:
            if rec.state not in ("draft", "returned"):
                raise ValidationError("A submitted or finalized measurement is immutable; create a correction instead.")
            if rec.submitted_by_id != self.env.user:
                raise AccessError("Only the original submitter can edit this measurement.")
        return super().write(vals)

    def _check_submitter_scope(self, target):
        user = self.env.user
        if target.scope == "user":
            if not user.has_group(_GROUP_EMPLOYEE) or target.user_id != user:
                raise AccessError("Employees may submit only their own assigned employee KPI measurements.")
            if not user.strategic_branch_warehouse_id:
                raise ValidationError("The employee must be mapped to a branch before submitting KPI measurements.")
            return
        if target.scope == "branch":
            if not user.has_group(_GROUP_BRANCH):
                raise AccessError("Only a Branch Manager can submit a branch-level manual measurement.")
            if not user.strategic_branch_warehouse_id or target.warehouse_id != user.strategic_branch_warehouse_id:
                raise AccessError("Branch Managers may submit measurements only for their own branch.")
            return
        raise AccessError("Head Office manual KPI submissions are deferred in the current release.")

    def _check_measurement_payload(self, is_resubmission=False):
        self.ensure_one()
        if not self.has_actual_value or not math.isfinite(self.actual_value):
            raise ValidationError("Enter a finite numeric actual value before submitting the measurement.")
        if not self.source_reference and not self.evidence_attachment_ids:
            raise ValidationError("Add a source reference or an evidence attachment before submitting.")
        if is_resubmission:
            if not (self.correction_note or "").strip():
                raise ValidationError("Explain the correction before resubmitting a returned measurement.")
            if not self.source_reference and not self.evidence_attachment_ids:
                raise ValidationError("A corrected submission must retain a source reference or evidence attachment.")
        self.target_id._check_target_scope_and_dates()
        if self.target_id.state != "approved":
            raise ValidationError("The linked KPI target is no longer approved.")
        version = self.kpi_id._get_definition_version_for_period(self.date_start, self.date_end)
        if version != self.definition_version_id:
            raise ValidationError("The measurement's KPI definition version no longer matches its reporting period.")

    def action_submit(self):
        for rec in self:
            if rec.state not in ("draft", "returned"):
                raise ValidationError("Only draft or returned measurements can be submitted.")
            if rec.submitted_by_id != self.env.user:
                raise AccessError("Only the original submitter can submit or resubmit this measurement.")
            rec._check_submitter_scope(rec.target_id)
            rec._check_measurement_payload(is_resubmission=rec.state == "returned")
            next_version = rec.submission_version + 1
            now = fields.Datetime.now()
            self.env["strategic.kpi.measurement.revision"].sudo().create({
                "measurement_id": rec.id,
                "submission_version": next_version,
                "kpi_id": rec.kpi_id.id,
                "target_id": rec.target_id.id,
                "definition_version_id": rec.definition_version_id.id,
                "date_start": rec.date_start,
                "date_end": rec.date_end,
                "scope": rec.scope,
                "warehouse_id": rec.warehouse_id.id or False,
                "user_id": rec.user_id.id or False,
                "actual_value": rec.actual_value,
                "unit": rec.unit,
                "source_reference": rec.source_reference,
                "submitter_note": rec.submitter_note,
                "correction_note": rec.correction_note,
                "evidence_attachment_ids": [(6, 0, rec.evidence_attachment_ids.ids)],
                "submitted_by_id": rec.submitted_by_id.id,
                "submitted_at": now,
                "corrects_measurement_id": rec.corrects_measurement_id.id or False,
                "correction_reason": rec.correction_reason,
            })
            super(StrategicKpiMeasurement, rec).write({
                "state": "submitted",
                "submission_version": next_version,
                "submitted_at": now,
                "reviewed_by_id": False,
                "reviewed_at": False,
                "review_note": False,
            })
        return True

    def _check_reviewer(self):
        self.ensure_one()
        if self.env.user == self.submitted_by_id:
            raise AccessError("A user cannot review or approve their own manual KPI submission.")
        if self.scope == "user":
            if not self.env.user.has_group(_GROUP_BRANCH):
                raise AccessError("Employee KPI measurements must be reviewed by a Branch Manager.")
            manager_branch = self.env.user.strategic_branch_warehouse_id
            submitter_branch = self.user_id.strategic_branch_warehouse_id
            if not manager_branch or manager_branch != submitter_branch:
                raise AccessError("A Branch Manager may review employee measurements only for their own branch.")
        elif self.scope == "branch":
            if not self.env.user.has_group(_GROUP_HEAD):
                raise AccessError("Branch KPI measurements must be reviewed by Head Office.")
        else:
            raise AccessError("No manual review workflow is enabled for organization-level submissions.")

    def _record_review(self, decision, reason):
        self.ensure_one()
        self.env["strategic.kpi.measurement.review"].sudo().create({
            "measurement_id": self.id,
            "submission_version": self.submission_version,
            "decision": decision,
            "reason": reason or False,
            "reviewed_by_id": self.env.user.id,
            "reviewed_at": fields.Datetime.now(),
        })

    def action_approve(self):
        for rec in self:
            if rec.state != "submitted":
                raise ValidationError("Only a submitted measurement can be approved.")
            rec._check_reviewer()
            rec._check_measurement_payload()
            if rec.evaluation_ids:
                raise ValidationError("This measurement already has an evaluation; refusing to create a duplicate.")
            now = fields.Datetime.now()
            super(StrategicKpiMeasurement, rec).write({
                "state": "approved", "reviewed_by_id": self.env.user.id, "reviewed_at": now,
            })
            rec._record_review("approved", rec.review_note)
            evaluation = self.env["strategic.kpi.evaluation"].sudo().create({
                "measurement_id": rec.id,
                "kpi_id": rec.kpi_id.id,
                "target_id": rec.target_id.id,
                "date_start": rec.date_start,
                "date_end": rec.date_end,
                "scope": rec.scope,
                "warehouse_id": rec.warehouse_id.id or False,
                "user_id": rec.user_id.id or False,
                "actual_value": rec.actual_value,
                "has_actual_value": True,
                "source_model": rec._name,
                "source_record_ref": "%s,%s,submission:%s" % (rec._name, rec.id, rec.submission_version),
                "reason": rec.submitter_note or rec.correction_note or False,
            })
            previous = rec.corrects_measurement_id
            if previous and previous.evaluation_ids:
                old_evaluations = previous.evaluation_ids.filtered(lambda item: not item.superseded_by_id)
                if len(old_evaluations) > 1:
                    raise ValidationError("The corrected measurement has multiple current evaluations; resolve history before approval.")
                if old_evaluations:
                    old_eval = old_evaluations.sudo()
                    old_eval.write({"superseded_by_id": evaluation.id})
            self.env['strategic.kpi.evaluation']._sync_linked_key_result_evaluation(rec.target_id, evaluation)
        return True

    def action_return_for_correction(self):
        for rec in self:
            if rec.state != "submitted":
                raise ValidationError("Only a submitted measurement can be returned for correction.")
            rec._check_reviewer()
            reason = (rec.review_note or "").strip()
            if not reason:
                raise ValidationError("Provide a correction reason before returning the measurement.")
            rec._record_review("returned", reason)
            super(StrategicKpiMeasurement, rec).write({
                "state": "returned", "reviewed_by_id": self.env.user.id,
                "reviewed_at": fields.Datetime.now(),
            })
        return True

    def action_reject(self):
        for rec in self:
            if rec.state != "submitted":
                raise ValidationError("Only a submitted measurement can be rejected.")
            rec._check_reviewer()
            reason = (rec.review_note or "").strip()
            if not reason:
                raise ValidationError("Provide a reason before rejecting the measurement.")
            rec._record_review("rejected", reason)
            super(StrategicKpiMeasurement, rec).write({
                "state": "rejected", "reviewed_by_id": self.env.user.id,
                "reviewed_at": fields.Datetime.now(),
            })
        return True

    def action_create_correction(self):
        self.ensure_one()
        if self.state != "approved":
            raise ValidationError("Only an approved measurement can be corrected through the controlled correction workflow.")
        if self.submitted_by_id != self.env.user:
            raise AccessError("Only the original submitter can create a correction for this measurement.")
        self._check_submitter_scope(self.target_id)
        return {
            "type": "ir.actions.act_window",
            "name": "Correct Approved Measurement",
            "res_model": self._name,
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_target_id": self.target_id.id,
                "default_corrects_measurement_id": self.id,
                "default_actual_value": self.actual_value,
                "default_has_actual_value": True,
                "default_source_reference": self.source_reference,
                "default_submitter_note": self.submitter_note,
                "default_correction_reason": False,
            },
        }

    def unlink(self):
        if any(rec.state != "draft" for rec in self):
            raise ValidationError("Submitted and reviewed measurement history cannot be deleted.")
        raise AccessError("Manual measurements are retained for audit; create a new draft instead of deleting records.")


class StrategicKpiMeasurementRevision(models.Model):
    _name = "strategic.kpi.measurement.revision"
    _description = "Strategic KPI Measurement Submission Snapshot"
    _order = "measurement_id, submission_version"

    measurement_id = fields.Many2one("strategic.kpi.measurement", required=True, ondelete="restrict", index=True)
    submission_version = fields.Integer(required=True, readonly=True)
    kpi_id = fields.Many2one("strategic.kpi.definition", required=True, readonly=True, ondelete="restrict")
    target_id = fields.Many2one("strategic.kpi.target", required=True, readonly=True, ondelete="restrict")
    definition_version_id = fields.Many2one("strategic.kpi.definition.version", required=True, readonly=True, ondelete="restrict")
    date_start = fields.Date(required=True, readonly=True)
    date_end = fields.Date(required=True, readonly=True)
    scope = fields.Selection([("organization", "Organization"), ("branch", "Branch"), ("user", "Employee")], required=True, readonly=True)
    warehouse_id = fields.Many2one("stock.warehouse", readonly=True)
    user_id = fields.Many2one("res.users", readonly=True)
    actual_value = fields.Float(required=True, readonly=True)
    unit = fields.Char(readonly=True)
    source_reference = fields.Char(readonly=True)
    submitter_note = fields.Text(readonly=True)
    correction_note = fields.Text(readonly=True)
    evidence_attachment_ids = fields.Many2many(
        "ir.attachment", "strategic_kpi_measurement_revision_attachment_rel",
        "revision_id", "attachment_id", readonly=True,
    )
    submitted_by_id = fields.Many2one("res.users", required=True, readonly=True, ondelete="restrict")
    submitted_at = fields.Datetime(required=True, readonly=True)
    corrects_measurement_id = fields.Many2one("strategic.kpi.measurement", readonly=True, ondelete="restrict")
    correction_reason = fields.Text(readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError("Submission snapshots are created only by the controlled submission workflow.")
        return super().create(vals_list)

    def write(self, vals):
        raise ValidationError("Submission snapshots are immutable.")

    def unlink(self):
        raise ValidationError("Submission snapshots are immutable and cannot be deleted.")


class StrategicKpiMeasurementReview(models.Model):
    _name = "strategic.kpi.measurement.review"
    _description = "Strategic KPI Measurement Review History"
    _order = "reviewed_at desc, id desc"

    measurement_id = fields.Many2one("strategic.kpi.measurement", required=True, ondelete="restrict", index=True)
    submission_version = fields.Integer(required=True, readonly=True)
    decision = fields.Selection(
        [("approved", "Approved"), ("returned", "Returned for Correction"), ("rejected", "Rejected")],
        required=True, readonly=True,
    )
    reason = fields.Text(readonly=True)
    reviewed_by_id = fields.Many2one("res.users", required=True, readonly=True, ondelete="restrict")
    reviewed_at = fields.Datetime(required=True, readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError("Review history is created only by controlled review actions.")
        return super().create(vals_list)

    def write(self, vals):
        raise ValidationError("Measurement review history is immutable.")

    def unlink(self):
        raise ValidationError("Measurement review history is immutable and cannot be deleted.")


class StrategicKpiEvaluationMeasurement(models.Model):
    _inherit = "strategic.kpi.evaluation"

    measurement_id = fields.Many2one(
        "strategic.kpi.measurement", readonly=True, ondelete="restrict", index=True, copy=False,
    )

    @api.model
    def _prepare_evaluation_values(self, vals):
        prepared = super()._prepare_evaluation_values(vals)
        measurement_id = prepared.get("measurement_id")
        kpi = self.env["strategic.kpi.definition"].browse(prepared.get("kpi_id")).exists()
        if measurement_id:
            measurement = self.env["strategic.kpi.measurement"].browse(measurement_id).exists()
            if not measurement or measurement.state != "approved":
                raise ValidationError("A manual KPI evaluation can only be created from an approved measurement.")
            if measurement.evaluation_ids:
                raise ValidationError("This manual measurement already has an evaluation.")
            comparisons = {
                "kpi_id": measurement.kpi_id.id,
                "target_id": measurement.target_id.id,
                "date_start": measurement.date_start,
                "date_end": measurement.date_end,
                "scope": measurement.scope,
                "warehouse_id": measurement.warehouse_id.id or False,
                "user_id": measurement.user_id.id or False,
                "actual_value": measurement.actual_value,
                "has_actual_value": True,
            }
            for field_name, expected in comparisons.items():
                if prepared.get(field_name) != expected:
                    raise ValidationError("Evaluation values must match the approved manual measurement snapshot.")
        elif kpi and kpi.source_type == "manual" and not self.env.su:
            raise AccessError("Manual KPI evaluations must be generated by approving a submitted measurement.")
        return prepared

    def write(self, vals):
        if "measurement_id" in vals:
            raise ValidationError("The source measurement of a historical KPI evaluation cannot be changed.")
        if "superseded_by_id" in vals and not self.env.su:
            raise AccessError("Only the controlled correction workflow may supersede an evaluation.")
        return super().write(vals)


class StrategicKpiTargetMeasurements(models.Model):
    _inherit = "strategic.kpi.target"

    measurement_ids = fields.One2many("strategic.kpi.measurement", "target_id", readonly=True)

    def action_open_measurements(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "KPI Measurements",
            "res_model": "strategic.kpi.measurement",
            "view_mode": "list,form",
            "domain": [("target_id", "=", self.id)],
            "context": {"default_target_id": self.id},
        }
