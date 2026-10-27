"""Inventory flow, production consumption and waste KPI source adapters."""

import math
from datetime import timedelta

from odoo import fields
from odoo.exceptions import ValidationError

from .pos import _sale_date_in_timezone, net_sales, period_bounds_utc, recipe_consumption, recipe_coverage, sales_orders
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
# Measured across products in company currency; no tracked product.
WASTE_VALUE_CALCULATIONS = {"inventory_waste_cost_amount", "inventory_waste_cost_to_sales_pct", "inventory_recorded_waste_cost"}
COUNT_CALCULATIONS = {"inventory_count_difference_quantity"}
STOCK_VALUE_CALCULATIONS = {"inventory_closing_stock_value"}
# Inventory control: recipes for sold menu items and planned counts (no tracked product); waste recording (a % for
# one product) and ingredient use between two counts (a quantity for one product).
RECIPE_COVERAGE_CALCULATIONS = {"inventory_recipe_coverage_pct"}
COUNT_PLAN_CALCULATIONS = {"inventory_count_plan_completion_pct"}
WASTE_RECORDING_CALCULATIONS = {"inventory_waste_recording_pct"}
INGREDIENT_USE_CALCULATIONS = {"inventory_ingredient_use_difference_quantity"}
SUPPORTED_CALCULATIONS = (WASTE_CALCULATIONS | BALANCE_CALCULATIONS | FLOW_CALCULATIONS | RECONCILIATION_CALCULATIONS
                          | WASTE_VALUE_CALCULATIONS | COUNT_CALCULATIONS | STOCK_VALUE_CALCULATIONS
                          | RECIPE_COVERAGE_CALCULATIONS | COUNT_PLAN_CALCULATIONS | WASTE_RECORDING_CALCULATIONS
                          | INGREDIENT_USE_CALCULATIONS)
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
    if calc in WASTE_VALUE_CALCULATIONS:
        return _collect_waste_value(target, version, calc)
    if calc in STOCK_VALUE_CALCULATIONS:
        return _collect_stock_value(target, version)
    if calc in RECIPE_COVERAGE_CALCULATIONS:
        return _collect_recipe_coverage(target, version)
    if calc in COUNT_PLAN_CALCULATIONS:
        return _collect_count_plan_completion(target, version)
    issue = (_validate_product_percent(target, version) if calc in WASTE_RECORDING_CALCULATIONS
             else validate_product_contract(target, version, "inventory"))
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
    if calc in COUNT_CALCULATIONS:
        return _collect_count_difference(target, version, start_utc, end_utc)
    if calc in WASTE_RECORDING_CALCULATIONS:
        return _collect_waste_recording(target, version, start_utc, end_utc)
    if calc in INGREDIENT_USE_CALCULATIONS:
        return _collect_ingredient_use_difference(target, version, start_utc, end_utc)
    return _collect_waste(target, version, start_utc, end_utc, calc)


def waste_move_cost(move):
    """Inventory cost of one done waste move, from Odoo's own valuation, or None if it is not reliable.

    Odoo 20 keeps valuation on the move itself (no stock.valuation.layer): ``value`` is the stock value
    that left, negative on an outgoing move, in company currency, once per move whatever its move lines.
    """
    if not move.is_out or move.value > 0:
        return None
    return -move.value


def valued_waste(env, company, start_utc, end_utc, warehouse=False, product=False):
    """Validated waste (done scrap moves) in the period and its inventory cost.

    Shared by the KPI and the goal's detail list. Returns ``(moves, total, problem)``;
    the total is None whenever any waste move is unclassified or has no reliable cost,
    so a partial figure, a sales price or a quantity is never shown as the waste cost.
    """
    Move = env["stock.move"].sudo()
    if not {"value", "is_out", "is_scrap", "scrap_reason_tag_ids", "quantity_product_uom"}.issubset(Move._fields):
        return Move.browse(), None, "Inventory Valuation is not installed, so Odoo records no cost for wasted stock."
    domain = [
        ("company_id", "=", company.id),
        ("state", "=", "done"),
        ("is_scrap", "=", True),
        ("date", ">=", fields.Datetime.to_string(start_utc)),
        ("date", "<", fields.Datetime.to_string(end_utc)),
    ]
    if warehouse:
        if not warehouse.view_location_id:
            return Move.browse(), None, "The branch warehouse has no stock location, so its waste cannot be isolated."
        domain.append(("location_id", "child_of", warehouse.view_location_id.id))
    if product:
        domain.append(("product_id", "=", product.id))
    moves = Move.search(domain, order="date, id")
    problems = []
    total = 0.0
    for move in moves:
        name = move.product_id.display_name
        tags = {(tag.name or "").strip().casefold() for tag in move.scrap_reason_tag_ids}
        cost = waste_move_cost(move)
        if not tags or tags.difference(_ALLOWED_WASTE_TAGS):
            problems.append("waste of %s has no Spoilage/Expiry or Damaged Goods reason" % name)
        elif cost is None:
            problems.append("waste of %s has no inventory valuation (only stored products are valued)" % name)
        elif move.quantity_product_uom > 0 and not cost:
            problems.append("%s has no cost, so its waste would count as free; set its cost in the product form" % name)
        else:
            total += cost
    if problems:
        return moves, None, "Waste cost cannot be calculated: %s." % "; ".join(sorted(set(problems))[:10])
    return moves, float(total), False


def net_pos_sales(env, company, start_utc, end_utc, warehouse=False):
    """Net POS sales excluding tax, with the POS sales KPI's rules. Returns ``(orders, amount, problem)``."""
    if "pos.order" not in env.registry.models:
        return None, None, "Point of Sale is not installed, so sales cannot be measured."
    orders, problem = sales_orders(env, company, start_utc, end_utc, warehouse)
    return orders, (None if problem else net_sales(orders)), problem


def _collect_waste_value(target, version, calc):
    Move = target.env["stock.move"].sudo()
    product = version.source_product_id
    if calc == "inventory_recorded_waste_cost":
        if not product:
            return source_result(target, version, DATA_QUALITY, None,
                                 "Select the product whose waste cost is measured.", "stock.move", Move.browse())
        if product.company_id and product.company_id != target.company_id:
            return source_result(target, version, DATA_QUALITY, None,
                                 "The tracked product must belong to the KPI company or be shared across companies.", "stock.move", Move.browse())
    elif product:
        return source_result(target, version, DATA_QUALITY, None,
                             "Waste cost is measured across all products and must not select a tracked product.", "stock.move", Move.browse())
    issue = validate_scope(target)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "stock.move", Move.browse())
    start_utc, end_utc, issue = reporting_period_utc(target)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "stock.move", Move.browse())
    warehouse = target.warehouse_id if target.scope == "branch" else False
    moves, cost, problem = valued_waste(target.env, target.company_id, start_utc, end_utc, warehouse, product)
    if problem:
        return source_result(target, version, DATA_QUALITY, None, problem, "stock.move", moves)
    if calc == "inventory_recorded_waste_cost":
        return source_result(
            target, version, VALID, cost,
            "Inventory cost Odoo recorded for %s validated waste records of %s (Spoilage/Expiry or Damaged Goods), in company "
            "currency. No waste records means zero recorded waste cost, not proof that nothing was wasted." % (len(moves), product.display_name),
            "stock.move", moves,
        )
    if calc == "inventory_waste_cost_amount":
        return source_result(
            target, version, VALID, cost,
            "Stock value of validated waste (scrapped with reason Spoilage/Expiry or Damaged Goods) in the period: %s waste records. "
            "No waste records means zero recorded waste, not proof that nothing was wasted." % len(moves),
            "stock.move", moves,
        )
    orders, sales, problem = net_pos_sales(target.env, target.company_id, start_utc, end_utc, warehouse)
    if problem:
        return source_result(target, version, DATA_QUALITY, None, problem, "stock.move+pos.order", moves, orders or [])
    if sales <= 0:
        return source_result(
            target, version, NOT_EVALUATED, None,
            "There were no net POS sales in this period, so waste cannot be compared with sales.",
            "stock.move+pos.order", moves, orders,
        )
    return source_result(
        target, version, VALID, cost / sales * 100.0,
        "Waste cost %.2f divided by net POS sales excluding tax %.2f (%s orders), for the same period and %s." % (
            cost, sales, len(orders), "branch" if warehouse else "company"),
        "stock.move+pos.order", moves, orders,
    )


def _collect_stock_value(target, version):
    """Value of one product's stock at the end of the period, from Odoo's own valuation at that date.

    For a branch, Odoo values the branch's share of the company quantity at the company valuation.
    """
    env = target.env
    Product = env["product.product"].sudo()
    product = version.source_product_id
    empty = env["stock.move"].browse()
    currency = target.company_id.currency_id
    if not product:
        return source_result(target, version, DATA_QUALITY, None, "Choose the product whose stock value is measured.", "product.product", empty)
    if (version.unit or "").strip() not in {(currency.symbol or "").strip(), (currency.name or "").strip()} - {""}:
        return source_result(target, version, DATA_QUALITY, None,
                             "Stock value must use the company currency unit (%s)." % (currency.symbol or currency.name), "product.product", empty)
    if "total_value" not in Product._fields:
        return source_result(target, version, DATA_QUALITY, None,
                             "Inventory Valuation is not installed, so Odoo records no stock value.", "product.product", empty)
    issue = validate_scope(target)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "product.product", empty)
    start_utc, end_utc, issue = reporting_period_utc(target)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "product.product", empty)
    if end_utc > fields.Datetime.to_datetime(fields.Datetime.now()):
        return source_result(target, version, NOT_EVALUATED, None,
                             "The period has not ended, so its closing stock value is not yet available.", "product.product", empty)
    context = {"to_date": target.date_end}
    if target.scope == "branch":
        context["warehouse_id"] = target.warehouse_id.id
    valued = Product.with_company(target.company_id).with_context(**context).browse(product.id)
    quantity, value = valued.qty_available, valued.total_value
    if not all(isinstance(number, (int, float)) and math.isfinite(number) for number in (quantity, value)):
        return source_result(target, version, DATA_QUALITY, None, "Stock valuation did not produce a finite value.", "product.product", empty)
    if quantity > 0 and not value:
        return source_result(target, version, DATA_QUALITY, None,
                             "%s has stock but no cost, so its stock value would show as zero; set its cost first." % product.display_name,
                             "product.product", empty)
    return source_result(
        target, version, VALID, float(value),
        "Stock value of %g %s of %s on %s, from Odoo's inventory valuation (not the sales price)%s."
        % (round(quantity, 4), product.uom_id.name, product.display_name, target.date_end,
           "; for a branch Odoo values its share of the company quantity" if target.scope == "branch" else ""),
        "product.product", empty,
    )


def _collect_count_difference(target, version, start_utc, end_utc):
    """Physical stock-count accuracy: counted minus system quantity, from counts applied in Odoo.

    Every applied count creates a done inventory move, including counts with no difference,
    so "no count" is distinguishable from "counted and correct". This is not stock-flow variance.
    """
    Move = target.env["stock.move"].sudo()
    locations, issue = _get_internal_scope_locations(target)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "stock.move", Move.browse())
    moves, issue = _moves_touching_scope(target, version, locations, start_utc, end_utc)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "stock.move", moves)
    counts = moves.filtered("is_inventory")
    if not counts:
        return source_result(
            target, version, NOT_EVALUATED, None,
            "No stock count was applied for this product in this period, so count accuracy cannot be measured.",
            "stock.move", moves,
        )
    scope_ids = set(locations.ids)
    total = sum(abs(_movement_delta(move, scope_ids)) for move in counts)
    return source_result(
        target, version, VALID, float(total),
        "Total difference between counted and system quantity over %s applied stock counts (Inventory > Physical Inventory). "
        "This is a physical-count measure, separate from stock-flow variance." % len(counts),
        "stock.move", counts,
    )


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


def pending_pos_deliveries(target, version, locations, start_utc, end_utc):
    """Stock moves of POS orders sold in the period that Inventory never completed (e.g. a lot number was required).

    Such sales reduced no stock, so measures of actual stock use cannot be complete while they exist.
    """
    env = target.env
    Move = env["stock.move"].sudo()
    if "pos_order_id" not in env["stock.picking"]._fields:
        return Move.browse()
    return Move.search([
        ("company_id", "=", target.company_id.id),
        ("product_id", "=", version.source_product_id.id),
        ("state", "not in", ("done", "cancel")),
        ("picking_id.pos_order_id", "!=", False),
        ("picking_id.pos_order_id.date_order", ">=", fields.Datetime.to_string(start_utc)),
        ("picking_id.pos_order_id.date_order", "<", fields.Datetime.to_string(end_utc)),
        ("location_id", "in", locations.ids),
    ], order="id")


def _pending_pos_issue(moves):
    pickings = moves.mapped("picking_id")
    return ("%s POS deliveries of %s sold in this period were not completed in Inventory (%s), so their ingredients "
            "were never taken out of stock and actual stock use is incomplete. Complete or fix these deliveries first."
            % (len(pickings), moves[:1].product_id.display_name, ", ".join(pickings.mapped("name")[:10])))


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
            # Recorded use: issues to production and to customers (POS and sales deliveries, including recipe
            # ingredients that POS kits deduct), net of customer returns. Scrap is waste, not use.
            if source_inside and move.location_dest_usage in ("production", "customer"):
                include, contribution = True, quantity
            elif destination_inside and move.location_usage == "customer":
                include, contribution = True, -quantity
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
    if calc == "inventory_actual_consumption_quantity":
        pending = pending_pos_deliveries(target, version, locations, start_utc, end_utc)
        if pending:
            return source_result(target, version, DATA_QUALITY, None, _pending_pos_issue(pending), "stock.move", moves | pending)
    if calc == "inventory_actual_consumption_quantity" and not selected:
        return source_result(
            target, version, NOT_EVALUATED, None,
            "No completed stock issues to production or customers were found for this product and reporting period; POS recipe estimates are not treated as actual stock consumption.",
            "stock.move", moves,
        )
    labels = {
        "inventory_actual_consumption_quantity": "actual consumption",
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
        "inventory_actual_consumption_quantity": "Recorded stock use: completed issues to production and to customers (POS and sales, including ingredients deducted by POS recipes), net of customer returns, excluding scrap. POS recipe estimates are a separate measure and are never added to these moves.",
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
    issues to customers (POS and sales deliveries, including recipe ingredients that
    POS kits deduct) and their returns, waste, inventory adjustments and transfers;
    the same classification as the Inventory dashboard. Anything else stays in the residual.
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
        elif (source_inside and destination_usage == "customer") or (source_usage == "customer" and destination_inside):
            # Sales: POS orders deduct stock to the customer location (kit recipes as ingredient moves); refunds return it.
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
    pending = pending_pos_deliveries(target, version, locations, start_utc, end_utc)
    if pending:
        return source_result(target, version, DATA_QUALITY, None, _pending_pos_issue(pending),
                             "stock.quant+stock.move", quants, opening_moves | closing_moves | period_moves | pending)
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
        "Stock-flow reconciliation residual = reconstructed closing stock minus opening stock and categorized net movements (supplier receipts/returns, production consumption, issues to and returns from customers including POS, tagged waste, inventory adjustments and transfers). This is not a physical-count variance; unclassified flows remain in the residual.",
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


# ---------------------------------------------------------------- inventory control
def _validate_product_percent(target, version):
    """A percentage about one product: the product is required and the unit is '%'."""
    if version.source_type != "inventory":
        return "Calculation type %s requires source type inventory." % version.calculation_type
    product = version.source_product_id
    if not product:
        return "The approved KPI definition version has no tracked source product."
    if product.company_id and product.company_id != target.company_id:
        return "The tracked product must belong to the KPI company or be shared across companies."
    if (version.unit or "").strip() != "%":
        return "This measure is a percentage and must use the '%' unit."
    return False


def _collect_recipe_coverage(target, version):
    """Share of distinct menu items marked 'Needs a recipe' and sold in the period that had a valid recipe for every sale.

    Each item counts once, whatever the quantity sold. Odoo recipes (Bills of Materials) have no approval status;
    only Manufacturing administrators can create or change one, so the active recipe in effect on the sale date is
    the approved recipe (same effective-date rule as the recipe-based consumption estimate).
    """
    env = target.env
    Line = env["pos.order.line"].sudo()
    if version.source_product_id:
        return source_result(target, version, DATA_QUALITY, None,
                             "Recipe coverage covers all menu items and must not select one product.", "pos.order.line", Line.browse())
    issue = validate_scope(target)
    if not issue:
        start_utc, end_utc, issue = reporting_period_utc(target)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "pos.order.line", Line.browse())
    company = target.company_id
    orders, problem = sales_orders(env, company, start_utc, end_utc, target.warehouse_id if target.scope == "branch" else False)
    if problem and not orders:
        return source_result(target, version, DATA_QUALITY, None, problem, "pos.config", Line.browse())
    items, missing, lines, boms = recipe_coverage(env, company, orders)
    if not items:
        marked = env["product.template"].sudo().search_count([
            ("strategic_recipe_required", "=", True), ("company_id", "in", [False, company.id])])
        return source_result(
            target, version, NOT_EVALUATED, None,
            "No menu item marked 'Needs a recipe' was sold in this period, so recipe coverage cannot be measured." if marked
            else "No menu item is marked 'Needs a recipe' yet. Mark the drinks and dishes made from ingredients (Point of "
                 "Sale > Products, Point of Sale tab) so recipe coverage can be measured.",
            "pos.order.line+mrp.bom", orders,
        )
    covered = len(items) - len(missing)
    reason = ("%s of %s menu items sold that need a recipe had a valid recipe for every sale (each item counted once, "
              "not by quantity)." % (covered, len(items)))
    if missing:
        reason += " Missing or invalid: %s." % "; ".join(
            "%s (%s)" % (product.display_name, why) for product, why in list(missing.items())[:10])
    return source_result(target, version, VALID, covered / len(items) * 100.0, reason,
                         "pos.order.line+mrp.bom", orders, lines, boms)


def count_plan_results(plans):
    """Status of each planned stock count: done, missed, open (not due yet) or cancelled.

    Done: every planned product has a count applied in Odoo (Inventory > Physical Inventory > Apply) at the branch
    between the planned date and the count-by date. Entering a counted quantity without applying it is not a count.
    A plan cancelled on or before its count-by date is left out; one cancelled later counts as missed.
    Returns ``{plan id: (status, products not counted, count moves)}``.
    """
    Move = plans.env["stock.move"].sudo()
    Location = plans.env["stock.location"].sudo()
    results = {}
    for plan in plans:
        moves, missing = Move.browse(), plan.product_ids
        start_utc, end_utc, problem = period_bounds_utc(plan.company_id, plan.date_planned, plan.date_due)
        if not problem and plan.warehouse_id.view_location_id:
            locations = Location.search([("id", "child_of", plan.warehouse_id.view_location_id.id), ("usage", "=", "internal")])
            moves = Move.search([
                ("company_id", "=", plan.company_id.id),
                ("product_id", "in", plan.product_ids.ids),
                ("state", "=", "done"),
                ("is_inventory", "=", True),
                ("date", ">=", fields.Datetime.to_string(start_utc)),
                ("date", "<", fields.Datetime.to_string(end_utc)),
                "|", ("location_id", "in", locations.ids), ("location_dest_id", "in", locations.ids),
            ])
            missing = plan.product_ids - moves.product_id
        if plan.state == "cancelled" and plan.cancelled_on and plan.cancelled_on <= plan.date_due:
            status = "cancelled"
        elif not missing:
            status = "done"
        elif plan.date_due < plan._company_today():
            status = "missed"
        else:
            status = "open"
        results[plan.id] = (status, missing, moves)
    return results


def _collect_count_plan_completion(target, version):
    """Share of the planned stock counts due in the period (and already due) that were applied in Odoo on time."""
    env = target.env
    Plan = env["strategic.stock.count.plan"].sudo()
    if version.source_product_id:
        return source_result(target, version, DATA_QUALITY, None,
                             "Planned count completion covers whole count plans and must not select one product.",
                             "strategic.stock.count.plan", Plan.browse())
    issue = validate_scope(target)
    if not issue:
        _start, _end, issue = reporting_period_utc(target)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "strategic.stock.count.plan", Plan.browse())
    domain = [("company_id", "=", target.company_id.id), ("date_due", ">=", target.date_start), ("date_due", "<=", target.date_end)]
    if target.scope == "branch":
        domain.append(("warehouse_id", "=", target.warehouse_id.id))
    plans = Plan.search(domain, order="date_due, id")
    results = count_plan_results(plans)
    due = plans.filtered(lambda plan: results[plan.id][0] in ("done", "missed"))
    moves = env["stock.move"].sudo().browse()
    for _status, _missing, plan_moves in results.values():
        moves |= plan_moves
    if not due:
        return source_result(
            target, version, NOT_EVALUATED, None,
            "No stock count is planned for this period. Plan counts under Strategic Management > Strategy & KPIs > "
            "Planned Stock Counts." if not plans else
            "No planned stock count was due yet in this period (counts not yet due and plans cancelled in time are left out).",
            "strategic.stock.count.plan", plans,
        )
    done = due.filtered(lambda plan: results[plan.id][0] == "done")
    reason = ("%s of %s planned stock counts due in this period were applied in Odoo between their planned and count-by "
              "dates." % (len(done), len(due)))
    missed = due - done
    if missed:
        reason += " Missed: %s." % "; ".join(
            "%s (not counted: %s)" % (plan.name, ", ".join(results[plan.id][1].mapped("display_name")[:5])) for plan in missed[:10])
    return source_result(target, version, VALID, len(done) / len(due) * 100.0, reason,
                         "strategic.stock.count.plan+stock.move", due, moves)


def _collect_waste_recording(target, version, start_utc, end_utc):
    """Share of the product's identified stock losses that were recorded as waste with an approved reason.

    Identified losses = recorded waste + stock found missing at the period's applied counts (net of counts that
    found extra stock). Counts are the independent evidence: waste that was never recorded only becomes visible when
    stock is counted, so a period without a count has no result. Waste without an approved reason counts as not
    properly recorded.
    """
    Move = target.env["stock.move"].sudo()
    product = version.source_product_id
    locations, issue = _get_internal_scope_locations(target)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "stock.move", Move.browse())
    moves, issue = _moves_touching_scope(target, version, locations, start_utc, end_utc)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "stock.move", moves)
    counts = moves.filtered("is_inventory")
    if not counts:
        return source_result(
            target, version, NOT_EVALUATED, None,
            "No stock count of %s was applied in this period. Waste that was never recorded only shows up when stock is "
            "counted, so waste recording cannot be checked." % product.display_name,
            "stock.move", moves,
        )
    pending = pending_pos_deliveries(target, version, locations, start_utc, end_utc)
    if pending:
        return source_result(target, version, DATA_QUALITY, None, _pending_pos_issue(pending), "stock.move", moves | pending)
    scope_ids = set(locations.ids)
    recorded = unclassified = 0.0
    unclassified_moves = Move.browse()
    for move in moves.filtered("is_scrap"):
        quantity = max(0.0, -_movement_delta(move, scope_ids))
        tags = {(tag.name or "").strip().casefold() for tag in move.scrap_reason_tag_ids}
        if tags and tags <= _ALLOWED_WASTE_TAGS:
            recorded += quantity
        else:
            unclassified += quantity
            unclassified_moves |= move
    found_missing = max(0.0, -sum(_movement_delta(move, scope_ids) for move in counts))
    identified = recorded + unclassified + found_missing
    unit = product.uom_id.name
    if identified <= 1e-9:
        return source_result(
            target, version, NOT_EVALUATED, None,
            "Stock was counted (%s applied counts) and no waste or missing stock was found, so there was nothing to record."
            % len(counts), "stock.move", moves,
        )
    reason = "%g %s of %g %s identified losses were recorded as waste with reason Spoilage/Expiry or Damaged Goods." % (
        round(recorded, 4), unit, round(identified, 4), unit)
    if unclassified:
        reason += " Recorded without an approved reason: %g %s (moves %s)." % (
            round(unclassified, 4), unit, ", ".join(map(str, unclassified_moves.ids[:10])))
    if found_missing:
        reason += (" Found missing at the stock counts and never recorded: %g %s; this may be unrecorded waste, a counting "
                   "error or an unrecorded transfer." % (round(found_missing, 4), unit))
    return source_result(target, version, VALID, recorded / identified * 100.0, reason, "stock.move", moves)


def _collect_ingredient_use_difference(target, version, start_utc, end_utc):
    """Ingredient use shown by stock counts minus the use POS sales and recipes explain, between two counts.

    Window: after the last count applied on the first counted day, up to the last count applied on the last counted
    day of the period (two different days are required). Counted use = opening count + receipts + transfers in -
    transfers out - recorded waste - closing count, i.e. every other stock change in the window, including the closing
    count's correction; that correction is what makes the figure independent of the recipe-generated POS moves.
    Expected use = POS sales in the window x the recipe in effect on each sale date. Positive = more used than the
    recipes explain. The counts must cover every branch location that holds the ingredient.
    """
    env = target.env
    Move = env["stock.move"].sudo()
    product = version.source_product_id
    locations, issue = _get_internal_scope_locations(target)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "stock.move", Move.browse())
    moves, issue = _moves_touching_scope(target, version, locations, start_utc, end_utc)
    if issue:
        return source_result(target, version, DATA_QUALITY, None, issue, "stock.move", moves)
    timezone_name = target.company_id.partner_id.tz
    counts = moves.filtered("is_inventory")
    count_days = sorted({_sale_date_in_timezone(move.date, timezone_name) for move in counts})
    if len(count_days) < 2:
        return source_result(
            target, version, NOT_EVALUATED, None,
            "Needs stock counts of %s applied on at least two different days in this period (found %s). The counts are the "
            "independent evidence of what was really used." % (product.display_name, len(count_days)),
            "stock.move", moves,
        )
    window_start = max(move.date for move in counts if _sale_date_in_timezone(move.date, timezone_name) == count_days[0])
    window_end = max(move.date for move in counts if _sale_date_in_timezone(move.date, timezone_name) == count_days[-1])
    scope_ids = set(locations.ids)
    counted_use = 0.0
    production = Move.browse()
    for move in moves.filtered(lambda move: window_start < move.date <= window_end):
        usages = (move.location_usage, move.location_dest_usage)
        # Recorded waste, receipts, supplier returns and transfers are explained stock changes.
        if move.is_scrap or "supplier" in usages or "transit" in usages or usages == ("internal", "internal"):
            continue
        if "production" in usages:
            production |= move
            continue
        counted_use -= _movement_delta(move, scope_ids)
    if production:
        return source_result(
            target, version, DATA_QUALITY, None,
            "%s was also used or produced by manufacturing orders between the counts; POS recipes cannot explain that use, "
            "so the difference is not calculated." % product.display_name, "stock.move", moves,
        )
    window_end_exclusive = window_end + timedelta(seconds=1)
    pending = pending_pos_deliveries(target, version, locations, window_start, window_end_exclusive)
    if pending:
        return source_result(target, version, DATA_QUALITY, None, _pending_pos_issue(pending), "stock.move", moves | pending)
    orders, problem = sales_orders(env, target.company_id, window_start, window_end_exclusive,
                                   target.warehouse_id if target.scope == "branch" else False)
    if problem and not orders:
        return source_result(target, version, DATA_QUALITY, None, problem, "stock.move", moves)
    _items, missing, _lines, _boms = recipe_coverage(env, target.company_id, orders)
    if missing:
        return source_result(
            target, version, DATA_QUALITY, None,
            "Menu items that need a recipe were sold between the counts without a valid one, so expected use would be too "
            "low: %s." % "; ".join("%s (%s)" % (item.display_name, why) for item, why in list(missing.items())[:10]),
            "stock.move+pos.order", moves, orders,
        )
    status, expected, reason, _model, _tokens, _refs = recipe_consumption(env, target.company_id, orders, product)
    if status != VALID:
        return source_result(target, version, DATA_QUALITY, None, reason, "stock.move+pos.order", moves, orders)
    unit = product.uom_id.name
    difference = counted_use - expected
    return source_result(
        target, version, VALID, float(difference),
        "Between the stock counts of %s and %s: the counts show %g %s used; POS sales and recipes explain %g %s; difference "
        "%+g %s. Receipts, transfers and recorded waste are left out because they are explained. A difference is a reason to "
        "investigate (unrecorded waste, counting errors, unrecorded transfers or recipe differences), not proof of loss."
        % (count_days[0], count_days[-1], round(counted_use, 4), unit, round(expected, 4), unit, round(difference, 4), unit),
        "stock.move+pos.order", moves, orders,
    )


# ---------------------------------------------------------------- current stock snapshot (inventory monitoring)
def _monitored_products(env, company, product=False, category=False):
    """Active storable products of the company, or one product, or a product category with its sub-categories."""
    domain = [("is_storable", "=", True), ("company_id", "in", [False, company.id])]
    if product:
        domain.append(("id", "=", product.id))
    if category:
        domain.append(("categ_id", "child_of", category.id))
    return env["product.product"].sudo().search(domain, order="name, id")


def stock_snapshot(env, company, warehouses, product=False, category=False):
    """Odoo's current stock of each monitored product in each branch, one row per branch and product.

    Quantities are Odoo's own for the warehouse (its internal locations), in the product's unit: on hand
    (qty_available), available after reservations (free_qty), reserved (on hand - available), pending incoming and
    outgoing moves, and forecast (virtual_available = on hand + incoming - outgoing, with no date horizon). Odoo keeps
    no stock record for empty stock, so a monitored product without one is listed at zero, not dropped. This is the
    stock now; it says nothing about a past period. Returns rows.
    """
    products = _monitored_products(env, company, product, category)
    rows = []
    for warehouse in warehouses.sorted(lambda w: (w.name, w.id)):
        for item in products.with_context(warehouse_id=warehouse.id):
            on_hand, available = item.qty_available, item.free_qty
            rows.append({"warehouse": warehouse, "branch": warehouse.display_name, "product": item, "name": item.display_name,
                         "unit": item.uom_id.name or "", "on_hand": on_hand, "available": available, "reserved": on_hand - available,
                         "incoming": item.incoming_qty, "outgoing": item.outgoing_qty, "forecast": item.virtual_available})
    return rows


def minimum_stock_rules(env, company, warehouses, product=False, category=False):
    """Active minimum-stock rules (reordering rules) of the branches, judged with Odoo's own rule semantics.

    Below minimum = the rule's forecast (qty_forecast: forecast at the rule's location within its lead time, plus
    quantities on draft purchase orders) is under its minimum. Odoo allows one rule per product and location, so each
    row is one rule; products without a rule are not judged. Returns rows.
    """
    domain = [("company_id", "=", company.id), ("warehouse_id", "in", warehouses.ids)]
    if product:
        domain.append(("product_id", "=", product.id))
    if category:
        domain.append(("product_id.categ_id", "child_of", category.id))
    rows = []
    for rule in env["stock.warehouse.orderpoint"].sudo().search(domain, order="warehouse_id, product_id, id"):
        forecast, minimum = rule.qty_forecast, rule.product_min_qty
        rows.append({"rule": rule, "warehouse": rule.warehouse_id, "branch": rule.warehouse_id.display_name, "product": rule.product_id,
                     "name": rule.product_id.display_name, "unit": rule.uom_id.name or "", "location": rule.location_id.display_name,
                     "on_hand": rule.qty_on_hand, "forecast": forecast, "minimum": minimum, "maximum": rule.product_max_qty,
                     "below": rule.uom_id.compare(forecast, minimum) < 0, "shortfall": max(0.0, minimum - forecast)})
    return rows


def unfinished_pos_deliveries(env, company, warehouses, product=False, category=False):
    """Stock moves of POS orders whose delivery is not completed (neither done nor cancelled), in the branches.

    In real-time POS stock mode a sale takes stock out through a delivery. While it stays unfinished, on-hand stock is
    unchanged, its reserved quantity lowers available stock and its full quantity lowers forecast stock. Some may simply
    be waiting; nothing here validates, cancels or unreserves them. Returns rows (one per move).
    """
    if "pos_order_id" not in env["stock.picking"]._fields:
        return []
    domain = [("company_id", "=", company.id), ("picking_id.pos_order_id", "!=", False), ("state", "not in", ("done", "cancel")),
              ("picking_id.picking_type_id.warehouse_id", "in", warehouses.ids)]
    if product:
        domain.append(("product_id", "=", product.id))
    if category:
        domain.append(("product_id.categ_id", "child_of", category.id))
    states = dict(env["stock.picking"]._fields["state"].selection)
    rows = []
    for move in env["stock.move"].sudo().search(domain, order="picking_id, id"):
        picking = move.picking_id
        tracked = move.product_id.tracking in ("lot", "serial")
        missing_lot = tracked and (not move.move_line_ids or any(not line.lot_id for line in move.move_line_ids))
        rows.append({"move": move, "warehouse": picking.picking_type_id.warehouse_id, "branch": picking.picking_type_id.warehouse_id.display_name,
                     "name": move.product_id.display_name, "unit": move.product_id.uom_id.name or "",
                     "reserved": move.quantity_product_uom, "to_deliver": move.product_qty, "delivery": picking.name,
                     "state": states.get(picking.state, picking.state), "order": picking.pos_order_id.name,
                     "hint": "lot number missing" if missing_lot else ""})
    return rows


def renamed_unit_note(env, products):
    """A warning when Odoo's 'Units' unit has been renamed (e.g. to 'Kg') and monitored products use it."""
    unit = env.ref("uom.product_uom_unit", raise_if_not_found=False)
    if not unit or (unit.name or "").strip().casefold() in ("units", "unit"):
        return ""
    affected = products.filtered(lambda item: item.uom_id == unit)
    if not affected:
        return ""
    return ("Odoo's 'Units' unit is renamed '%s' in this database: %s %s counted in pieces under that name, which cannot be "
            "converted to grams or kilograms." % (unit.name, ", ".join(affected.mapped("display_name")[:5]), "is" if len(affected) == 1 else "are"))
