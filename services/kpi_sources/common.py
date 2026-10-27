"""Common helpers for controlled operational KPI source adapters."""

from datetime import datetime, time, timedelta
import hashlib
import json
import math

import pytz

from odoo import fields
from odoo.exceptions import ValidationError, UserError

VALID = "valid"
NOT_EVALUATED = "not_evaluated"
DATA_QUALITY = "data_quality_issue"


def reporting_period_utc(target):
    """Return inclusive-start/exclusive-end naive UTC datetimes for a local date period."""
    if not target.company_id or not target.date_start or not target.date_end:
        return None, None, "The KPI target is missing its company or reporting-period boundaries."
    start_date = fields.Date.to_date(target.date_start)
    end_date = fields.Date.to_date(target.date_end)
    if start_date > end_date:
        return None, None, "The target reporting period starts after it ends."
    timezone_name = target.company_id.partner_id.tz
    if not timezone_name:
        return None, None, "The company contact has no timezone configured; source date boundaries cannot be evaluated safely."
    try:
        company_tz = pytz.timezone(timezone_name)
        local_start = company_tz.localize(datetime.combine(start_date, time.min), is_dst=None)
        local_end = company_tz.localize(datetime.combine(end_date + timedelta(days=1), time.min), is_dst=None)
        return (
            local_start.astimezone(pytz.UTC).replace(tzinfo=None),
            local_end.astimezone(pytz.UTC).replace(tzinfo=None),
            False,
        )
    except (pytz.UnknownTimeZoneError, pytz.InvalidTimeError, ValueError, TypeError) as exc:
        return None, None, "The company timezone or period boundary is invalid: %s" % exc


def validate_product_contract(target, version, expected_source_type):
    """Validate a product-quantity source contract without silently converting KPI units."""
    if version.source_type != expected_source_type:
        return "Calculation type %s requires source type %s." % (
            version.calculation_type, expected_source_type
        )
    product = version.source_product_id
    if not product:
        return "The approved KPI definition version has no tracked source product."
    if not product.uom_id or (version.unit or "").strip().casefold() != (product.uom_id.name or "").strip().casefold():
        return "The KPI version unit must match the tracked product's base unit (%s)." % (
            product.uom_id.name if product.uom_id else "missing"
        )
    product_company = getattr(product, "company_id", False)
    if product_company and product_company != target.company_id:
        return "The tracked product must belong to the KPI company or be shared across companies."
    return False


def validate_scope(target):
    if target.scope not in ("organization", "branch"):
        return "Operational source evaluation supports organization or branch targets, not employee-level targets."
    if target.scope == "branch" and not target.warehouse_id:
        return "A branch-scoped operational target must identify its branch warehouse."
    return False


def record_tokens(*recordsets):
    tokens = []
    for records in recordsets:
        for record in records:
            tokens.append({
                "model": record._name,
                "id": record.id,
                "write_date": fields.Datetime.to_string(record.write_date)
                if "write_date" in record._fields and record.write_date else None,
                "create_date": fields.Datetime.to_string(record.create_date)
                if "create_date" in record._fields and record.create_date else None,
            })
    return sorted(tokens, key=lambda token: (token["model"], token["id"]))


def record_refs(*recordsets):
    parts = []
    count = 0
    for records in recordsets:
        if not records:
            continue
        count += len(records)
        parts.append("%s:%s" % (
            records._name,
            ",".join(str(record_id) for record_id in records.ids[:500]),
        ))
    if count > 500:
        parts.append("reference_list_truncated=true")
    return "; ".join(parts)


def source_result(target, version, status, value, reason, source_model, *recordsets):
    """Build the standard normalized payload and deterministic source snapshot key."""
    finite_value = isinstance(value, (int, float)) and math.isfinite(value)
    normalized_value = round(float(value), 10) if finite_value else None
    tokens = record_tokens(*recordsets)
    refs = record_refs(*recordsets)
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
        "value": normalized_value,
        "tokens": tokens,
        "reason": reason or False,
    }
    source_key = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "status": status,
        "has_value": normalized_value is not None and status == VALID,
        "value": normalized_value,
        "unit": version.unit,
        "reason": reason or False,
        "source_model": source_model or False,
        "source_record_ref": refs or False,
        "source_key": source_key,
    }


def unit_price_unit(company, product):
    """Unit of a price per unit: company currency per the product's base unit, e.g. "Rs/kg"."""
    currency = company.currency_id
    return "%s/%s" % (currency.symbol or currency.name or "", product.uom_id.name or "")


def validate_unit_price_contract(target, version, expected_source_type):
    """A price per unit needs its product and the "<currency>/<product unit>" unit; returns a problem or False."""
    if version.source_type != expected_source_type:
        return "Calculation type %s requires source type %s." % (version.calculation_type, expected_source_type)
    product = version.source_product_id
    if not product or not product.uom_id:
        return "Choose the product whose price per unit is measured."
    if product.company_id and product.company_id != target.company_id:
        return "The tracked product must belong to the KPI company or be shared across companies."
    expected = unit_price_unit(target.company_id, product)
    if (version.unit or "").strip() != expected:
        return "A price per unit must use the unit %s." % expected
    return False


def units_compatible(from_uom, to_uom):
    """True when Odoo can convert between the units: the same unit, or units sharing a reference unit (g and kg).

    Odoo 20 has no unit categories and would otherwise convert unrelated units (L and kg) 1:1.
    """
    if from_uom == to_uom:
        return True
    return not hasattr(from_uom, "_has_common_reference") or from_uom._has_common_reference(to_uom)


def convert_quantity(quantity, from_uom, to_uom):
    """Convert between compatible units with Odoo's rules; raise ValidationError for unrelated units."""
    if not from_uom or not to_uom:
        raise ValidationError("A quantity is missing its unit of measure.")
    if not units_compatible(from_uom, to_uom):
        raise ValidationError("%s cannot be converted to %s: they measure different things and no conversion is defined."
                              % (from_uom.name, to_uom.name))
    try:
        converted = from_uom._compute_quantity(quantity, to_uom)
    except (UserError, ValidationError, ZeroDivisionError, TypeError, ValueError) as exc:
        raise ValidationError("%s could not be converted to %s: %s" % (from_uom.name, to_uom.name, exc))
    if not math.isfinite(converted):
        raise ValidationError("Unit conversion produced a non-finite value.")
    return converted


def quantity_to_product_uom(quantity, uom, product):
    if not uom or not product or not product.uom_id:
        raise ValidationError("Source quantity is missing its unit of measure or tracked product.")
    # Odoo 20 has no unit categories and would convert e.g. litres into kilograms 1:1; only units sharing a
    # reference unit (g and kg, mL and L) are converted.
    if not units_compatible(uom, product.uom_id):
        raise ValidationError("%s cannot be converted to %s: they measure different things and no conversion is defined."
                              % (uom.name, product.uom_id.name))
    try:
        converted = uom._compute_quantity(quantity, product.uom_id)
    except (UserError, ValidationError, ZeroDivisionError, TypeError, ValueError) as exc:
        raise ValidationError("Source quantity could not be converted to the tracked product unit: %s" % exc)
    if not math.isfinite(converted):
        raise ValidationError("Source quantity conversion produced a non-finite value.")
    return converted
