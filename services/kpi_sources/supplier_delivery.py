"""Requirement-aligned, quantity-weighted supplier on-time delivery adapter."""

import math

from odoo import fields
from odoo.exceptions import UserError, ValidationError

from .common import DATA_QUALITY, NOT_EVALUATED, VALID, reporting_period_utc, source_result, validate_scope

SUPPORTED_CALCULATIONS = {"supplier_on_time_delivery_pct"}


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
    if (version.unit or "").strip() != "%" or version.source_product_id:
        return source_result(
            target, version, DATA_QUALITY, None,
            "Supplier on-time delivery requires unit '%' and no single tracked source product.",
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
    for line in lines:
        baseline_date = fields.Datetime.to_datetime(line.strategic_otd_baseline_date)
        due_quantity = line.strategic_otd_baseline_quantity
        timely_moves = receipt_moves.filtered(
            lambda move: move.purchase_line_id == line
            and move.product_id == line.strategic_otd_baseline_product_id
            and fields.Datetime.to_datetime(move.date) <= baseline_date
            and (target.scope != "branch" or move.location_dest_id.warehouse_id == target.warehouse_id)
        )
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
