"""KPI baselines: the reference value a target is compared with."""

import math
from datetime import timedelta
from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .kpi import (KPI_SCOPES)


class StrategicKpiBaseline(models.Model):
    """Approved, scope-specific KPI baseline with immutable audit provenance."""

    _name = "strategic.kpi.baseline"
    _description = "Strategic KPI Baseline"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "effective_from desc, id desc"

    name = fields.Char(compute="_compute_name", store=True)
    kpi_id = fields.Many2one(
        "strategic.kpi.definition", required=True, ondelete="restrict", index=True
    )
    definition_version_id = fields.Many2one(
        "strategic.kpi.definition.version",
        required=True,
        ondelete="restrict",
        index=True,
        string="KPI Definition Version",
    )
    company_id = fields.Many2one(
        related="kpi_id.company_id", store=True, readonly=True, index=True
    )
    scope = fields.Selection(
        KPI_SCOPES,
        required=True,
        default="organization",
        index=True,
        tracking=True,
    )
    warehouse_id = fields.Many2one(
        "stock.warehouse", string="Branch Warehouse", index=True
    )
    user_id = fields.Many2one("res.users", string="Employee", index=True)
    baseline_value = fields.Float(required=True, tracking=True)
    source_period_start = fields.Date(required=True, index=True)
    source_period_end = fields.Date(required=True, index=True)
    effective_from = fields.Date(required=True, index=True, tracking=True)
    effective_to = fields.Date(readonly=True, index=True, copy=False, tracking=True)
    source_note = fields.Text(required=True, tracking=True)
    change_reason = fields.Text(required=True, tracking=True)
    state = fields.Selection(
        [("draft", "Draft"), ("approved", "Approved")],
        required=True,
        default="draft",
        index=True,
        tracking=True,
        copy=False,
    )
    created_by_id = fields.Many2one(
        "res.users",
        required=True,
        default=lambda self: self.env.user,
        readonly=True,
        copy=False,
    )
    created_at = fields.Datetime(
        required=True, default=fields.Datetime.now, readonly=True, copy=False
    )
    approved_by_id = fields.Many2one(
        "res.users", readonly=True, copy=False
    )
    approved_at = fields.Datetime(readonly=True, copy=False)
    target_ids = fields.One2many("strategic.kpi.target", "baseline_id", string="Linked Targets")

    @api.depends(
        "kpi_id.name",
        "kpi_id.code",
        "scope",
        "warehouse_id.name",
        "user_id.name",
        "effective_from",
    )
    def _compute_name(self):
        for baseline in self:
            scope_label = dict(baseline._fields["scope"].selection).get(
                baseline.scope, "Scope"
            )
            if baseline.scope == "branch" and baseline.warehouse_id:
                scope_label = baseline.warehouse_id.display_name
            elif baseline.scope == "user" and baseline.user_id:
                scope_label = baseline.user_id.display_name
            kpi_label = baseline.kpi_id.code or baseline.kpi_id.name or "KPI"
            baseline.name = "%s baseline — %s — %s" % (
                kpi_label,
                scope_label,
                baseline.effective_from or "Draft",
            )

    @api.onchange("kpi_id", "source_period_start", "source_period_end")
    def _onchange_definition_version_for_source_period(self):
        for baseline in self:
            if not baseline.kpi_id or not baseline.source_period_start or not baseline.source_period_end:
                baseline.definition_version_id = False
                continue
            if baseline.source_period_start > baseline.source_period_end:
                baseline.definition_version_id = False
                continue
            try:
                baseline.definition_version_id = baseline.kpi_id._get_definition_version_for_period(
                    baseline.source_period_start, baseline.source_period_end
                )
            except ValidationError:
                baseline.definition_version_id = False

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and not self.env.user.has_group(
            "strategic_management.group_strategic_head_office_manager"
        ):
            raise AccessError("Only the Head Office Manager can create KPI baselines.")
        protected = {"approved_by_id", "approved_at", "created_by_id", "created_at", "effective_to"}
        normalized = []
        # Read context defaults explicitly: the form can show a readonly
        # KPI/version field populated by onchange even when the web client
        # omits that readonly field from the create payload.
        context_defaults = self.default_get(
            ["kpi_id", "source_period_start", "source_period_end"]
        )
        for values in vals_list:
            values = dict(values)
            if values.get("state", "draft") != "draft":
                raise ValidationError(
                    "KPI baselines must start in Draft and be approved through the approval workflow."
                )
            if protected.intersection(values):
                raise ValidationError("Baseline audit fields are system-controlled.")

            # Always resolve a missing readonly version server-side. This is
            # necessary because an onchange-populated readonly Many2one is not
            # guaranteed to be included in the web client's create payload.
            if not values.get("definition_version_id"):
                kpi_id = values.get("kpi_id") or context_defaults.get("kpi_id")
                source_start = (
                    values.get("source_period_start")
                    or context_defaults.get("source_period_start")
                )
                source_end = (
                    values.get("source_period_end")
                    or context_defaults.get("source_period_end")
                )
                if not kpi_id:
                    raise ValidationError(
                        "Select a KPI before creating its baseline."
                    )
                if not source_start or not source_end:
                    raise ValidationError(
                        "Enter the baseline source-period start and end dates so the KPI definition version can be resolved."
                    )
                kpi = self.env["strategic.kpi.definition"].browse(kpi_id).exists()
                if not kpi:
                    raise ValidationError(
                        "The selected KPI no longer exists; choose a valid KPI."
                    )
                version = kpi._get_definition_version_for_period(
                    source_start, source_end
                )
                values["definition_version_id"] = version.id

            normalized.append(values)
        return super().create(normalized)

    def write(self, vals):
        protected = {
            "kpi_id",
            "definition_version_id",
            "company_id",
            "state",
            "created_by_id",
            "created_at",
            "approved_by_id",
            "approved_at",
            "effective_to",
        }
        if protected.intersection(vals):
            raise ValidationError(
                "Baseline identity, workflow state and audit fields are system-controlled."
            )
        if any(baseline.state == "approved" for baseline in self):
            raise ValidationError(
                "Approved KPI baselines are immutable. Create a new baseline revision instead."
            )
        return super().write(vals)

    def unlink(self):
        if any(baseline.state == "approved" for baseline in self):
            raise ValidationError(
                "Approved KPI baselines cannot be deleted; create a new baseline revision instead."
            )
        return super().unlink()

    @api.constrains(
        "kpi_id",
        "definition_version_id",
        "scope",
        "warehouse_id",
        "user_id",
        "baseline_value",
        "source_period_start",
        "source_period_end",
        "effective_from",
        "effective_to",
    )
    def _check_baseline_contract(self):
        for baseline in self:
            if (
                baseline.source_period_start
                and baseline.source_period_end
                and baseline.source_period_start > baseline.source_period_end
            ):
                raise ValidationError(
                    "KPI baseline source-period start must not be after its end date."
                )
            if baseline.effective_to and baseline.effective_from and baseline.effective_to < baseline.effective_from:
                raise ValidationError("KPI baseline effective end must not be before its effective start.")
            if not math.isfinite(baseline.baseline_value):
                raise ValidationError("KPI baseline value must be a finite number.")

            if baseline.scope == "branch" and not baseline.warehouse_id:
                raise ValidationError("Branch KPI baselines require a branch warehouse.")
            if baseline.scope != "branch" and baseline.warehouse_id:
                raise ValidationError("Only branch KPI baselines may set a warehouse.")
            if baseline.scope == "user" and not baseline.user_id:
                raise ValidationError("Employee KPI baselines require an employee.")
            if baseline.scope != "user" and baseline.user_id:
                raise ValidationError("Only employee KPI baselines may set an employee.")

            if baseline.kpi_id and baseline.definition_version_id:
                version = baseline.definition_version_id
                if version.kpi_id != baseline.kpi_id:
                    raise ValidationError(
                        "The baseline definition version must belong to the selected KPI."
                    )
                if version.state != "approved":
                    raise ValidationError(
                        "A KPI baseline must reference an approved definition version."
                    )
                if baseline.source_period_start and baseline.source_period_end:
                    if (
                        version.effective_from > baseline.source_period_start
                        or (
                            version.effective_to
                            and version.effective_to < baseline.source_period_end
                        )
                    ):
                        raise ValidationError(
                            "The selected KPI definition version must cover the entire baseline source period."
                        )
                if (
                    baseline.warehouse_id
                    and baseline.warehouse_id.company_id != baseline.kpi_id.company_id
                ):
                    raise ValidationError(
                        "The baseline branch warehouse must belong to the KPI company."
                    )
                if (
                    baseline.user_id
                    and baseline.kpi_id.company_id not in baseline.user_id.company_ids
                ):
                    raise ValidationError(
                        "The baseline employee must have access to the KPI company."
                    )

    def action_approve(self):
        if not self.env.su and not self.env.user.has_group(
            "strategic_management.group_strategic_head_office_manager"
        ):
            raise AccessError("Only the Head Office Manager can approve KPI baselines.")

        for baseline in self:
            if baseline.state != "draft":
                raise ValidationError("Only draft KPI baselines can be approved.")
            baseline._check_baseline_contract()

            domain = [
                ("kpi_id", "=", baseline.kpi_id.id),
                ("scope", "=", baseline.scope),
                ("state", "=", "approved"),
            ]
            if baseline.scope == "branch":
                domain.append(("warehouse_id", "=", baseline.warehouse_id.id))
            else:
                domain.append(("warehouse_id", "=", False))
            if baseline.scope == "user":
                domain.append(("user_id", "=", baseline.user_id.id))
            else:
                domain.append(("user_id", "=", False))

            approved_versions = self.sudo().search(
                domain, order="effective_from desc, id desc"
            )
            if approved_versions.filtered(
                lambda other: other.effective_from == baseline.effective_from
            ):
                raise ValidationError(
                    "An approved baseline already exists for this KPI, scope and effective date."
                )
            latest = approved_versions[:1]
            if latest and baseline.effective_from <= latest.effective_from:
                raise ValidationError(
                    "A revised baseline must have a later effective date than the latest approved baseline for this scope."
                )
            if latest:
                linked_targets = self.env["strategic.kpi.target"].sudo().search([
                    ("baseline_id", "=", latest.id),
                    ("state", "=", "approved"),
                    ("date_end", ">=", baseline.effective_from),
                ])
                if linked_targets:
                    raise ValidationError(
                        "An approved target linked to the current baseline extends into the new baseline period. "
                        "Revise or replace that target before approving this baseline revision."
                    )
                super(StrategicKpiBaseline, latest).write({
                    "effective_to": baseline.effective_from - timedelta(days=1),
                })

            # Use the model implementation directly for this controlled workflow
            # transition, while ordinary write() continues to protect audit fields.
            super(StrategicKpiBaseline, baseline).write(
                {
                    "state": "approved",
                    "approved_by_id": self.env.user.id,
                    "approved_at": fields.Datetime.now(),
                }
            )
        return True
