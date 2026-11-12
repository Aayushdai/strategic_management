"""KPI targets: the value a KPI should reach for one period and scope, approved by Head Office."""

import math
from odoo import api, fields, models
from odoo.exceptions import ValidationError

from .kpi import (KPI_SCOPES)


class StrategicKpiTarget(models.Model):
    _name = "strategic.kpi.target"
    _description = "Strategic KPI Period Target"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "date_start desc, kpi_id"
    name = fields.Char(compute="_compute_name", store=True)
    kpi_id = fields.Many2one("strategic.kpi.definition", required=True, ondelete="restrict", index=True)
    calculation_type = fields.Selection(related="kpi_id.calculation_type", readonly=True)
    company_id = fields.Many2one(related="kpi_id.company_id", store=True, readonly=True)
    date_start = fields.Date(required=True, index=True)
    date_end = fields.Date(required=True, index=True)
    scope = fields.Selection(
        KPI_SCOPES,
        required=True,
        default="organization",
        index=True,
    )
    warehouse_id = fields.Many2one("stock.warehouse", string="Branch Warehouse", index=True)
    user_id = fields.Many2one("res.users", string="Employee", index=True)
    target_basis = fields.Selection(
        [
            ("absolute", "Absolute Target"),
            ("baseline_relative", "Baseline-relative Target"),
        ],
        required=True,
        default="absolute",
        tracking=True,
        help=(
            "For a baseline-relative target, select an approved baseline. "
            "Target Value remains the final approved numeric threshold; the baseline is retained as its comparison and audit anchor."
        ),
    )
    baseline_id = fields.Many2one(
        "strategic.kpi.baseline",
        string="Approved Baseline",
        ondelete="restrict",
        index=True,
        help="Required for a baseline-relative target. The selected baseline must match KPI, scope, and effective period.",
    )
    target_value = fields.Float(
        help="Final approved numeric target threshold. For baseline-relative targets, this is the resolved threshold, not a delta or percentage change."
    )
    range_min = fields.Float()
    range_max = fields.Float()
    unit = fields.Char(related="kpi_id.unit", readonly=True)
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("approved", "Approved"),
            ("archived", "Archived"),
        ],
        required=True,
        default="draft",
        tracking=True,
    )
    evaluation_ids = fields.One2many("strategic.kpi.evaluation", "target_id")

    @api.depends("kpi_id.name", "date_start", "date_end", "scope", "warehouse_id.name", "user_id.name")
    def _compute_name(self):
        for target in self:
            scope_name = dict(target._fields["scope"].selection).get(target.scope)
            if target.scope == "branch" and target.warehouse_id:
                scope_name = target.warehouse_id.display_name
            elif target.scope == "user" and target.user_id:
                scope_name = target.user_id.display_name
            target.name = "%s: %s (%s to %s)" % (
                target.kpi_id.name or "KPI",
                scope_name or "Scope",
                target.date_start or "",
                target.date_end or "",
            )

    @api.model
    def _resolve_for_period(
        self,
        kpi,
        date_start,
        date_end,
        scope=None,
        warehouse_id=None,
        user_id=None,
        required=True,
        definition_version=None,
        key_result=None,
    ):
        """Resolve exactly one approved target for a KPI reporting period.

        Generic KPI monitoring resolves by KPI, scope and full period coverage.
        Key Result scoring must pass ``key_result``; in that mode this method
        validates and returns only its explicitly linked target and never falls
        back to a generic KPI target.

        If ``required`` is false, no matching generic target returns an empty
        target recordset. Ambiguous matches always fail closed.
        """
        Kpi = self.env["strategic.kpi.definition"]
        if not getattr(kpi, "_name", None) == "strategic.kpi.definition":
            try:
                kpi = Kpi.browse(int(kpi))
            except (TypeError, ValueError):
                kpi = Kpi.browse()
        else:
            kpi = kpi.with_env(self.env)
        kpi = kpi.exists()
        if len(kpi) != 1:
            raise ValidationError("Select exactly one valid KPI to resolve its target.")

        start = fields.Date.to_date(date_start) if date_start else False
        end = fields.Date.to_date(date_end) if date_end else False
        if not start or not end or start > end:
            raise ValidationError("A valid KPI target reporting period is required.")

        def _record_id(value):
            return getattr(value, "id", value) or False

        KeyResult = self.env["strategic.key.result"]
        key_result_record = False
        if key_result:
            if not getattr(key_result, "_name", None) == "strategic.key.result":
                key_result = KeyResult.browse(_record_id(key_result))
            else:
                key_result = key_result.with_env(self.env)
            key_result_record = key_result.exists()
            if len(key_result_record) != 1:
                raise ValidationError("The supplied Key Result does not exist.")
            key_result_record = key_result_record[0]
            if key_result_record.primary_kpi_id != kpi:
                raise ValidationError(
                    "The Key Result's primary KPI does not match the KPI being resolved."
                )
            # A Key Result owns an explicit target link. Never search generic
            # targets as a fallback when resolving Key Result scoring.
            if scope is None:
                scope = key_result_record.scope
            if warehouse_id is None:
                warehouse_id = key_result_record.warehouse_id.id or False
            if user_id is None:
                user_id = False
            if key_result_record.date_start != start or key_result_record.date_end != end:
                raise ValidationError(
                    "Key Result target resolution must use the exact Key Result reporting period."
                )
            if key_result_record.scope != scope:
                raise ValidationError("The resolved target scope must match the Key Result scope.")
            if key_result_record.warehouse_id.id != _record_id(warehouse_id):
                raise ValidationError("The resolved target branch must match the Key Result branch.")
        else:
            scope = scope or "organization"
            warehouse_id = _record_id(warehouse_id)
            user_id = _record_id(user_id)

        if scope not in {"organization", "branch", "user"}:
            raise ValidationError("Choose a valid target scope.")
        if scope == "branch" and not _record_id(warehouse_id):
            raise ValidationError("Branch target resolution requires a branch warehouse.")
        if scope != "branch" and _record_id(warehouse_id):
            raise ValidationError("Only branch target resolution may specify a warehouse.")
        if scope == "user" and not _record_id(user_id):
            raise ValidationError("Employee target resolution requires an employee.")
        if scope != "user" and _record_id(user_id):
            raise ValidationError("Only employee target resolution may specify an employee.")

        # Resolve the one approved KPI definition contract applicable to the
        # entire requested period. If explicitly supplied, it must be that
        # exact version; definition versions and target periods are independent.
        expected_definition_version = kpi._get_definition_version_for_period(start, end)
        if definition_version:
            if not getattr(definition_version, "_name", None) == "strategic.kpi.definition.version":
                definition_version = self.env["strategic.kpi.definition.version"].browse(
                    _record_id(definition_version)
                )
            else:
                definition_version = definition_version.with_env(self.env)
            if definition_version != expected_definition_version:
                raise ValidationError(
                    "The requested KPI definition version does not cover the full target reporting period."
                )

        def _matches_context(target, require_exact_period=False):
            if not target or target.state != "approved" or target.kpi_id != kpi:
                return False
            if target.scope != scope:
                return False
            if target.warehouse_id.id != _record_id(warehouse_id):
                return False
            if target.user_id.id != _record_id(user_id):
                return False
            if require_exact_period:
                if target.date_start != start or target.date_end != end:
                    return False
            elif not (
                target.date_start and target.date_end
                and target.date_start <= start and target.date_end >= end
            ):
                return False
            # Reject a target that crosses KPI formula-version boundaries. A
            # target is eligible only when its whole effective period resolves
            # to the same approved KPI definition version as this request.
            try:
                target_version = target.kpi_id._get_definition_version_for_period(
                    target.date_start, target.date_end
                )
            except ValidationError:
                return False
            if target_version != expected_definition_version:
                return False
            if target.target_basis == "baseline_relative":
                baseline = target.baseline_id
                if not baseline or baseline.state != "approved":
                    return False
                if baseline.kpi_id != kpi or baseline.scope != scope:
                    return False
                if baseline.warehouse_id.id != _record_id(warehouse_id):
                    return False
                if baseline.user_id.id != _record_id(user_id):
                    return False
                if baseline.effective_from and baseline.effective_from > start:
                    return False
                if baseline.effective_to and baseline.effective_to < end:
                    return False
            elif target.baseline_id:
                # Do not use a malformed absolute target that carries a baseline.
                return False
            return True

        if key_result_record:
            explicit_target = key_result_record.target_id.exists()
            if not explicit_target or not _matches_context(
                explicit_target, require_exact_period=True
            ):
                raise ValidationError(
                    "The Key Result's explicitly linked target is missing, unapproved, or does not match its KPI, version, scope and period."
                )
            return explicit_target

        domain = [
            ("kpi_id", "=", kpi.id),
            ("company_id", "=", kpi.company_id.id),
            ("state", "=", "approved"),
            ("scope", "=", scope),
            ("date_start", "<=", start),
            ("date_end", ">=", end),
        ]
        domain.append(("warehouse_id", "=", _record_id(warehouse_id)))
        domain.append(("user_id", "=", _record_id(user_id)))
        candidates = self.search(domain).filtered(_matches_context)

        if not candidates:
            if not required:
                return self.browse()
            raise ValidationError(
                "No unique approved KPI target covers the full period %s to %s for %s at scope %s. "
                "Create/approve a target for the exact KPI, scope and period; generic targets are not used as Key Result fallbacks."
                % (start, end, kpi.display_name, scope)
            )
        if len(candidates) != 1:
            raise ValidationError(
                "More than one approved KPI target matches %s to %s for %s at scope %s. "
                "Resolve the overlapping target periods; KPI evaluation will not choose one arbitrarily."
                % (start, end, kpi.display_name, scope)
            )
        return candidates[0]

    @api.constrains("date_start", "date_end", "scope", "warehouse_id", "user_id")
    def _check_target_scope_and_dates(self):
        for target in self:
            if target.date_start and target.date_end and target.date_start > target.date_end:
                raise ValidationError("KPI target start date must not be after end date.")
            if target.scope == "branch" and not target.warehouse_id:
                raise ValidationError("Branch KPI targets require a branch warehouse.")
            if target.scope != "branch" and target.warehouse_id:
                raise ValidationError("Only branch KPI targets may set a warehouse.")
            if target.scope == "user" and not target.user_id:
                raise ValidationError("Employee KPI targets require an employee.")
            if target.scope != "user" and target.user_id:
                raise ValidationError("Only employee KPI targets may set an employee.")

    @api.constrains("scope", "kpi_id")
    def _check_individual_ownership(self):
        for target in self:
            if target.scope == "user" and not target.kpi_id.individual_outcome:
                raise ValidationError(
                    "%s is not an individual employee KPI, so it cannot have an employee target. "
                    "Set it at organization or branch level, and link the employee's work through tasks." % target.kpi_id.display_name
                )

    @api.constrains("target_value", "range_min", "range_max", "kpi_id", "date_start", "date_end")
    def _check_target_values(self):
        for target in self:
            version = target.kpi_id._get_definition_version_for_period(
                target.date_start, target.date_end
            )
            numeric_values = (target.target_value, target.range_min, target.range_max)
            if any(not math.isfinite(value) for value in numeric_values):
                raise ValidationError("KPI target values must be finite numbers.")
            if version.direction == "range":
                if target.range_min > target.range_max:
                    raise ValidationError("KPI range minimum must not be greater than maximum.")
            elif target.target_value < 0:
                raise ValidationError("One-sided KPI targets cannot be negative in the first vertical slice.")

    @api.constrains(
        "target_basis", "baseline_id", "kpi_id", "date_start", "date_end",
        "scope", "warehouse_id", "user_id", "state",
    )
    def _check_target_baseline_contract(self):
        for target in self:
            if target.target_basis == "absolute":
                if target.baseline_id:
                    raise ValidationError(
                        "Absolute targets cannot reference a baseline. Select Baseline-relative Target or clear the baseline."
                    )
                continue

            if not target.baseline_id:
                raise ValidationError(
                    "Baseline-relative targets require an explicitly selected approved baseline."
                )
            baseline = target.baseline_id
            if baseline.state != "approved":
                raise ValidationError("A baseline-relative target must reference an approved baseline.")
            if baseline.kpi_id != target.kpi_id:
                raise ValidationError("The selected baseline must belong to the same KPI as the target.")
            if baseline.scope != target.scope:
                raise ValidationError("The selected baseline must use the same scope as the target.")
            if target.scope == "branch" and baseline.warehouse_id != target.warehouse_id:
                raise ValidationError("The selected baseline must belong to the same branch as the target.")
            if target.scope == "user" and baseline.user_id != target.user_id:
                raise ValidationError("The selected baseline must belong to the same employee as the target.")
            if not target.date_start or not target.date_end:
                raise ValidationError("Set the target period before selecting a baseline.")
            if baseline.effective_from and baseline.effective_from > target.date_start:
                raise ValidationError("The selected baseline is not yet effective at the start of the target period.")
            if baseline.effective_to and baseline.effective_to < target.date_end:
                raise ValidationError("The selected baseline does not cover the full target period.")

            target_version = target.kpi_id._get_definition_version_for_period(
                target.date_start, target.date_end
            )
            baseline_version = baseline.definition_version_id
            if (
                baseline_version.unit != target_version.unit
                or baseline_version.direction != target_version.direction
            ):
                raise ValidationError(
                    "The selected baseline KPI unit and performance direction must match the target-period KPI definition version."
                )

    def action_approve(self):
        for target in self:
            target._check_target_scope_and_dates()
            target._check_individual_ownership()
            target._check_target_values()
            target._check_target_baseline_contract()
        self.write({"state": "approved"})

    def action_archive(self):
        self.write({"state": "archived"})
