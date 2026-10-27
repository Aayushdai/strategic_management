"""Requirement-aligned, quantity-weighted supplier on-time delivery adapter."""

from datetime import datetime, timedelta
import math

import pytz

from odoo import fields
from odoo.exceptions import UserError, ValidationError

from .common import (DATA_QUALITY, NOT_EVALUATED, VALID, quantity_to_product_uom, reporting_period_utc, source_result,
                     units_compatible, validate_scope)

SUPPORTED_CALCULATIONS = {
    "supplier_on_time_delivery_pct",
    # Same cohort and fail-closed rules, scored differently (business-goal measures).
    "purchase_on_time_delivery_count_pct",
    "purchase_average_delivery_delay_days",
}
_UNITS = {"purchase_average_delivery_delay_days": "days"}


def _weighted_on_time_delivery_percentage(due_and_on_time_quantities):
    """Return on-time received quantity / quantity due, with partial credit per line.

    Input is an iterable of (quantity_due, quantity_received_by_original_promise).
    Receipt quantity is capped at the line's due quantity so an over-receipt cannot
    inflate the KPI above 100%. Invalid values are rejected by returning None.
    """
    denominator = 0.0
    on_time_numerator = 0.0
    for due_quantity, on_time_quantity in due_and_on_time_quantities:
        if (
            not isinstance(due_quantity, (int, float))
            or not isinstance(on_time_quantity, (int, float))
            or not math.isfinite(due_quantity)
            or not math.isfinite(on_time_quantity)
            or due_quantity <= 0
            or on_time_quantity < 0
        ):
            return None
        denominator += float(due_quantity)
        on_time_numerator += min(float(due_quantity), float(on_time_quantity))
    if denominator <= 0 or not math.isfinite(denominator) or not math.isfinite(on_time_numerator):
        return None
    return 100.0 * on_time_numerator / denominator


def collect_supplier_delivery_result(target, version):
    """Evaluate quantity due by the immutable PO-confirmation baseline.

    The cohort is assigned by the baseline promised date captured at confirmation.
    Partial on-time receipts count proportionally. Amendments/cancellations/returns
    that need an unapproved policy fail closed as Data Quality Issue instead of
    silently changing the agreed denominator or receipt interpretation.
    """
    Line = target.env["purchase.order.line"].sudo()
    Move = target.env["stock.move"].sudo()
    Order = target.env["purchase.order"].sudo()

    if version.source_type != "purchase" or version.calculation_type not in SUPPORTED_CALCULATIONS:
        return source_result(
            target, version, DATA_QUALITY, None,
            "Supplier on-time delivery requires the Purchase source and supplier_on_time_delivery_pct calculation.",
            "purchase.order.line", Line.browse(),
        )
    expected_unit = _UNITS.get(version.calculation_type, "%")
    if (version.unit or "").strip() != expected_unit or version.source_product_id:
        return source_result(
            target, version, DATA_QUALITY, None,
            "Supplier on-time delivery requires unit '%s' and no single tracked source product." % expected_unit,
            "purchase.order.line", Line.browse(),
        )
    issue = validate_scope(target)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "purchase.order.line", Line.browse())
    start_utc, end_utc, issue = reporting_period_utc(target)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "purchase.order.line", Line.browse())

    required_order_fields = {"company_id", "state", "picking_type_id", "partner_id"}
    required_line_fields = {
        "order_id", "product_id", "product_qty", "product_uom_qty", "uom_id",
        "date_promised", "date_planned", "display_type", "strategic_otd_baseline_captured",
        "strategic_otd_baseline_date", "strategic_otd_baseline_quantity",
        "strategic_otd_baseline_product_id", "strategic_otd_baseline_partner_id",
        "strategic_otd_baseline_company_id", "strategic_otd_baseline_warehouse_id",
    }
    required_move_fields = {
        "purchase_line_id", "state", "date", "location_usage", "location_dest_usage",
        "quantity_product_uom", "is_scrap", "origin_returned_move_id", "product_id",
    }
    missing = sorted(required_order_fields.difference(Order._fields))
    missing += sorted(required_line_fields.difference(Line._fields))
    missing += sorted(required_move_fields.difference(Move._fields))
    if missing:
        return source_result(
            target, version, DATA_QUALITY, None,
            "Installed Purchase/Stock models lack fields required for the approved supplier-delivery contract: %s." % ", ".join(missing),
            "purchase.order.line+stock.move", Line.browse(), Move.browse(),
        )

    if target.scope == "branch":
        if not target.warehouse_id or target.warehouse_id.company_id != target.company_id:
            return source_result(
                target, version, DATA_QUALITY, None,
                "The branch target must identify a warehouse belonging to its KPI company.",
                "purchase.order.line+stock.move", Line.browse(), Move.browse(),
            )

    start_text, end_text = fields.Datetime.to_string(start_utc), fields.Datetime.to_string(end_utc)
    now_utc = fields.Datetime.to_datetime(fields.Datetime.now())
    now_text = fields.Datetime.to_string(now_utc)

    # Existing confirmed PO lines from before this baseline feature cannot safely be
    # assigned to a due cohort by their current editable promise date. Flag likely
    # in-period candidates instead of pretending the current date is the baseline.
    candidate_domain = [
        ("order_id.company_id", "=", target.company_id.id),
        ("order_id.state", "in", ["purchase", "done"]),
        ("display_type", "=", False),
        ("product_id", "!=", False),
        ("product_id.type", "!=", "service"),
        "|",
        "&", ("date_promised", ">=", start_text), ("date_promised", "<", end_text),
        "&", ("date_planned", ">=", start_text), ("date_planned", "<", end_text),
    ]
    if target.scope == "branch":
        candidate_domain.append(("order_id.picking_type_id.warehouse_id", "=", target.warehouse_id.id))
    candidates = Line.search(candidate_domain, order="order_id, id")
    missing_baselines = candidates.filtered(
        lambda line: not line.strategic_otd_baseline_captured
        or not line.strategic_otd_baseline_date
        or not line.strategic_otd_baseline_product_id
        or not line.strategic_otd_baseline_partner_id
        or not line.strategic_otd_baseline_company_id
        or not line.strategic_otd_baseline_quantity
    )
    if missing_baselines:
        return source_result(
            target, version, DATA_QUALITY, None,
            "Supplier OTD cannot use current editable dates as a substitute for the original PO-confirmation baseline. Missing baseline snapshot on line IDs: %s." % ", ".join(map(str, missing_baselines.ids[:30])),
            "purchase.order.line", missing_baselines.mapped("order_id"), missing_baselines,
        )

    baseline_domain = [
        ("strategic_otd_baseline_captured", "=", True),
        ("strategic_otd_baseline_company_id", "=", target.company_id.id),
        ("strategic_otd_baseline_date", ">=", start_text),
        ("strategic_otd_baseline_date", "<", end_text),
        ("display_type", "=", False),
        ("strategic_otd_baseline_product_id.type", "!=", "service"),
    ]
    if target.scope == "branch":
        baseline_domain.append(("strategic_otd_baseline_warehouse_id", "=", target.warehouse_id.id))

    cancelled_lines = Line.search(baseline_domain + [("order_id.state", "=", "cancel")])
    cancelled_lines = cancelled_lines.filtered(lambda line: fields.Datetime.to_datetime(line.strategic_otd_baseline_date) <= now_utc)
    if cancelled_lines:
        return source_result(
            target, version, DATA_QUALITY, None,
            "Supplier OTD cannot score cancelled PO lines until the cancellation policy is approved (line IDs: %s)." % ", ".join(map(str, cancelled_lines.ids[:30])),
            "purchase.order.line", cancelled_lines.mapped("order_id"), cancelled_lines,
        )

    lines = Line.search(baseline_domain + [("order_id.state", "in", ["purchase", "done"])], order="strategic_otd_baseline_date, order_id, id")
    lines = lines.filtered(lambda line: fields.Datetime.to_datetime(line.strategic_otd_baseline_date) <= now_utc)
    if not lines:
        return source_result(
            target, version, NOT_EVALUATED, None,
            "No PO quantities with original promised dates elapsed in this reporting-period cohort. Future-due quantities are not scored as late before their baseline deadline.",
            "purchase.order.line", lines,
        )

    # Do not silently reinterpret unresolved changes to product, ordered quantity,
    # supplier, company or warehouse. A revised promise date is allowed but never
    # replaces the immutable baseline used for the cohort.
    changed_lines = []
    for line in lines:
        if line.product_id != line.strategic_otd_baseline_product_id:
            changed_lines.append("line %s product changed" % line.id)
            continue
        if line.order_id.partner_id != line.strategic_otd_baseline_partner_id:
            changed_lines.append("line %s supplier reassigned" % line.id)
        if line.order_id.company_id != line.strategic_otd_baseline_company_id:
            changed_lines.append("line %s company changed" % line.id)
        current_warehouse = line.order_id.picking_type_id.warehouse_id
        if current_warehouse != line.strategic_otd_baseline_warehouse_id:
            changed_lines.append("line %s receipt warehouse changed" % line.id)
        try:
            if not units_compatible(line.uom_id, line.strategic_otd_baseline_product_id.uom_id):
                raise ValidationError("unrelated units")
            current_quantity = line.uom_id._compute_quantity(
                line.product_qty, line.strategic_otd_baseline_product_id.uom_id,
                rounding_method="HALF-UP",
            )
        except (UserError, ValidationError, ZeroDivisionError, TypeError, ValueError):
            current_quantity = float("nan")
        baseline_quantity = line.strategic_otd_baseline_quantity
        if not math.isfinite(current_quantity) or not math.isfinite(baseline_quantity) or baseline_quantity <= 0:
            return source_result(
                target, version, DATA_QUALITY, None,
                "PO line %s has an invalid baseline quantity or UoM conversion." % line.id,
                "purchase.order.line", lines.mapped("order_id"), lines,
            )
        if abs(current_quantity - baseline_quantity) > max(1e-6, abs(baseline_quantity) * 1e-6):
            changed_lines.append("line %s ordered quantity changed after confirmation" % line.id)
    if changed_lines:
        return source_result(
            target, version, DATA_QUALITY, None,
            "Supplier OTD found post-confirmation changes whose scoring policy is not approved: %s." % "; ".join(changed_lines[:30]),
            "purchase.order.line", lines.mapped("order_id"), lines,
        )

    receipt_moves = Move.search([
        ("purchase_line_id", "in", lines.ids),
        ("state", "=", "done"),
        ("is_scrap", "=", False),
        ("location_usage", "=", "supplier"),
        ("location_dest_usage", "=", "internal"),
        ("origin_returned_move_id", "=", False),
    ], order="date, id")
    invalid_moves = receipt_moves.filtered(lambda move: (
        not isinstance(move.quantity_product_uom, (int, float))
        or not math.isfinite(move.quantity_product_uom)
        or move.quantity_product_uom < 0
        or not move.date
    ))
    if invalid_moves:
        return source_result(
            target, version, DATA_QUALITY, None,
            "Completed receipt moves contain invalid quantities or missing actual dates (move IDs: %s)." % ", ".join(map(str, invalid_moves.ids[:30])),
            "purchase.order.line+stock.move", lines.mapped("order_id"), lines, receipt_moves,
        )

    # Return handling was explicitly left for approval in the KPI specification.
    # If one exists on this cohort, avoid silently counting/reversing it.
    all_related_moves = Move.search([
        ("purchase_line_id", "in", lines.ids),
        ("state", "=", "done"),
    ])
    return_moves = all_related_moves.filtered(
        lambda move: move.location_usage == "internal" and move.location_dest_usage == "supplier"
    )
    linked_return_moves = Move.search([
        ("origin_returned_move_id", "in", receipt_moves.ids),
        ("state", "=", "done"),
    ]) if receipt_moves else Move.browse()
    if return_moves or linked_return_moves:
        involved = return_moves | linked_return_moves
        return source_result(
            target, version, DATA_QUALITY, None,
            "Supplier OTD cannot score this cohort until the approved return/rejected-delivery policy is defined (move IDs: %s)." % ", ".join(map(str, involved.ids[:30])),
            "purchase.order.line+stock.move", lines.mapped("order_id"), lines, all_related_moves,
        )

    due_and_on_time = []
    notes = []
    delays = []  # per line: days late at completion, None while not fully received
    for line in lines:
        baseline_date = fields.Datetime.to_datetime(line.strategic_otd_baseline_date)
        due_quantity = line.strategic_otd_baseline_quantity
        line_moves = receipt_moves.filtered(
            lambda move: move.purchase_line_id == line
            and move.product_id == line.strategic_otd_baseline_product_id
            and (target.scope != "branch" or move.location_dest_id.warehouse_id == target.warehouse_id)
        )
        timely_moves = line_moves.filtered(lambda move: fields.Datetime.to_datetime(move.date) <= baseline_date)
        cumulative, completed_at = 0.0, None
        for move in line_moves.sorted(lambda move: (move.date, move.id)):
            cumulative += move.quantity_product_uom
            if cumulative >= due_quantity - 1e-6:
                completed_at = fields.Datetime.to_datetime(move.date)
                break
        delays.append(None if completed_at is None else max(0.0, (completed_at - baseline_date).total_seconds() / 86400.0))
        received_by_baseline = sum(timely_moves.mapped("quantity_product_uom"))
        if not math.isfinite(received_by_baseline) or received_by_baseline < 0:
            return source_result(
                target, version, DATA_QUALITY, None,
                "Supplier OTD received quantity is invalid for PO line %s." % line.id,
                "purchase.order.line+stock.move", lines.mapped("order_id"), lines, receipt_moves,
            )
        on_time_quantity = min(due_quantity, received_by_baseline)
        due_and_on_time.append((due_quantity, on_time_quantity))
        current_promise = fields.Datetime.to_string(line.date_promised) if line.date_promised else "missing"
        notes.append(
            "line=%s; original_baseline=%s; current_promise=%s; due_qty=%s %s; on_time_qty=%s %s; partial_credit=proportional"
            % (
                line.id, fields.Datetime.to_string(baseline_date), current_promise,
                due_quantity, line.strategic_otd_baseline_product_id.uom_id.name,
                on_time_quantity, line.strategic_otd_baseline_product_id.uom_id.name,
            )
        )

    if version.calculation_type == "purchase_on_time_delivery_count_pct":
        # A delivery (PO line) is on time only when its full due quantity arrived by the original promised date.
        on_time = sum(1 for due, timely in due_and_on_time if timely >= due - 1e-6)
        return source_result(
            target, version, VALID, round(100.0 * on_time / len(due_and_on_time), 4),
            "%s of %s deliveries due in this period arrived in full by the original promised date. "
            "A partly on-time delivery counts as late." % (on_time, len(due_and_on_time)),
            "purchase.order.line+stock.move", lines.mapped("order_id"), lines, receipt_moves,
        )
    if version.calculation_type == "purchase_average_delivery_delay_days":
        # Population: deliveries due in this period that are fully received; outstanding ones are reported, not averaged.
        completed = [delay for delay in delays if delay is not None]
        outstanding = len(delays) - len(completed)
        if not completed:
            return source_result(
                target, version, NOT_EVALUATED, None,
                "No delivery due in this period has been fully received yet (%s outstanding)." % outstanding,
                "purchase.order.line+stock.move", lines.mapped("order_id"), lines, receipt_moves,
            )
        return source_result(
            target, version, VALID, round(sum(completed) / len(completed), 4),
            "Average days late (0 if early) at full receipt, over %s completed deliveries; %s still outstanding are not included." % (
                len(completed), outstanding),
            "purchase.order.line+stock.move", lines.mapped("order_id"), lines, receipt_moves,
        )

    percentage = _weighted_on_time_delivery_percentage(due_and_on_time)
    if percentage is None or not math.isfinite(percentage):
        return source_result(
            target, version, DATA_QUALITY, None,
            "Supplier on-time delivery could not produce a valid quantity-weighted percentage.",
            "purchase.order.line+stock.move", lines.mapped("order_id"), lines, receipt_moves,
        )
    detail = "; ".join(notes[:100])
    if len(notes) > 100:
        detail += "; detail_truncated=true"
    denominator = sum(pair[0] for pair in due_and_on_time)
    numerator = sum(pair[1] for pair in due_and_on_time)
    reason = (
        "Quantity-weighted supplier on-time delivery = quantity received by the original confirmed promised date / quantity due in the original-baseline reporting cohort × 100. "
        "Partial on-time receipts count proportionally; excess receipts are capped at the original due quantity. Due cohorts use the immutable date captured at PO confirmation, not a later editable promise date. "
        "Post-confirmation quantity/product/supplier/company/warehouse changes, cancelled due lines and returns fail closed until their policy is approved. "
        "Baseline quantities: %.10g; on-time quantities: %.10g; snapshot: %s" % (denominator, numerator, detail)
    )
    return source_result(
        target, version, VALID, float(percentage), reason,
        "purchase.order.line+stock.move", lines.mapped("order_id"), lines, receipt_moves,
    )


# ---------------------------------------------------------------- supplier performance (purchasing monitoring)
def delivery_performance_facts(env, company, date_from, date_to, warehouses, partner=False, product=False, category=False):
    """Confirmed order lines due on a day of the period, once that day has fully passed, with their receipts.

    Due day = the promised date captured when the order was confirmed (strategic_otd_baseline_date), as a calendar
    day in the company time zone; due quantity = the quantity captured then, in the product's base unit. Receipts =
    completed moves from a supplier into stock for the line (draft, waiting and cancelled receipts do not count),
    each kept with its own date and quantity, so partial deliveries are judged shipment by shipment. Receipts after
    the period still count as (late) deliveries of the period's lines. Returns to the supplier do not change when
    goods arrived. Cancelled orders and RFQs are not due. Lines that cannot be judged are listed in ``excluded`` with
    the reason, never dropped silently. Returns (facts, excluded, problem).
    """
    from .pos import period_bounds_utc                    # local: pos is a sibling source module
    from .purchase_goals import _filter_domain

    Line = env["purchase.order.line"].sudo()
    Move = env["stock.move"].sudo()
    if not company.partner_id.tz:
        return [], [], "The company contact has no timezone configured, so delivery days cannot be determined safely."
    if not warehouses:
        return [], [], "No branch warehouse is configured, so purchase orders cannot be assigned to a branch."
    timezone = pytz.timezone(company.partner_id.tz)
    today = datetime.now(pytz.UTC).astimezone(timezone).date()
    last_due_day = min(fields.Date.to_date(date_to), today - timedelta(days=1))
    date_from = fields.Date.to_date(date_from)
    if date_from > last_due_day:
        return [], [], False
    start_utc, end_utc, problem = period_bounds_utc(company, date_from, last_due_day)
    if problem:
        return [], [], problem
    start_text, end_text = fields.Datetime.to_string(start_utc), fields.Datetime.to_string(end_utc)
    lines = Line.search([
        ("order_id.company_id", "=", company.id),
        ("order_id.state", "in", ["purchase", "done"]),
        ("order_id.picking_type_id.warehouse_id", "in", warehouses.ids),
        ("display_type", "=", False),
        ("product_id", "!=", False),
        ("product_id.type", "!=", "service"),
        "|",
        "&", ("strategic_otd_baseline_date", ">=", start_text), ("strategic_otd_baseline_date", "<", end_text),
        "&", "&", ("strategic_otd_baseline_date", "=", False), ("date_promised", ">=", start_text), ("date_promised", "<", end_text),
    ] + _filter_domain("", partner, product, category), order="order_id, id")

    def local_day(value):
        return pytz.UTC.localize(fields.Datetime.to_datetime(value)).astimezone(timezone).date()

    receipts = Move.search([
        ("purchase_line_id", "in", lines.ids), ("state", "=", "done"), ("is_scrap", "=", False),
        ("location_usage", "=", "supplier"), ("location_dest_usage", "=", "internal"), ("origin_returned_move_id", "=", False),
    ], order="date, id")
    facts, excluded = [], []
    for line in lines:
        order = line.order_id
        reason = False
        if not line.strategic_otd_baseline_date or line.strategic_otd_baseline_quantity <= 0:
            reason = "no promised date was recorded when the order was confirmed"
        elif line.product_id != line.strategic_otd_baseline_product_id or order.partner_id != line.strategic_otd_baseline_partner_id:
            reason = "product or supplier changed after confirmation"
        elif order.picking_type_id.warehouse_id != line.strategic_otd_baseline_warehouse_id:
            reason = "receiving branch changed after confirmation"
        else:
            try:
                current = quantity_to_product_uom(line.product_qty, line.uom_id, line.product_id)
            except ValidationError as exc:
                current, reason = None, str(exc)
            if current is not None and abs(current - line.strategic_otd_baseline_quantity) > max(1e-6, abs(current) * 1e-6):
                reason = "ordered quantity changed after confirmation"
        if reason:
            excluded.append({"order": order.name, "product": line.product_id.display_name, "reason": reason})
            continue
        expected = local_day(line.strategic_otd_baseline_date)
        due = line.strategic_otd_baseline_quantity
        shipments = [(local_day(move.date), move.quantity_product_uom) for move in receipts if move.purchase_line_id == line]
        received = sum(quantity for _day, quantity in shipments)
        facts.append({
            "line": line, "order": order, "expected": expected, "due": due, "unit": line.product_id.uom_id.name or "",
            "received": received, "on_time": min(due, sum(quantity for day, quantity in shipments if day <= expected)),
            "undelivered": max(0.0, due - received), "receipts": [(day, quantity, (day - expected).days) for day, quantity in shipments],
            "supplier": order.partner_id.commercial_partner_id.display_name or "No supplier",
            "product": line.product_id.display_name, "category": line.product_id.categ_id.display_name or "No category",
            "branch": order.picking_type_id.warehouse_id.display_name or "",
        })
    return facts, excluded, False


def on_time_rates(facts):
    """{unit: on-time %} = quantity received by the due day / quantity due x 100, per unit of measure.

    Quantities in different units are never weighted against each other. Undelivered quantity counts as not on time.
    """
    totals = {}
    for fact in facts:
        due, on_time = totals.get(fact["unit"], (0.0, 0.0))
        totals[fact["unit"]] = (due + fact["due"], on_time + fact["on_time"])
    return {unit: on_time / due * 100.0 for unit, (due, on_time) in totals.items() if due > 0}


def average_delays(facts):
    """{unit: days} = sum(days after the due day x quantity received) / quantity received, per unit of measure.

    Negative = early, 0 = on the due day, positive = late; each receipt weighs by its quantity. Undelivered quantity has
    no delay yet and is reported apart, so a unit with no receipts has no average (not zero)."""
    totals = {}
    for fact in facts:
        for _day, quantity, days in fact["receipts"]:
            weighted, received = totals.get(fact["unit"], (0.0, 0.0))
            totals[fact["unit"]] = (weighted + days * quantity, received + quantity)
    return {unit: weighted / received for unit, (weighted, received) in totals.items() if received > 0}
