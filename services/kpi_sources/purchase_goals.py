"""Purchasing business-goal measures built on recorded purchase and stock history.

Each measure reads dated records (order confirmation, completed receipt moves) for its period; none
substitutes a current state for history it needs. Shared rules with the Purchasing overview:
confirmed orders = states purchase/done, dated by date_approve (date_order if missing), branch = receipt
warehouse, amounts excluding tax converted to company currency at the confirmation date.
"""

from collections import defaultdict
from datetime import timedelta
import math

from odoo import fields
from odoo.exceptions import ValidationError

from .common import (DATA_QUALITY, NOT_EVALUATED, VALID, quantity_to_product_uom, reporting_period_utc, source_result,
                     validate_product_contract, validate_scope, validate_unit_price_contract)
from .purchase import CONFIRMED_ORDER_STATES, _collect_confirmed_order_quantity, confirmed_purchase_commitments, scope_warehouses, to_company_amount

GOAL_CALCULATIONS = {
    "purchase_product_quantity_limit",
    "purchase_unit_price_change_pct",
    "purchase_top_supplier_share_pct",
    "purchase_outstanding_amount",
    "purchase_average_receipt_lead_days",
    "purchase_average_unit_price",
}
_TOLERANCE = 1e-6


def collect_purchase_goal_result(target, version):
    empty = target.env["purchase.order"].browse()
    issue = validate_scope(target)
    start_utc = end_utc = None
    if not issue:
        start_utc, end_utc, issue = reporting_period_utc(target)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "purchase.order", empty)
    handler = {
        "purchase_product_quantity_limit": _quantity_limit,
        "purchase_unit_price_change_pct": _price_change,
        "purchase_top_supplier_share_pct": _top_supplier_share,
        "purchase_outstanding_amount": _outstanding_amount,
        "purchase_average_receipt_lead_days": _receipt_lead_days,
        "purchase_average_unit_price": _average_unit_price,
    }[version.calculation_type]
    return handler(target, version, start_utc, end_utc, scope_warehouses(target))


def _confirmed_in_period_domain(prefix, start_utc, end_utc):
    start_text, end_text = fields.Datetime.to_string(start_utc), fields.Datetime.to_string(end_utc)
    approve, ordered = prefix + "date_approve", prefix + "date_order"
    return [
        "|",
        "&", "&", (approve, "!=", False), (approve, ">=", start_text), (approve, "<", end_text),
        "&", (approve, "=", False), "&", (ordered, ">=", start_text), (ordered, "<", end_text),
    ]


def _receipt_moves(env, lines, before=None):
    """Completed supplier receipts for the lines, plus returns (which these measures do not yet score)."""
    Move = env["stock.move"].sudo()
    domain = [("purchase_line_id", "in", lines.ids), ("state", "=", "done")]
    if before:
        domain.append(("date", "<", fields.Datetime.to_string(before)))
    moves = Move.search(domain, order="date, id")
    receipts = moves.filtered(lambda m: m.location_usage == "supplier" and m.location_dest_usage == "internal" and not m.origin_returned_move_id)
    returns = moves - receipts
    return receipts, returns


# ---------------------------------------------------------------- A3 product quantity limit

def _quantity_limit(target, version, start_utc, end_utc, warehouses):
    """Ordered quantity of one product, in its base unit; a period with no confirmed orders is zero."""
    issue = validate_product_contract(target, version, "purchase")
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "purchase.order", target.env["purchase.order"].browse())
    result = _collect_confirmed_order_quantity(target, version, start_utc, end_utc)
    if result["status"] == NOT_EVALUATED and not result.get("source_record_ref"):
        # The order query succeeded and found nothing: for a "buy no more than" limit that is zero, not missing data.
        return source_result(target, version, VALID, 0.0,
                             "No purchase orders were confirmed in this scope and period; the ordered quantity is zero.",
                             "purchase.order", target.env["purchase.order"].browse())
    return result


# ---------------------------------------------------------------- A2 / D2 unit price change

def _filter_domain(prefix, partner=False, product=False, category=False):
    """Supplier (with its contacts), product and product category (with sub-categories) filters on order lines."""
    domain = []
    if partner:
        domain.append((prefix + "order_id.partner_id", "child_of", partner.commercial_partner_id.id))
    if product:
        domain.append((prefix + "product_id", "=", product.id))
    if category:
        domain.append((prefix + "product_id.categ_id", "child_of", category.id))
    return domain


def purchase_line_facts(env, company, start_utc, end_utc, warehouses, partner=False, product=False, category=False):
    """Confirmed purchase-order lines of the period, one fact per line.

    A fact holds the line amount excluding tax, after discount, in company currency (converted at the confirmation
    date), the quantity in the product's base unit and what it is grouped by (supplier, product, its current category,
    branch). Lines are never added to order totals, so nothing is counted twice. Returns (lines, facts, problems).
    """
    Line = env["purchase.order.line"].sudo()
    lines = Line.search([
        ("order_id.company_id", "=", company.id),
        ("order_id.state", "in", CONFIRMED_ORDER_STATES),
        ("order_id.picking_type_id.warehouse_id", "in", warehouses.ids),
        ("display_type", "=", False),
    ] + _confirmed_in_period_domain("order_id.", start_utc, end_utc) + _filter_domain("", partner, product, category),
        order="order_id, id")
    facts, problems = [], []
    for line in lines:
        order = line.order_id
        quantity = None
        if line.product_id:
            try:
                quantity = quantity_to_product_uom(line.product_qty, line.uom_id, line.product_id)
            except ValidationError as exc:
                problems.append("%s: %s" % (order.name, exc))
                continue
        confirmed = order.date_approve or order.date_order
        amount, problem = to_company_amount(env, company, line.price_subtotal, order.currency_id, confirmed, order.name)
        if problem:
            problems.append(problem)
            continue
        facts.append({"line": line, "order": order, "amount": amount, "quantity": quantity,
                      "unit": line.product_id.uom_id.name or "", "date": confirmed,
                      "supplier": order.partner_id.commercial_partner_id.display_name or "No supplier",
                      "product": line.product_id.display_name or line.name or "", "category": line.product_id.categ_id.display_name or "No category",
                      "branch": order.picking_type_id.warehouse_id.display_name or ""})
    return lines, facts, problems


def weighted_unit_price(env, company, product, start_utc, end_utc, warehouses, partner=False):
    """Quantity-weighted unit price (excl. tax, company currency, per product base unit) of confirmed lines.

    Returns (lines, price or None, quantity, problems).
    """
    lines, facts, problems = purchase_line_facts(env, company, start_utc, end_utc, warehouses, partner=partner, product=product)
    priced = [fact for fact in facts if fact["quantity"] and fact["quantity"] > 0]
    quantity = sum(fact["quantity"] for fact in priced)
    value = sum(fact["amount"] for fact in priced)
    return lines, (value / quantity if quantity > _TOLERANCE else None), quantity, problems


def received_move_facts(env, company, start_utc, end_utc, warehouses, partner=False, product=False, category=False):
    """Completed receipts of purchase-order lines in the period, dated by completion, and returns to the supplier.

    Receipt = done move from a supplier location into stock (multi-step receptions count their first step only);
    return = done move from stock back to a supplier location, counted negative. Quantities in the product's base
    unit; supplier and branch come from the purchase order. Draft, waiting and cancelled moves are not receipts.
    Returns (moves, facts).
    """
    Move = env["stock.move"].sudo()
    moves = Move.search([
        ("company_id", "=", company.id),
        ("purchase_line_id", "!=", False),
        ("state", "=", "done"),
        ("date", ">=", fields.Datetime.to_string(start_utc)),
        ("date", "<", fields.Datetime.to_string(end_utc)),
        ("purchase_line_id.order_id.picking_type_id.warehouse_id", "in", warehouses.ids),
    ] + _filter_domain("purchase_line_id.", partner, product, category), order="date, id")
    facts = []
    for move in moves:
        if move.location_usage == "supplier" and move.location_dest_usage == "internal" and not move.origin_returned_move_id:
            sign = 1.0
        elif move.location_usage == "internal" and move.location_dest_usage == "supplier":
            sign = -1.0
        else:
            continue
        order = move.purchase_line_id.order_id
        facts.append({"move": move, "order": order, "amount": None, "quantity": sign * move.quantity_product_uom,
                      "unit": move.product_id.uom_id.name or "", "date": move.date,
                      "supplier": order.partner_id.commercial_partner_id.display_name or "No supplier",
                      "product": move.product_id.display_name, "category": move.product_id.categ_id.display_name or "No category",
                      "branch": order.picking_type_id.warehouse_id.display_name or ""})
    return moves, facts


def _average_unit_price(target, version, start_utc, end_utc, warehouses):
    """Purchase price paid per base unit: confirmed amounts excluding tax divided by quantities in the product's unit."""
    empty = target.env["purchase.order.line"].browse()
    issue = validate_unit_price_contract(target, version, "purchase")
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "purchase.order.line", empty)
    product = version.source_product_id
    lines, price, quantity, problems = weighted_unit_price(target.env, target.company_id, product, start_utc, end_utc, warehouses)
    if problems:
        return source_result(target, version, DATA_QUALITY, None,
                             "The purchase price cannot be calculated: %s" % "; ".join(problems[:10]), "purchase.order.line", lines)
    if price is None:
        return source_result(target, version, NOT_EVALUATED, None,
                             "No confirmed purchase of %s in this scope and period, so there is no price to measure." % product.display_name,
                             "purchase.order.line", lines)
    return source_result(
        target, version, VALID, price,
        "Purchase price per %s: confirmed amounts excluding tax in company currency divided by %g %s ordered; order "
        "lines in other compatible units are converted to %s. This is what was paid to suppliers, not a selling price."
        % (product.uom_id.name, round(quantity, 4), product.uom_id.name, product.uom_id.name),
        "purchase.order.line", lines,
    )


def _price_change(target, version, start_utc, end_utc, warehouses):
    """Change of the product's weighted unit price versus the immediately preceding period of equal length."""
    product = version.source_product_id
    empty = target.env["purchase.order.line"].browse()
    if not product:
        return source_result(target, version, DATA_QUALITY, None, "Choose the product whose price is compared.", "purchase.order.line", empty)
    days = (target.date_end - target.date_start).days + 1
    base_label = "%s to %s" % (target.date_start - timedelta(days=days), target.date_start - timedelta(days=1))
    span = end_utc - start_utc
    lines, current, _qty, problems = weighted_unit_price(target.env, target.company_id, product, start_utc, end_utc, warehouses)
    base_lines, baseline, _base_qty, base_problems = weighted_unit_price(
        target.env, target.company_id, product, start_utc - span, start_utc, warehouses)
    if problems or base_problems:
        return source_result(target, version, DATA_QUALITY, None,
                             "Prices cannot be compared reliably: %s." % "; ".join((problems + base_problems)[:10]),
                             "purchase.order.line", lines | base_lines)
    if baseline is None or baseline <= 0:
        return source_result(target, version, NOT_EVALUATED, None,
                             "%s was not bought in the comparison period (%s), so a price change cannot be measured." % (product.display_name, base_label),
                             "purchase.order.line", base_lines)
    if current is None:
        return source_result(target, version, NOT_EVALUATED, None,
                             "%s has not been bought in this period yet, so there is no new price to compare." % product.display_name,
                             "purchase.order.line", lines)
    change = (current - baseline) / baseline * 100.0
    currency = target.company_id.currency_id
    return source_result(
        target, version, VALID, round(change, 4),
        "Weighted unit price excluding tax: %s %s per %s in this period versus %s %s in %s." % (
            currency.round(current), currency.symbol, product.uom_id.name, currency.round(baseline), currency.symbol, base_label),
        "purchase.order.line", lines | base_lines,
    )


# ---------------------------------------------------------------- D3 supplier concentration

def supplier_shares(env, company, start_utc, end_utc, warehouses):
    """Confirmed spend per supplier, largest first. Returns (orders, shares, total, issue)."""
    orders, rows, total, issue = confirmed_purchase_commitments(env, company, start_utc, end_utc, warehouses)
    if issue:
        return orders, [], None, issue
    by_supplier = defaultdict(lambda: {"amount": 0.0, "orders": 0})
    for row in rows:
        bucket = by_supplier[(row["supplier_id"], row["supplier"])]
        bucket["amount"] += row["amount"]
        bucket["orders"] += 1
    shares = sorted(
        ({"supplier": name, "amount": data["amount"], "orders": data["orders"],
          "share": data["amount"] / total * 100.0 if total else 0.0} for (_sid, name), data in by_supplier.items()),
        key=lambda item: -item["amount"])
    return orders, shares, total, False


def _top_supplier_share(target, version, start_utc, end_utc, warehouses):
    orders, shares, total, issue = supplier_shares(target.env, target.company_id, start_utc, end_utc, warehouses)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "purchase.order", orders)
    if not shares or not total or total <= 0:
        return source_result(target, version, NOT_EVALUATED, None,
                             "No purchase orders were confirmed in this period, so supplier shares are undefined.",
                             "purchase.order", orders)
    top = shares[0]
    return source_result(
        target, version, VALID, round(top["share"], 4),
        "Largest supplier %s: %s of %s confirmed purchasing (%s suppliers). Measures concentration only, not supply quality." % (
            top["supplier"], top["amount"], total, len(shares)),
        "purchase.order", orders,
    )


# ---------------------------------------------------------------- B4 value awaiting delivery

def received_before(lines, as_of_utc):
    """Quantity received per purchase line (product base unit) from moves completed before as_of_utc.

    The same rule as Odoo's own received quantity on the order line (purchase_stock, _prepare_qty_received), only
    dated: a return to the supplier is owed again when it updates the order quantities (to_refund) or is not linked to
    a receipt; dropship returns into stock and re-receipts of returns that do not update quantities are not counted;
    anything Odoo does not count as received (e.g. internal steps) is ignored. Returns {line id: quantity}.
    """
    Move = lines.env["stock.move"].sudo()
    received = defaultdict(float)
    for move in Move.search([("purchase_line_id", "in", lines.ids), ("state", "=", "done"),
                             ("date", "<", fields.Datetime.to_string(as_of_utc))], order="date, id"):
        origin = move.origin_returned_move_id
        if move._is_purchase_return():
            if not origin or move.to_refund:
                received[move.purchase_line_id.id] -= move.quantity_product_uom
        elif origin and origin._is_dropshipped() and not move._is_dropshipped_returned():
            continue
        elif origin and origin._is_purchase_return() and not move.to_refund or not move._should_count_for_quantity_received():
            continue
        else:
            received[move.purchase_line_id.id] += move.quantity_product_uom
    return received


def open_purchase_lines(env, company, as_of_utc, warehouses, partner=False, product=False, category=False):
    """Goods lines of orders confirmed before as_of_utc that still had quantity to receive at that moment.

    Remaining = ordered quantity (product base unit) minus completed supplier receipts dated before as_of_utc, so a
    partly received line stays open for the rest and a fully received line drops out. Goods returned to the supplier
    and transfers count exactly as in Odoo's own received quantity (received_before). Expected arrival = the line's
    date_planned. Returns (lines, items, problems); item amounts exclude tax, in company currency.
    """
    Line = env["purchase.order.line"].sudo()
    as_of_text = fields.Datetime.to_string(as_of_utc)
    lines = Line.search([
        ("order_id.company_id", "=", company.id),
        ("order_id.state", "in", CONFIRMED_ORDER_STATES),
        ("order_id.picking_type_id.warehouse_id", "in", warehouses.ids),
        ("display_type", "=", False),
        ("product_id", "!=", False),
        ("product_id.type", "!=", "service"),
        "|", "&", ("order_id.date_approve", "!=", False), ("order_id.date_approve", "<", as_of_text),
        "&", ("order_id.date_approve", "=", False), ("order_id.date_order", "<", as_of_text),
    ] + _filter_domain("", partner, product, category), order="order_id, id")
    received = received_before(lines, as_of_utc)
    items, problems = [], []
    for line in lines:
        order = line.order_id
        try:
            ordered = quantity_to_product_uom(line.product_qty, line.uom_id, line.product_id)
        except ValidationError as exc:
            problems.append("%s: %s" % (order.name, exc))
            continue
        remaining = ordered - received[line.id]
        if ordered <= _TOLERANCE or remaining <= _TOLERANCE:
            continue
        amount, problem = to_company_amount(env, company, line.price_subtotal * remaining / ordered, order.currency_id,
                                            order.date_approve or order.date_order, order.name)
        if problem:
            problems.append(problem)
            continue
        items.append({"line": line, "order": order, "remaining": remaining, "unit": line.product_id.uom_id.name or "",
                      "amount": amount, "expected": line.date_planned,
                      "supplier": order.partner_id.commercial_partner_id.display_name or "No supplier",
                      "product": line.product_id.display_name, "category": line.product_id.categ_id.display_name or "No category",
                      "branch": order.picking_type_id.warehouse_id.display_name or ""})
    return lines, items, problems


def outstanding_purchases(env, company, as_of_utc, warehouses):
    """Unreceived value (excl. tax, company currency) of goods lines on orders confirmed before as_of_utc.

    Received quantity counts only completed supplier receipts dated before as_of_utc, so the value is the
    position at that moment. Returns (lines, rows by order, total, problems).
    """
    lines, items, problems = open_purchase_lines(env, company, as_of_utc, warehouses)
    by_order = {}
    total = 0.0
    for item in items:
        order, amount = item["order"], item["amount"]
        total += amount
        row = by_order.setdefault(order.id, {
            "id": order.id, "name": order.name, "supplier": order.partner_id.display_name or "",
            "branch": order.picking_type_id.warehouse_id.display_name or "",
            "confirmed": fields.Datetime.to_string(order.date_approve or order.date_order), "amount": 0.0,
        })
        row["amount"] += amount
    return lines, list(by_order.values()), company.currency_id.round(total), problems


def _outstanding_amount(target, version, start_utc, end_utc, warehouses):
    now = fields.Datetime.to_datetime(fields.Datetime.now())
    lines, rows, total, problems = outstanding_purchases(target.env, target.company_id, min(end_utc, now), warehouses)
    if problems:
        return source_result(target, version, DATA_QUALITY, None,
                             "The value awaiting delivery cannot be measured reliably: %s." % "; ".join(problems[:10]),
                             "purchase.order.line", lines)
    return source_result(
        target, version, VALID, total,
        "%s confirmed orders still waiting for delivery at the end of the period (or now, if earlier); unreceived quantity x unit price, excluding tax." % len(rows),
        "purchase.order.line", lines,
    )


# ---------------------------------------------------------------- B5 confirmation-to-receipt time

def _receipt_lead_days(target, version, start_utc, end_utc, warehouses):
    """Average days from order confirmation to the receipt that completes the order.

    Population: orders confirmed in the period whose goods have all been received. Orders still waiting
    are not included (reported in the note) and are not counted as late or on time.
    """
    env = target.env
    Order = env["purchase.order"].sudo()
    start_text, end_text = fields.Datetime.to_string(start_utc), fields.Datetime.to_string(end_utc)
    orders = Order.search([
        ("company_id", "=", target.company_id.id), ("state", "in", CONFIRMED_ORDER_STATES),
        ("picking_type_id.warehouse_id", "in", warehouses.ids),
        ("date_approve", ">=", start_text), ("date_approve", "<", end_text),
    ], order="date_approve, id")
    lines = orders.order_line.filtered(lambda l: not l.display_type and l.product_id and l.product_id.type != "service")
    receipts, returns = _receipt_moves(env, lines)
    if returns:
        return source_result(target, version, DATA_QUALITY, None,
                             "Returns to suppliers are not yet scored (move IDs %s)." % ", ".join(map(str, returns.ids[:20])),
                             "purchase.order", orders)
    moves_by_line = defaultdict(list)
    for move in receipts:
        moves_by_line[move.purchase_line_id.id].append(move)
    lead_days, waiting = [], 0
    for order in orders:
        order_lines = lines.filtered(lambda l: l.order_id == order)
        if not order_lines:
            continue
        completed_at = []
        for line in order_lines:
            due = quantity_to_product_uom(line.product_qty, line.uom_id, line.product_id)
            cumulative, done_at = 0.0, None
            for move in moves_by_line[line.id]:
                cumulative += move.quantity_product_uom
                if cumulative >= due - _TOLERANCE:
                    done_at = fields.Datetime.to_datetime(move.date)
                    break
            completed_at.append(done_at)
        if any(when is None for when in completed_at):
            waiting += 1
            continue
        confirmed = fields.Datetime.to_datetime(order.date_approve)
        lead_days.append(max(0.0, (max(completed_at) - confirmed).total_seconds() / 86400.0))
    if not lead_days:
        return source_result(target, version, NOT_EVALUATED, None,
                             "No order confirmed in this period has been fully received yet (%s waiting)." % waiting,
                             "purchase.order", orders)
    return source_result(
        target, version, VALID, round(sum(lead_days) / len(lead_days), 4),
        "%s orders fully received; %s still waiting and not included." % (len(lead_days), waiting),
        "purchase.order", orders,
    )
