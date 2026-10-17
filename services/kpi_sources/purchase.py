"""Purchase commitment and completed-receipt KPI source adapters."""

import math

from odoo import fields
from odoo.exceptions import UserError, ValidationError

from .common import (
    DATA_QUALITY,
    NOT_EVALUATED,
    VALID,
    quantity_to_product_uom,
    reporting_period_utc,
    source_result,
    validate_product_contract,
    validate_scope,
)

SUPPORTED_CALCULATIONS = {
    "purchase_ordered_quantity",
    "purchase_received_quantity",
}


def collect_purchase_result(target, version):
    """Measure confirmed PO quantity or completed supplier receipts for one product.

    Ordered quantity is assigned to the period of the PO's approval timestamp.
    Received quantity is assigned to the actual completion timestamp on a done
    inbound supplier-to-internal stock move. Internal warehouse transfers are
    not counted as new supplier receipts.
    """
    calc = version.calculation_type
    if calc not in SUPPORTED_CALCULATIONS:
        return source_result(
            target, version, DATA_QUALITY, None,
            "The approved KPI definition version does not select a supported Purchase calculation.",
            "", target.env["purchase.order"].browse(),
        )
    issue = validate_product_contract(target, version, "purchase")
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "", target.env["purchase.order"].browse())
    issue = validate_scope(target)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "", target.env["purchase.order"].browse())
    start_utc, end_utc, issue = reporting_period_utc(target)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "", target.env["purchase.order"].browse())

    env = target.env
    if calc == "purchase_ordered_quantity":
        return _collect_confirmed_order_quantity(target, version, start_utc, end_utc)
    return _collect_completed_supplier_receipts(target, version, start_utc, end_utc)


def _branch_purchase_domain(target, model):
    """Return the correct branch domain or an explicit schema/data-quality issue."""
    if target.scope != "branch":
        return [], False
    if "picking_type_id" not in model._fields:
        return [], "The installed Purchase model does not expose a receipt operation type for branch mapping."
    picking_type = model.env["stock.picking.type"]
    if "warehouse_id" not in picking_type._fields:
        return [], "The installed operation type model does not expose a warehouse mapping."
    return [("picking_type_id.warehouse_id", "=", target.warehouse_id.id)], False


def _collect_confirmed_order_quantity(target, version, start_utc, end_utc):
    env = target.env
    Order = env["purchase.order"].sudo()
    Line = env["purchase.order.line"].sudo()
    required_order_fields = {"company_id", "state", "date_approve", "order_line"}
    if not required_order_fields.issubset(Order._fields):
        return source_result(
            target, version, DATA_QUALITY, None,
            "The installed Purchase order model does not expose the fields required for confirmed-order quantity.",
            "purchase.order", Order.browse(),
        )
    branch_domain, issue = _branch_purchase_domain(target, Order)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "purchase.order", Order.browse())
    order_domain = [
        ("company_id", "=", target.company_id.id),
        ("state", "in", ["purchase", "done"]),
        ("date_approve", ">=", fields.Datetime.to_string(start_utc)),
        ("date_approve", "<", fields.Datetime.to_string(end_utc)),
    ] + branch_domain
    orders = Order.search(order_domain, order="date_approve, id")
    if not orders:
        return source_result(
            target, version, NOT_EVALUATED, None,
            "No confirmed Purchase Orders were found in this company, branch and reporting period; no zero quantity has been assumed.",
            "purchase.order", orders,
        )

    required_line_fields = {"order_id", "product_id", "product_qty", "uom_id"}
    if not required_line_fields.issubset(Line._fields):
        return source_result(
            target, version, DATA_QUALITY, None,
            "The installed Purchase order-line model does not expose the quantity and UoM fields required for conversion.",
            "purchase.order+purchase.order.line", orders,
        )
    line_domain = [("order_id", "in", orders.ids), ("product_id", "=", version.source_product_id.id)]
    if "display_type" in Line._fields:
        line_domain.append(("display_type", "=", False))
    lines = Line.search(line_domain, order="order_id, id")
    total = 0.0
    try:
        for line in lines:
            total += quantity_to_product_uom(line.product_qty, line.uom_id, version.source_product_id)
    except ValidationError as exc:
        return source_result(
            target, version, DATA_QUALITY, None,
            "Confirmed PO quantity is incomplete or has an invalid UoM: %s" % exc,
            "purchase.order+purchase.order.line", orders, lines,
        )
    if not math.isfinite(total):
        return source_result(
            target, version, DATA_QUALITY, None,
            "Confirmed PO quantity did not produce a finite numeric value.",
            "purchase.order+purchase.order.line", orders, lines,
        )
    return source_result(
        target, version, VALID, float(total),
        "Total quantity on confirmed Purchase Orders approved within the reporting period, converted to the tracked product's base unit. A confirmed PO without a matching product line contributes zero.",
        "purchase.order+purchase.order.line", orders, lines,
    )


def _collect_completed_supplier_receipts(target, version, start_utc, end_utc):
    env = target.env
    Move = env["stock.move"].sudo()
    required_fields = {
        "company_id", "product_id", "state", "date", "location_usage",
        "location_dest_usage", "quantity_product_uom", "is_scrap", "picking_type_id",
    }
    if not required_fields.issubset(Move._fields):
        return source_result(
            target, version, DATA_QUALITY, None,
            "The installed Inventory model does not expose the required completed receipt, location, or UoM fields.",
            "stock.move", Move.browse(),
        )
    branch_domain, issue = _branch_purchase_domain(target, Move)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "stock.move", Move.browse())
    moves = Move.search([
        ("company_id", "=", target.company_id.id),
        ("product_id", "=", version.source_product_id.id),
        ("state", "=", "done"),
        ("date", ">=", fields.Datetime.to_string(start_utc)),
        ("date", "<", fields.Datetime.to_string(end_utc)),
        ("location_usage", "=", "supplier"),
        ("location_dest_usage", "=", "internal"),
        ("is_scrap", "=", False),
    ] + branch_domain, order="date, id")
    if not moves:
        return source_result(
            target, version, NOT_EVALUATED, None,
            "No completed supplier-to-internal stock receipts were found for this product and reporting period; no zero quantity has been assumed.",
            "stock.move", moves,
        )
    quantities = moves.mapped("quantity_product_uom")
    if any(not isinstance(quantity, (int, float)) or not math.isfinite(quantity) or quantity < 0 for quantity in quantities):
        return source_result(
            target, version, DATA_QUALITY, None,
            "Completed receipt moves contain negative or invalid quantities.",
            "stock.move", moves,
        )
    return source_result(
        target, version, VALID, float(sum(quantities)),
        "Quantity on completed supplier-to-internal stock moves, assigned by actual move completion date and converted by Odoo to the product's base unit. Internal transfers are excluded to avoid double counting receipts.",
        "stock.move", moves,
    )
