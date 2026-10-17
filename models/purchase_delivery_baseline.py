"""Capture the immutable supplier-delivery baseline at Purchase confirmation."""

import math

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


_BASELINE_FIELDS = {
    "strategic_otd_baseline_captured",
    "strategic_otd_baseline_date",
    "strategic_otd_baseline_quantity",
    "strategic_otd_baseline_product_id",
    "strategic_otd_baseline_partner_id",
    "strategic_otd_baseline_company_id",
    "strategic_otd_baseline_warehouse_id",
}


class PurchaseOrderLine(models.Model):
    _inherit = "purchase.order.line"

    strategic_otd_baseline_captured = fields.Boolean(
        string="Supplier OTD Baseline Captured", readonly=True, copy=False, index=True
    )
    strategic_otd_baseline_date = fields.Datetime(
        string="Supplier OTD Original Promised Date", readonly=True, copy=False, index=True
    )
    strategic_otd_baseline_quantity = fields.Float(
        string="Supplier OTD Quantity at Confirmation", readonly=True, copy=False,
        digits="Product Unit",
        help="Ordered quantity snapshotted at confirmation in the product's base unit.",
    )
    strategic_otd_baseline_product_id = fields.Many2one(
        "product.product", string="Supplier OTD Original Product", readonly=True,
        copy=False, ondelete="restrict", index=True,
    )
    strategic_otd_baseline_partner_id = fields.Many2one(
        "res.partner", string="Supplier OTD Original Vendor", readonly=True,
        copy=False, ondelete="restrict", index=True,
    )
    strategic_otd_baseline_company_id = fields.Many2one(
        "res.company", string="Supplier OTD Original Company", readonly=True,
        copy=False, ondelete="restrict", index=True,
    )
    strategic_otd_baseline_warehouse_id = fields.Many2one(
        "stock.warehouse", string="Supplier OTD Original Warehouse", readonly=True,
        copy=False, ondelete="restrict", index=True,
    )

    @api.model_create_multi
    def create(self, vals_list):
        protected = _BASELINE_FIELDS.intersection(set().union(*(set(vals) for vals in vals_list))) if vals_list else set()
        if protected:
            raise UserError(_("Supplier delivery baseline fields are system-managed and cannot be supplied during creation."))
        lines = super().create(vals_list)
        lines.filtered(lambda line: line.order_id.state in ("purchase", "done"))._capture_strategic_otd_baseline()
        return lines

    def write(self, vals):
        if _BASELINE_FIELDS.intersection(vals):
            raise UserError(_("Supplier delivery baseline fields are immutable and system-managed."))
        return super().write(vals)

    def _capture_strategic_otd_baseline(self):
        """Snapshot each confirmed PO line once; do not overwrite on later amendments."""
        for line in self.filtered(lambda rec: not rec.display_type and not rec.strategic_otd_baseline_captured):
            product = line.product_id
            quantity = 0.0
            if product and product.uom_id and line.uom_id:
                try:
                    quantity = line.uom_id._compute_quantity(
                        line.product_qty, product.uom_id, rounding_method="HALF-UP"
                    )
                except (UserError, ValidationError, ZeroDivisionError, TypeError, ValueError):
                    quantity = float("nan")
            warehouse = line.order_id.picking_type_id.warehouse_id
            vals = {
                "strategic_otd_baseline_captured": True,
                "strategic_otd_baseline_date": line.date_promised or False,
                "strategic_otd_baseline_quantity": quantity if math.isfinite(quantity) else 0.0,
                "strategic_otd_baseline_product_id": product.id or False,
                "strategic_otd_baseline_partner_id": line.order_id.partner_id.id or False,
                "strategic_otd_baseline_company_id": line.order_id.company_id.id or False,
                "strategic_otd_baseline_warehouse_id": warehouse.id or False,
            }
            # Bypass only this class's public write guard; the caller is an internal
            # model method and these values are never accepted from an RPC payload.
            super(PurchaseOrderLine, line).write(vals)


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    def button_approve(self, force=False):
        result = super().button_approve(force=force)
        confirmed_lines = self.filtered(
            lambda order: order.state in ("purchase", "done")
        ).mapped("order_line")
        confirmed_lines._capture_strategic_otd_baseline()
        return result
