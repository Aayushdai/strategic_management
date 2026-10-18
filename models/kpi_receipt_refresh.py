"""Refresh received-quantity KPI snapshots after a completed supplier receipt."""

import logging

from odoo import api, fields, models

from ..services.kpi_sources import collect_purchase_result

_logger = logging.getLogger(__name__)


class StrategicKpiTarget(models.Model):
    _inherit = "strategic.kpi.target"

    @api.model
    def _refresh_purchase_received_targets_for_pickings(self, pickings):
        """Re-evaluate applicable approved targets after inbound stock is done.

        This internal event hook does not use the interactive role gate: the
        source snapshot is created by the system in response to a completed
        stock operation. User-facing evaluation actions retain their own access
        checks, and evaluation record access remains governed by record rules.
        """
        Target = self.sudo()
        Evaluation = self.env["strategic.kpi.evaluation"].sudo()
        refreshed = 0

        completed_pickings = pickings.sudo().filtered(
            lambda picking: (
                picking.state == "done"
                and picking.picking_type_id.code == "incoming"
                and picking.date_done
            )
        )
        for picking in completed_pickings:
            supplier_receipts = picking.move_ids.filtered(
                lambda move: (
                    move.state == "done"
                    and move.location_usage == "supplier"
                    and move.location_dest_usage == "internal"
                    and not move.is_scrap
                )
            )
            if not supplier_receipts:
                continue

            timezone_name = picking.company_id.partner_id.tz or "UTC"
            receipt_local_date = fields.Datetime.context_timestamp(
                picking.with_context(tz=timezone_name), picking.date_done
            ).date()
            targets = Target.search([
                ("company_id", "=", picking.company_id.id),
                ("state", "=", "approved"),
                ("scope", "in", ["organization", "branch"]),
                ("date_start", "<=", receipt_local_date),
                ("date_end", ">=", receipt_local_date),
            ], order="id")
            branch_warehouse = picking.picking_type_id.warehouse_id

            for target in targets:
                try:
                    with self.env.cr.savepoint():
                        version = target.kpi_id._get_definition_version_for_period(
                            target.date_start, target.date_end
                        )
                        if (
                            version.source_type != "purchase"
                            or version.calculation_type != "purchase_received_quantity"
                            or not version.source_product_id
                            or version.source_product_id not in supplier_receipts.product_id
                        ):
                            continue
                        if target.scope == "branch":
                            if not branch_warehouse or target.warehouse_id != branch_warehouse:
                                continue
                        elif target.scope != "organization":
                            continue

                        result = collect_purchase_result(target, version)
                        Evaluation._create_from_source_adapter(target, version, result)
                        refreshed += 1
                except Exception:
                    # KPI snapshot failure must not roll back a valid inventory
                    # receipt. Keep the failure visible for diagnosis.
                    _logger.exception(
                        "Automatic received-quantity KPI refresh failed for target %s (id=%s) after picking %s (id=%s)",
                        target.display_name,
                        target.id,
                        picking.display_name,
                        picking.id,
                    )
        return refreshed
