from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.kpi_sources.common import unit_price_unit


_ACCOUNTING_CALCULATIONS = {
    "accounting_net_profit",
    "accounting_net_profit_margin_pct",
}
# Business-goal measures that are not product quantities: unit rule and "no product" are enforced.
_MONETARY_CALCULATIONS = {"purchase_confirmed_amount", "purchase_outstanding_amount", "accounting_overdue_supplier_bills_amount",
                          "inventory_waste_cost_amount", "pos_net_sales", "pos_average_order_value", "accounting_total_expenses"}
_PERCENT_CALCULATIONS = {"purchase_top_supplier_share_pct", "purchase_on_time_delivery_count_pct", "inventory_waste_cost_to_sales_pct",
                         "pos_sales_growth_pct", "accounting_expense_to_income_pct", "inventory_recipe_coverage_pct",
                         "inventory_count_plan_completion_pct"}
# Amounts in company currency for one tracked product.
_PRODUCT_MONEY_CALCULATIONS = {"inventory_recorded_waste_cost", "pos_product_net_sales", "inventory_closing_stock_value"}
# Prices per unit: company currency per the product's base unit ("Rs/kg"); purchase cost and selling price stay separate.
_UNIT_PRICE_CALCULATIONS = {"purchase_average_unit_price", "pos_average_unit_price"}
_DAYS_CALCULATIONS = {"purchase_average_receipt_lead_days", "purchase_average_delivery_delay_days"}

REPORTING_FREQUENCIES = [("daily", "Daily"), ("weekly", "Weekly"), ("monthly", "Monthly"), ("quarterly", "Quarterly"), ("yearly", "Yearly")]
# Sales measures the POS collector can restrict to hours of the day.
TIME_WINDOW_CALCULATIONS = {"pos_net_sales", "pos_average_order_value"}
_SOURCE_CALCULATIONS = {
    "accounting_net_profit": "accounting",
    "accounting_net_profit_margin_pct": "accounting",
    "pos_sold_quantity": "pos",
    "pos_recipe_consumption_estimate": "pos",
    "purchase_ordered_quantity": "purchase",
    "purchase_received_quantity": "purchase",
    "supplier_on_time_delivery_pct": "purchase",
    "purchase_confirmed_amount": "purchase",
    "purchase_product_quantity_limit": "purchase",
    "purchase_unit_price_change_pct": "purchase",
    "purchase_top_supplier_share_pct": "purchase",
    "purchase_outstanding_amount": "purchase",
    "purchase_average_receipt_lead_days": "purchase",
    "purchase_on_time_delivery_count_pct": "purchase",
    "purchase_average_delivery_delay_days": "purchase",
    "accounting_overdue_supplier_bills_amount": "accounting",
    "inventory_recorded_waste_quantity": "inventory",
    "inventory_validated_waste_quantity": "inventory",
    "inventory_opening_stock_quantity": "inventory",
    "inventory_closing_stock_quantity": "inventory",
    "inventory_actual_consumption_quantity": "inventory",
    "inventory_transfer_in_quantity": "inventory",
    "inventory_transfer_out_quantity": "inventory",
    "inventory_adjustment_quantity": "inventory",
    "inventory_unexplained_variance_quantity": "inventory",
    "inventory_waste_cost_amount": "inventory",
    "inventory_waste_cost_to_sales_pct": "inventory",
    "inventory_count_difference_quantity": "inventory",
    "inventory_recorded_waste_cost": "inventory",
    "pos_net_sales": "pos",
    "pos_sales_growth_pct": "pos",
    "pos_average_order_value": "pos",
    "pos_product_net_sales": "pos",
    "accounting_total_expenses": "accounting",
    "accounting_expense_to_income_pct": "accounting",
    "purchase_average_unit_price": "purchase",
    "pos_average_unit_price": "pos",
    "inventory_closing_stock_value": "inventory",
    "inventory_recipe_coverage_pct": "inventory",
    "inventory_count_plan_completion_pct": "inventory",
    "inventory_waste_recording_pct": "inventory",
    "inventory_ingredient_use_difference_quantity": "inventory",
}


def _validate_calculation_contract(env, source_type, calculation_type, product_id, unit, company_id):
    """Enforce the source type, tracked-product, and unit contract for automatic KPIs."""
    expected_source = _SOURCE_CALCULATIONS.get(calculation_type)
    if not expected_source:
        return
    if source_type != expected_source:
        raise ValidationError(
            "Calculation type %s requires source type %s." % (calculation_type, expected_source)
        )
    if calculation_type == "supplier_on_time_delivery_pct":
        if (unit or "").strip() != "%":
            raise ValidationError("Supplier On-Time Delivery must use the % unit.")
        if product_id:
            raise ValidationError("Supplier On-Time Delivery is calculated across eligible purchase lines and must not select one tracked product.")
        return
    if calculation_type in _PERCENT_CALCULATIONS or calculation_type in _DAYS_CALCULATIONS:
        expected = "%" if calculation_type in _PERCENT_CALCULATIONS else "days"
        if product_id:
            raise ValidationError("This measure covers all products and must not select a tracked source product.")
        if (unit or "").strip() != expected:
            raise ValidationError("This measure must use the '%s' unit." % expected)
        return
    if calculation_type == "purchase_unit_price_change_pct":
        if not product_id:
            raise ValidationError("Select the product whose purchase price is compared.")
        if (unit or "").strip() != "%":
            raise ValidationError("A price change is measured in '%'.")
        return
    if calculation_type == "inventory_waste_recording_pct":
        if not product_id:
            raise ValidationError("Select the product whose waste recording is checked.")
        if (unit or "").strip() != "%":
            raise ValidationError("Waste recording is measured in '%'.")
        return
    if calculation_type in _UNIT_PRICE_CALCULATIONS:
        product = env["product.product"].browse(product_id).exists() if product_id else env["product.product"]
        if not product:
            raise ValidationError("Select the product whose price per unit is measured.")
        company = env["res.company"].browse(company_id or env.company.id).exists()
        if company and product.company_id and product.company_id != company:
            raise ValidationError("The tracked product must belong to the KPI company or be shared across companies.")
        expected = unit_price_unit(company, product)
        if (unit or "").strip() != expected:
            raise ValidationError("A price per unit must use the unit %s (currency per the product's unit)." % expected)
        return
    if calculation_type in _PRODUCT_MONEY_CALCULATIONS:
        # Money for one product: the product is required, the unit is the company currency, never its quantity unit.
        product = env["product.product"].browse(product_id).exists() if product_id else env["product.product"]
        if not product:
            raise ValidationError("Select the product for this measure.")
        company = env["res.company"].browse(company_id or env.company.id).exists()
        if company and product.company_id and product.company_id != company:
            raise ValidationError("The tracked product must belong to the KPI company or be shared across companies.")
        currency = company.currency_id if company else False
        accepted_units = {(currency.symbol or "").strip(), (currency.name or "").strip()} - {""} if currency else set()
        if (unit or "").strip() not in accepted_units:
            raise ValidationError("This measure is an amount and must use the company currency unit (%s), not the product's unit."
                                  % (currency.symbol or currency.name if currency else "the company currency"))
        return
    if calculation_type in _ACCOUNTING_CALCULATIONS or calculation_type in _MONETARY_CALCULATIONS:
        if product_id:
            raise ValidationError("Monetary KPIs are measured across products and must not select a tracked source product.")
        if calculation_type == "accounting_net_profit_margin_pct":
            if (unit or "").strip() != "%":
                raise ValidationError("Net Profit Margin must use the % unit.")
        else:
            company = env["res.company"].browse(company_id or env.company.id).exists()
            currency = company.currency_id if company else False
            accepted_units = {
                (currency.symbol or "").strip(),
                (currency.name or "").strip(),
            } if currency else set()
            accepted_units.discard("")
            if (unit or "").strip() not in accepted_units:
                expected = currency.symbol or currency.name if currency else "the company currency"
                raise ValidationError(
                    "Monetary KPIs must use the company currency unit (%s)." % expected
                )
        return
    product = env["product.product"].browse(product_id).exists() if product_id else env["product.product"]
    if not product:
        raise ValidationError("Select the tracked source product or ingredient for this calculation.")
    company = env["res.company"].browse(company_id or env.company.id).exists()
    product_company = getattr(product, "company_id", False)
    if company and product_company and product_company != company:
        raise ValidationError("The tracked product must belong to the KPI company or be shared across companies.")
    base_unit = product.uom_id.name if product.uom_id else ""
    if (unit or "").strip().casefold() != (base_unit or "").strip().casefold():
        raise ValidationError(
            "For quantity-based source calculations, the KPI unit must match the tracked product's base unit (%s)."
            % (base_unit or "missing")
        )


# Shared by a KPI definition and the versions frozen from it: one list, so a new choice reaches both.
KPI_DIRECTIONS = [
    ("higher", "Higher is Better"),
    ("lower", "Lower is Better"),
    ("range", "Target Range"),
]
KPI_SOURCE_TYPES = [
    ("manual", "Controlled Manual Submission"),
    ("accounting", "Accounting"),
    ("purchase", "Purchase"),
    ("pos", "Point of Sale"),
    ("inventory", "Inventory"),
    ("other", "Other"),
]
KPI_CALCULATION_TYPES = [
    ("manual_value", "Manual Value"),
    ("accounting_net_profit", "Net Profit (Posted Accounting)"),
    ("accounting_net_profit_margin_pct", "Net Profit Margin (Posted Accounting)"),
    ("pos_sold_quantity", "POS Sold Quantity"),
    ("pos_recipe_consumption_estimate", "POS Recipe-Based Estimated Consumption"),
    ("purchase_ordered_quantity", "Confirmed Purchase Order Quantity"),
    ("purchase_received_quantity", "Completed Supplier Receipt Quantity"),
    ("supplier_on_time_delivery_pct", "Supplier On-Time Delivery Percentage"),
    ("purchase_confirmed_amount", "Confirmed Purchase Commitments (excluding tax)"),
    ("purchase_product_quantity_limit", "Ordered Quantity of a Product (limit)"),
    ("purchase_unit_price_change_pct", "Unit Price Change vs Previous Period"),
    ("purchase_top_supplier_share_pct", "Largest Supplier Share of Purchasing"),
    ("purchase_outstanding_amount", "Purchase Value Awaiting Delivery"),
    ("purchase_average_receipt_lead_days", "Average Days from Order to Receipt"),
    ("purchase_on_time_delivery_count_pct", "Deliveries Received in Full on Time"),
    ("purchase_average_delivery_delay_days", "Average Delivery Delay (days)"),
    ("accounting_overdue_supplier_bills_amount", "Overdue Supplier Bills"),
    ("inventory_recorded_waste_quantity", "Recorded Waste Quantity"),
    ("inventory_validated_waste_quantity", "Validated Waste Quantity"),
    ("inventory_opening_stock_quantity", "Opening Stock Quantity"),
    ("inventory_closing_stock_quantity", "Closing Stock Quantity"),
    ("inventory_actual_consumption_quantity", "Actual Consumption Quantity (production and sales issues)"),
    ("inventory_transfer_in_quantity", "Inter-Warehouse Transfer In Quantity"),
    ("inventory_transfer_out_quantity", "Inter-Warehouse Transfer Out Quantity"),
    ("inventory_adjustment_quantity", "Net Inventory Adjustment Quantity"),
    ("inventory_unexplained_variance_quantity", "Unexplained Stock-Flow Variance Quantity"),
    ("inventory_waste_cost_amount", "Validated Waste Cost"),
    ("inventory_waste_cost_to_sales_pct", "Waste Cost as % of Net POS Sales"),
    ("inventory_count_difference_quantity", "Physical Count Difference Quantity"),
    ("inventory_recipe_coverage_pct", "Recipe Coverage % (menu items sold with a valid recipe)"),
    ("inventory_count_plan_completion_pct", "Planned Stock Count Completion %"),
    ("inventory_waste_recording_pct", "Waste Recording Compliance %"),
    ("inventory_ingredient_use_difference_quantity", "Unexplained Ingredient Difference (between stock counts)"),
    ("inventory_recorded_waste_cost", "Recorded Waste Cost of a Product"),
    ("pos_net_sales", "POS Net Sales (excluding tax)"),
    ("pos_sales_growth_pct", "POS Sales Growth vs Previous Period"),
    ("pos_average_order_value", "POS Average Order Value"),
    ("pos_product_net_sales", "POS Net Sales of a Product"),
    ("accounting_total_expenses", "Total Expenses and Direct Costs (Posted)"),
    ("accounting_expense_to_income_pct", "Expenses as % of Income (Posted)"),
    ("purchase_average_unit_price", "Average Purchase Price per Unit"),
    ("pos_average_unit_price", "Average Selling Price per Unit (POS)"),
    ("inventory_closing_stock_value", "Closing Stock Value of a Product"),
]


# Who a KPI figure is for; also used by measurements.
KPI_SCOPES = [
    ("organization", "Organization"),
    ("branch", "Branch"),
    ("user", "Employee"),
]


class StrategicKpiDefinition(models.Model):
    _name = "strategic.kpi.definition"
    _description = "Strategic KPI Definition"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "code, name"
    name = fields.Char(required=True, tracking=True)
    code = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    unit = fields.Char(required=True, default="Unit")
    direction = fields.Selection(
        KPI_DIRECTIONS,
        required=True,
        default="higher",
        tracking=True,
    )
    source_type = fields.Selection(
        KPI_SOURCE_TYPES,
        required=True,
        default="manual",
        tracking=True,
    )
    has_time_window = fields.Boolean("Limited to a Time Window", tracking=True)
    time_window_start = fields.Float("Window From", help="Hours of the day in the company time zone, inclusive.")
    time_window_end = fields.Float("Window To", help="Hours of the day in the company time zone, exclusive.")
    reporting_frequency = fields.Selection(REPORTING_FREQUENCIES, tracking=True, help="How often results are produced, through this KPI's reporting schedules.")
    reporting_schedule_ids = fields.One2many("strategic.kpi.reporting.schedule", "kpi_id")
    description = fields.Text()
    individual_outcome = fields.Boolean(
        "Individual Employee KPI",
        tracking=True,
        help=(
            "On only when one employee owns the outcome and it can be measured for that person alone. "
            "Organization and branch results are never an employee's personal result, even when the employee contributes to them."
        ),
    )
    calculation_type = fields.Selection(
        KPI_CALCULATION_TYPES,
        required=True,
        default="manual_value",
        tracking=True,
        help="Controlled calculation method. Formula/source changes require a new approved KPI definition version.",
    )
    source_product_id = fields.Many2one(
        "product.product",
        string="Tracked Source Product / Ingredient",
        ondelete="restrict",
        tracking=True,
        help=(
            "Product tracked by POS, Purchase, or Inventory quantity calculations. "
            "Leave empty for accounting financial calculations."
        ),
    )
    target_ids = fields.One2many("strategic.kpi.target", "kpi_id")
    evaluation_ids = fields.One2many("strategic.kpi.evaluation", "kpi_id")
    version_ids = fields.One2many(
        "strategic.kpi.definition.version", "kpi_id",
        string="Definition Versions",
    )
    baseline_ids = fields.One2many(
        "strategic.kpi.baseline", "kpi_id", string="Approved Baselines"
    )

    @api.constrains("individual_outcome", "calculation_type")
    def _check_individual_outcome(self):
        for kpi in self.filtered("individual_outcome"):
            if kpi.calculation_type != "manual_value":
                raise ValidationError("%s is calculated from business records, so it cannot be an individual employee KPI." % kpi.display_name)
        for kpi in self.filtered(lambda definition: not definition.individual_outcome):
            if self.env["strategic.kpi.target"].sudo().search_count([("kpi_id", "=", kpi.id), ("scope", "=", "user"), ("state", "=", "approved")], limit=1):
                raise ValidationError("Archive the employee targets of %s before removing its individual ownership." % kpi.display_name)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            calculation_type = vals.get("calculation_type", "manual_value")
            source_type = vals.get("source_type", "manual")
            if calculation_type in _SOURCE_CALCULATIONS:
                _validate_calculation_contract(
                    self.env,
                    source_type,
                    calculation_type,
                    vals.get("source_product_id"),
                    vals.get("unit", "Unit"),
                    vals.get("company_id") or self.env.company.id,
                )
        kpis = super().create(vals_list)
        version_model = self.env["strategic.kpi.definition.version"].sudo().with_context(
            _create_initial_kpi_version=True
        )
        for kpi in kpis:
            if not kpi.sudo().version_ids:
                version_model.create({
                    "kpi_id": kpi.id,
                    "version_number": 1,
                    "unit": kpi.unit,
                    "direction": kpi.direction,
                    "source_type": kpi.source_type,
                    "calculation_type": kpi.calculation_type,
                    "source_product_id": kpi.source_product_id.id or False,
                    "description": kpi.description,
                    "has_time_window": kpi.has_time_window,
                    "time_window_start": kpi.time_window_start,
                    "time_window_end": kpi.time_window_end,
                    # The initial contract is the legacy contract for any
                    # backfilled historical reporting period. Subsequent
                    # revisions use explicit effective dates.
                    "effective_from": "1970-01-01",
                    "change_reason": "Initial KPI definition contract.",
                    "state": "approved",
                    "approved_by_id": self.env.user.id,
                    "approved_at": fields.Datetime.now(),
                })
        return kpis

    def write(self, vals):
        versioned_fields = {
            "unit", "direction", "source_type", "calculation_type",
            "source_product_id", "description", "has_time_window", "time_window_start", "time_window_end",
        }
        if versioned_fields.intersection(vals):
            raise ValidationError(
                "KPI calculation-contract fields are versioned. Create and approve a new definition version instead."
            )
        return super().write(vals)

    def _sync_from_approved_version(self, version):
        """Update legacy display fields without exposing an ORM bypass to RPC callers."""
        self.ensure_one()
        return super(StrategicKpiDefinition, self).write({
            "unit": version.unit,
            "direction": version.direction,
            "source_type": version.source_type,
            "calculation_type": version.calculation_type,
            "source_product_id": version.source_product_id.id or False,
            "description": version.description,
            "has_time_window": version.has_time_window,
            "time_window_start": version.time_window_start,
            "time_window_end": version.time_window_end,
        })

    def _get_definition_version_for_period(self, date_start, date_end):
        """Return the unique approved contract covering the entire evaluation period."""
        self.ensure_one()
        start = fields.Date.to_date(date_start) if date_start else False
        end = fields.Date.to_date(date_end) if date_end else False
        if not start or not end or start > end:
            raise ValidationError("A valid KPI evaluation period is required to resolve its definition version.")

        Version = self.env["strategic.kpi.definition.version"].sudo()
        versions = Version.search([("kpi_id", "=", self.id), ("state", "=", "approved")])
        if not versions:
            # Backfill older KPI records lazily on first use after upgrading.
            # Initial version creation is idempotent and preserves the legacy contract.
            versions = Version.with_context(_create_initial_kpi_version=True).create({
                "kpi_id": self.id,
                "unit": self.unit,
                "direction": self.direction,
                "source_type": self.source_type,
                "calculation_type": self.calculation_type,
                "source_product_id": self.source_product_id.id or False,
                "description": self.description,
                "effective_from": "1970-01-01",
                "change_reason": "Initial KPI definition contract migrated from legacy definition.",
                "state": "approved",
                "approved_by_id": self.sudo().write_uid.id or self.sudo().create_uid.id,
                "approved_at": fields.Datetime.now(),
            })
        matching = versions.filtered(lambda version: (
            version.effective_from and version.effective_from <= start
            and (not version.effective_to or version.effective_to >= end)
        ))
        if len(matching) != 1:
            raise ValidationError(
                "No unique approved KPI definition version covers the full period %s to %s for %s. "
                "Check the effective dates and approved versions."
                % (start, end, self.display_name)
            )
        return matching
    _code_company_unique = models.Constraint(
        "UNIQUE(code, company_id)",
        "KPI code must be unique per company.",
    )


class StrategicKpiDefinitionVersion(models.Model):
    _name = "strategic.kpi.definition.version"
    _description = "Strategic KPI Definition Version"
    _order = "kpi_id, version_number desc"

    name = fields.Char(compute="_compute_name", store=True)
    kpi_id = fields.Many2one(
        "strategic.kpi.definition", required=True, ondelete="restrict", index=True
    )
    company_id = fields.Many2one(related="kpi_id.company_id", store=True, readonly=True)
    version_number = fields.Integer(required=True, readonly=True, copy=False)
    unit = fields.Char(required=True)
    direction = fields.Selection(
        KPI_DIRECTIONS,
        required=True,
    )
    source_type = fields.Selection(
        KPI_SOURCE_TYPES,
        required=True,
    )
    calculation_type = fields.Selection(
        KPI_CALCULATION_TYPES,
        required=True,
        default="manual_value",
        help="Controlled calculation method frozen as part of this KPI definition version.",
    )
    source_product_id = fields.Many2one(
        "product.product",
        string="Tracked Source Product / Ingredient",
        ondelete="restrict",
        help=(
            "Product tracked by POS, Purchase, or Inventory quantity calculations. "
            "Leave empty for accounting financial calculations."
        ),
    )
    description = fields.Text()
    effective_from = fields.Date(required=True, index=True)
    effective_to = fields.Date(readonly=True, index=True)
    state = fields.Selection(
        [("draft", "Draft"), ("approved", "Approved")],
        required=True,
        default="draft",
        index=True,
    )
    change_reason = fields.Text(required=True)
    created_by_id = fields.Many2one(
        "res.users", required=True, default=lambda self: self.env.user, readonly=True
    )
    created_at = fields.Datetime(required=True, default=fields.Datetime.now, readonly=True)
    approved_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    approved_at = fields.Datetime(readonly=True, copy=False)
    evaluation_ids = fields.One2many(
        "strategic.kpi.evaluation", "definition_version_id", string="Evaluations"
    )

    _version_number_unique = models.Constraint(
        "UNIQUE(kpi_id, version_number)",
        "KPI definition version numbers must be unique per KPI.",
    )

    @api.depends("kpi_id.name", "kpi_id.code", "version_number")
    def _compute_name(self):
        for version in self:
            label = version.kpi_id.code or version.kpi_id.name or "KPI"
            version.name = "%s — Version %s" % (label, version.version_number or "new")

    has_time_window = fields.Boolean("Limited to a Time Window")
    time_window_start = fields.Float("Window From", help="Hours of the day in the company time zone, inclusive.")
    time_window_end = fields.Float("Window To", help="Hours of the day in the company time zone, exclusive.")

    @api.constrains("has_time_window", "time_window_start", "time_window_end", "calculation_type")
    def _check_time_window(self):
        for version in self.filtered("has_time_window"):
            if version.calculation_type not in TIME_WINDOW_CALCULATIONS:
                raise ValidationError("A time window applies only to POS net sales and POS average order value.")
            if not 0 <= version.time_window_start < version.time_window_end <= 24:
                raise ValidationError("The time window must start before it ends, within one day.")

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = [dict(vals) for vals in vals_list]
        initial_creation = self.env.su and self.env.context.get("_create_initial_kpi_version")
        next_number_by_kpi = {}

        # This form's KPI field is readonly. The web client may omit it from
        # the create payload even though default_kpi_id populated the form.
        context_defaults = self.default_get(["kpi_id"])
        for vals in vals_list:
            kpi_id = vals.get("kpi_id") or context_defaults.get("kpi_id")
            if not kpi_id:
                raise ValidationError("A KPI definition version must reference a KPI.")
            kpi = self.env["strategic.kpi.definition"].browse(kpi_id).exists()
            if not kpi:
                raise ValidationError("Select a valid KPI before creating its definition version.")
            kpi_id = kpi.id
            vals["kpi_id"] = kpi_id

            if vals.get("state") == "approved" and not initial_creation:
                raise ValidationError("KPI definition versions must be approved through the approval workflow.")
            if not initial_creation and not vals.get("change_reason"):
                raise ValidationError("Provide a reason for the KPI definition revision.")
            if kpi_id not in next_number_by_kpi:
                existing = self.sudo().search([("kpi_id", "=", kpi_id)])
                next_number_by_kpi[kpi_id] = max(existing.mapped("version_number") or [0]) + 1
            vals["version_number"] = next_number_by_kpi[kpi_id]
            next_number_by_kpi[kpi_id] += 1
            if initial_creation:
                vals["state"] = "approved"
        return super().create(vals_list)

    def write(self, vals):
        immutable_metadata = {
            "kpi_id", "version_number", "company_id", "effective_to",
            "created_by_id", "created_at", "approved_by_id", "approved_at",
        }
        if immutable_metadata.intersection(vals):
            raise ValidationError("KPI version identity, timeline and audit metadata are system-controlled.")
        if "state" in vals:
            raise ValidationError("Use the KPI definition-version approval workflow to change its state.")
        if any(version.state == "approved" for version in self):
            raise ValidationError("Approved KPI definition versions are immutable. Create a new revision.")
        return super().write(vals)

    def unlink(self):
        if any(version.state == "approved" or version.evaluation_ids for version in self):
            raise ValidationError("Approved or referenced KPI definition versions cannot be deleted.")
        return super().unlink()

    def action_approve(self):
        if not self.env.su and not self.env.user.has_group(
            "strategic_management.group_strategic_head_office_manager"
        ):
            raise AccessError("Only the Head Office Manager can approve KPI definition versions.")

        for version in self:
            if version.state != "draft":
                raise ValidationError("Only draft KPI definition versions can be approved.")
            if not version.effective_from:
                raise ValidationError("Set an effective date before approving the KPI definition version.")
            if version.calculation_type in _SOURCE_CALCULATIONS:
                _validate_calculation_contract(
                    self.env,
                    version.source_type,
                    version.calculation_type,
                    version.source_product_id.id or False,
                    version.unit,
                    version.kpi_id.company_id.id,
                )
            elif version.calculation_type == "manual_value" and version.source_type == "pos":
                # Existing POS definitions remain readable but cannot be used as an automatic source
                # until their calculation contract is explicitly versioned and approved.
                pass

            approved = self.sudo().search(
                [("kpi_id", "=", version.kpi_id.id), ("state", "=", "approved")],
                order="effective_from asc, version_number asc",
            )
            if approved.filtered(lambda item: item.effective_from == version.effective_from):
                raise ValidationError(
                    "A KPI definition version already exists with this effective start date."
                )
            latest = approved[-1:] if approved else self.browse()
            if latest and version.effective_from <= latest.effective_from:
                raise ValidationError(
                    "A KPI revision must start after the latest approved version. "
                    "Overlapping or backdated revisions are not allowed."
                )

            # End the previous version one day before the new version starts.
            # Use super() to make this controlled timeline update without
            # weakening the normal immutability guard on approved versions.
            if latest:
                previous_end = version.effective_from - timedelta(days=1)
                super(StrategicKpiDefinitionVersion, latest).write({"effective_to": previous_end})

            super(StrategicKpiDefinitionVersion, version).write({
                "state": "approved",
                "approved_by_id": self.env.user.id,
                "approved_at": fields.Datetime.now(),
            })
            version.kpi_id._sync_from_approved_version(version)
        return True
