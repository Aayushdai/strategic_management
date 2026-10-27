"""Authorized, on-demand entry point for operational KPI source adapters."""

from odoo import fields, models, _
from odoo.exceptions import AccessError, ValidationError

from ..services.kpi_sources import (
    collect_accounting_result,
    collect_inventory_result,
    collect_pos_result,
    collect_purchase_result,
    collect_supplier_delivery_result,
)


_GROUP_HEAD = "strategic_management.group_strategic_head_office_manager"
_GROUP_BRANCH = "strategic_management.group_strategic_branch_manager"
_SOURCE_ADAPTERS = {
    "accounting": {
        "accounting_net_profit",
        "accounting_net_profit_margin_pct",
        "accounting_overdue_supplier_bills_amount",
        "accounting_total_expenses",
        "accounting_expense_to_income_pct",
    },
    "pos": {
        "pos_net_sales",
        "pos_sales_growth_pct",
        "pos_average_order_value",
        "pos_product_net_sales",
        "pos_average_unit_price",
        "pos_sold_quantity",
        "pos_recipe_consumption_estimate",
    },
    "purchase": {
        "purchase_ordered_quantity",
        "purchase_received_quantity",
        "supplier_on_time_delivery_pct",
        "purchase_confirmed_amount",
        "purchase_product_quantity_limit",
        "purchase_unit_price_change_pct",
        "purchase_top_supplier_share_pct",
        "purchase_outstanding_amount",
        "purchase_average_receipt_lead_days",
        "purchase_on_time_delivery_count_pct",
        "purchase_average_delivery_delay_days",
        "purchase_average_unit_price",
    },
    "inventory": {
        "inventory_recorded_waste_quantity",
        "inventory_validated_waste_quantity",
        "inventory_opening_stock_quantity",
        "inventory_closing_stock_quantity",
        "inventory_actual_consumption_quantity",
        "inventory_transfer_in_quantity",
        "inventory_transfer_out_quantity",
        "inventory_adjustment_quantity",
        "inventory_unexplained_variance_quantity",
        "inventory_waste_cost_amount",
        "inventory_waste_cost_to_sales_pct",
        "inventory_count_difference_quantity",
        "inventory_recorded_waste_cost",
        "inventory_closing_stock_value",
        "inventory_recipe_coverage_pct",
        "inventory_count_plan_completion_pct",
        "inventory_waste_recording_pct",
        "inventory_ingredient_use_difference_quantity",
    },
}
# Limits judged over the whole period: an interim value must not be stored as the result.
_FINAL_ONLY_CALCULATIONS = {
    "purchase_confirmed_amount", "purchase_product_quantity_limit", "purchase_unit_price_change_pct",
    "purchase_top_supplier_share_pct", "purchase_outstanding_amount", "purchase_average_receipt_lead_days",
    "purchase_on_time_delivery_count_pct", "purchase_average_delivery_delay_days", "accounting_overdue_supplier_bills_amount",
    "inventory_waste_cost_amount", "inventory_waste_cost_to_sales_pct", "inventory_count_difference_quantity",
    "inventory_recorded_waste_cost", "purchase_average_unit_price",
}
# Calculations scored by the supplier-delivery collector (promised-date baseline cohort).
DELIVERY_CALCULATIONS = {
    "supplier_on_time_delivery_pct", "purchase_on_time_delivery_count_pct", "purchase_average_delivery_delay_days",
}
_SOURCE_COLLECTORS = {
    "accounting": collect_accounting_result,
    "pos": collect_pos_result,
    "purchase": collect_purchase_result,
    "inventory": collect_inventory_result,
}


def source_collector_for(version):
    """Return the operational collector for a definition version, or False."""
    collector = (
        collect_supplier_delivery_result
        if version.calculation_type in DELIVERY_CALCULATIONS
        else _SOURCE_COLLECTORS.get(version.source_type)
    )
    if not collector or version.calculation_type not in _SOURCE_ADAPTERS.get(version.source_type, set()):
        return False
    return collector


class StrategicKpiTarget(models.Model):
    _inherit = "strategic.kpi.target"

    def action_evaluate_source(self):
        """Collect a source snapshot and open its immutable KPI evaluation."""
        self.ensure_one()
        user = self.env.user
        is_head = user.has_group(_GROUP_HEAD)
        is_branch_manager = user.has_group(_GROUP_BRANCH)
        if not (is_head or is_branch_manager):
            raise AccessError(_("Only Head Office or Branch Managers can request operational KPI evaluations."))
        if self.company_id not in self.env.companies:
            raise AccessError(_("You cannot evaluate a KPI target for a company outside your allowed companies."))
        if self.state != "approved":
            raise ValidationError(_("Only an approved KPI target can be evaluated from operational data."))
        if not is_head:
            if self.scope != "branch":
                raise AccessError(_("Branch Managers may evaluate only their own branch-level targets."))
            if not user.strategic_branch_warehouse_id or self.warehouse_id != user.strategic_branch_warehouse_id:
                raise AccessError(_("Branch Managers may evaluate only targets for their own branch."))

        version = self.kpi_id._get_definition_version_for_period(self.date_start, self.date_end)
        if version.calculation_type in _FINAL_ONLY_CALCULATIONS:
            company_today = fields.Date.context_today(self.with_context(tz=self.company_id.partner_id.tz or "UTC"))
            if self.date_end >= company_today:
                raise ValidationError(_(
                    "This limit is judged on the whole period. The final result can be calculated after %s; "
                    "until then the goal shows spending so far.", self.date_end))
        collector = source_collector_for(version)
        if not collector:
            raise ValidationError(_("The approved KPI definition version does not select an operational source adapter supported by this release."))

        result = collector(self, version)
        evaluation = self.env["strategic.kpi.evaluation"]._create_from_source_adapter(
            self, version, result
        )
        return {
            "type": "ir.actions.act_window",
            "name": _("KPI Evaluation"),
            "res_model": "strategic.kpi.evaluation",
            "view_mode": "form",
            "res_id": evaluation.id,
            "target": "current",
        }
