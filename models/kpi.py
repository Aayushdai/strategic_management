import math
from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import float_is_zero

class StrategicKpiDefinition(models.Model):
    _name = "strategic.kpi.definition"
    _description = "Strategic KPI Definition"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "code, name"
    name = fields.Char(required=True, tracking=True)
    code = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    unit = fields.Char(required=True, default="Unit")
    direction = fields.Selection(
        [
            ("higher", "Higher is Better"),
            ("lower", "Lower is Better"),
            ("range", "Target Range"),
        ],
        required=True,
        default="higher",
        tracking=True,
    )
    source_type = fields.Selection(
        [
            ("manual", "Controlled Manual Submission"),
            ("accounting", "Accounting"),
            ("purchase", "Purchase"),
            ("pos", "Point of Sale"),
            ("inventory", "Inventory"),
            ("other", "Other"),
        ],
        required=True,
        default="manual",
        tracking=True,
    )
    description = fields.Text()
    target_ids = fields.One2many("strategic.kpi.target", "kpi_id")
    evaluation_ids = fields.One2many("strategic.kpi.evaluation", "kpi_id")
    _code_company_unique = models.Constraint(
        "UNIQUE(code, company_id)",
        "KPI code must be unique per company.",
    )

class StrategicKpiTarget(models.Model):
    _name = "strategic.kpi.target"
    _description = "Strategic KPI Period Target"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "date_start desc, kpi_id"
    name = fields.Char(compute="_compute_name", store=True)
    kpi_id = fields.Many2one("strategic.kpi.definition", required=True, ondelete="restrict", index=True)
    company_id = fields.Many2one(related="kpi_id.company_id", store=True, readonly=True)
    date_start = fields.Date(required=True, index=True)
    date_end = fields.Date(required=True, index=True)
    scope = fields.Selection(
        [
            ("organization", "Organization"),
            ("branch", "Branch"),
            ("user", "Employee"),
        ],
        required=True,
        default="organization",
        index=True,
    )
    warehouse_id = fields.Many2one("stock.warehouse", string="Branch Warehouse", index=True)
    user_id = fields.Many2one("res.users", string="Employee", index=True)
    target_value = fields.Float()
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

    @api.constrains("target_value", "range_min", "range_max", "kpi_id")
    def _check_target_values(self):
        for target in self:
            if target.kpi_id.direction == "range":
                if target.range_min > target.range_max:
                    raise ValidationError("KPI range minimum must not be greater than maximum.")
            elif target.target_value < 0:
                raise ValidationError("One-sided KPI targets cannot be negative in the first vertical slice.")

    def action_approve(self):
        self.write({"state": "approved"})

    def action_archive(self):
        self.write({"state": "archived"})

class StrategicKpiEvaluation(models.Model):
    _name = "strategic.kpi.evaluation"
    _description = "Strategic KPI Evaluation History"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "evaluated_at desc, id desc"
    name = fields.Char(compute="_compute_name", store=True)
    kpi_id = fields.Many2one("strategic.kpi.definition", required=True, ondelete="restrict", index=True)
    target_id = fields.Many2one("strategic.kpi.target", ondelete="restrict", index=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    date_start = fields.Date(required=True, index=True)
    date_end = fields.Date(required=True, index=True)
    scope = fields.Selection(
        [
            ("organization", "Organization"),
            ("branch", "Branch"),
            ("user", "Employee"),
        ],
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
    attainment = fields.Float(readonly=True, help="Attainment percentage.")
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
    evaluated_at = fields.Datetime(default=fields.Datetime.now, required=True, readonly=True)
    evaluated_by_id = fields.Many2one(
        "res.users",
        default=lambda self: self.env.user,
        required=True,
        readonly=True,
    )
    superseded_by_id = fields.Many2one("strategic.kpi.evaluation", readonly=True, copy=False)

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
                evaluation.unit = target.unit
                kpi = target.kpi_id
            else:
                kpi = evaluation.kpi_id
                if kpi:
                    evaluation.unit = kpi.unit
            if not kpi:
                evaluation.gap = 0.0
                evaluation.attainment = 0.0
                evaluation.status = "not_evaluated"
                evaluation.attainment_status = "not_evaluated"
                evaluation.reason = "Select a KPI or target before evaluating."
                continue
            result = self._calculate_result(
                direction=kpi.direction,
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
        return super().create([self._prepare_evaluation_values(vals) for vals in vals_list])

    def write(self, vals):
        immutable = {
            "kpi_id",
            "target_id",
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
            "status",
            "attainment_status",
            "reason",
            "source_model",
            "source_record_ref",
        }
        if immutable.intersection(vals):
            raise ValidationError("KPI evaluations are historical records. Create a new evaluation or correction instead of editing results.")
        return super().write(vals)

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
        target = self.env["strategic.kpi.target"].browse(vals["target_id"]) if vals.get("target_id") else False
        kpi = target.kpi_id if target else self.env["strategic.kpi.definition"].browse(vals["kpi_id"])
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
                "unit": target.unit,
            })
        else:
            vals.setdefault("unit", kpi.unit)
        vals.update(
            self._calculate_result(
                direction=kpi.direction,
                target_value=vals.get("target_value", 0.0),
                range_min=vals.get("range_min", 0.0),
                range_max=vals.get("range_max", 0.0),
                actual_value=vals.get("actual_value", 0.0),
                has_actual_value=vals.get("has_actual_value", False),
            )
        )
        return vals

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
