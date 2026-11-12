"""OKRs: an objective's OKR objectives and their key results, scored from KPI evaluations."""

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from .strategy import (SCORING_STATUSES)


class StrategicOkrObjective(models.Model):
    _name = "strategic.okr.objective"
    _description = "OKR Objective"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "date_start desc, name"

    name = fields.Char(required=True, tracking=True)
    strategic_objective_id = fields.Many2one(
        "strategic.objective",
        required=True,
        ondelete="cascade",
        index=True,
    )
    company_id = fields.Many2one(
        related="strategic_objective_id.company_id",
        store=True,
        readonly=True,
    )
    owner_id = fields.Many2one(
        "res.users",
        required=True,
        default=lambda self: self.env.user,
    )
    date_start = fields.Date(required=True)
    date_end = fields.Date(required=True)
    scope = fields.Selection(
        related="strategic_objective_id.scope",
        store=True,
        readonly=True,
    )
    warehouse_id = fields.Many2one(
        related="strategic_objective_id.warehouse_id",
        store=True,
        readonly=True,
    )
    key_result_ids = fields.One2many(
        "strategic.key.result",
        "okr_objective_id",
    )
    score = fields.Float(compute="_compute_score")
    scoring_status = fields.Selection(
        SCORING_STATUSES,
        compute="_compute_score",
    )

    @api.constrains("date_start", "date_end", "strategic_objective_id")
    def _check_dates(self):
        for okr in self:
            if (
                okr.date_start
                and okr.date_end
                and okr.date_start > okr.date_end
            ):
                raise ValidationError(
                    "OKR objective start date must not be after end date."
                )

            if (
                okr.date_end
                and okr.strategic_objective_id.deadline
                and okr.date_end > okr.strategic_objective_id.deadline
            ):
                raise ValidationError(
                    "OKR objective end date cannot be after the strategic objective deadline."
                )

    @api.depends(
        "key_result_ids.required",
        "key_result_ids.attainment",
        "key_result_ids.scoring_status",
    )
    def _compute_score(self):
        eval_model = self.env["strategic.kpi.evaluation"]

        for okr in self:
            key_results = okr.key_result_ids.filtered("required")

            if (
                not key_results
                or any(kr.scoring_status != "valid" for kr in key_results)
            ):
                okr.score = 0.0
                okr.scoring_status = "not_evaluated"
                continue

            okr.score = (
                sum(key_results.mapped("attainment")) / len(key_results)
            )
            okr.scoring_status = eval_model._attainment_status(okr.score)


class StrategicKeyResult(models.Model):
    _name = "strategic.key.result"
    _description = "Strategic Key Result"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "sequence, id"

    sequence = fields.Integer(default=10)
    name = fields.Char(required=True, tracking=True)
    okr_objective_id = fields.Many2one(
        "strategic.okr.objective",
        required=True,
        ondelete="cascade",
        index=True,
    )
    strategic_objective_id = fields.Many2one(
        related="okr_objective_id.strategic_objective_id",
        store=True,
        readonly=True,
    )
    company_id = fields.Many2one(
        related="okr_objective_id.company_id",
        store=True,
        readonly=True,
    )
    required = fields.Boolean(default=True)
    primary_kpi_id = fields.Many2one(
        "strategic.kpi.definition",
        required=True,
        ondelete="restrict",
        index=True,
    )
    supporting_kpi_ids = fields.Many2many(
        "strategic.kpi.definition",
        "strategic_key_result_supporting_kpi_rel",
        "key_result_id",
        "kpi_id",
        string="Supporting KPIs",
    )
    target_id = fields.Many2one(
        "strategic.kpi.target",
        required=True,
        ondelete="restrict",
        index=True,
    )
    evaluation_id = fields.Many2one(
        "strategic.kpi.evaluation",
        ondelete="restrict",
        index=True,
    )
    date_start = fields.Date(
        related="okr_objective_id.date_start",
        store=True,
        readonly=True,
    )
    date_end = fields.Date(
        related="okr_objective_id.date_end",
        store=True,
        readonly=True,
    )
    scope = fields.Selection(
        related="okr_objective_id.scope",
        store=True,
        readonly=True,
    )
    warehouse_id = fields.Many2one(
        related="okr_objective_id.warehouse_id",
        store=True,
        readonly=True,
    )
    unit = fields.Char(
        related="primary_kpi_id.unit",
        readonly=True,
    )
    attainment = fields.Float(compute="_compute_scoring")
    scoring_status = fields.Selection(
        [
            ("valid", "Valid"),
            ("not_evaluated", "Not Evaluated"),
            ("data_quality_issue", "Data Quality Issue"),
        ],
        compute="_compute_scoring",
    )
    attainment_status = fields.Selection(
        SCORING_STATUSES,
        compute="_compute_scoring",
    )

    @api.model_create_multi
    def create(self, vals_list):
        key_results = super().create(vals_list)
        # A target must be approved before its Key Result exists, so it may already have
        # evaluations; link the current one for this exact target (same rule as
        # strategic.kpi.evaluation._sync_linked_key_result_evaluation).
        # sudo: discovery only, limited to this Key Result's own target and KPI.
        Evaluation = self.env["strategic.kpi.evaluation"].sudo()
        for key_result in key_results.filtered(lambda kr: not kr.evaluation_id):
            current = Evaluation.search([
                ("target_id", "=", key_result.target_id.id),
                ("kpi_id", "=", key_result.primary_kpi_id.id),
                ("superseded_by_id", "=", False),
            ], order="evaluated_at desc, id desc", limit=1)
            if current:
                key_result.write({"evaluation_id": current.id})
        return key_results

    @api.depends(
        "evaluation_id.status",
        "evaluation_id.attainment",
        "evaluation_id.attainment_status",
    )
    def _compute_scoring(self):
        for key_result in self:
            # Read the linked evaluation only for this computed score.
            # This does not grant the user direct access to evaluation records.
            evaluation = key_result.evaluation_id.sudo()

            if not evaluation:
                key_result.attainment = 0.0
                key_result.scoring_status = "not_evaluated"
                key_result.attainment_status = "not_evaluated"

            elif evaluation.status == "valid":
                key_result.attainment = evaluation.attainment
                key_result.scoring_status = "valid"
                key_result.attainment_status = evaluation.attainment_status

            elif evaluation.status == "data_quality_issue":
                key_result.attainment = 0.0
                key_result.scoring_status = "data_quality_issue"
                key_result.attainment_status = "data_quality_issue"

            else:
                key_result.attainment = 0.0
                key_result.scoring_status = "not_evaluated"
                key_result.attainment_status = "not_evaluated"

    @api.constrains(
        "primary_kpi_id",
        "supporting_kpi_ids",
        "target_id",
        "evaluation_id",
        "okr_objective_id",
    )
    def _check_kpi_target_evaluation(self):
        for key_result in self:
            okr = key_result.okr_objective_id

            if key_result.primary_kpi_id in key_result.supporting_kpi_ids:
                raise ValidationError(
                    "Supporting KPIs cannot include the primary scoring KPI."
                )

            if key_result.target_id.kpi_id != key_result.primary_kpi_id:
                raise ValidationError(
                    "Key Result target must belong to its primary KPI."
                )

            if key_result.target_id.state != "approved":
                raise ValidationError(
                    "Key Result scoring requires an approved KPI target."
                )

            if key_result.target_id.scope != okr.strategic_objective_id.scope:
                raise ValidationError(
                    "Key Result target scope must match its OKR scope."
                )

            if (
                okr.strategic_objective_id.scope == "branch"
                and key_result.target_id.warehouse_id != okr.warehouse_id
            ):
                raise ValidationError(
                    "Branch Key Result target must match the OKR branch."
                )

            if (
                okr.strategic_objective_id.scope == "organization"
                and key_result.target_id.warehouse_id
            ):
                raise ValidationError(
                    "Organization Key Result targets cannot be branch-specific."
                )

            if (
                key_result.target_id.date_start != okr.date_start
                or key_result.target_id.date_end != okr.date_end
            ):
                raise ValidationError(
                    "Key Result target period must match the OKR reporting period."
                )

            if key_result.evaluation_id:
                if key_result.evaluation_id.kpi_id != key_result.primary_kpi_id:
                    raise ValidationError(
                        "Key Result evaluation must belong to its primary KPI."
                    )

                if key_result.evaluation_id.target_id != key_result.target_id:
                    raise ValidationError(
                        "Key Result evaluation must use the Key Result target."
                    )
