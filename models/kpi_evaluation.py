"""KPI evaluations: the recorded result of one KPI target, kept as an immutable history."""

import math
from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.tools import float_is_zero

from .kpi import (KPI_SCOPES)


class StrategicKpiEvaluation(models.Model):
    _name = "strategic.kpi.evaluation"
    _description = "Strategic KPI Evaluation History"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "evaluated_at desc, id desc"
    name = fields.Char(compute="_compute_name", store=True)
    kpi_id = fields.Many2one("strategic.kpi.definition", required=True, ondelete="restrict", index=True)
    target_id = fields.Many2one("strategic.kpi.target", ondelete="restrict", index=True)
    definition_version_id = fields.Many2one(
        "strategic.kpi.definition.version", required=False, readonly=True,
        ondelete="restrict", index=True, copy=False,
        string="KPI Definition Version",
    )
    target_basis = fields.Selection(
        [("absolute", "Absolute Target"), ("baseline_relative", "Baseline-relative Target")],
        required=True,
        default="absolute",
        readonly=True,
        copy=False,
    )
    baseline_id = fields.Many2one(
        "strategic.kpi.baseline", readonly=True, ondelete="restrict", index=True, copy=False
    )
    baseline_value = fields.Float(
        readonly=True,
        copy=False,
        help="Snapshot of the immutable baseline value linked to the target used for this evaluation.",
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
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
    target_value = fields.Float(readonly=True)
    range_min = fields.Float(readonly=True)
    range_max = fields.Float(readonly=True)
    actual_value = fields.Float()
    has_actual_value = fields.Boolean(default=False)
    unit = fields.Char()
    gap = fields.Float(readonly=True)
    attainment = fields.Float(
        readonly=True,
        aggregator="avg",
        help="Attainment percentage; reporting groups average evaluated attainment rather than summing percentages.",
    )
    progress_to_target = fields.Float(
        readonly=True,
        aggregator="avg",
        help="Baseline-relative targets only: share of the distance from baseline to target covered, "
             "(baseline - actual) / (baseline - target) x 100 for lower-is-better. Below 0 means worse than "
             "baseline; above 100 means beyond target. Separate from attainment.",
    )
    has_progress_to_target = fields.Boolean(readonly=True)
    status = fields.Selection(
        [
            ("valid", "Valid / Evaluated"),
            ("not_evaluated", "Not Evaluated"),
            ("data_quality_issue", "Data Quality Issue"),
            ("pending_review", "Pending Review"),
        ],
        required=True,
        default="not_evaluated",
        tracking=True,
    )
    attainment_status = fields.Selection(
        [
            ("not_evaluated", "Not Evaluated"),
            ("data_quality_issue", "Data Quality Issue"),
            ("pending_review", "Pending Review"),
            ("not_achieved", "Not Achieved"),
            ("almost_met", "Almost Met"),
            ("achieved", "Achieved"),
            ("exceeded", "Exceeded"),
        ],
        required=True,
        default="not_evaluated",
        tracking=True,
    )
    reason = fields.Text(readonly=True)
    source_model = fields.Char()
    source_record_ref = fields.Char(string="Source Record References")
    source_note = fields.Text(
        string="Source Calculation Notes", readonly=True, copy=False,
        help="Explanation or qualification returned by the operational source adapter.",
    )
    source_key = fields.Char(
        string="Source Snapshot Key", index=True, readonly=True, copy=False,
        help="Deterministic fingerprint preventing duplicate evaluations from an unchanged operational source snapshot.",
    )
    evaluated_at = fields.Datetime(default=fields.Datetime.now, required=True, readonly=True)
    evaluated_by_id = fields.Many2one(
        "res.users",
        default=lambda self: self.env.user,
        required=True,
        readonly=True,
    )
    superseded_by_id = fields.Many2one("strategic.kpi.evaluation", readonly=True, copy=False)

    _source_key_unique = models.Constraint(
        "UNIQUE(source_key)",
        "An operational source snapshot can only create one KPI evaluation.",
    )

    @api.depends("kpi_id.name", "date_start", "date_end")
    def _compute_name(self):
        for evaluation in self:
            evaluation.name = "%s evaluation (%s to %s)" % (
                evaluation.kpi_id.name or "KPI",
                evaluation.date_start or "",
                evaluation.date_end or "",
            )

    @api.onchange("target_id", "kpi_id", "actual_value", "has_actual_value")
    def _onchange_target_or_actual(self):
        """Preview target snapshots and attainment before the evaluation is saved."""
        for evaluation in self:
            target = evaluation.target_id
            if target:
                # The target is authoritative for all target/scope snapshot fields.
                evaluation.kpi_id = target.kpi_id
                evaluation.company_id = target.company_id
                evaluation.date_start = target.date_start
                evaluation.date_end = target.date_end
                evaluation.scope = target.scope
                evaluation.warehouse_id = target.warehouse_id
                evaluation.user_id = target.user_id
                evaluation.target_value = target.target_value
                evaluation.range_min = target.range_min
                evaluation.range_max = target.range_max
                evaluation.target_basis = target.target_basis
                evaluation.baseline_id = target.baseline_id
                evaluation.baseline_value = target.baseline_id.baseline_value if target.baseline_id else 0.0
                kpi = target.kpi_id
            else:
                kpi = evaluation.kpi_id
            if not kpi:
                evaluation.gap = 0.0
                evaluation.attainment = 0.0
                evaluation.status = "not_evaluated"
                evaluation.attainment_status = "not_evaluated"
                evaluation.reason = "Select a KPI or target before evaluating."
                continue
            version = False
            if evaluation.date_start and evaluation.date_end:
                try:
                    version = kpi._get_definition_version_for_period(
                        evaluation.date_start, evaluation.date_end
                    )
                except ValidationError as exc:
                    evaluation.gap = 0.0
                    evaluation.attainment = 0.0
                    evaluation.status = "not_evaluated"
                    evaluation.attainment_status = "not_evaluated"
                    evaluation.reason = str(exc)
                    continue
            direction = version.direction if version else kpi.direction
            evaluation.definition_version_id = version
            evaluation.unit = version.unit if version else kpi.unit
            result = self._calculate_result(
                direction=direction,
                target_value=evaluation.target_value or 0.0,
                range_min=evaluation.range_min or 0.0,
                range_max=evaluation.range_max or 0.0,
                actual_value=evaluation.actual_value or 0.0,
                has_actual_value=evaluation.has_actual_value,
            )
            evaluation.gap = result["gap"]
            evaluation.attainment = result["attainment"]
            evaluation.status = result["status"]
            evaluation.attainment_status = result["attainment_status"]
            evaluation.reason = result["reason"]

    @api.model_create_multi
    def create(self, vals_list):
        if any("source_key" in vals or "source_note" in vals for vals in vals_list):
            raise AccessError("Operational source provenance is assigned only by the controlled source adapter workflow.")
        return super().create([self._prepare_evaluation_values(vals) for vals in vals_list])

    def write(self, vals):
        immutable = {
            "kpi_id",
            "target_id",
            "definition_version_id",
            "target_basis",
            "baseline_id",
            "baseline_value",
            "company_id",
            "date_start",
            "date_end",
            "scope",
            "warehouse_id",
            "user_id",
            "target_value",
            "range_min",
            "range_max",
            "actual_value",
            "has_actual_value",
            "unit",
            "gap",
            "attainment",
            "progress_to_target",
            "has_progress_to_target",
            "status",
            "attainment_status",
            "reason",
            "source_model",
            "source_record_ref",
            "source_note",
            "source_key",
        }
        if immutable.intersection(vals):
            raise ValidationError("KPI evaluations are historical records. Create a new evaluation or correction instead of editing results.")
        return super().write(vals)

    @api.model
    def _create_from_source_adapter(self, target, version, result):
        """Create an immutable evaluation for one normalized source result.

        User-triggered callers must perform role and scope checks before reaching
        this RPC-inaccessible method. Trusted internal event hooks must likewise
        restrict company, target state, source calculation, period and branch scope.
        It deliberately bypasses the public create guard only for the internal
        source_key/provenance payload.
        """
        target.ensure_one()
        version.ensure_one()
        source_key = result.get("source_key")
        if not source_key:
            raise ValidationError("The source adapter did not provide an idempotency key.")
        Evaluation = self.sudo()
        existing = Evaluation.search([("source_key", "=", source_key)], limit=1)
        if existing:
            Evaluation._sync_linked_key_result_evaluation(target, existing)
            return existing

        prepared = Evaluation._prepare_source_adapter_values(target, result)
        prepared["source_key"] = source_key
        # Call the inherited ORM create directly: the public override intentionally
        # rejects a caller-supplied source_key. This method is private (RPC-inaccessible).
        evaluation = super(StrategicKpiEvaluation, Evaluation).create([prepared])
        previous = Evaluation.search([
            ("target_id", "=", target.id),
            ("source_key", "!=", False),
            ("id", "!=", evaluation.id),
            ("superseded_by_id", "=", False),
        ], order="evaluated_at desc, id desc")
        if previous:
            previous[:1].write({"superseded_by_id": evaluation.id})
        Evaluation._sync_linked_key_result_evaluation(target, evaluation)
        return evaluation

    @api.model
    def _prepare_source_adapter_values(self, target, result):
        """Return evaluation values for one source result without creating a record.

        Shared by immutable evaluation creation and the live (read-only) branch
        dashboard preview so both apply the same attainment rules.
        """
        is_valid = result.get("status") == "valid" and result.get("has_value", False)
        vals = {
            "target_id": target.id,
            "actual_value": result.get("value") if is_valid else 0.0,
            "has_actual_value": bool(is_valid),
            "source_model": result.get("source_model") or False,
            "source_record_ref": result.get("source_record_ref") or False,
        }
        prepared = self._prepare_evaluation_values(vals)
        prepared.update({
            "source_model": result.get("source_model") or False,
            "source_record_ref": result.get("source_record_ref") or False,
            "source_note": result.get("reason") or False,
        })
        if not is_valid:
            reason = result.get("reason") or "The operational source did not produce a valid measurement."
            status = result.get("status") if result.get("status") in ("not_evaluated", "data_quality_issue") else "data_quality_issue"
            prepared.update({
                "actual_value": 0.0,
                "has_actual_value": False,
                "gap": 0.0,
                "attainment": 0.0,
                "progress_to_target": 0.0,
                "has_progress_to_target": False,
                "status": status,
                "attainment_status": status,
                "reason": reason,
            })
        return prepared

    @api.model
    def _sync_linked_key_result_evaluation(self, target, evaluation):
        """Link the current source snapshot to the Key Result explicitly assigned to this target."""
        target.ensure_one()
        evaluation.ensure_one()
        if evaluation.target_id != target or evaluation.superseded_by_id:
            return

        key_results = self.env["strategic.key.result"].sudo().search([
            ("target_id", "=", target.id),
            ("primary_kpi_id", "=", target.kpi_id.id),
        ])
        if len(key_results) > 1:
            raise ValidationError(
                "This KPI target is linked to more than one Key Result; "
                "the current evaluation cannot be assigned unambiguously."
            )
        if key_results and key_results.evaluation_id != evaluation:
            key_results.write({"evaluation_id": evaluation.id})

    @api.constrains("date_start", "date_end", "scope", "warehouse_id", "user_id")
    def _check_evaluation_scope_and_dates(self):
        for evaluation in self:
            if evaluation.date_start and evaluation.date_end and evaluation.date_start > evaluation.date_end:
                raise ValidationError("KPI evaluation start date must not be after end date.")
            if evaluation.scope == "branch" and not evaluation.warehouse_id:
                raise ValidationError("Branch KPI evaluations require a branch warehouse.")
            if evaluation.scope != "branch" and evaluation.warehouse_id:
                raise ValidationError("Only branch KPI evaluations may set a warehouse.")
            if evaluation.scope == "user" and not evaluation.user_id:
                raise ValidationError("Employee KPI evaluations require an employee.")
            if evaluation.scope != "user" and evaluation.user_id:
                raise ValidationError("Only employee KPI evaluations may set an employee.")

    @api.model
    def _prepare_evaluation_values(self, vals):
        vals = dict(vals)
        target = self.env["strategic.kpi.target"].browse(vals["target_id"]).exists() if vals.get("target_id") else False
        if vals.get("target_id") and not target:
            raise ValidationError("The selected KPI target no longer exists.")
        if target and target.state != "approved":
            raise ValidationError("KPI evaluations require an approved, non-archived target.")
        if target:
            Target = self.env["strategic.kpi.target"]
            # A target linked explicitly from a Key Result must be validated in
            # that Key Result's context. Generic KPI monitoring may legitimately
            # have another target for the same period/scope; that must not make
            # an explicit KR target ambiguous or trigger a generic fallback.
            # Use sudo only to discover the relational owner(s) for validation;
            # this does not grant the caller read access to Key Result records.
            linked_key_results = self.env["strategic.key.result"].sudo().search([
                ("target_id", "=", target.id),
                ("primary_kpi_id", "=", target.kpi_id.id),
            ])
            if linked_key_results:
                if len(linked_key_results) != 1:
                    raise ValidationError(
                        "This KPI target is explicitly linked to more than one Key Result. "
                        "Each Key Result scoring context must have one unambiguous target link."
                    )
                key_result = linked_key_results[0]
                resolved_target = Target._resolve_for_period(
                    target.kpi_id,
                    key_result.date_start,
                    key_result.date_end,
                    scope=key_result.scope,
                    warehouse_id=key_result.warehouse_id.id or False,
                    user_id=False,
                    required=True,
                    key_result=key_result,
                )
            else:
                # Targets not explicitly assigned to a Key Result use the
                # generic KPI-monitoring resolver, which fails closed on
                # missing or overlapping approved matches.
                resolved_target = Target._resolve_for_period(
                    target.kpi_id,
                    target.date_start,
                    target.date_end,
                    scope=target.scope,
                    warehouse_id=target.warehouse_id.id or False,
                    user_id=target.user_id.id or False,
                    required=True,
                )
            if resolved_target != target:
                raise ValidationError(
                    "The selected KPI target is not valid for this KPI reporting context."
                )
        kpi = target.kpi_id if target else self.env["strategic.kpi.definition"].browse(vals["kpi_id"])
        date_start = target.date_start if target else vals.get("date_start")
        date_end = target.date_end if target else vals.get("date_end")
        version = kpi._get_definition_version_for_period(date_start, date_end)
        vals["definition_version_id"] = version.id
        if target:
            # Always snapshot authoritative values from the selected target.
            # The web client can submit default zero/empty values from the form,
            # so setdefault() can preserve those defaults incorrectly.
            vals.update({
                "kpi_id": target.kpi_id.id,
                "company_id": target.company_id.id,
                "date_start": target.date_start,
                "date_end": target.date_end,
                "scope": target.scope,
                "warehouse_id": target.warehouse_id.id or False,
                "user_id": target.user_id.id or False,
                "target_value": target.target_value,
                "range_min": target.range_min,
                "range_max": target.range_max,
                "unit": version.unit,
                "target_basis": target.target_basis,
                "baseline_id": target.baseline_id.id or False,
                "baseline_value": target.baseline_id.baseline_value if target.baseline_id else 0.0,
            })
        else:
            vals.setdefault("unit", version.unit)
            # Baseline references are server-derived snapshots. A caller cannot
            # manufacture a baseline reference on a standalone evaluation.
            vals["target_basis"] = "absolute"
            vals["baseline_id"] = False
            vals["baseline_value"] = 0.0
        vals["unit"] = version.unit
        vals.update(
            self._calculate_result(
                direction=version.direction,
                target_value=vals.get("target_value", 0.0),
                range_min=vals.get("range_min", 0.0),
                range_max=vals.get("range_max", 0.0),
                actual_value=vals.get("actual_value", 0.0),
                has_actual_value=vals.get("has_actual_value", False),
            )
        )
        vals.update(self._calculate_baseline_progress(version.direction, vals))
        return vals

    @api.model
    def _calculate_baseline_progress(self, direction, vals):
        """Progress from baseline toward target; only for valid baseline-relative evaluations."""
        none = {"progress_to_target": 0.0, "has_progress_to_target": False}
        if vals.get("target_basis") != "baseline_relative" or vals.get("status") != "valid" or not vals.get("has_actual_value"):
            return none
        baseline = vals.get("baseline_value", 0.0)
        target = vals.get("target_value", 0.0)
        actual = vals.get("actual_value", 0.0)
        if direction == "lower":
            span, moved = baseline - target, baseline - actual
        elif direction == "higher":
            span, moved = target - baseline, actual - baseline
        else:
            return none
        # The target must improve on the baseline; otherwise progress is undefined.
        if not all(self._is_finite_number(value) for value in (baseline, target, actual)) or span <= 0 or float_is_zero(span, precision_digits=6):
            return none
        return {"progress_to_target": round(moved / span * 100.0, 4), "has_progress_to_target": True}

    @api.model
    def _calculate_result(self, direction, target_value=0.0, range_min=0.0, range_max=0.0, actual_value=0.0, has_actual_value=False):
        if not has_actual_value:
            return self._not_evaluated("Actual KPI source data is missing.")
        numbers = [target_value, range_min, range_max, actual_value]
        if any(not self._is_finite_number(value) for value in numbers):
            return self._data_quality_issue("KPI calculation received a non-finite numeric value.")
        precision = 6
        if direction == "range":
            if range_min > range_max:
                return self._data_quality_issue("Range minimum is greater than range maximum.")
            in_range = range_min <= actual_value <= range_max
            return {
                "gap": 0.0 if in_range else min(abs(actual_value - range_min), abs(actual_value - range_max)),
                "attainment": 100.0 if in_range else 0.0,
                "status": "valid",
                "attainment_status": "achieved" if in_range else "not_achieved",
                "reason": False,
            }
        if float_is_zero(target_value, precision_digits=precision):
            return self._not_evaluated("KPI target is zero; attainment cannot be divided safely.")
        if target_value < 0:
            return self._data_quality_issue("Negative one-sided KPI targets are not supported by this calculation rule.")
        if direction == "higher":
            attainment = (actual_value / target_value) * 100.0
            gap = actual_value - target_value
        elif direction == "lower":
            if float_is_zero(actual_value, precision_digits=precision):
                attainment = 100.0 if target_value >= 0 else 0.0
            else:
                attainment = (target_value / actual_value) * 100.0
            gap = target_value - actual_value
        else:
            return self._data_quality_issue("Unsupported KPI direction.")
        if not self._is_finite_number(attainment):
            return self._data_quality_issue("KPI attainment calculation did not produce a finite number.")
        return {
            "gap": gap,
            "attainment": attainment,
            "status": "valid",
            "attainment_status": self._attainment_status(attainment),
            "reason": False,
        }

    @api.model
    def _attainment_status(self, attainment):
        if attainment < 80.0:
            return "not_achieved"
        if attainment < 100.0:
            return "almost_met"
        if math.isclose(attainment, 100.0, rel_tol=0.0, abs_tol=0.000001):
            return "achieved"
        return "exceeded"

    @api.model
    def _not_evaluated(self, reason):
        return {
            "gap": 0.0,
            "attainment": 0.0,
            "status": "not_evaluated",
            "attainment_status": "not_evaluated",
            "reason": reason,
        }

    @api.model
    def _data_quality_issue(self, reason):
        return {
            "gap": 0.0,
            "attainment": 0.0,
            "status": "data_quality_issue",
            "attainment_status": "data_quality_issue",
            "reason": reason,
        }

    @staticmethod
    def _is_finite_number(value):
        return isinstance(value, (int, float)) and math.isfinite(value)
