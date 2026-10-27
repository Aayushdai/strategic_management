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
    "purchase_confirmed_amount",
    # Business-goal measures, implemented in purchase_goals.py.
    "purchase_average_unit_price",
    "purchase_product_quantity_limit",
    "purchase_unit_price_change_pct",
    "purchase_top_supplier_share_pct",
    "purchase_outstanding_amount",
    "purchase_average_receipt_lead_days",
}
CONFIRMED_ORDER_STATES = ("purchase", "done")


def scope_warehouses(target):
    """Branch target -> its warehouse; organization target -> every warehouse of the company."""
    if target.scope == "branch":
        return target.warehouse_id
    return target.env["stock.warehouse"].sudo().search([("company_id", "=", target.company_id.id)])


def to_company_amount(env, company, amount, currency, when, label):
    """Convert an order amount to company currency at the given date.

    Returns (amount, problem). A missing currency or exchange rate is a problem, never a 1:1 conversion.
    """
    company_currency = company.currency_id
    amount = float(amount or 0.0)
    if not currency or not company_currency:
        return None, "%s has no currency" % label
    if currency != company_currency:
        rate_date = fields.Date.to_date(when)
        if not env["res.currency.rate"].sudo().search_count([
            ("currency_id", "=", currency.id), ("name", "<=", rate_date), ("company_id", "in", [company.id, False]),
        ], limit=1):
            return None, "%s is in %s with no exchange rate on or before %s" % (label, currency.name, rate_date)
        amount = currency._convert(amount, company_currency, company, rate_date)
    if not math.isfinite(amount):
        return None, "%s has an invalid amount" % label
    return company_currency.round(amount), False


def confirmed_purchase_commitments(env, company, start_utc, end_utc, warehouses):
    """Confirmed purchase-order value, excluding tax, in company currency, for [start_utc, end_utc).

    Single calculation shared by the Purchasing overview and the purchase_confirmed_amount KPI.
    An order belongs to the period of its confirmation (date_approve); orders without one fall back
    to date_order, as the overview always has. Only current states are read, so an order confirmed and
    later cancelled is excluded: there is no confirmation-time snapshot. Branch = receipt warehouse.
    Returns (orders, rows, total, issue); issue is a plain-language reason when the value is unreliable.
    """
    Order = env["purchase.order"].sudo()
    required = {"company_id", "state", "date_approve", "date_order", "amount_untaxed", "currency_id", "picking_type_id"}
    if not required.issubset(Order._fields):
        return Order.browse(), [], None, "The installed Purchase app does not expose the order fields needed to measure confirmed spending."
    if not warehouses:
        return Order.browse(), [], None, "No branch warehouse is configured, so purchase orders cannot be assigned to a branch."
    start_text = fields.Datetime.to_string(start_utc)
    end_text = fields.Datetime.to_string(end_utc)
    orders = Order.search([
        ("company_id", "=", company.id),
        ("state", "in", CONFIRMED_ORDER_STATES),
        ("picking_type_id.warehouse_id", "in", warehouses.ids),
        "|",
        "&", "&", ("date_approve", "!=", False), ("date_approve", ">=", start_text), ("date_approve", "<", end_text),
        "&", ("date_approve", "=", False), "&", ("date_order", ">=", start_text), ("date_order", "<", end_text),
    ], order="date_approve, id")
    company_currency = company.currency_id
    rows, total, problems = [], 0.0, []
    for order in orders:
        confirmed_at = order.date_approve or order.date_order
        # Converted at the confirmation date; a missing rate is unreliable, never treated as 1:1.
        amount, problem = to_company_amount(env, company, order.amount_untaxed, order.currency_id, confirmed_at, order.name)
        if problem:
            problems.append(problem)
            continue
        currency = order.currency_id
        total += amount
        warehouse = order.picking_type_id.warehouse_id
        rows.append({
            "id": order.id, "name": order.name, "supplier": order.partner_id.display_name or "",
            "supplier_id": order.partner_id.commercial_partner_id.id,
            "branch": warehouse.display_name or "", "confirmed": fields.Datetime.to_string(confirmed_at),
            "amount": amount, "currency": currency.name, "original_amount": float(order.amount_untaxed or 0.0),
        })
    if problems:
        return orders, rows, None, "Confirmed spending cannot be measured reliably: %s." % "; ".join(problems[:10])
    return orders, rows, company_currency.round(total), False


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
    if calc == "purchase_confirmed_amount":
        return _collect_confirmed_amount(target, version)
    from .purchase_goals import GOAL_CALCULATIONS, collect_purchase_goal_result  # local: purchase_goals imports this module
    if calc in GOAL_CALCULATIONS:
        return collect_purchase_goal_result(target, version)
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


def _collect_confirmed_amount(target, version):
    """Confirmed purchase commitments (excl. tax) for the target's company, branch scope and period."""
    empty = target.env["purchase.order"].browse()
    if version.source_product_id:
        return source_result(target, version, DATA_QUALITY, None,
                             "Confirmed spending is measured across all products and must not select one product.", "purchase.order", empty)
    issue = validate_scope(target)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "purchase.order", empty)
    start_utc, end_utc, issue = reporting_period_utc(target)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "purchase.order", empty)
    if target.scope == "branch":
        warehouses = target.warehouse_id
    else:
        warehouses = target.env["stock.warehouse"].sudo().search([("company_id", "=", target.company_id.id)])
    orders, _rows, total, issue = confirmed_purchase_commitments(target.env, target.company_id, start_utc, end_utc, warehouses)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "purchase.order", orders)
    reason = (
        "Value of purchase orders confirmed in the reporting period, excluding tax, in company currency."
        if orders else
        "No purchase orders were confirmed in this scope and period; confirmed spending is zero."
    )
    return source_result(target, version, VALID, total, reason, "purchase.order", orders)


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
    common_domain = [
        ("company_id", "=", target.company_id.id),
        ("product_id", "=", version.source_product_id.id),
        ("state", "=", "done"),
        ("date", ">=", fields.Datetime.to_string(start_utc)),
        ("date", "<", fields.Datetime.to_string(end_utc)),
        ("is_scrap", "=", False),
    ] + branch_domain
    moves = Move.search(common_domain + [("location_usage", "=", "supplier"), ("location_dest_usage", "=", "internal")], order="date, id")
    # Goods sent back to the supplier in the period are not received goods.
    returns = Move.search(common_domain + [("location_usage", "=", "internal"), ("location_dest_usage", "=", "supplier")], order="date, id")
    if not moves and not returns:
        return source_result(
            target, version, NOT_EVALUATED, None,
            "No completed supplier-to-internal stock receipts were found for this product and reporting period; no zero quantity has been assumed.",
            "stock.move", moves,
        )
    quantities = (moves | returns).mapped("quantity_product_uom")
    if any(not isinstance(quantity, (int, float)) or not math.isfinite(quantity) or quantity < 0 for quantity in quantities):
        return source_result(
            target, version, DATA_QUALITY, None,
            "Completed receipt moves contain negative or invalid quantities.",
            "stock.move", moves | returns,
        )
    received = sum(moves.mapped("quantity_product_uom"))
    returned = sum(returns.mapped("quantity_product_uom"))
    return source_result(
        target, version, VALID, float(received - returned),
        "Quantity on completed supplier-to-internal stock moves (%g) minus goods returned to the supplier (%g), assigned by actual move completion date and converted by Odoo to the product's base unit. Internal transfers are excluded to avoid double counting receipts."
        % (received, returned),
        "stock.move", moves | returns,
    )
