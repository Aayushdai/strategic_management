"""Planned stock counts: the schedule that 'Complete planned stock counts' is measured against.

Odoo keeps only the next planned count date on a location or quant and overwrites it after each count, so past
plans could not be counted. Whether a plan was done is never ticked by hand: it is read from the counts applied in
Inventory (services/kpi_sources/inventory.py, count_plan_results).
"""

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from ..services.kpi_sources.inventory import count_plan_results

_LOCKED_FIELDS = {"company_id", "warehouse_id", "product_ids", "date_planned", "date_due"}


class StrategicStockCountPlan(models.Model):
    _name = "strategic.stock.count.plan"
    _description = "Planned Stock Count"
    _order = "date_planned desc, id desc"

    name = fields.Char(compute="_compute_name", store=True)
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company, index=True)
    warehouse_id = fields.Many2one("stock.warehouse", string="Branch", required=True, index=True,
                                   domain="[('company_id', '=', company_id)]")
    product_ids = fields.Many2many("product.product", string="Products to count", required=True,
                                   domain="[('is_storable', '=', True)]")
    date_planned = fields.Date("Planned date", required=True)
    date_due = fields.Date("Count by", required=True, compute="_compute_date_due", store=True, readonly=False, precompute=True,
                           help="Last day the count is still on time. Defaults to the planned date.")
    user_id = fields.Many2one("res.users", string="Responsible")
    state = fields.Selection([("planned", "Planned"), ("cancelled", "Cancelled")], default="planned", required=True, readonly=True)
    cancel_reason = fields.Char(help="Required to cancel. A plan cancelled after its count-by date still counts as missed.")
    cancelled_on = fields.Date(readonly=True)
    count_status = fields.Selection(
        [("open", "Not due yet"), ("done", "Counted"), ("missed", "Missed"), ("cancelled", "Cancelled")],
        compute="_compute_count_status",
        help="Counted when every product has a count applied in Inventory > Physical Inventory between the planned "
             "and count-by dates.",
    )
    missing_product_ids = fields.Many2many("product.product", string="Not counted yet", compute="_compute_count_status")

    @api.depends("warehouse_id", "date_planned")
    def _compute_name(self):
        for plan in self:
            plan.name = "%s count %s" % (plan.warehouse_id.code or plan.warehouse_id.name or "", plan.date_planned or "")

    @api.depends("date_planned")
    def _compute_date_due(self):
        for plan in self:
            if plan.date_planned and (not plan.date_due or plan.date_due < plan.date_planned):
                plan.date_due = plan.date_planned

    def _compute_count_status(self):
        saved = self.filtered("id")
        results = count_plan_results(saved) if saved else {}
        for plan in self:
            status, missing, _moves = results.get(plan.id, ("open", plan.product_ids, None))
            plan.count_status = status
            plan.missing_product_ids = missing

    def _company_today(self):
        self.ensure_one()
        return fields.Date.context_today(self.with_context(tz=self.company_id.partner_id.tz or "UTC"))

    @api.constrains("date_planned", "date_due", "warehouse_id", "company_id")
    def _check_plan(self):
        for plan in self:
            if plan.date_due < plan.date_planned:
                raise ValidationError("The count-by date cannot be before the planned date.")
            if plan.warehouse_id.company_id != plan.company_id:
                raise ValidationError("The branch must belong to the plan's company.")

    def _check_not_past_due(self):
        for plan in self:
            if plan.date_due and plan.date_due < plan._company_today():
                raise ValidationError(
                    "%s is past its count-by date (%s); its result stays on record and it cannot be changed or deleted."
                    % (plan.name, plan.date_due))

    def write(self, vals):
        if _LOCKED_FIELDS & set(vals):
            self._check_not_past_due()
        return super().write(vals)

    def unlink(self):
        self._check_not_past_due()
        return super().unlink()

    def action_cancel(self):
        for plan in self:
            if not plan.cancel_reason:
                raise ValidationError("Enter why %s is cancelled first." % plan.name)
            plan.write({"state": "cancelled", "cancelled_on": plan._company_today()})
