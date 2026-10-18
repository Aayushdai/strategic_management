"""Stock picking event hook for completed supplier receipt KPI refreshes."""

import logging

from odoo import models

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = "stock.picking"

    def _action_done(self):
        result = super()._action_done()
        completed_pickings = self.filtered(lambda picking: picking.state == "done")
        if completed_pickings:
            try:
                self.env["strategic.kpi.target"].sudo()._refresh_purchase_received_targets_for_pickings(
                    completed_pickings
                )
            except Exception:
                # Inventory validation is primary; a KPI failure is logged and
                # can be recovered by a later operational evaluation/scheduler.
                _logger.exception(
                    "Automatic received-quantity KPI refresh failed after stock picking validation: %s",
                    ", ".join(completed_pickings.mapped("display_name")),
                )
        return result
