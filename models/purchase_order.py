"""Refresh approved purchase-quantity KPI snapshots on PO confirmation."""

import logging

from odoo import fields, models

from ..services.kpi_sources.common import reporting_period_utc
from ..services.kpi_sources.purchase import collect_purchase_result


_logger = logging.getLogger(__name__)


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    def button_approve(self, force=False):
        """Refresh relevant ordered-quantity KPIs once a PO becomes confirmed."""
        result = super().button_approve(force=force)
        confirmed_orders = self.filtered(
            lambda order: order.state in ("purchase", "done") and order.date_approve
        )
        confirmed_orders._refresh_strategic_purchase_ordered_kpis()
        return result

    def _refresh_strategic_purchase_ordered_kpis(self):
        """Create fresh snapshots only for approved targets covering this PO's period.

        This is an internal event hook, not a user-facing evaluation action. The
        existing collector continues to enforce source, branch and UoM contracts;
        evaluation records remain immutable and source-key idempotency is retained.
        A KPI-side failure is logged and must not roll back the business operation
        of confirming a purchase order.
        """
        Target = self.env["strategic.kpi.target"].sudo()
        Evaluation = self.env["strategic.kpi.evaluation"].sudo()

        for order in self:
            approval_time = fields.Datetime.to_datetime(order.date_approve) if order.date_approve else False
            if not approval_time or not order.company_id:
                continue
            ordered_product_ids = set(order.order_line.mapped("product_id").ids)
            if not ordered_product_ids:
                continue

            targets = Target.with_company(order.company_id).search([
                ("company_id", "=", order.company_id.id),
                ("state", "=", "approved"),
                ("scope", "in", ["organization", "branch"]),
            ])

            for target in targets:
                try:
                    # An order routed to Branch A must not refresh Branch B's
                    # target. Organization-level targets intentionally aggregate
                    # confirmed orders across the company.
                    if target.scope == "branch":
                        order_warehouse = order.picking_type_id.warehouse_id
                        if not order_warehouse or target.warehouse_id != order_warehouse:
                            continue

                    version = target.kpi_id._get_definition_version_for_period(
                        target.date_start, target.date_end
                    )
                    if (
                        version.source_type != "purchase"
                        or version.calculation_type != "purchase_ordered_quantity"
                        or not version.source_product_id
                        or version.source_product_id.id not in ordered_product_ids
                    ):
                        continue

                    period_start, period_end, issue = reporting_period_utc(target)
                    if issue:
                        _logger.warning(
                            "Skipping automatic purchase KPI refresh for target %s: %s",
                            target.display_name,
                            issue,
                        )
                        continue
                    if not (period_start <= approval_time < period_end):
                        continue

                    # Isolate each target so a single malformed KPI definition
                    # cannot prevent confirmation or refresh of other targets.
                    with self.env.cr.savepoint():
                        result = collect_purchase_result(target, version)
                        Evaluation.with_company(order.company_id)._create_from_source_adapter(
                            target, version, result
                        )
                except Exception:
                    _logger.exception(
                        "Automatic ordered-quantity KPI refresh failed for target %s "
                        "after confirming Purchase Order %s",
                        target.display_name,
                        order.name,
                    )
