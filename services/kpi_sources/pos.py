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


VALID = "valid"
NOT_EVALUATED = "not_evaluated"
DATA_QUALITY = "data_quality_issue"
_SUPPORTED_CALCULATIONS = {
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
        ("state", "in", ["paid", "done"]),
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


def _collect_recipe_consumption(target, version, orders, Line):
    return _result(target, version, *recipe_consumption(target.env, company, orders, version.source_product_id))


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
            output_qty_in_bom_uom = line.product_id.uom_id._compute_quantity(
                line.qty, bom[bom_uom_field]
            )
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
                component_qty = component[line_uom_field]._compute_quantity(
                    component.product_qty * recipe_multiplier,
                    tracked_product.uom_id,
                )
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
