"""Authorized, on-demand entry point for operational KPI source adapters."""

from odoo import models, _
from odoo.exceptions import AccessError, ValidationError

from ..services.kpi_sources import (
    collect_inventory_result,
    collect_pos_result,
    collect_purchase_result,
    collect_supplier_delivery_result,
)


_GROUP_HEAD = "strategic_management.group_strategic_head_office_manager"
_GROUP_BRANCH = "strategic_management.group_strategic_branch_manager"
_SOURCE_ADAPTERS = {
    "pos": {
        "pos_sold_quantity",
        "pos_recipe_consumption_estimate",
    },
    "purchase": {
        "purchase_ordered_quantity",
        "purchase_received_quantity",
        "supplier_on_time_delivery_pct",
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
    },
}
_SOURCE_COLLECTORS = {
    "pos": collect_pos_result,
    "purchase": collect_purchase_result,
    "inventory": collect_inventory_result,
}


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
        collector = (
            collect_supplier_delivery_result
            if version.calculation_type == "supplier_on_time_delivery_pct"
            else _SOURCE_COLLECTORS.get(version.source_type)
        )
        supported = _SOURCE_ADAPTERS.get(version.source_type, set())
        if not collector or version.calculation_type not in supported:
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
