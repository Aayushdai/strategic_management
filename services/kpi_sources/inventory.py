"""Inventory flow, production consumption and waste KPI source adapters."""

import math
from odoo import fields
from odoo.exceptions import ValidationError

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

WASTE_CALCULATIONS = {
    "inventory_recorded_waste_quantity",
    "inventory_validated_waste_quantity",
}
BALANCE_CALCULATIONS = {
    "inventory_opening_stock_quantity",
    "inventory_closing_stock_quantity",
}
FLOW_CALCULATIONS = {
    "inventory_actual_consumption_quantity",
    "inventory_transfer_in_quantity",
    "inventory_transfer_out_quantity",
    "inventory_adjustment_quantity",
}
RECONCILIATION_CALCULATIONS = {"inventory_unexplained_variance_quantity"}
SUPPORTED_CALCULATIONS = WASTE_CALCULATIONS | BALANCE_CALCULATIONS | FLOW_CALCULATIONS | RECONCILIATION_CALCULATIONS
_ALLOWED_WASTE_TAGS = {"spoilage/expiry", "damaged goods"}


def collect_inventory_result(target, version):
    """Collect one approved stock-flow quantity without treating variance as waste.

    Historical opening/closing stock is reconstructed from current on-hand quants and
    completed stock moves after the requested boundary. It is returned only when the
    installed models expose the complete movement contract. Closing stock is not
    evaluated before the target period has ended.
    """
    calc = version.calculation_type
    Move = target.env["stock.move"].sudo()
    if calc not in SUPPORTED_CALCULATIONS:
        return source_result(
            target, version, DATA_QUALITY, None,
            "The approved KPI definition version does not select a supported Inventory calculation.",
            "stock.move", Move.browse(),
        )
    issue = validate_product_contract(target, version, "inventory")
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "stock.move", Move.browse())
    issue = validate_scope(target)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "stock.move", Move.browse())
    start_utc, end_utc, issue = reporting_period_utc(target)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "stock.move", Move.browse())

    if calc in BALANCE_CALCULATIONS:
        return _collect_historical_stock_balance(target, version, start_utc, end_utc, calc)
    if calc in FLOW_CALCULATIONS:
        return _collect_inventory_flow(target, version, start_utc, end_utc, calc)
    if calc in RECONCILIATION_CALCULATIONS:
        return _collect_unexplained_stock_flow_variance(target, version, start_utc, end_utc)
    return _collect_waste(target, version, start_utc, end_utc, calc)


def _get_internal_scope_locations(target):
    """Resolve the tracked warehouse(s) to internal locations for balance calculations."""
    env = target.env
    Warehouse = env["stock.warehouse"].sudo()
    Location = env["stock.location"].sudo()
    if target.scope == "branch":
        warehouse = target.warehouse_id
        if not warehouse or warehouse.company_id != target.company_id:
            return Location.browse(), "The branch target must identify a warehouse belonging to its KPI company."
        warehouses = warehouse
    else:
        warehouses = Warehouse.search([("company_id", "=", target.company_id.id)])
    roots = warehouses.mapped("view_location_id")
    if not roots:
        return Location.browse(), "No stock warehouse view location exists for this company and scope."
    locations = Location.browse()
    for root in roots:
        locations |= Location.search([("id", "child_of", root.id), ("usage", "=", "internal")])
    if not locations:
        return Location.browse(), "No internal stock locations could be resolved for this warehouse scope."
    return locations, False


def _validate_move_schema(Move):
    required = {
        "company_id", "product_id", "state", "date", "quantity_product_uom",
        "location_id", "location_dest_id", "location_usage", "location_dest_usage",
        "is_scrap", "is_inventory", "scrap_reason_tag_ids",
    }
    missing = sorted(required.difference(Move._fields))
    if missing:
        return "The installed stock.move model is missing required inventory-flow fields: %s." % ", ".join(missing)
    return False


def _movement_delta(move, location_ids):
    """Signed effect of a completed move on on-hand quantity inside a location scope."""
    source_inside = move.location_id.id in location_ids
    destination_inside = move.location_dest_id.id in location_ids
    quantity = move.quantity_product_uom
    if source_inside and not destination_inside:
        return -quantity
    if destination_inside and not source_inside:
        return quantity
    return 0.0


def _moves_touching_scope(target, version, locations, start_utc, end_utc=None):
    Move = target.env["stock.move"].sudo()
    issue = _validate_move_schema(Move)
    if issue:
        return Move.browse(), issue
    domain = [
        ("company_id", "=", target.company_id.id),
        ("product_id", "=", version.source_product_id.id),
        ("state", "=", "done"),
        ("date", ">=", fields.Datetime.to_string(start_utc)),
        "|", ("location_id", "in", locations.ids), ("location_dest_id", "in", locations.ids),
    ]
    if end_utc:
        domain.extend([("date", "<", fields.Datetime.to_string(end_utc))])
    moves = Move.search(domain, order="date, id")
    invalid = moves.filtered(lambda move: (
        not isinstance(move.quantity_product_uom, (int, float))
        or not math.isfinite(move.quantity_product_uom)
        or move.quantity_product_uom < 0
    ))
    if invalid:
        return moves, "Completed stock moves contain negative or invalid base-unit quantities (move IDs: %s)." % ", ".join(map(str, invalid.ids[:20]))
    return moves, False


def _collect_historical_stock_balance(target, version, start_utc, end_utc, calc):
    env = target.env
    Quant = env["stock.quant"].sudo()
    Location = env["stock.location"].sudo()
    locations, issue = _get_internal_scope_locations(target)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "stock.quant+stock.move", Quant.browse())
    if "quantity" not in Quant._fields or "location_id" not in Quant._fields or "product_id" not in Quant._fields:
        return source_result(
            target, version, DATA_QUALITY, None,
            "The installed stock.quant model does not expose current on-hand quantities by product and location.",
            "stock.quant+stock.move", Quant.browse(),
        )

    boundary = start_utc if calc == "inventory_opening_stock_quantity" else end_utc
    now_utc = fields.Datetime.to_datetime(fields.Datetime.now())
    if calc == "inventory_closing_stock_quantity" and end_utc > now_utc:
        return source_result(
            target, version, NOT_EVALUATED, None,
            "The reporting period has not ended, so its closing stock balance is not yet available.",
            "stock.quant+stock.move", Quant.browse(),
        )
    if boundary > now_utc:
        return source_result(
            target, version, NOT_EVALUATED, None,
            "The opening stock boundary has not occurred yet; no future stock balance has been estimated.",
            "stock.quant+stock.move", Quant.browse(),
        )

    quants = Quant.search([
        ("product_id", "=", version.source_product_id.id),
        ("location_id", "in", locations.ids),
    ], order="location_id, id")
    current_quantity = sum(quants.mapped("quantity"))
    moves, issue = _moves_touching_scope(target, version, locations, boundary)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "stock.quant+stock.move", quants, moves)
    scope_ids = set(locations.ids)
    net_change_since_boundary = sum(_movement_delta(move, scope_ids) for move in moves)
    historical_quantity = current_quantity - net_change_since_boundary
    if not math.isfinite(historical_quantity):
        return source_result(
            target, version, DATA_QUALITY, None,
            "Historical stock reconstruction did not produce a finite numeric quantity.",
            "stock.quant+stock.move", quants, moves,
        )
    label = "opening" if calc == "inventory_opening_stock_quantity" else "closing"
    return source_result(
        target, version, VALID, float(historical_quantity),
        "Historical %s stock reconstructed from current on-hand quants and all completed stock movements at or after the boundary. Internal transfers within the selected scope have zero net effect. Source completeness depends on retaining stock move history." % label,
        "stock.quant+stock.move", quants, moves,
    )


def _collect_inventory_flow(target, version, start_utc, end_utc, calc):
    env = target.env
    Move = env["stock.move"].sudo()
    locations, issue = _get_internal_scope_locations(target)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "stock.move", Move.browse())
    if calc in ("inventory_transfer_in_quantity", "inventory_transfer_out_quantity") and target.scope == "branch":
        if not target.warehouse_id:
            return source_result(target, version, DATA_QUALITY, None, "Branch transfer KPI requires a branch warehouse.", "stock.move", Move.browse())
    moves, issue = _moves_touching_scope(target, version, locations, start_utc, end_utc)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "stock.move", moves)
    location_ids = set(locations.ids)
    total = 0.0
    selected = Move.browse()
    company_warehouse_ids = set(env["stock.warehouse"].sudo().search([("company_id", "=", target.company_id.id)]).ids)
    if calc in ("inventory_transfer_in_quantity", "inventory_transfer_out_quantity") and target.scope == "organization":
        # At organization scope, count only inter-warehouse transfers within this company.
        location_wh_fields_exist = "warehouse_id" in env["stock.location"]._fields
        if not location_wh_fields_exist:
            return source_result(target, version, DATA_QUALITY, None, "The installed stock.location model cannot map internal locations to warehouses for organization transfer reporting.", "stock.move", moves)

    for move in moves:
        if move.is_scrap:
            continue
        quantity = move.quantity_product_uom
        source_inside = move.location_id.id in location_ids
        destination_inside = move.location_dest_id.id in location_ids
        include = False
        contribution = 0.0

        if calc == "inventory_actual_consumption_quantity":
            include = source_inside and move.location_dest_usage == "production"
            contribution = quantity if include else 0.0
        elif calc in ("inventory_transfer_in_quantity", "inventory_transfer_out_quantity"):
            if move.location_usage != "internal" or move.location_dest_usage != "internal":
                continue
            if target.scope == "branch":
                if calc == "inventory_transfer_in_quantity":
                    include = destination_inside and not source_inside
                else:
                    include = source_inside and not destination_inside
            else:
                source_warehouse_id = move.location_id.warehouse_id.id if move.location_id.warehouse_id else False
                destination_warehouse_id = move.location_dest_id.warehouse_id.id if move.location_dest_id.warehouse_id else False
                is_company_transfer = (
                    source_warehouse_id in company_warehouse_ids
                    and destination_warehouse_id in company_warehouse_ids
                    and source_warehouse_id != destination_warehouse_id
                )
                include = is_company_transfer
            contribution = quantity if include else 0.0
        elif calc == "inventory_adjustment_quantity":
            include = bool(move.is_inventory or move.location_usage == "inventory" or move.location_dest_usage == "inventory")
            contribution = _movement_delta(move, location_ids) if include else 0.0

        if include:
            selected |= move
            total += contribution

    if not math.isfinite(total):
        return source_result(target, version, DATA_QUALITY, None, "Inventory flow did not produce a finite numeric quantity.", "stock.move", moves)
    if calc == "inventory_actual_consumption_quantity" and not selected:
        return source_result(
            target, version, NOT_EVALUATED, None,
            "No completed internal-to-production stock movements were found for this product and reporting period; POS recipe estimates are not treated as actual stock consumption.",
            "stock.move", moves,
        )
    labels = {
        "inventory_actual_consumption_quantity": "actual production consumption",
        "inventory_transfer_in_quantity": "inter-warehouse transfer-in",
        "inventory_transfer_out_quantity": "inter-warehouse transfer-out",
        "inventory_adjustment_quantity": "net inventory adjustment",
    }
    if not selected:
        return source_result(
            target, version, VALID, 0.0,
            "No %s movements were recorded for this product and reporting period. This is zero recorded movement, not evidence that unrecorded activity did not occur." % labels[calc],
            "stock.move", moves,
        )
    explanation = {
        "inventory_actual_consumption_quantity": "Completed internal-to-production stock moves, excluding scrap. POS-derived recipe consumption remains an estimate and is not substituted for actual stock moves.",
        "inventory_transfer_in_quantity": "Quantity transferred into the selected branch, or total inter-warehouse transfer-in throughput for an organization-scoped target. Only completed internal-to-internal movements between distinct warehouse scopes are counted.",
        "inventory_transfer_out_quantity": "Quantity transferred out of the selected branch, or total inter-warehouse transfer-out throughput for an organization-scoped target. Only completed internal-to-internal movements between distinct warehouse scopes are counted.",
        "inventory_adjustment_quantity": "Signed net adjustment affecting the selected stock scope through inventory adjustments. Positive means stock added; negative means stock removed. Scrap is separately classified as waste.",
    }
    return source_result(
        target, version, VALID, float(total), explanation[calc], "stock.move", selected,
    )


def _collect_unexplained_stock_flow_variance(target, version, start_utc, end_utc):
    """Reconcile system closing stock against separately categorized movement flows.

    This is a stock-flow residual, not a physical stock-count variance. It compares
    the reconstructed stock balance to receipts, supplier returns, production use,
    waste, inventory adjustments and transfers. Other stock outflows (for example,
    goods moved to customers) remain in the residual until explicitly classified.
    """
    env = target.env
    Quant = env["stock.quant"].sudo()
    Move = env["stock.move"].sudo()
    now_utc = fields.Datetime.to_datetime(fields.Datetime.now())
    if end_utc > now_utc:
        return source_result(
            target, version, NOT_EVALUATED, None,
            "The reporting period has not ended, so stock-flow variance cannot yet be reconciled against closing stock.",
            "stock.quant+stock.move", Quant.browse(), Move.browse(),
        )
    locations, issue = _get_internal_scope_locations(target)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "stock.quant+stock.move", Quant.browse(), Move.browse())
    quants = Quant.search([
        ("product_id", "=", version.source_product_id.id),
        ("location_id", "in", locations.ids),
    ], order="location_id, id")
    current_quantity = sum(quants.mapped("quantity"))

    opening_moves, issue = _moves_touching_scope(target, version, locations, start_utc)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "stock.quant+stock.move", quants, opening_moves)
    closing_moves, issue = _moves_touching_scope(target, version, locations, end_utc)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "stock.quant+stock.move", quants, opening_moves, closing_moves)
    period_moves, issue = _moves_touching_scope(target, version, locations, start_utc, end_utc)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "stock.quant+stock.move", quants, opening_moves, closing_moves, period_moves)

    scope_ids = set(locations.ids)
    opening_quantity = current_quantity - sum(_movement_delta(move, scope_ids) for move in opening_moves)
    closing_quantity = current_quantity - sum(_movement_delta(move, scope_ids) for move in closing_moves)
    categorized_net_flow = 0.0
    issues = []
    categorized_moves = Move.browse()
    allowed_tags = _ALLOWED_WASTE_TAGS

    for move in period_moves:
        source_inside = move.location_id.id in scope_ids
        destination_inside = move.location_dest_id.id in scope_ids
        delta = _movement_delta(move, scope_ids)
        source_usage = move.location_usage
        destination_usage = move.location_dest_usage

        if move.is_scrap:
            tags = [(tag.name or "").strip().casefold() for tag in move.scrap_reason_tag_ids]
            if not tags:
                issues.append("Scrap move %s has no waste category tag." % move.id)
                continue
            unexpected = sorted(set(tag for tag in tags if tag not in allowed_tags))
            if unexpected:
                issues.append("Scrap move %s has unsupported waste category tag(s): %s." % (move.id, ", ".join(unexpected)))
                continue
            categorized_moves |= move
            categorized_net_flow += delta
        elif move.is_inventory or source_usage == "inventory" or destination_usage == "inventory":
            categorized_moves |= move
            categorized_net_flow += delta
        elif source_usage == "internal" and destination_usage == "internal":
            # Internal transfers are a stock-flow item; within one organization their
            # net impact is zero, while branch-scoped targets see cross-boundary flow.
            categorized_moves |= move
            categorized_net_flow += delta
        elif source_inside and destination_usage == "production":
            categorized_moves |= move
            categorized_net_flow += delta
        elif source_usage == "supplier" and destination_inside:
            categorized_moves |= move
            categorized_net_flow += delta
        elif source_inside and destination_usage == "supplier":
            categorized_moves |= move
            categorized_net_flow += delta
        # Any other movement touching the scope is intentionally left in the residual
        # until its business meaning is explicitly classified.

    if issues:
        return source_result(
            target, version, DATA_QUALITY, None,
            "Stock-flow variance cannot be classified because waste reasons are missing or unsupported: %s" % "; ".join(issues[:20]),
            "stock.quant+stock.move", quants, opening_moves | closing_moves | period_moves,
        )
    expected_closing = opening_quantity + categorized_net_flow
    variance = closing_quantity - expected_closing
    if not all(math.isfinite(value) for value in (opening_quantity, closing_quantity, categorized_net_flow, variance)):
        return source_result(
            target, version, DATA_QUALITY, None,
            "Stock-flow reconciliation did not produce finite numeric quantities.",
            "stock.quant+stock.move", quants, opening_moves | closing_moves | period_moves,
        )
    return source_result(
        target, version, VALID, float(variance),
        "Stock-flow reconciliation residual = reconstructed closing stock minus opening stock and categorized net movements (supplier receipts/returns, production consumption, tagged waste, inventory adjustments and transfers). This is not a physical-count variance; unclassified flows remain in the residual.",
        "stock.quant+stock.move", quants, opening_moves | closing_moves | period_moves,
    )


def _collect_waste(target, version, start_utc, end_utc, calc):
    Move = target.env["stock.move"].sudo()
    required_fields = {
        "company_id", "product_id", "state", "date", "create_date", "is_scrap",
        "scrap_reason_tag_ids", "product_qty", "quantity_product_uom", "location_id",
    }
    if not required_fields.issubset(Move._fields):
        missing = ", ".join(sorted(required_fields.difference(Move._fields)))
        return source_result(
            target, version, DATA_QUALITY, None,
            "The installed stock.move model cannot classify waste reliably; missing fields: %s." % missing,
            "stock.move", Move.browse(),
        )

    domain = [
        ("company_id", "=", target.company_id.id),
        ("product_id", "=", version.source_product_id.id),
        ("is_scrap", "=", True),
        ("state", "!=", "cancel"),
    ]
    if calc == "inventory_validated_waste_quantity":
        domain.extend([
            ("state", "=", "done"),
            ("date", ">=", fields.Datetime.to_string(start_utc)),
            ("date", "<", fields.Datetime.to_string(end_utc)),
        ])
    else:
        domain.extend([
            ("create_date", ">=", fields.Datetime.to_string(start_utc)),
            ("create_date", "<", fields.Datetime.to_string(end_utc)),
        ])

    if target.scope == "branch":
        warehouse = target.warehouse_id
        if not warehouse or not warehouse.view_location_id:
            return source_result(
                target, version, DATA_QUALITY, None,
                "The branch warehouse has no stock view location, so its scrap movements cannot be isolated safely.",
                "stock.move", Move.browse(),
            )
        domain.append(("location_id", "child_of", warehouse.view_location_id.id))

    moves = Move.search(domain, order="date, id")
    if not moves:
        label = "recorded" if calc == "inventory_recorded_waste_quantity" else "validated"
        return source_result(
            target, version, VALID, 0.0,
            "No %s scrap movements were recorded for this product and reporting period; this is zero %s waste, not proof that no physical waste occurred." % (label, label),
            "stock.move", moves,
        )

    total = 0.0
    data_issues = []
    for move in moves:
        tags = [(tag.name or "").strip().casefold() for tag in move.scrap_reason_tag_ids]
        if not tags:
            data_issues.append("Scrap move %s has no waste category tag." % move.id)
            continue
        unexpected = sorted(set(tag for tag in tags if tag not in _ALLOWED_WASTE_TAGS))
        if unexpected:
            data_issues.append("Scrap move %s has unsupported waste category tag(s): %s." % (move.id, ", ".join(unexpected)))
            continue
        raw_quantity = move.quantity_product_uom if move.state == "done" else move.product_qty
        try:
            quantity = quantity_to_product_uom(raw_quantity, version.source_product_id.uom_id, version.source_product_id)
        except ValidationError as exc:
            data_issues.append("Scrap move %s: %s" % (move.id, exc))
            continue
        if not math.isfinite(quantity) or quantity < 0:
            data_issues.append("Scrap move %s has a negative or invalid waste quantity.")
            continue
        total += quantity

    if data_issues:
        return source_result(
            target, version, DATA_QUALITY, None,
            "Waste data is incomplete or cannot be classified under the approved categories: %s" % "; ".join(data_issues[:20]),
            "stock.move", moves,
        )
    if not math.isfinite(total):
        return source_result(target, version, DATA_QUALITY, None, "Waste quantity did not produce a finite numeric value.", "stock.move", moves)
    label = "recorded" if calc == "inventory_recorded_waste_quantity" else "validated"
    return source_result(
        target, version, VALID, float(total),
        "Total %s scrap quantity for the tracked product in its base unit. Allowed reason categories are Spoilage/Expiry and Damaged Goods only. Inventory variance is not classified as waste." % label,
        "stock.move", moves,
    )
