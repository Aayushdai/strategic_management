"""Point-of-Sale source adapter for Strategic Performance Management.

The adapter returns normalized measurement payloads only. It does not apply KPI
attainment rules, create evaluations, or mutate operational POS/MRP records.
"""

from datetime import datetime, time, timedelta
import hashlib
import json
import math

import pytz

from odoo import fields
from odoo.exceptions import UserError, ValidationError

from .common import convert_quantity, units_compatible, validate_unit_price_contract


VALID = "valid"
NOT_EVALUATED = "not_evaluated"
DATA_QUALITY = "data_quality_issue"
# Odoo 20 POS order states that count as a sale (draft and cancelled orders do not). Shared with the dashboards.
POS_SALE_STATES = ["paid", "done"]
# Sales amounts in company currency; only pos_product_net_sales tracks a product.
SALES_CALCULATIONS = {"pos_net_sales", "pos_sales_growth_pct", "pos_average_order_value", "pos_product_net_sales",
                      "pos_average_unit_price"}
_PRODUCT_SALES_CALCULATIONS = {"pos_product_net_sales", "pos_average_unit_price"}
_SUPPORTED_CALCULATIONS = {
    "pos_net_sales",
    "pos_sales_growth_pct",
    "pos_average_order_value",
    "pos_product_net_sales",
    "pos_average_unit_price",
    "pos_sold_quantity",
    "pos_recipe_consumption_estimate",
}


def collect_pos_result(target, version):
    """Collect a POS-backed result for an approved strategic KPI target.

    Supported first-batch calculations:
      * pos_sold_quantity: net product quantity sold through completed POS orders.
      * pos_recipe_consumption_estimate: recipe-derived component quantity for
        completed POS sales, using the BoM effective on each sale date.

    The caller is responsible for role/target-scope authorization before calling.
    """
    if version.source_type != "pos" or version.calculation_type not in _SUPPORTED_CALCULATIONS:
        return _result(
            target, version, DATA_QUALITY, None,
            "The approved KPI definition version does not select a supported POS calculation.",
            "", [], [],
        )
    if version.calculation_type in SALES_CALCULATIONS:
        return _collect_sales(target, version)
    if not version.source_product_id:
        return _result(
            target, version, DATA_QUALITY, None,
            "The approved KPI definition version has no tracked source product or ingredient.",
            "", [], [],
        )
    source_uom = version.source_product_id.uom_id
    if not source_uom or not (version.unit or "").strip() or (version.unit or "").strip().casefold() != (source_uom.name or "").strip().casefold():
        return _result(
            target, version, DATA_QUALITY, None,
            "The KPI version unit must match the tracked product's base unit (%s)." % (source_uom.name if source_uom else "missing"),
            "", [], [],
        )
    if not target.company_id or not target.date_start or not target.date_end:
        return _result(
            target, version, DATA_QUALITY, None,
            "The target is missing its company or reporting-period boundaries.",
            "", [], [],
        )
    if target.date_start > target.date_end:
        return _result(
            target, version, DATA_QUALITY, None,
            "The target reporting period starts after it ends.",
            "", [], [],
        )

    env = target.env
    company_tz_name = target.company_id.partner_id.tz
    if not company_tz_name:
        return _result(
            target, version, DATA_QUALITY, None,
            "The company contact has no timezone configured; POS date boundaries cannot be evaluated safely.",
            "", [], [],
        )
    try:
        company_tz = pytz.timezone(company_tz_name)
        local_start = company_tz.localize(datetime.combine(fields.Date.to_date(target.date_start), time.min), is_dst=None)
        local_end = company_tz.localize(
            datetime.combine(fields.Date.to_date(target.date_end) + timedelta(days=1), time.min),
            is_dst=None,
        )
        start_utc = local_start.astimezone(pytz.UTC).replace(tzinfo=None)
        end_utc = local_end.astimezone(pytz.UTC).replace(tzinfo=None)
    except (pytz.UnknownTimeZoneError, pytz.InvalidTimeError, ValueError, TypeError) as exc:
        return _result(
            target, version, DATA_QUALITY, None,
            "The company timezone or period boundary is invalid: %s" % exc,
            "", [], [],
        )

    order_domain = [
        ("company_id", "=", target.company_id.id),
        ("state", "in", POS_SALE_STATES),
        ("date_order", ">=", fields.Datetime.to_string(start_utc)),
        ("date_order", "<", fields.Datetime.to_string(end_utc)),
    ]
    if target.scope == "branch":
        pos_config = env["pos.config"].sudo()
        picking_type = env["stock.picking.type"].sudo()
        if "picking_type_id" not in pos_config._fields or "warehouse_id" not in picking_type._fields:
            return _result(
                target, version, DATA_QUALITY, None,
                "Branch POS mapping is unavailable: the installed POS stock configuration does not expose POS-to-warehouse links.",
                "", [], [],
            )
        mapped_configs = pos_config.search([
            ("company_id", "=", target.company_id.id),
            ("picking_type_id.warehouse_id", "=", target.warehouse_id.id),
        ])
        if not mapped_configs:
            return _result(
                target, version, DATA_QUALITY, None,
                "No Point of Sale configuration maps to this branch warehouse. Configure and verify the POS-to-warehouse mapping before evaluating this target.",
                "pos.config", [], [],
            )
        order_domain.append(("config_id", "in", mapped_configs.ids))
    elif target.scope != "organization":
        return _result(
            target, version, DATA_QUALITY, None,
            "POS source evaluation supports organization or branch targets, not employee-level targets.",
            "", [], [],
        )

    Order = env["pos.order"].sudo()
    Line = env["pos.order.line"].sudo()
    orders = Order.search(order_domain, order="date_order, id")
    if not orders:
        return _result(
            target, version, NOT_EVALUATED, None,
            "No paid or completed POS orders were found for this company, scope, and reporting period; zero has not been assumed.",
            "pos.order", [], [],
        )

    if version.calculation_type == "pos_sold_quantity":
        lines = Line.search([
            ("order_id", "in", orders.ids),
            ("product_id", "=", version.source_product_id.id),
        ], order="order_id, id")
        quantity = sum(lines.mapped("qty"))
        if not math.isfinite(quantity):
            return _result(
                target, version, DATA_QUALITY, None,
                "POS sold quantities contain a non-finite value.",
                "pos.order.line", _record_tokens(orders) + _record_tokens(lines), _refs(orders, lines),
            )
        # A valid completed POS dataset with no lines for the tracked product is a
        # measured zero. Refund lines remain signed negative quantities in Odoo.
        refs = _refs(orders, lines)
        tokens = _record_tokens(orders) + _record_tokens(lines)
        return _result(
            target, version, VALID, float(quantity),
            "Net sold quantity from paid/completed POS orders; refund quantities retain their Odoo sign."
            if lines.filtered(lambda line: line.qty < 0)
            else "Net sold quantity from paid/completed POS orders.",
            "pos.order.line", tokens, refs,
        )

    return _collect_recipe_consumption(target, version, orders, Line)


def period_bounds_utc(company, date_start, date_end):
    """Company-timezone day bounds as naive UTC: inclusive start, exclusive end. Returns (start, end, problem)."""
    if not company.partner_id.tz:
        return None, None, "The company contact has no timezone configured; POS date boundaries cannot be evaluated safely."
    try:
        company_tz = pytz.timezone(company.partner_id.tz)
        local_start = company_tz.localize(datetime.combine(fields.Date.to_date(date_start), time.min), is_dst=None)
        local_end = company_tz.localize(datetime.combine(fields.Date.to_date(date_end) + timedelta(days=1), time.min), is_dst=None)
    except (pytz.UnknownTimeZoneError, pytz.InvalidTimeError, ValueError, TypeError) as exc:
        return None, None, "The company timezone or period boundary is invalid: %s" % exc
    return local_start.astimezone(pytz.UTC).replace(tzinfo=None), local_end.astimezone(pytz.UTC).replace(tzinfo=None), False


def sales_orders(env, company, start_utc, end_utc, warehouse=False):
    """Paid or posted POS orders of the company (or of the branch's mapped POS) in the period.

    Refund orders are included: their amounts are negative, so they reduce net sales.
    Returns ``(orders, problem)``; orders in another currency are a problem, never converted silently.
    """
    Order = env["pos.order"].sudo()
    config_domain = [("company_id", "=", company.id)]
    if warehouse:
        config_domain.append(("picking_type_id.warehouse_id", "=", warehouse.id))
    configs = env["pos.config"].sudo().with_context(active_test=False).search(config_domain)
    if not configs:
        return Order.browse(), "No Point of Sale is linked to %s, so its sales cannot be measured." % (
            warehouse.display_name if warehouse else "this company")
    orders = Order.search([
        ("company_id", "=", company.id),
        ("state", "in", POS_SALE_STATES),
        ("date_order", ">=", fields.Datetime.to_string(start_utc)),
        ("date_order", "<", fields.Datetime.to_string(end_utc)),
        ("config_id", "in", configs.ids),
    ], order="date_order, id")
    foreign = orders.filtered(lambda order: order.currency_id != company.currency_id)
    if foreign:
        return orders, "%s POS orders are in another currency (%s); sales cannot be added up safely." % (
            len(foreign), ", ".join(sorted(set(foreign.mapped("currency_id.name")))))
    return orders, False


def net_sales(orders):
    """Sales excluding tax; refund orders carry negative amounts (the Business Overview convention)."""
    return float(sum(order.amount_total - order.amount_tax for order in orders))


def customer_orders(orders):
    """Customer orders for an average: refunds reduce revenue but are not new customer orders."""
    return orders.filtered(lambda order: not order.is_refund)


def window_sales(env, company, date_from, date_to, window_start, window_end, warehouse=False, product=False, category=False):
    """POS sales between two times of day, on every date from ``date_from`` to ``date_to`` (company time zone).

    The window is ``window_start`` (inclusive) to ``window_end`` (exclusive), in hours: 17.0 to 21.0 is 17:00-20:59:59.
    Sales: paid or posted orders (draft and cancelled ones are not sales); order lines excluding tax, after discounts,
    so nothing is counted twice. Order-level discount and tip lines belong to no menu item, so they count only when no
    product or category filter is set. A refund is a POS order of its own with negative lines: it reduces sales and
    quantity on the day and time it was made and is not an order. Orders: customer orders with at least one counted
    line. Quantity: per unit of measure (combo summary rows skipped, their items counted).
    Returns totals, per-day, per-product and per-branch figures, plus each counted order with its local time ("facts",
    for time breakdowns that must add up to the same totals), or ``{"problem": ...}`` when the data cannot be measured.
    """
    start_utc, end_utc, problem = period_bounds_utc(company, date_from, date_to)
    if problem:
        return {"problem": problem}
    orders, problem = sales_orders(env, company, start_utc, end_utc, warehouse)
    if problem:
        return {"problem": problem}
    timezone = pytz.timezone(company.partner_id.tz)
    categories = env["pos.category"].sudo().search([("id", "child_of", category.id)]) if category else False

    def bucket():
        return {"revenue": 0.0, "orders": 0, "quantity": {}}

    def add(totals, revenue, counted_order, quantities):
        totals["revenue"] += revenue
        totals["orders"] += counted_order
        for unit, qty in quantities.items():
            totals["quantity"][unit] = totals["quantity"].get(unit, 0.0) + qty

    days, branches, products, total, facts = {}, {}, {}, bucket(), []
    for offset in range((fields.Date.to_date(date_to) - fields.Date.to_date(date_from)).days + 1):
        days[fields.Date.to_date(date_from) + timedelta(days=offset)] = bucket()
    for order in orders:
        local = pytz.UTC.localize(fields.Datetime.to_datetime(order.date_order)).astimezone(timezone)
        hour = local.hour + local.minute / 60.0 + local.second / 3600.0
        if not window_start <= hour < window_end:
            continue
        lines = order.lines.filtered(lambda line: (not product or line.product_id == product) and (
            not categories or line.product_id.pos_categ_ids & categories))
        if (product or categories) and not lines:
            continue
        revenue, quantities = 0.0, {}
        for line in lines:
            revenue += line.price_subtotal
            if line.combo_line_ids:
                continue
            unit = line.product_id.uom_id.name or ""
            quantities[unit] = quantities.get(unit, 0.0) + line.qty
            item = products.setdefault(line.product_id.id, {"name": line.product_id.display_name, "unit": unit, "qty": 0.0, "revenue": 0.0})
            item["qty"] += line.qty
            item["revenue"] += line.price_subtotal
        counted_order = 0 if order.is_refund else 1
        branch = order.config_id.picking_type_id.warehouse_id
        add(total, revenue, counted_order, quantities)
        add(days.setdefault(local.date(), bucket()), revenue, counted_order, quantities)
        add(branches.setdefault(branch.id or 0, dict(bucket(), name=branch.display_name or "POS without a branch")),
            revenue, counted_order, quantities)
        facts.append({"order": order, "local": local.replace(tzinfo=None), "revenue": revenue, "orders": counted_order,
                      "quantity": quantities, "branch": branch.display_name or "POS without a branch"})
    return {"problem": False, "orders_in_period": len(orders), "total": total, "days": days, "branches": branches,
            "products": sorted(products.values(), key=lambda item: (-item["qty"], item["name"])), "facts": facts}


def _collect_sales(target, version):
    calc = version.calculation_type
    env = target.env
    company = target.company_id
    currency = company.currency_id
    unit = (version.unit or "").strip()
    if calc == "pos_sales_growth_pct" and unit != "%":
        return _result(target, version, DATA_QUALITY, None, "Sales growth must use the % unit.", "", [], [])
    if calc == "pos_average_unit_price":
        problem = validate_unit_price_contract(target, version, "pos")
        if problem:
            return _result(target, version, DATA_QUALITY, None, problem, "", [], [])
    elif calc != "pos_sales_growth_pct" and unit not in {(currency.symbol or "").strip(), (currency.name or "").strip()} - {""}:
        return _result(target, version, DATA_QUALITY, None,
                       "Sales amounts must use the company currency unit (%s)." % (currency.symbol or currency.name), "", [], [])
    if (calc in _PRODUCT_SALES_CALCULATIONS) != bool(version.source_product_id):
        return _result(target, version, DATA_QUALITY, None,
                       "Product sales need the tracked product; the other sales measures cover all products.", "", [], [])
    if target.scope not in ("organization", "branch") or (target.scope == "branch" and not target.warehouse_id):
        return _result(target, version, DATA_QUALITY, None,
                       "POS sales are measured for the company or one branch warehouse.", "", [], [])
    if not target.date_start or not target.date_end or target.date_start > target.date_end:
        return _result(target, version, DATA_QUALITY, None, "A valid reporting period is required.", "", [], [])
    start_utc, end_utc, problem = period_bounds_utc(company, target.date_start, target.date_end)
    if problem:
        return _result(target, version, DATA_QUALITY, None, problem, "", [], [])
    warehouse = target.warehouse_id if target.scope == "branch" else False
    if calc == "pos_sales_growth_pct" and end_utc > fields.Datetime.to_datetime(fields.Datetime.now()):
        return _result(target, version, NOT_EVALUATED, None,
                       "Growth is compared once the period has ended; a partial period would not be comparable.", "", [], [])
    orders, problem = sales_orders(env, company, start_utc, end_utc, warehouse)
    tokens, refs = _record_tokens(orders), _refs(orders)
    if problem:
        return _result(target, version, DATA_QUALITY, None, problem, "pos.order", tokens, refs)
    if not orders:
        return _result(target, version, NOT_EVALUATED, None,
                       "No paid or posted POS orders were found for this scope and period; zero sales have not been assumed.",
                       "pos.order", tokens, refs)
    total = net_sales(orders)
    if calc == "pos_net_sales":
        return _result(target, version, VALID, total,
                       "Net sales excluding tax from %s paid or posted POS orders; refunds are subtracted." % len(orders),
                       "pos.order", tokens, refs)
    if calc == "pos_average_order_value":
        count = len(customer_orders(orders))
        if not count:
            return _result(target, version, NOT_EVALUATED, None,
                           "There were no customer orders (only refunds) in this period, so no average can be calculated.",
                           "pos.order", tokens, refs)
        return _result(target, version, VALID, total / count,
                       "Net sales excluding tax (%.2f, refunds subtracted) divided by %s customer orders; refund orders are "
                       "not counted as orders." % (total, count), "pos.order", tokens, refs)
    if calc == "pos_average_unit_price":
        product = version.source_product_id
        lines = env["pos.order.line"].sudo().search([
            ("order_id", "in", orders.ids), ("product_id", "=", product.id)], order="order_id, id")
        quantity = float(sum(lines.mapped("qty")))
        if quantity <= 0:
            return _result(target, version, NOT_EVALUATED, None,
                           "%s was not sold (net of refunds) in this period, so there is no selling price." % product.display_name,
                           "pos.order.line", tokens + _record_tokens(lines), _refs(orders, lines))
        value = float(sum(lines.mapped("price_subtotal"))) / quantity
        return _result(target, version, VALID, value,
                       "Selling price per %s: POS sales excluding tax divided by %g %s sold, refunds subtracted from both. "
                       "This is what customers paid, not a purchase cost." % (product.uom_id.name, quantity, product.uom_id.name),
                       "pos.order.line", tokens + _record_tokens(lines), _refs(orders, lines))
    if calc == "pos_product_net_sales":
        lines = env["pos.order.line"].sudo().search([
            ("order_id", "in", orders.ids), ("product_id", "=", version.source_product_id.id)], order="order_id, id")
        value = float(sum(lines.mapped("price_subtotal")))
        return _result(target, version, VALID, value,
                       "Sales excluding tax of %s from %s POS lines; refund lines carry negative amounts and are subtracted."
                       % (version.source_product_id.display_name, len(lines)),
                       "pos.order.line", tokens + _record_tokens(lines), _refs(orders, lines))
    # Sales growth against the immediately preceding period of the same length.
    length = (fields.Date.to_date(target.date_end) - fields.Date.to_date(target.date_start)).days + 1
    previous_end = fields.Date.to_date(target.date_start) - timedelta(days=1)
    previous_start = previous_end - timedelta(days=length - 1)
    prev_start_utc, prev_end_utc, problem = period_bounds_utc(company, previous_start, previous_end)
    previous, problem = (None, problem) if problem else sales_orders(env, company, prev_start_utc, prev_end_utc, warehouse)
    if problem:
        return _result(target, version, DATA_QUALITY, None, problem, "pos.order", tokens, refs)
    baseline = net_sales(previous)
    tokens, refs = tokens + _record_tokens(previous), _refs(orders | previous)
    if not previous or baseline <= 0:
        return _result(target, version, NOT_EVALUATED, None,
                       "The previous period (%s to %s) has no positive net sales, so growth cannot be calculated."
                       % (previous_start, previous_end), "pos.order", tokens, refs)
    return _result(target, version, VALID, (total - baseline) / baseline * 100.0,
                   "Net sales %.2f compared with %.2f in the previous period (%s to %s), same scope and rules."
                   % (total, baseline, previous_start, previous_end), "pos.order", tokens, refs)


def _collect_recipe_consumption(target, version, orders, Line):
    return _result(target, version, *recipe_consumption(target.env, target.company_id, orders, version.source_product_id))


def recipe_consumption(env, company, orders, tracked_product):
    """Estimate ``tracked_product`` consumed by completed POS ``orders`` from recipes.

    Uses the BoM effective on each sale date. Returns
    ``(status, value, reason, source_model, tokens, refs)``; shared by the KPI
    adapter and the Inventory dashboard so both apply identical rules.
    """
    Line = env["pos.order.line"].sudo()
    # Recipe consumption is based on positive completed sales. A POS refund does
    # not prove ingredients were physically returned to stock, so negative/refund
    # lines must not cancel the estimated preparation/consumption from a sale.
    sale_lines = Line.search([("order_id", "in", orders.ids)], order="order_id, id").filtered(
        lambda line: line.qty > 0 and not line.order_id.is_refund_or_negative()
    )
    if not sale_lines:
        return (
            VALID, 0.0,
            "No positive completed POS sale lines were found in this period. Refund/negative lines are excluded because they do not establish that ingredients were returned to stock.",
            "pos.order.line+mrp.bom", _record_tokens(orders), _refs(orders),
        )
    Bom = env["mrp.bom"].sudo().with_context(active_test=False)
    # Odoo 20 Community has no explicit BoM effective dates (recipe versions are
    # separate BoMs; validity then follows creation/change history) and names the
    # unit field ``uom_id``; older versions use ``product_uom_id``.
    bom_uom_field = "product_uom_id" if "product_uom_id" in Bom._fields else "uom_id"
    BomLine = env["mrp.bom.line"]
    line_uom_field = "product_uom_id" if "product_uom_id" in BomLine._fields else "uom_id"
    if not {"product_tmpl_id", "product_qty", "bom_line_ids", bom_uom_field}.issubset(Bom._fields) or line_uom_field not in BomLine._fields:
        return (
            DATA_QUALITY, None,
            "The installed BoM model does not expose the effective-date and component fields required for historical recipe evaluation.",
            "pos.order.line+mrp.bom", _record_tokens(orders) + _record_tokens(sale_lines), _refs(orders, sale_lines),
        )

    total = 0.0
    used_boms = {}
    data_issues = []
    for line in sale_lines:
        # A POS service line cannot contribute ingredient consumption.
        if not line.product_id or not line.product_id.uom_id:
            data_issues.append("POS line %s has no product/unit." % line.id)
            continue
        sale_date = _sale_date_in_timezone(line.order_id.date_order, company.partner_id.tz)
        if not sale_date:
            data_issues.append("POS order %s has an invalid date." % line.order_id.id)
            continue
        # Combo parent rows summarize the selection; their child rows represent
        # the sold items and must not be counted a second time.
        if "combo_line_ids" in line._fields and line.combo_line_ids:
            continue
        # Free-text custom attributes are not a reliable ingredient mapping.
        if "custom_attribute_value_ids" in line._fields and line.custom_attribute_value_ids:
            data_issues.append("POS line %s contains custom attributes that are not mapped to recipe components." % line.id)
            continue
        bom, issue = _resolve_bom(
            Bom, line.product_id, company, sale_date,
            company.partner_id.tz,
        )
        if issue:
            data_issues.append("POS line %s: %s" % (line.id, issue))
            continue
        if not bom:
            # No BoM exists for this sale product, so it is outside the recipe-
            # mapped set. We do not invent ingredient usage for it.
            continue
        used_boms[bom.id] = bom
        if not bom.product_qty or not math.isfinite(bom.product_qty) or bom.product_qty <= 0:
            data_issues.append("BoM %s has a non-positive or invalid output quantity." % bom.id)
            continue
        try:
            output_qty_in_bom_uom = convert_quantity(line.qty, line.product_id.uom_id, bom[bom_uom_field])
            recipe_multiplier = output_qty_in_bom_uom / bom.product_qty
            for component in bom.bom_line_ids:
                if component.product_id != tracked_product:
                    continue
                # Variant-specific BoM components apply only to matching product
                # variant attribute values.
                if "bom_product_template_attribute_value_ids" in component._fields:
                    required_variant_values = component.bom_product_template_attribute_value_ids
                    sold_variant_values = getattr(
                        line.product_id, "product_template_variant_value_ids", env["product.template.attribute.value"]
                    )
                    if required_variant_values and any(value not in sold_variant_values for value in required_variant_values):
                        continue
                # Unrelated units (a gram recipe line on a product kept in another kind of unit) are refused,
                # never converted 1:1.
                component_qty = convert_quantity(
                    component.product_qty * recipe_multiplier, component[line_uom_field], tracked_product.uom_id)
                total += component_qty
        except (UserError, ValidationError, ZeroDivisionError, TypeError, ValueError) as exc:
            data_issues.append("BoM %s could not be normalized to the tracked product unit: %s" % (bom.id, exc))

    tokens = _record_tokens(orders) + _record_tokens(sale_lines) + _record_tokens(list(used_boms.values()))
    refs = _refs(orders, sale_lines, list(used_boms.values()))
    if data_issues:
        return (
            DATA_QUALITY, None,
            "Recipe consumption is incomplete or ambiguous: %s" % "; ".join(data_issues[:20]),
            "pos.order.line+mrp.bom", tokens, refs,
        )
    if not used_boms:
        return (
            DATA_QUALITY, None,
            "No effective recipe BoMs were found for completed POS sales. The system cannot distinguish non-recipe products from missing recipe mappings, so it will not assume zero ingredient consumption.",
            "pos.order.line+mrp.bom", tokens, refs,
        )
    if not math.isfinite(total):
        return (
            DATA_QUALITY, None,
            "Recipe-based consumption did not produce a finite numeric value.",
            "pos.order.line+mrp.bom", tokens, refs,
        )
    return (
        VALID, float(total),
        "Estimated ingredient consumption based on completed POS sales and the BoM effective on each sale date. This is an estimate, not proof of actual stock consumption.",
        "pos.order.line+mrp.bom", tokens, refs,
    )


def _recipe_problem(bom, product):
    """Why a recipe cannot estimate ingredient use for ``product``, or False when it can."""
    bom_uom = bom.product_uom_id if "product_uom_id" in bom._fields else bom.uom_id
    if not bom.bom_line_ids:
        return "its recipe has no ingredients"
    if not bom.product_qty or not math.isfinite(bom.product_qty) or bom.product_qty <= 0:
        return "its recipe has no valid output quantity"
    if not units_compatible(product.uom_id, bom_uom):
        return "its recipe is in %s, which cannot be converted from %s" % (bom_uom.name, product.uom_id.name)
    for line in bom.bom_line_ids:
        line_uom = line.product_uom_id if "product_uom_id" in line._fields else line.uom_id
        if not line.product_qty or line.product_qty <= 0:
            return "ingredient %s has no quantity" % line.product_id.display_name
        if not units_compatible(line_uom, line.product_id.uom_id):
            return "ingredient %s is entered in %s, which cannot be converted to %s" % (
                line.product_id.display_name, line_uom.name, line.product_id.uom_id.name)
    return False


def recipe_coverage(env, company, orders):
    """Menu items marked 'Needs a recipe' sold in ``orders``, and those that lacked a valid recipe.

    An item is covered only when every one of its sales had a valid recipe in effect on the sale date (the rule
    recipe_consumption uses). Returns ``(sold items, {item: reason}, sale lines, recipes used)``.
    """
    lines = env["pos.order.line"].sudo().search([("order_id", "in", orders.ids)], order="order_id, id").filtered(
        lambda line: line.qty > 0 and not line.order_id.is_refund_or_negative()
        and line.product_id.product_tmpl_id.strategic_recipe_required
        # Combo parent rows summarize the selection; their child rows are the sold items.
        and not ("combo_line_ids" in line._fields and line.combo_line_ids)
    )
    Bom = env["mrp.bom"].sudo().with_context(active_test=False)
    timezone_name = company.partner_id.tz
    missing, boms, checked = {}, Bom.browse(), set()
    for line in lines:
        product = line.product_id
        sale_date = _sale_date_in_timezone(line.order_id.date_order, timezone_name)
        if product in missing or (product.id, sale_date) in checked:
            continue
        checked.add((product.id, sale_date))
        if not sale_date:
            missing[product] = "order %s has an invalid date" % line.order_id.name
            continue
        bom, issue = _resolve_bom(Bom, product, company, sale_date, timezone_name)
        problem = issue or (_recipe_problem(bom, product) if bom else "no recipe on %s" % sale_date)
        if problem:
            missing[product] = problem
        else:
            boms |= bom
    return lines.product_id, missing, lines, boms


def _resolve_bom(Bom, sold_product, company, sale_date, timezone_name):
    """Return the single effective normal BoM, preferring a variant-specific BoM."""
    domain = [
        ("company_id", "in", [False, company.id]),
        # Kits are how POS sales deduct recipe ingredients (pos_mrp); manufacturing
        # BoMs remain valid recipe sources. Both at once is ambiguous and fails closed.
        ("type", "in", ["normal", "phantom"]),
        "|", ("product_id", "=", sold_product.id),
             "&", ("product_id", "=", False), ("product_tmpl_id", "=", sold_product.product_tmpl_id.id),
    ]
    candidates = Bom.search(domain)
    effective = Bom.browse()
    invalid_history = []
    for bom in candidates:
        explicit_start = fields.Date.to_date(bom.date_start) if "date_start" in bom._fields and bom.date_start else False
        explicit_stop = fields.Date.to_date(bom.date_stop) if "date_stop" in bom._fields and bom.date_stop else False
        created_on = _sale_date_in_timezone(bom.create_date, timezone_name) if bom.create_date else False
        effective_start = explicit_start or created_on
        if not effective_start:
            invalid_history.append("BoM %s has no verifiable effective start date." % bom.id)
            continue
        # A future recipe or one already ended before the sale is not a candidate
        # for this sale date and must not poison an otherwise valid historical map.
        if effective_start > sale_date or (explicit_stop and explicit_stop < sale_date):
            continue
        if not bom.active and not explicit_stop:
            invalid_history.append("Archived BoM %s has no effective end date; historical applicability is unknown." % bom.id)
            continue
        if not explicit_stop and bom.write_date:
            last_changed = _sale_date_in_timezone(bom.write_date, timezone_name)
            if last_changed and last_changed > sale_date:
                invalid_history.append(
                    "BoM %s was changed after sale date %s without an effective end date; its historical recipe cannot be reconstructed."
                    % (bom.id, sale_date)
                )
                continue
        effective |= bom
    if invalid_history:
        return Bom.browse(), "; ".join(invalid_history)
    variant = effective.filtered(lambda bom: bom.product_id == sold_product)
    if variant:
        effective = variant
    else:
        effective = effective.filtered(lambda bom: not bom.product_id)
    if len(effective) > 1:
        return Bom.browse(), "more than one effective BoM matches this product on %s." % sale_date
    if effective:
        return effective, False

    # If recipe records exist but none covers the sale date, fail closed instead
    # of silently reusing today's recipe or fabricating a zero consumption value.
    if candidates:
        return Bom.browse(), "no single BoM covers the sale date %s." % sale_date
    return Bom.browse(), False


def _sale_date_in_timezone(date_order, timezone_name):
    if not date_order:
        return False
    try:
        utc_value = pytz.UTC.localize(fields.Datetime.to_datetime(date_order))
        return utc_value.astimezone(pytz.timezone(timezone_name)).date()
    except (pytz.UnknownTimeZoneError, pytz.InvalidTimeError, ValueError, TypeError):
        return False


def _record_tokens(records):
    tokens = []
    for record in records:
        tokens.append({
            "model": record._name,
            "id": record.id,
            "write_date": fields.Datetime.to_string(record.write_date) if "write_date" in record._fields and record.write_date else None,
        })
    return tokens


def _refs(orders=(), lines=(), boms=()):
    parts = []
    if orders:
        parts.append("pos.order:%s" % ",".join(str(record.id) for record in orders[:500]))
    if lines:
        parts.append("pos.order.line:%s" % ",".join(str(record.id) for record in lines[:500]))
    if boms:
        parts.append("mrp.bom:%s" % ",".join(str(record.id) for record in boms[:500]))
    if sum(len(records) for records in (orders, lines, boms)) > 500:
        parts.append("reference_list_truncated=true")
    return "; ".join(parts)


def _result(target, version, status, value, reason, source_model, tokens, refs):
    """Create a deterministic, traceable source payload consumed by the KPI engine."""
    payload = {
        "target_id": target.id,
        "definition_version_id": version.id,
        "calculation_type": version.calculation_type,
        "source_product_id": version.source_product_id.id or False,
        "company_id": target.company_id.id,
        "period_start": str(target.date_start),
        "period_end": str(target.date_end),
        "scope": target.scope,
        "warehouse_id": target.warehouse_id.id or False,
        "status": status,
        "value": round(value, 10) if isinstance(value, (int, float)) and math.isfinite(value) else None,
        "tokens": tokens,
        "reason": reason or False,
    }
    source_key = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "status": status,
        "has_value": value is not None and status == VALID,
        "value": value,
        "unit": version.unit,
        "reason": reason or False,
        "source_model": source_model or False,
        "source_record_ref": refs or False,
        "source_key": source_key,
    }
