import math
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.tools import float_is_zero


_SOURCE_CALCULATIONS = {
    "pos_sold_quantity": "pos",
    "pos_recipe_consumption_estimate": "pos",
    "purchase_ordered_quantity": "purchase",
    "purchase_received_quantity": "purchase",
    "supplier_on_time_delivery_pct": "purchase",
    "inventory_recorded_waste_quantity": "inventory",
    "inventory_validated_waste_quantity": "inventory",
    "inventory_opening_stock_quantity": "inventory",
    "inventory_closing_stock_quantity": "inventory",
    "inventory_actual_consumption_quantity": "inventory",
    "inventory_transfer_in_quantity": "inventory",
    "inventory_transfer_out_quantity": "inventory",
    "inventory_adjustment_quantity": "inventory",
    "inventory_unexplained_variance_quantity": "inventory",
}


def _validate_calculation_contract(env, source_type, calculation_type, product_id, unit, company_id):
    """Enforce the source type, product and unit contract for automatic quantity KPIs."""
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
        [
            ("higher", "Higher is Better"),
            ("lower", "Lower is Better"),
            ("range", "Target Range"),
        ],
        required=True,
        default="higher",
        tracking=True,
    )
    source_type = fields.Selection(
        [
            ("manual", "Controlled Manual Submission"),
            ("accounting", "Accounting"),
            ("purchase", "Purchase"),
            ("pos", "Point of Sale"),
            ("inventory", "Inventory"),
            ("other", "Other"),
        ],
        required=True,
        default="manual",
        tracking=True,
    )
    description = fields.Text()
    calculation_type = fields.Selection(
        [
            ("manual_value", "Manual Value"),
            ("pos_sold_quantity", "POS Sold Quantity"),
            ("pos_recipe_consumption_estimate", "POS Recipe-Based Estimated Consumption"),
            ("purchase_ordered_quantity", "Confirmed Purchase Order Quantity"),
            ("purchase_received_quantity", "Completed Supplier Receipt Quantity"),
            ("supplier_on_time_delivery_pct", "Supplier On-Time Delivery Percentage"),
            ("inventory_recorded_waste_quantity", "Recorded Waste Quantity"),
            ("inventory_validated_waste_quantity", "Validated Waste Quantity"),
            ("inventory_opening_stock_quantity", "Opening Stock Quantity"),
            ("inventory_closing_stock_quantity", "Closing Stock Quantity"),
            ("inventory_actual_consumption_quantity", "Actual Production Consumption Quantity"),
            ("inventory_transfer_in_quantity", "Inter-Warehouse Transfer In Quantity"),
            ("inventory_transfer_out_quantity", "Inter-Warehouse Transfer Out Quantity"),
            ("inventory_adjustment_quantity", "Net Inventory Adjustment Quantity"),
            ("inventory_unexplained_variance_quantity", "Unexplained Stock-Flow Variance Quantity"),
        ],
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
        help="Product tracked by an operational POS, Purchase, or Inventory quantity calculation; recipe estimates use this as the ingredient.",
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
            "source_product_id", "description",
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
        [("higher", "Higher is Better"), ("lower", "Lower is Better"), ("range", "Target Range")],
        required=True,
    )
    source_type = fields.Selection(
        [
            ("manual", "Controlled Manual Submission"),
            ("accounting", "Accounting"),
            ("purchase", "Purchase"),
            ("pos", "Point of Sale"),
            ("inventory", "Inventory"),
            ("other", "Other"),
        ],
        required=True,
    )
    calculation_type = fields.Selection(
        [
            ("manual_value", "Manual Value"),
            ("pos_sold_quantity", "POS Sold Quantity"),
            ("pos_recipe_consumption_estimate", "POS Recipe-Based Estimated Consumption"),
            ("purchase_ordered_quantity", "Confirmed Purchase Order Quantity"),
            ("purchase_received_quantity", "Completed Supplier Receipt Quantity"),
            ("supplier_on_time_delivery_pct", "Supplier On-Time Delivery Percentage"),
            ("inventory_recorded_waste_quantity", "Recorded Waste Quantity"),
            ("inventory_validated_waste_quantity", "Validated Waste Quantity"),
            ("inventory_opening_stock_quantity", "Opening Stock Quantity"),
            ("inventory_closing_stock_quantity", "Closing Stock Quantity"),
            ("inventory_actual_consumption_quantity", "Actual Production Consumption Quantity"),
            ("inventory_transfer_in_quantity", "Inter-Warehouse Transfer In Quantity"),
            ("inventory_transfer_out_quantity", "Inter-Warehouse Transfer Out Quantity"),
            ("inventory_adjustment_quantity", "Net Inventory Adjustment Quantity"),
            ("inventory_unexplained_variance_quantity", "Unexplained Stock-Flow Variance Quantity"),
        ],
        required=True,
        default="manual_value",
        help="Controlled calculation method frozen as part of this KPI definition version.",
    )
    source_product_id = fields.Many2one(
        "product.product",
        string="Tracked Source Product / Ingredient",
        ondelete="restrict",
        help="Product tracked by an operational POS, Purchase, or Inventory quantity calculation; recipe estimates use this as the ingredient.",
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


class StrategicKpiTarget(models.Model):
    _name = "strategic.kpi.target"
    _description = "Strategic KPI Period Target"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "date_start desc, kpi_id"
    name = fields.Char(compute="_compute_name", store=True)
    kpi_id = fields.Many2one("strategic.kpi.definition", required=True, ondelete="restrict", index=True)
    calculation_type = fields.Selection(related="kpi_id.calculation_type", readonly=True)
    company_id = fields.Many2one(related="kpi_id.company_id", store=True, readonly=True)
    date_start = fields.Date(required=True, index=True)
    date_end = fields.Date(required=True, index=True)
    scope = fields.Selection(
        [
            ("organization", "Organization"),
            ("branch", "Branch"),
            ("user", "Employee"),
        ],
        required=True,
        default="organization",
        index=True,
    )
    warehouse_id = fields.Many2one("stock.warehouse", string="Branch Warehouse", index=True)
    user_id = fields.Many2one("res.users", string="Employee", index=True)
    target_basis = fields.Selection(
        [
            ("absolute", "Absolute Target"),
            ("baseline_relative", "Baseline-relative Target"),
        ],
        required=True,
        default="absolute",
        tracking=True,
        help=(
            "For a baseline-relative target, select an approved baseline. "
            "Target Value remains the final approved numeric threshold; the baseline is retained as its comparison and audit anchor."
        ),
    )
    baseline_id = fields.Many2one(
        "strategic.kpi.baseline",
        string="Approved Baseline",
        ondelete="restrict",
        index=True,
        help="Required for a baseline-relative target. The selected baseline must match KPI, scope, and effective period.",
    )
    target_value = fields.Float(
        help="Final approved numeric target threshold. For baseline-relative targets, this is the resolved threshold, not a delta or percentage change."
    )
    range_min = fields.Float()
    range_max = fields.Float()
    unit = fields.Char(related="kpi_id.unit", readonly=True)
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("approved", "Approved"),
            ("archived", "Archived"),
        ],
        required=True,
        default="draft",
        tracking=True,
    )
    evaluation_ids = fields.One2many("strategic.kpi.evaluation", "target_id")

    @api.depends("kpi_id.name", "date_start", "date_end", "scope", "warehouse_id.name", "user_id.name")
    def _compute_name(self):
        for target in self:
            scope_name = dict(target._fields["scope"].selection).get(target.scope)
            if target.scope == "branch" and target.warehouse_id:
                scope_name = target.warehouse_id.display_name
            elif target.scope == "user" and target.user_id:
                scope_name = target.user_id.display_name
            target.name = "%s: %s (%s to %s)" % (
                target.kpi_id.name or "KPI",
                scope_name or "Scope",
                target.date_start or "",
                target.date_end or "",
            )

    @api.model
    def _resolve_for_period(
        self,
        kpi,
        date_start,
        date_end,
        scope=None,
        warehouse_id=None,
        user_id=None,
        required=True,
        definition_version=None,
        key_result=None,
    ):
        """Resolve exactly one approved target for a KPI reporting period.

        Generic KPI monitoring resolves by KPI, scope and full period coverage.
        Key Result scoring must pass ``key_result``; in that mode this method
        validates and returns only its explicitly linked target and never falls
        back to a generic KPI target.

        If ``required`` is false, no matching generic target returns an empty
        target recordset. Ambiguous matches always fail closed.
        """
        Kpi = self.env["strategic.kpi.definition"]
        if not getattr(kpi, "_name", None) == "strategic.kpi.definition":
            try:
                kpi = Kpi.browse(int(kpi))
            except (TypeError, ValueError):
                kpi = Kpi.browse()
        else:
            kpi = kpi.with_env(self.env)
        kpi = kpi.exists()
        if len(kpi) != 1:
            raise ValidationError("Select exactly one valid KPI to resolve its target.")

        start = fields.Date.to_date(date_start) if date_start else False
        end = fields.Date.to_date(date_end) if date_end else False
        if not start or not end or start > end:
            raise ValidationError("A valid KPI target reporting period is required.")

        def _record_id(value):
            return getattr(value, "id", value) or False

        KeyResult = self.env["strategic.key.result"]
        key_result_record = False
        if key_result:
            if not getattr(key_result, "_name", None) == "strategic.key.result":
                key_result = KeyResult.browse(_record_id(key_result))
            else:
                key_result = key_result.with_env(self.env)
            key_result_record = key_result.exists()
            if len(key_result_record) != 1:
                raise ValidationError("The supplied Key Result does not exist.")
            key_result_record = key_result_record[0]
            if key_result_record.primary_kpi_id != kpi:
                raise ValidationError(
                    "The Key Result's primary KPI does not match the KPI being resolved."
                )
            # A Key Result owns an explicit target link. Never search generic
            # targets as a fallback when resolving Key Result scoring.
            if scope is None:
                scope = key_result_record.scope
            if warehouse_id is None:
                warehouse_id = key_result_record.warehouse_id.id or False
            if user_id is None:
                user_id = False
            if key_result_record.date_start != start or key_result_record.date_end != end:
                raise ValidationError(
                    "Key Result target resolution must use the exact Key Result reporting period."
                )
            if key_result_record.scope != scope:
                raise ValidationError("The resolved target scope must match the Key Result scope.")
            if key_result_record.warehouse_id.id != _record_id(warehouse_id):
                raise ValidationError("The resolved target branch must match the Key Result branch.")
        else:
            scope = scope or "organization"
            warehouse_id = _record_id(warehouse_id)
            user_id = _record_id(user_id)

        if scope not in {"organization", "branch", "user"}:
            raise ValidationError("Choose a valid target scope.")
        if scope == "branch" and not _record_id(warehouse_id):
            raise ValidationError("Branch target resolution requires a branch warehouse.")
        if scope != "branch" and _record_id(warehouse_id):
            raise ValidationError("Only branch target resolution may specify a warehouse.")
        if scope == "user" and not _record_id(user_id):
            raise ValidationError("Employee target resolution requires an employee.")
        if scope != "user" and _record_id(user_id):
            raise ValidationError("Only employee target resolution may specify an employee.")

        # Resolve the one approved KPI definition contract applicable to the
        # entire requested period. If explicitly supplied, it must be that
        # exact version; definition versions and target periods are independent.
        expected_definition_version = kpi._get_definition_version_for_period(start, end)
        if definition_version:
            if not getattr(definition_version, "_name", None) == "strategic.kpi.definition.version":
                definition_version = self.env["strategic.kpi.definition.version"].browse(
                    _record_id(definition_version)
                )
            else:
                definition_version = definition_version.with_env(self.env)
            if definition_version != expected_definition_version:
                raise ValidationError(
                    "The requested KPI definition version does not cover the full target reporting period."
                )

        def _matches_context(target, require_exact_period=False):
            if not target or target.state != "approved" or target.kpi_id != kpi:
                return False
            if target.scope != scope:
                return False
            if target.warehouse_id.id != _record_id(warehouse_id):
                return False
            if target.user_id.id != _record_id(user_id):
                return False
            if require_exact_period:
                if target.date_start != start or target.date_end != end:
                    return False
            elif not (
                target.date_start and target.date_end
                and target.date_start <= start and target.date_end >= end
            ):
                return False
            # Reject a target that crosses KPI formula-version boundaries. A
            # target is eligible only when its whole effective period resolves
            # to the same approved KPI definition version as this request.
            try:
                target_version = target.kpi_id._get_definition_version_for_period(
                    target.date_start, target.date_end
                )
            except ValidationError:
                return False
            if target_version != expected_definition_version:
                return False
            if target.target_basis == "baseline_relative":
                baseline = target.baseline_id
                if not baseline or baseline.state != "approved":
                    return False
                if baseline.kpi_id != kpi or baseline.scope != scope:
                    return False
                if baseline.warehouse_id.id != _record_id(warehouse_id):
                    return False
                if baseline.user_id.id != _record_id(user_id):
                    return False
                if baseline.effective_from and baseline.effective_from > start:
                    return False
                if baseline.effective_to and baseline.effective_to < end:
                    return False
            elif target.baseline_id:
                # Do not use a malformed absolute target that carries a baseline.
                return False
            return True

        if key_result_record:
            explicit_target = key_result_record.target_id.exists()
            if not explicit_target or not _matches_context(
                explicit_target, require_exact_period=True
            ):
                raise ValidationError(
                    "The Key Result's explicitly linked target is missing, unapproved, or does not match its KPI, version, scope and period."
                )
            return explicit_target

        domain = [
            ("kpi_id", "=", kpi.id),
            ("company_id", "=", kpi.company_id.id),
            ("state", "=", "approved"),
            ("scope", "=", scope),
            ("date_start", "<=", start),
            ("date_end", ">=", end),
        ]
        domain.append(("warehouse_id", "=", _record_id(warehouse_id)))
        domain.append(("user_id", "=", _record_id(user_id)))
        candidates = self.search(domain).filtered(_matches_context)

        if not candidates:
            if not required:
                return self.browse()
            raise ValidationError(
                "No unique approved KPI target covers the full period %s to %s for %s at scope %s. "
                "Create/approve a target for the exact KPI, scope and period; generic targets are not used as Key Result fallbacks."
                % (start, end, kpi.display_name, scope)
            )
        if len(candidates) != 1:
            raise ValidationError(
                "More than one approved KPI target matches %s to %s for %s at scope %s. "
                "Resolve the overlapping target periods; KPI evaluation will not choose one arbitrarily."
                % (start, end, kpi.display_name, scope)
            )
        return candidates[0]

    @api.constrains("date_start", "date_end", "scope", "warehouse_id", "user_id")
    def _check_target_scope_and_dates(self):
        for target in self:
            if target.date_start and target.date_end and target.date_start > target.date_end:
                raise ValidationError("KPI target start date must not be after end date.")
            if target.scope == "branch" and not target.warehouse_id:
                raise ValidationError("Branch KPI targets require a branch warehouse.")
            if target.scope != "branch" and target.warehouse_id:
                raise ValidationError("Only branch KPI targets may set a warehouse.")
            if target.scope == "user" and not target.user_id:
                raise ValidationError("Employee KPI targets require an employee.")
            if target.scope != "user" and target.user_id:
                raise ValidationError("Only employee KPI targets may set an employee.")

    @api.constrains("target_value", "range_min", "range_max", "kpi_id", "date_start", "date_end")
    def _check_target_values(self):
        for target in self:
            version = target.kpi_id._get_definition_version_for_period(
                target.date_start, target.date_end
            )
            numeric_values = (target.target_value, target.range_min, target.range_max)
            if any(not math.isfinite(value) for value in numeric_values):
                raise ValidationError("KPI target values must be finite numbers.")
            if version.direction == "range":
                if target.range_min > target.range_max:
                    raise ValidationError("KPI range minimum must not be greater than maximum.")
            elif target.target_value < 0:
                raise ValidationError("One-sided KPI targets cannot be negative in the first vertical slice.")

    @api.constrains(
        "target_basis", "baseline_id", "kpi_id", "date_start", "date_end",
        "scope", "warehouse_id", "user_id", "state",
    )
    def _check_target_baseline_contract(self):
        for target in self:
            if target.target_basis == "absolute":
                if target.baseline_id:
                    raise ValidationError(
                        "Absolute targets cannot reference a baseline. Select Baseline-relative Target or clear the baseline."
                    )
                continue

            if not target.baseline_id:
                raise ValidationError(
                    "Baseline-relative targets require an explicitly selected approved baseline."
                )
            baseline = target.baseline_id
            if baseline.state != "approved":
                raise ValidationError("A baseline-relative target must reference an approved baseline.")
            if baseline.kpi_id != target.kpi_id:
                raise ValidationError("The selected baseline must belong to the same KPI as the target.")
            if baseline.scope != target.scope:
                raise ValidationError("The selected baseline must use the same scope as the target.")
            if target.scope == "branch" and baseline.warehouse_id != target.warehouse_id:
                raise ValidationError("The selected baseline must belong to the same branch as the target.")
            if target.scope == "user" and baseline.user_id != target.user_id:
                raise ValidationError("The selected baseline must belong to the same employee as the target.")
            if not target.date_start or not target.date_end:
                raise ValidationError("Set the target period before selecting a baseline.")
            if baseline.effective_from and baseline.effective_from > target.date_start:
                raise ValidationError("The selected baseline is not yet effective at the start of the target period.")
            if baseline.effective_to and baseline.effective_to < target.date_end:
                raise ValidationError("The selected baseline does not cover the full target period.")

            target_version = target.kpi_id._get_definition_version_for_period(
                target.date_start, target.date_end
            )
            baseline_version = baseline.definition_version_id
            if (
                baseline_version.unit != target_version.unit
                or baseline_version.direction != target_version.direction
            ):
                raise ValidationError(
                    "The selected baseline KPI unit and performance direction must match the target-period KPI definition version."
                )

    def write(self, vals):
        if vals.get("state") == "archived":
            active_links = self.env["strategic.xmatrix.alignment"].sudo().search_count([
                ("target_id", "in", self.ids),
                ("state", "=", "active"),
            ])
            if active_links:
                raise ValidationError(
                    "Revise or supersede every active X-Matrix alignment that uses this KPI target before archiving it."
                )
        return super().write(vals)

    def action_approve(self):
        for target in self:
            target._check_target_scope_and_dates()
            target._check_target_values()
            target._check_target_baseline_contract()
        self.write({"state": "approved"})

    def action_archive(self):
        self.write({"state": "archived"})

class StrategicKpiEvaluation(models.Model):
    _name = "strategic.kpi.evaluation"
    _description = "Strategic KPI Evaluation History"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "evaluated_at desc, id desc"
    name = fields.Char(compute="_compute_name", store=True)
    kpi_id = fields.Many2one("strategic.kpi.definition", required=True, ondelete="restrict", index=True)
    target_id = fields.Many2one("strategic.kpi.target", ondelete="restrict", index=True)
    definition_version_id = fields.Many2one(
        "strategic.kpi.definition.version", required=False, readonly=True,
        ondelete="restrict", index=True, copy=False,
        string="KPI Definition Version",
    )
    target_basis = fields.Selection(
        [("absolute", "Absolute Target"), ("baseline_relative", "Baseline-relative Target")],
        required=True,
        default="absolute",
        readonly=True,
        copy=False,
    )
    baseline_id = fields.Many2one(
        "strategic.kpi.baseline", readonly=True, ondelete="restrict", index=True, copy=False
    )
    baseline_value = fields.Float(
        readonly=True,
        copy=False,
        help="Snapshot of the immutable baseline value linked to the target used for this evaluation.",
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    date_start = fields.Date(required=True, index=True)
    date_end = fields.Date(required=True, index=True)
    scope = fields.Selection(
        [
            ("organization", "Organization"),
            ("branch", "Branch"),
            ("user", "Employee"),
        ],
        required=True,
        default="organization",
        index=True,
    )
    warehouse_id = fields.Many2one("stock.warehouse", string="Branch Warehouse", index=True)
    user_id = fields.Many2one("res.users", string="Employee", index=True)
    target_value = fields.Float(readonly=True)
    range_min = fields.Float(readonly=True)
    range_max = fields.Float(readonly=True)
    actual_value = fields.Float()
    has_actual_value = fields.Boolean(default=False)
    unit = fields.Char()
    gap = fields.Float(readonly=True)
    attainment = fields.Float(
        readonly=True,
        aggregator="avg",
        help="Attainment percentage; reporting groups average evaluated attainment rather than summing percentages.",
    )
    status = fields.Selection(
        [
            ("valid", "Valid / Evaluated"),
            ("not_evaluated", "Not Evaluated"),
            ("data_quality_issue", "Data Quality Issue"),
            ("pending_review", "Pending Review"),
        ],
        required=True,
        default="not_evaluated",
        tracking=True,
    )
    attainment_status = fields.Selection(
        [
            ("not_evaluated", "Not Evaluated"),
            ("data_quality_issue", "Data Quality Issue"),
            ("pending_review", "Pending Review"),
            ("not_achieved", "Not Achieved"),
            ("almost_met", "Almost Met"),
            ("achieved", "Achieved"),
            ("exceeded", "Exceeded"),
        ],
        required=True,
        default="not_evaluated",
        tracking=True,
    )
    reason = fields.Text(readonly=True)
    source_model = fields.Char()
    source_record_ref = fields.Char(string="Source Record References")
    source_note = fields.Text(
        string="Source Calculation Notes", readonly=True, copy=False,
        help="Explanation or qualification returned by the operational source adapter.",
    )
    source_key = fields.Char(
        string="Source Snapshot Key", index=True, readonly=True, copy=False,
        help="Deterministic fingerprint preventing duplicate evaluations from an unchanged operational source snapshot.",
    )
    evaluated_at = fields.Datetime(default=fields.Datetime.now, required=True, readonly=True)
    evaluated_by_id = fields.Many2one(
        "res.users",
        default=lambda self: self.env.user,
        required=True,
        readonly=True,
    )
    superseded_by_id = fields.Many2one("strategic.kpi.evaluation", readonly=True, copy=False)

    _source_key_unique = models.Constraint(
        "UNIQUE(source_key)",
        "An operational source snapshot can only create one KPI evaluation.",
    )

    @api.depends("kpi_id.name", "date_start", "date_end")
    def _compute_name(self):
        for evaluation in self:
            evaluation.name = "%s evaluation (%s to %s)" % (
                evaluation.kpi_id.name or "KPI",
                evaluation.date_start or "",
                evaluation.date_end or "",
            )

    @api.onchange("target_id", "kpi_id", "actual_value", "has_actual_value")
    def _onchange_target_or_actual(self):
        """Preview target snapshots and attainment before the evaluation is saved."""
        for evaluation in self:
            target = evaluation.target_id
            if target:
                # The target is authoritative for all target/scope snapshot fields.
                evaluation.kpi_id = target.kpi_id
                evaluation.company_id = target.company_id
                evaluation.date_start = target.date_start
                evaluation.date_end = target.date_end
                evaluation.scope = target.scope
                evaluation.warehouse_id = target.warehouse_id
                evaluation.user_id = target.user_id
                evaluation.target_value = target.target_value
                evaluation.range_min = target.range_min
                evaluation.range_max = target.range_max
                evaluation.target_basis = target.target_basis
                evaluation.baseline_id = target.baseline_id
                evaluation.baseline_value = target.baseline_id.baseline_value if target.baseline_id else 0.0
                kpi = target.kpi_id
            else:
                kpi = evaluation.kpi_id
            if not kpi:
                evaluation.gap = 0.0
                evaluation.attainment = 0.0
                evaluation.status = "not_evaluated"
                evaluation.attainment_status = "not_evaluated"
                evaluation.reason = "Select a KPI or target before evaluating."
                continue
            version = False
            if evaluation.date_start and evaluation.date_end:
                try:
                    version = kpi._get_definition_version_for_period(
                        evaluation.date_start, evaluation.date_end
                    )
                except ValidationError as exc:
                    evaluation.gap = 0.0
                    evaluation.attainment = 0.0
                    evaluation.status = "not_evaluated"
                    evaluation.attainment_status = "not_evaluated"
                    evaluation.reason = str(exc)
                    continue
            direction = version.direction if version else kpi.direction
            evaluation.definition_version_id = version
            evaluation.unit = version.unit if version else kpi.unit
            result = self._calculate_result(
                direction=direction,
                target_value=evaluation.target_value or 0.0,
                range_min=evaluation.range_min or 0.0,
                range_max=evaluation.range_max or 0.0,
                actual_value=evaluation.actual_value or 0.0,
                has_actual_value=evaluation.has_actual_value,
            )
            evaluation.gap = result["gap"]
            evaluation.attainment = result["attainment"]
            evaluation.status = result["status"]
            evaluation.attainment_status = result["attainment_status"]
            evaluation.reason = result["reason"]

    @api.model_create_multi
    def create(self, vals_list):
        if any("source_key" in vals or "source_note" in vals for vals in vals_list):
            raise AccessError("Operational source provenance is assigned only by the controlled source adapter workflow.")
        return super().create([self._prepare_evaluation_values(vals) for vals in vals_list])

    def write(self, vals):
        immutable = {
            "kpi_id",
            "target_id",
            "definition_version_id",
            "target_basis",
            "baseline_id",
            "baseline_value",
            "company_id",
            "date_start",
            "date_end",
            "scope",
            "warehouse_id",
            "user_id",
            "target_value",
            "range_min",
            "range_max",
            "actual_value",
            "has_actual_value",
            "unit",
            "gap",
            "attainment",
            "status",
            "attainment_status",
            "reason",
            "source_model",
            "source_record_ref",
            "source_note",
            "source_key",
        }
        if immutable.intersection(vals):
            raise ValidationError("KPI evaluations are historical records. Create a new evaluation or correction instead of editing results.")
        return super().write(vals)

    @api.model
    def _create_from_source_adapter(self, target, version, result):
        """Create an immutable evaluation for one normalized source result.

        This private method is called only after the target action has checked the
        business role and scope. It deliberately bypasses the public create guard
        only for the internal source_key/provenance payload.
        """
        target.ensure_one()
        version.ensure_one()
        source_key = result.get("source_key")
        if not source_key:
            raise ValidationError("The source adapter did not provide an idempotency key.")
        Evaluation = self.sudo()
        existing = Evaluation.search([("source_key", "=", source_key)], limit=1)
        if existing:
            return existing

        is_valid = result.get("status") == "valid" and result.get("has_value", False)
        vals = {
            "target_id": target.id,
            "actual_value": result.get("value") if is_valid else 0.0,
            "has_actual_value": bool(is_valid),
            "source_model": result.get("source_model") or False,
            "source_record_ref": result.get("source_record_ref") or False,
        }
        prepared = Evaluation._prepare_evaluation_values(vals)
        prepared.update({
            "source_key": source_key,
            "source_model": result.get("source_model") or False,
            "source_record_ref": result.get("source_record_ref") or False,
            "source_note": result.get("reason") or False,
        })
        if not is_valid:
            reason = result.get("reason") or "The operational source did not produce a valid measurement."
            status = result.get("status") if result.get("status") in ("not_evaluated", "data_quality_issue") else "data_quality_issue"
            prepared.update({
                "actual_value": 0.0,
                "has_actual_value": False,
                "gap": 0.0,
                "attainment": 0.0,
                "status": status,
                "attainment_status": status,
                "reason": reason,
            })
        # Call the inherited ORM create directly: the public override intentionally
        # rejects a caller-supplied source_key. This method is private (RPC-inaccessible).
        evaluation = super(StrategicKpiEvaluation, Evaluation).create([prepared])
        previous = Evaluation.search([
            ("target_id", "=", target.id),
            ("source_key", "!=", False),
            ("id", "!=", evaluation.id),
            ("superseded_by_id", "=", False),
        ], order="evaluated_at desc, id desc")
        if previous:
            previous[:1].write({"superseded_by_id": evaluation.id})
        return evaluation

    @api.constrains("date_start", "date_end", "scope", "warehouse_id", "user_id")
    def _check_evaluation_scope_and_dates(self):
        for evaluation in self:
            if evaluation.date_start and evaluation.date_end and evaluation.date_start > evaluation.date_end:
                raise ValidationError("KPI evaluation start date must not be after end date.")
            if evaluation.scope == "branch" and not evaluation.warehouse_id:
                raise ValidationError("Branch KPI evaluations require a branch warehouse.")
            if evaluation.scope != "branch" and evaluation.warehouse_id:
                raise ValidationError("Only branch KPI evaluations may set a warehouse.")
            if evaluation.scope == "user" and not evaluation.user_id:
                raise ValidationError("Employee KPI evaluations require an employee.")
            if evaluation.scope != "user" and evaluation.user_id:
                raise ValidationError("Only employee KPI evaluations may set an employee.")

    @api.model
    def _prepare_evaluation_values(self, vals):
        vals = dict(vals)
        target = self.env["strategic.kpi.target"].browse(vals["target_id"]).exists() if vals.get("target_id") else False
        if vals.get("target_id") and not target:
            raise ValidationError("The selected KPI target no longer exists.")
        if target and target.state != "approved":
            raise ValidationError("KPI evaluations require an approved, non-archived target.")
        if target:
            Target = self.env["strategic.kpi.target"]
            # A target linked explicitly from a Key Result must be validated in
            # that Key Result's context. Generic KPI monitoring may legitimately
            # have another target for the same period/scope; that must not make
            # an explicit KR target ambiguous or trigger a generic fallback.
            # Use sudo only to discover the relational owner(s) for validation;
            # this does not grant the caller read access to Key Result records.
            linked_key_results = self.env["strategic.key.result"].sudo().search([
                ("target_id", "=", target.id),
                ("primary_kpi_id", "=", target.kpi_id.id),
            ])
            if linked_key_results:
                if len(linked_key_results) != 1:
                    raise ValidationError(
                        "This KPI target is explicitly linked to more than one Key Result. "
                        "Each Key Result scoring context must have one unambiguous target link."
                    )
                key_result = linked_key_results[0]
                resolved_target = Target._resolve_for_period(
                    target.kpi_id,
                    key_result.date_start,
                    key_result.date_end,
                    scope=key_result.scope,
                    warehouse_id=key_result.warehouse_id.id or False,
                    user_id=False,
                    required=True,
                    key_result=key_result,
                )
            else:
                # Targets not explicitly assigned to a Key Result use the
                # generic KPI-monitoring resolver, which fails closed on
                # missing or overlapping approved matches.
                resolved_target = Target._resolve_for_period(
                    target.kpi_id,
                    target.date_start,
                    target.date_end,
                    scope=target.scope,
                    warehouse_id=target.warehouse_id.id or False,
                    user_id=target.user_id.id or False,
                    required=True,
                )
            if resolved_target != target:
                raise ValidationError(
                    "The selected KPI target is not valid for this KPI reporting context."
                )
        kpi = target.kpi_id if target else self.env["strategic.kpi.definition"].browse(vals["kpi_id"])
        date_start = target.date_start if target else vals.get("date_start")
        date_end = target.date_end if target else vals.get("date_end")
        version = kpi._get_definition_version_for_period(date_start, date_end)
        vals["definition_version_id"] = version.id
        if target:
            # Always snapshot authoritative values from the selected target.
            # The web client can submit default zero/empty values from the form,
            # so setdefault() can preserve those defaults incorrectly.
            vals.update({
                "kpi_id": target.kpi_id.id,
                "company_id": target.company_id.id,
                "date_start": target.date_start,
                "date_end": target.date_end,
                "scope": target.scope,
                "warehouse_id": target.warehouse_id.id or False,
                "user_id": target.user_id.id or False,
                "target_value": target.target_value,
                "range_min": target.range_min,
                "range_max": target.range_max,
                "unit": version.unit,
                "target_basis": target.target_basis,
                "baseline_id": target.baseline_id.id or False,
                "baseline_value": target.baseline_id.baseline_value if target.baseline_id else 0.0,
            })
        else:
            vals.setdefault("unit", version.unit)
            # Baseline references are server-derived snapshots. A caller cannot
            # manufacture a baseline reference on a standalone evaluation.
            vals["target_basis"] = "absolute"
            vals["baseline_id"] = False
            vals["baseline_value"] = 0.0
        vals["unit"] = version.unit
        vals.update(
            self._calculate_result(
                direction=version.direction,
                target_value=vals.get("target_value", 0.0),
                range_min=vals.get("range_min", 0.0),
                range_max=vals.get("range_max", 0.0),
                actual_value=vals.get("actual_value", 0.0),
                has_actual_value=vals.get("has_actual_value", False),
            )
        )
        return vals

    @api.model
    def _calculate_result(self, direction, target_value=0.0, range_min=0.0, range_max=0.0, actual_value=0.0, has_actual_value=False):
        if not has_actual_value:
            return self._not_evaluated("Actual KPI source data is missing.")
        numbers = [target_value, range_min, range_max, actual_value]
        if any(not self._is_finite_number(value) for value in numbers):
            return self._data_quality_issue("KPI calculation received a non-finite numeric value.")
        precision = 6
        if direction == "range":
            if range_min > range_max:
                return self._data_quality_issue("Range minimum is greater than range maximum.")
            in_range = range_min <= actual_value <= range_max
            return {
                "gap": 0.0 if in_range else min(abs(actual_value - range_min), abs(actual_value - range_max)),
                "attainment": 100.0 if in_range else 0.0,
                "status": "valid",
                "attainment_status": "achieved" if in_range else "not_achieved",
                "reason": False,
            }
        if float_is_zero(target_value, precision_digits=precision):
            return self._not_evaluated("KPI target is zero; attainment cannot be divided safely.")
        if target_value < 0:
            return self._data_quality_issue("Negative one-sided KPI targets are not supported by this calculation rule.")
        if direction == "higher":
            attainment = (actual_value / target_value) * 100.0
            gap = actual_value - target_value
        elif direction == "lower":
            if float_is_zero(actual_value, precision_digits=precision):
                attainment = 100.0 if target_value >= 0 else 0.0
            else:
                attainment = (target_value / actual_value) * 100.0
            gap = target_value - actual_value
        else:
            return self._data_quality_issue("Unsupported KPI direction.")
        if not self._is_finite_number(attainment):
            return self._data_quality_issue("KPI attainment calculation did not produce a finite number.")
        return {
            "gap": gap,
            "attainment": attainment,
            "status": "valid",
            "attainment_status": self._attainment_status(attainment),
            "reason": False,
        }

    @api.model
    def _attainment_status(self, attainment):
        if attainment < 80.0:
            return "not_achieved"
        if attainment < 100.0:
            return "almost_met"
        if math.isclose(attainment, 100.0, rel_tol=0.0, abs_tol=0.000001):
            return "achieved"
        return "exceeded"

    @api.model
    def _not_evaluated(self, reason):
        return {
            "gap": 0.0,
            "attainment": 0.0,
            "status": "not_evaluated",
            "attainment_status": "not_evaluated",
            "reason": reason,
        }

    @api.model
    def _data_quality_issue(self, reason):
        return {
            "gap": 0.0,
            "attainment": 0.0,
            "status": "data_quality_issue",
            "attainment_status": "data_quality_issue",
            "reason": reason,
        }

    @staticmethod
    def _is_finite_number(value):
        return isinstance(value, (int, float)) and math.isfinite(value)


class StrategicKpiBaseline(models.Model):
    """Approved, scope-specific KPI baseline with immutable audit provenance."""

    _name = "strategic.kpi.baseline"
    _description = "Strategic KPI Baseline"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "effective_from desc, id desc"

    name = fields.Char(compute="_compute_name", store=True)
    kpi_id = fields.Many2one(
        "strategic.kpi.definition", required=True, ondelete="restrict", index=True
    )
    definition_version_id = fields.Many2one(
        "strategic.kpi.definition.version",
        required=True,
        ondelete="restrict",
        index=True,
        string="KPI Definition Version",
    )
    company_id = fields.Many2one(
        related="kpi_id.company_id", store=True, readonly=True, index=True
    )
    scope = fields.Selection(
        [
            ("organization", "Organization"),
            ("branch", "Branch"),
            ("user", "Employee"),
        ],
        required=True,
        default="organization",
        index=True,
        tracking=True,
    )
    warehouse_id = fields.Many2one(
        "stock.warehouse", string="Branch Warehouse", index=True
    )
    user_id = fields.Many2one("res.users", string="Employee", index=True)
    baseline_value = fields.Float(required=True, tracking=True)
    source_period_start = fields.Date(required=True, index=True)
    source_period_end = fields.Date(required=True, index=True)
    effective_from = fields.Date(required=True, index=True, tracking=True)
    effective_to = fields.Date(readonly=True, index=True, copy=False, tracking=True)
    source_note = fields.Text(required=True, tracking=True)
    change_reason = fields.Text(required=True, tracking=True)
    state = fields.Selection(
        [("draft", "Draft"), ("approved", "Approved")],
        required=True,
        default="draft",
        index=True,
        tracking=True,
        copy=False,
    )
    created_by_id = fields.Many2one(
        "res.users",
        required=True,
        default=lambda self: self.env.user,
        readonly=True,
        copy=False,
    )
    created_at = fields.Datetime(
        required=True, default=fields.Datetime.now, readonly=True, copy=False
    )
    approved_by_id = fields.Many2one(
        "res.users", readonly=True, copy=False
    )
    approved_at = fields.Datetime(readonly=True, copy=False)
    target_ids = fields.One2many("strategic.kpi.target", "baseline_id", string="Linked Targets")

    @api.depends(
        "kpi_id.name",
        "kpi_id.code",
        "scope",
        "warehouse_id.name",
        "user_id.name",
        "effective_from",
    )
    def _compute_name(self):
        for baseline in self:
            scope_label = dict(baseline._fields["scope"].selection).get(
                baseline.scope, "Scope"
            )
            if baseline.scope == "branch" and baseline.warehouse_id:
                scope_label = baseline.warehouse_id.display_name
            elif baseline.scope == "user" and baseline.user_id:
                scope_label = baseline.user_id.display_name
            kpi_label = baseline.kpi_id.code or baseline.kpi_id.name or "KPI"
            baseline.name = "%s baseline — %s — %s" % (
                kpi_label,
                scope_label,
                baseline.effective_from or "Draft",
            )

    @api.onchange("kpi_id", "source_period_start", "source_period_end")
    def _onchange_definition_version_for_source_period(self):
        for baseline in self:
            if not baseline.kpi_id or not baseline.source_period_start or not baseline.source_period_end:
                baseline.definition_version_id = False
                continue
            if baseline.source_period_start > baseline.source_period_end:
                baseline.definition_version_id = False
                continue
            try:
                baseline.definition_version_id = baseline.kpi_id._get_definition_version_for_period(
                    baseline.source_period_start, baseline.source_period_end
                )
            except ValidationError:
                baseline.definition_version_id = False

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and not self.env.user.has_group(
            "strategic_management.group_strategic_head_office_manager"
        ):
            raise AccessError("Only the Head Office Manager can create KPI baselines.")
        protected = {"approved_by_id", "approved_at", "created_by_id", "created_at", "effective_to"}
        normalized = []
        # Read context defaults explicitly: the form can show a readonly
        # KPI/version field populated by onchange even when the web client
        # omits that readonly field from the create payload.
        context_defaults = self.default_get(
            ["kpi_id", "source_period_start", "source_period_end"]
        )
        for values in vals_list:
            values = dict(values)
            if values.get("state", "draft") != "draft":
                raise ValidationError(
                    "KPI baselines must start in Draft and be approved through the approval workflow."
                )
            if protected.intersection(values):
                raise ValidationError("Baseline audit fields are system-controlled.")

            # Always resolve a missing readonly version server-side. This is
            # necessary because an onchange-populated readonly Many2one is not
            # guaranteed to be included in the web client's create payload.
            if not values.get("definition_version_id"):
                kpi_id = values.get("kpi_id") or context_defaults.get("kpi_id")
                source_start = (
                    values.get("source_period_start")
                    or context_defaults.get("source_period_start")
                )
                source_end = (
                    values.get("source_period_end")
                    or context_defaults.get("source_period_end")
                )
                if not kpi_id:
                    raise ValidationError(
                        "Select a KPI before creating its baseline."
                    )
                if not source_start or not source_end:
                    raise ValidationError(
                        "Enter the baseline source-period start and end dates so the KPI definition version can be resolved."
                    )
                kpi = self.env["strategic.kpi.definition"].browse(kpi_id).exists()
                if not kpi:
                    raise ValidationError(
                        "The selected KPI no longer exists; choose a valid KPI."
                    )
                version = kpi._get_definition_version_for_period(
                    source_start, source_end
                )
                values["definition_version_id"] = version.id

            normalized.append(values)
        return super().create(normalized)

    def write(self, vals):
        protected = {
            "kpi_id",
            "definition_version_id",
            "company_id",
            "state",
            "created_by_id",
            "created_at",
            "approved_by_id",
            "approved_at",
            "effective_to",
        }
        if protected.intersection(vals):
            raise ValidationError(
                "Baseline identity, workflow state and audit fields are system-controlled."
            )
        if any(baseline.state == "approved" for baseline in self):
            raise ValidationError(
                "Approved KPI baselines are immutable. Create a new baseline revision instead."
            )
        return super().write(vals)

    def unlink(self):
        if any(baseline.state == "approved" for baseline in self):
            raise ValidationError(
                "Approved KPI baselines cannot be deleted; create a new baseline revision instead."
            )
        return super().unlink()

    @api.constrains(
        "kpi_id",
        "definition_version_id",
        "scope",
        "warehouse_id",
        "user_id",
        "baseline_value",
        "source_period_start",
        "source_period_end",
        "effective_from",
        "effective_to",
    )
    def _check_baseline_contract(self):
        for baseline in self:
            if (
                baseline.source_period_start
                and baseline.source_period_end
                and baseline.source_period_start > baseline.source_period_end
            ):
                raise ValidationError(
                    "KPI baseline source-period start must not be after its end date."
                )
            if baseline.effective_to and baseline.effective_from and baseline.effective_to < baseline.effective_from:
                raise ValidationError("KPI baseline effective end must not be before its effective start.")
            if not math.isfinite(baseline.baseline_value):
                raise ValidationError("KPI baseline value must be a finite number.")

            if baseline.scope == "branch" and not baseline.warehouse_id:
                raise ValidationError("Branch KPI baselines require a branch warehouse.")
            if baseline.scope != "branch" and baseline.warehouse_id:
                raise ValidationError("Only branch KPI baselines may set a warehouse.")
            if baseline.scope == "user" and not baseline.user_id:
                raise ValidationError("Employee KPI baselines require an employee.")
            if baseline.scope != "user" and baseline.user_id:
                raise ValidationError("Only employee KPI baselines may set an employee.")

            if baseline.kpi_id and baseline.definition_version_id:
                version = baseline.definition_version_id
                if version.kpi_id != baseline.kpi_id:
                    raise ValidationError(
                        "The baseline definition version must belong to the selected KPI."
                    )
                if version.state != "approved":
                    raise ValidationError(
                        "A KPI baseline must reference an approved definition version."
                    )
                if baseline.source_period_start and baseline.source_period_end:
                    if (
                        version.effective_from > baseline.source_period_start
                        or (
                            version.effective_to
                            and version.effective_to < baseline.source_period_end
                        )
                    ):
                        raise ValidationError(
                            "The selected KPI definition version must cover the entire baseline source period."
                        )
                if (
                    baseline.warehouse_id
                    and baseline.warehouse_id.company_id != baseline.kpi_id.company_id
                ):
                    raise ValidationError(
                        "The baseline branch warehouse must belong to the KPI company."
                    )
                if (
                    baseline.user_id
                    and baseline.kpi_id.company_id not in baseline.user_id.company_ids
                ):
                    raise ValidationError(
                        "The baseline employee must have access to the KPI company."
                    )

    def action_approve(self):
        if not self.env.su and not self.env.user.has_group(
            "strategic_management.group_strategic_head_office_manager"
        ):
            raise AccessError("Only the Head Office Manager can approve KPI baselines.")

        for baseline in self:
            if baseline.state != "draft":
                raise ValidationError("Only draft KPI baselines can be approved.")
            baseline._check_baseline_contract()

            domain = [
                ("kpi_id", "=", baseline.kpi_id.id),
                ("scope", "=", baseline.scope),
                ("state", "=", "approved"),
            ]
            if baseline.scope == "branch":
                domain.append(("warehouse_id", "=", baseline.warehouse_id.id))
            else:
                domain.append(("warehouse_id", "=", False))
            if baseline.scope == "user":
                domain.append(("user_id", "=", baseline.user_id.id))
            else:
                domain.append(("user_id", "=", False))

            approved_versions = self.sudo().search(
                domain, order="effective_from desc, id desc"
            )
            if approved_versions.filtered(
                lambda other: other.effective_from == baseline.effective_from
            ):
                raise ValidationError(
                    "An approved baseline already exists for this KPI, scope and effective date."
                )
            latest = approved_versions[:1]
            if latest and baseline.effective_from <= latest.effective_from:
                raise ValidationError(
                    "A revised baseline must have a later effective date than the latest approved baseline for this scope."
                )
            if latest:
                linked_targets = self.env["strategic.kpi.target"].sudo().search([
                    ("baseline_id", "=", latest.id),
                    ("state", "=", "approved"),
                    ("date_end", ">=", baseline.effective_from),
                ])
                if linked_targets:
                    raise ValidationError(
                        "An approved target linked to the current baseline extends into the new baseline period. "
                        "Revise or replace that target before approving this baseline revision."
                    )
                super(StrategicKpiBaseline, latest).write({
                    "effective_to": baseline.effective_from - timedelta(days=1),
                })

            # Use the model implementation directly for this controlled workflow
            # transition, while ordinary write() continues to protect audit fields.
            super(StrategicKpiBaseline, baseline).write(
                {
                    "state": "approved",
                    "approved_by_id": self.env.user.id,
                    "approved_at": fields.Datetime.now(),
                }
            )
        return True
