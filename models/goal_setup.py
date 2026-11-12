"""Business Goals: owner-facing setup that drives the existing objective -> OKR -> KPI chain.

A business goal is a normal ``strategic.objective`` (draft until approved). This module
only stores the owner's setup choices and walks the existing governed steps:
measure -> readiness -> draft target -> target approval -> OKR/Key Result -> submit.
It never activates an objective itself and never invents measurement values.
"""

from datetime import timedelta
from types import SimpleNamespace
import json

import pytz

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import format_amount

from .goal_templates import GOAL_TYPES, TEMPLATE_AREAS, TEMPLATE_BY_KEY, TEMPLATES, measure_bar, target_bounds
from .kpi import _validate_calculation_contract
from .kpi_source_adapter import source_collector_for
from ..services.kpi_sources.accounting import overdue_supplier_bills
from ..services.kpi_sources.common import reporting_period_utc, unit_price_unit
from ..services.kpi_sources.inventory import valued_waste, waste_move_cost
from ..services.kpi_sources.purchase import confirmed_purchase_commitments, scope_warehouses
from ..services.kpi_sources.purchase_goals import outstanding_purchases, supplier_shares

_GROUP_HEAD = "strategic_management.group_strategic_head_office_manager"

BUSINESS_AREAS = [
    ("sales_finance", "Sales & Finance"),
    ("inventory_waste", "Inventory & Waste"),
    ("purchasing", "Purchasing"),
    ("people", "People"),
    ("customers", "Customers"),
    ("operations", "Operations"),
]
_AREA_LABELS = dict(BUSINESS_AREAS)
# Balanced Scorecard perspective for a new goal; perspective is not the business area.
_AREA_PERSPECTIVE = {
    "sales_finance": "financial",
    "inventory_waste": "internal_process",
    "purchasing": "internal_process",
    "people": "learning_growth",
    "customers": "customer",
    "operations": "internal_process",
}
# A measured goal's area is derived from its Key Result KPIs' source type.
_SOURCE_AREA = {"pos": "sales_finance", "accounting": "sales_finance", "purchase": "purchasing", "inventory": "inventory_waste"}

# Measures the owner can choose. Each maps onto an existing calculation with a working collector.
MEASURES = {
    "pos_sold_quantity": {
        "area": "sales_finance", "label": "Quantity sold in POS for one product", "source_type": "pos",
        "needs_product": True, "direction": "higher", "app": "pos",
    },
    "pos_net_sales": {"area": "sales_finance", "label": "Net sales", "source_type": "pos", "unit": "currency", "template_only": True},
    "pos_sales_growth_pct": {"area": "sales_finance", "label": "Sales growth", "source_type": "pos", "unit": "%", "template_only": True},
    "pos_average_order_value": {"area": "sales_finance", "label": "Average order value", "source_type": "pos",
                                "unit": "currency", "template_only": True},
    "pos_product_net_sales": {"area": "sales_finance", "label": "Product sales", "source_type": "pos", "needs_product": True,
                              "unit": "currency", "template_only": True},
    "accounting_net_profit": {"area": "sales_finance", "label": "Net profit", "source_type": "accounting",
                              "unit": "currency", "template_only": True},
    "accounting_total_expenses": {"area": "sales_finance", "label": "Total expenses and direct costs",
                                  "source_type": "accounting", "unit": "currency", "template_only": True},
    "accounting_expense_to_income_pct": {"area": "sales_finance", "label": "Expenses as % of income",
                                         "source_type": "accounting", "unit": "%", "template_only": True},
    "accounting_net_profit_margin_pct": {
        "area": "sales_finance", "label": "Net profit margin % (company-wide)", "source_type": "accounting",
        "needs_product": False, "unit": "%", "direction": "higher", "app": "accounting",
    },
    # Purchasing measures are chosen through goal templates, which set an inclusive range target.
    "purchase_confirmed_amount": {"area": "purchasing", "label": "Confirmed purchasing commitments (excluding tax)",
                                  "source_type": "purchase", "unit": "currency", "template_only": True},
    "purchase_product_quantity_limit": {"area": "purchasing", "label": "Ordered quantity of a product",
                                        "source_type": "purchase", "needs_product": True, "template_only": True},
    "purchase_unit_price_change_pct": {"area": "purchasing", "label": "Unit price change versus the previous period",
                                       "source_type": "purchase", "needs_product": True, "unit": "%", "template_only": True},
    "purchase_top_supplier_share_pct": {"area": "purchasing", "label": "Largest supplier's share of purchasing",
                                        "source_type": "purchase", "unit": "%", "template_only": True},
    "purchase_outstanding_amount": {"area": "purchasing", "label": "Value of orders awaiting delivery",
                                    "source_type": "purchase", "unit": "currency", "template_only": True},
    "purchase_average_receipt_lead_days": {"area": "purchasing", "label": "Average days from order to receipt",
                                           "source_type": "purchase", "unit": "days", "template_only": True},
    "purchase_on_time_delivery_count_pct": {"area": "purchasing", "label": "Deliveries received in full on time",
                                            "source_type": "purchase", "unit": "%", "template_only": True},
    "purchase_average_delivery_delay_days": {"area": "purchasing", "label": "Average delivery delay",
                                             "source_type": "purchase", "unit": "days", "template_only": True},
    "supplier_on_time_delivery_pct": {"area": "purchasing", "label": "Ordered quantity received on time",
                                      "source_type": "purchase", "unit": "%", "template_only": True},
    "purchase_average_unit_price": {"area": "purchasing", "label": "Purchase price per unit", "source_type": "purchase",
                                    "needs_product": True, "unit": "unit_price", "template_only": True},
    "pos_average_unit_price": {"area": "sales_finance", "label": "Selling price per unit", "source_type": "pos",
                               "needs_product": True, "unit": "unit_price", "template_only": True},
    "inventory_closing_stock_value": {"area": "inventory_waste", "label": "Stock value", "source_type": "inventory",
                                      "needs_product": True, "unit": "currency", "template_only": True},
    "inventory_unexplained_variance_quantity": {"area": "inventory_waste", "label": "Unexplained stock difference",
                                                "source_type": "inventory", "needs_product": True, "template_only": True},
    "inventory_validated_waste_quantity": {"area": "inventory_waste", "label": "Validated waste",
                                           "source_type": "inventory", "needs_product": True, "template_only": True},
    "inventory_recorded_waste_cost": {"area": "inventory_waste", "label": "Waste cost", "source_type": "inventory",
                                      "needs_product": True, "unit": "currency", "template_only": True},
    "inventory_waste_cost_amount": {"area": "inventory_waste", "label": "Waste cost (all products)",
                                    "source_type": "inventory", "unit": "currency", "template_only": True},
    "inventory_waste_cost_to_sales_pct": {"area": "inventory_waste", "label": "Waste cost as % of net sales",
                                          "source_type": "inventory", "unit": "%", "template_only": True},
    "inventory_closing_stock_quantity": {"area": "inventory_waste", "label": "Closing stock",
                                         "source_type": "inventory", "needs_product": True, "template_only": True},
    "inventory_count_difference_quantity": {"area": "inventory_waste", "label": "Stock count difference",
                                            "source_type": "inventory", "needs_product": True, "template_only": True},
    "inventory_recipe_coverage_pct": {"area": "inventory_waste", "label": "Recipe coverage", "source_type": "inventory",
                                      "unit": "%", "template_only": True},
    "inventory_count_plan_completion_pct": {"area": "inventory_waste", "label": "Planned stock count completion",
                                            "source_type": "inventory", "unit": "%", "template_only": True},
    "inventory_waste_recording_pct": {"area": "inventory_waste", "label": "Waste recording compliance", "source_type": "inventory",
                                      "needs_product": True, "unit": "%", "template_only": True},
    "inventory_ingredient_use_difference_quantity": {"area": "inventory_waste", "label": "Unexplained ingredient difference",
                                                     "source_type": "inventory", "needs_product": True, "template_only": True},
    # Monitoring metrics (the 'Monitor sales in a time window' option): shown live, no KPI target.
    "pos_window_sales": {"area": "sales_finance", "label": "POS sales revenue", "source_type": "pos", "unit": "currency",
                         "template_only": True, "monitor": "revenue"},
    "pos_window_orders": {"area": "sales_finance", "label": "Number of POS orders", "source_type": "pos",
                          "template_only": True, "monitor": "orders"},
    "pos_window_quantity": {"area": "sales_finance", "label": "Quantity sold", "source_type": "pos",
                            "template_only": True, "monitor": "quantity"},
    "pos_window_average_order_value": {"area": "sales_finance", "label": "Average order value", "source_type": "pos",
                                       "unit": "currency", "template_only": True, "monitor": "aov"},
    # Monitoring metrics (the 'Monitor purchasing' option): shown live, no KPI target.
    "purchase_mon_value": {"area": "purchasing", "label": "Confirmed purchase-order value", "source_type": "purchase",
                           "unit": "currency", "template_only": True, "monitor": "value"},
    "purchase_mon_ordered": {"area": "purchasing", "label": "Quantity ordered", "source_type": "purchase",
                             "template_only": True, "monitor": "ordered"},
    "purchase_mon_received": {"area": "purchasing", "label": "Quantity received", "source_type": "purchase",
                              "template_only": True, "monitor": "received"},
    "purchase_mon_outstanding": {"area": "purchasing", "label": "Orders awaiting delivery", "source_type": "purchase",
                                 "template_only": True, "monitor": "outstanding"},
    "purchase_mon_overdue": {"area": "purchasing", "label": "Overdue purchase orders", "source_type": "purchase",
                             "template_only": True, "monitor": "overdue"},
    "purchase_mon_unit_price": {"area": "purchasing", "label": "Purchase price per product", "source_type": "purchase",
                                "template_only": True, "monitor": "unit_price"},
    "purchase_mon_on_time": {"area": "purchasing", "label": "Supplier on-time delivery rate", "source_type": "purchase",
                             "template_only": True, "monitor": "on_time"},
    "purchase_mon_delay": {"area": "purchasing", "label": "Average supplier delivery delay", "source_type": "purchase",
                           "template_only": True, "monitor": "delay"},
    "purchase_mon_price_comparison": {"area": "purchasing", "label": "Supplier price comparison", "source_type": "purchase",
                                      "template_only": True, "monitor": "price_comparison"},
    # Monitoring measures (the 'Monitor inventory' option): Odoo's current stock, no KPI target.
    "inv_mon_on_hand": {"area": "inventory_waste", "label": "Current stock (on hand)", "source_type": "inventory",
                        "template_only": True, "monitor": "on_hand"},
    "inv_mon_available": {"area": "inventory_waste", "label": "Available stock", "source_type": "inventory",
                          "template_only": True, "monitor": "available"},
    "inv_mon_forecast": {"area": "inventory_waste", "label": "Forecast stock", "source_type": "inventory",
                         "template_only": True, "monitor": "forecast"},
    "inv_mon_below_minimum": {"area": "inventory_waste", "label": "Products below minimum stock", "source_type": "inventory",
                              "template_only": True, "monitor": "below_minimum"},
    "inv_mon_negative": {"area": "inventory_waste", "label": "Negative stock", "source_type": "inventory",
                         "template_only": True, "monitor": "negative"},
    "inv_mon_pos_reserved": {"area": "inventory_waste", "label": "Stock held by unfinished POS deliveries", "source_type": "inventory",
                             "template_only": True, "monitor": "pos_reserved"},
    "accounting_overdue_supplier_bills_amount": {"area": "purchasing", "label": "Overdue supplier bills",
                                                 "source_type": "accounting", "unit": "currency", "template_only": True},
}
# Plain-language reason shown when an area has no supported measure yet.
_AREA_GAPS = {
    "sales_finance": "Choose one of the Sales & Finance goal options above.",
    "inventory_waste": "Choose one of the Inventory & Waste goal options above.",
    "purchasing": "Choose one of the Purchasing goal options above.",
    "people": "No People measures exist yet.",
    "customers": "No Customer measures exist yet.",
    "operations": "No Operations measures exist yet.",
}

_SETUP_STATES = [
    ("needs_setup", "Needs setup"),
    ("ready", "Ready for target"),
    ("target_pending", "Target awaiting approval"),
    ("measured", "Ready to submit"),
    ("submitted", "Submitted"),
]
_LOCKED_STATES = ("target_pending", "measured", "submitted")
# Monitoring reporting periods: rolling ones end today; custom uses the setup's own dates.
_PERIOD_PRESETS = [("today", "Today"), ("this_week", "This week"), ("this_month", "This month"), ("custom", "Custom dates")]


def _time_label(hours):
    minutes = int(round((hours or 0.0) * 60))
    return "%02d:%02d" % (minutes // 60, minutes % 60)


def _waiting_bar(label):
    """Neutral, unfilled progress placeholder for a configured goal without a valid result: no percentage, never 0%."""
    return {"kind": "waiting", "pct": False, "bar": 0, "tone": "neutral", "margin": False, "label": label}


def _human_date(value):
    """'2027-01-31' -> '31 Jan 2027' for owner-facing messages."""
    return fields.Date.to_date(value).strftime("%d %b %Y") if value else "(no date)"


class _ProbeTarget:
    """Read-only stand-in for a KPI target, so readiness can run a collector without writing records."""

    def __init__(self, env, company, date_start, date_end, scope="organization", warehouse=None):
        self.env = env
        self.id = False
        self.scope = scope
        self.company_id = company
        self.warehouse_id = warehouse if warehouse is not None else env["stock.warehouse"]
        self.date_start = date_start
        self.date_end = date_end


class StrategicObjectiveBusinessArea(models.Model):
    _inherit = "strategic.objective"

    business_area = fields.Selection(
        BUSINESS_AREAS,
        index=True,
        help="Business area chosen when the goal was created. Measured goals also appear in the areas of their Key Result KPIs.",
    )
    goal_setup_ids = fields.One2many("strategic.goal.setup", "objective_id", readonly=True)

    def action_reject(self, reason):
        """A rejected goal goes back to its pre-submit setup step, so its owner can revise it and submit again."""
        result = super().action_reject(reason)
        for setup in self.goal_setup_ids.filtered(lambda setup: setup.setup_state == "submitted"):
            setup.sudo().write({"setup_state": "ready" if setup._is_monitoring() else "measured"})
        return result

    def _review_target_summary(self):
        setup = self.goal_setup_ids[:1]
        return (setup and setup._request_summary()) or super()._review_target_summary()
    goal_type = fields.Selection(
        GOAL_TYPES, compute="_compute_goal_type", store=True, readonly=False, tracking=True,
        help="Monitoring: values and trends, no target. Optimization: a target to reach. Compliance: a rule or required "
             "process is followed. Business Goals sets it from the chosen goal option.",
    )

    @api.depends("goal_setup_ids.template_key")
    def _compute_goal_type(self):
        for objective in self:
            template = TEMPLATE_BY_KEY.get(objective.goal_setup_ids[:1].template_key)
            objective.goal_type = template.get("goal_type", "optimization") if template else (objective.goal_type or "optimization")

    def _goal_areas(self):
        """Stored intake area plus the areas implied by the Key Result KPIs (cross-functional goals list in each)."""
        self.ensure_one()
        areas = [self.business_area] if self.business_area else []
        for kpi in self.okr_objective_ids.key_result_ids.primary_kpi_id:
            area = _SOURCE_AREA.get(kpi.source_type)
            if area and area not in areas:
                areas.append(area)
        return areas


class StrategicGoalSetup(models.Model):
    _name = "strategic.goal.setup"
    _description = "Business Goal Setup"
    _order = "id desc"

    objective_id = fields.Many2one("strategic.objective", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="objective_id.company_id", store=True, readonly=True)
    business_area = fields.Selection(related="objective_id.business_area", store=True, readonly=True)
    desired_result = fields.Char(help="The result in the owner's own words, e.g. 'Sell 20 lattes'.")
    measure_key = fields.Selection([(key, measure["label"]) for key, measure in MEASURES.items()])
    template_key = fields.Char(help="Purchasing goal option (models/goal_templates.py) that set the measure and target rule.")
    product_id = fields.Many2one("product.product")
    target_value = fields.Float()
    period_start = fields.Date()
    period_end = fields.Date()
    kpi_id = fields.Many2one("strategic.kpi.definition", readonly=True)
    target_id = fields.Many2one("strategic.kpi.target", readonly=True)
    okr_id = fields.Many2one("strategic.okr.objective", readonly=True)
    key_result_id = fields.Many2one("strategic.key.result", readonly=True)
    setup_state = fields.Selection(_SETUP_STATES, required=True, default="needs_setup", readonly=True)
    readiness_json = fields.Text(readonly=True)
    # Monitoring settings: the daily time window (company time zone; start included, end excluded), a rolling or
    # custom reporting period, and an optional POS category (the product filter is product_id).
    window_start = fields.Float(help="Start of the daily time window in hours (17.5 = 17:30), included.")
    window_end = fields.Float(help="End of the daily time window in hours, excluded (21.0 stops before 21:00).")
    period_preset = fields.Selection(_PERIOD_PRESETS, help="Monitoring period; 'Custom dates' uses the period dates.")
    pos_category_id = fields.Many2one("pos.category", string="POS category")
    partner_id = fields.Many2one("res.partner", string="Supplier", help="Purchasing monitoring: one supplier (with its contacts).")
    product_category_id = fields.Many2one("product.category", string="Product category",
                                          help="Purchasing monitoring: products of this category and its sub-categories.")

    _objective_unique = models.Constraint("UNIQUE(objective_id)", "A business goal has exactly one setup record.")

    # ------------------------------------------------------------------ helpers

    def _is_head_office(self):
        return self.env.user.has_group(_GROUP_HEAD)

    def _require_head_office(self):
        if not self._is_head_office():
            raise AccessError("Only a Head Office Manager can perform this step.")

    def _company_today(self, company):
        return fields.Date.context_today(self.with_context(tz=company.partner_id.tz or "UTC"))

    @api.model
    def _resolve_plan(self, deadline, plan_id=False):
        Plan = self.env["strategic.plan"]
        domain = [("company_id", "=", self.env.company.id), ("active", "=", True),
                  "|", ("date_end", "=", False), ("date_end", ">=", deadline),
                  "|", ("date_start", "=", False), ("date_start", "<=", deadline)]
        plans = Plan.search(domain, order="date_end, id")
        if plan_id:
            plan = plans.filtered(lambda p: p.id == int(plan_id))
            if not plan:
                raise UserError(
                    "The chosen strategic plan cannot hold this goal: it is archived, or its dates do not include the "
                    "deadline %s. %s" % (_human_date(deadline), self._plan_help())
                )
            return plan
        if not plans:
            # Every objective belongs to a plan (its company comes from the plan), so a draft needs one too.
            raise UserError("Your goal has not been saved yet: no strategic plan covers the deadline %s. %s"
                            % (_human_date(deadline), self._plan_help()))
        if len(plans) > 1:
            raise UserError("More than one active strategic plan covers this deadline. Choose the plan for this goal.")
        return plans

    def _plan_help(self):
        """Plain-language fix for a missing plan: which dates work now, and where Head Office changes plans."""
        latest = self.env["strategic.plan"].search(
            [("company_id", "=", self.env.company.id), ("active", "=", True)], order="date_end desc", limit=1)
        where = "Head Office can extend a plan or add one under Strategy & KPIs > Strategy > Strategic Plans."
        if not latest:
            return "There is no active strategic plan. %s" % where
        return "The current plan '%s' runs until %s. Choose a deadline on or before that date, or: %s" % (
            latest.name, _human_date(latest.date_end), where)

    def _check_plan_still_valid(self):
        """Before approval steps: the goal's plan must still be active and cover its deadline."""
        objective = self.objective_id
        plan = objective.plan_id
        if not plan.active or (plan.date_end and objective.deadline and objective.deadline > plan.date_end) \
                or (plan.date_start and objective.deadline and objective.deadline < plan.date_start):
            raise UserError(
                "This goal cannot go forward: its strategic plan '%s' is archived or no longer covers the deadline %s. "
                "Head Office must extend or reactivate the plan under Strategy & KPIs > Strategy > Strategic Plans."
                % (plan.name, _human_date(objective.deadline))
            )

    def _measure(self):
        return MEASURES.get(self.measure_key) or {}

    def _unit(self):
        measure = self._measure()
        unit = measure.get("unit", "product" if measure.get("needs_product") else "")
        if unit == "product":
            return self.product_id.uom_id.name or ""
        if unit == "currency":
            return self.company_id.currency_id.symbol or self.company_id.currency_id.name or ""
        if unit == "unit_price":
            return unit_price_unit(self.company_id, self.product_id)
        return unit

    def _template(self):
        return TEMPLATE_BY_KEY.get(self.template_key) or {}

    def _rule(self):
        return self._template().get("rule")

    def _is_budget(self):
        return self.template_key == "budget"

    def _direction(self):
        rule = self._rule()
        if rule == "at_least":
            return "higher"
        return "range" if rule else self._measure().get("direction", "higher")

    def _money(self, amount):
        return format_amount(self.env, amount or 0.0, self.company_id.currency_id)

    def _show(self, value):
        """Owner-facing value in the template's kind: money, quantity, percent, change or days."""
        kind = self._template().get("kind")
        if kind == "money":
            return self._money(value)
        if kind == "percent":
            return "%s%%" % round(value, 1)
        if kind == "change":
            return "%s%s%%" % ("+" if value > 0 else "", round(value, 1))
        if kind == "days":
            return "%s days" % round(value, 1)
        if kind == "unit_price":
            return "%s/%s" % (self._money(value), self.product_id.uom_id.name or "")
        return "%g %s" % (round(value, 4), self._unit())

    def _target_label(self, value):
        rule, kind = self._rule(), self._template().get("kind")
        if rule == "ceiling":
            return ("Up to %s" if kind == "money" else "At most %s") % self._show(value)
        if rule == "floor":
            return "At least %s%%" % value
        if rule == "reduce_by":
            return "Price down at least %s%%" % value
        if rule == "rise_at_most":
            return "Price rise no more than %s%%" % value
        if rule == "at_least":
            return "Growth of at least %s%%" % value if kind == "change" else "At least %s" % self._show(value)
        if rule == "tolerance":
            return "Within ±%s" % self._show(value)
        return "%g %s" % (value, self._unit())

    def _request_summary(self):
        """'At most 50 L · Milk purchased · 01 Oct 2026 – 31 Oct 2026': the proposed target in one line."""
        self.ensure_one()
        if not self.measure_key:
            return False
        measure = self._template().get("title") or dict(self._fields["measure_key"].selection).get(self.measure_key)
        target = "No target (monitoring)" if self._is_monitoring() else self._target_label(self.target_value)
        period = "%s – %s" % (_human_date(self.period_start), _human_date(self.period_end)) if self.period_start and self.period_end else False
        return " · ".join(part for part in (target, measure, period) if part)

    def _find_kpi(self):
        """Reuse the lowest-id KPI whose approved version for the period matches the measure exactly."""
        self.ensure_one()
        measure = self._measure()
        candidates = self.env["strategic.kpi.definition"].search([
            ("company_id", "=", self.company_id.id),
            ("calculation_type", "=", self.measure_key),
            ("source_type", "=", measure["source_type"]),
            ("source_product_id", "=", self.product_id.id if measure.get("needs_product") else False),
        ], order="id")
        unit = self._unit().strip().casefold()
        for kpi in candidates:
            try:
                version = kpi._get_definition_version_for_period(self.period_start, self.period_end)
            except ValidationError:
                continue
            if (
                version.state == "approved"
                and version.calculation_type == self.measure_key
                and version.source_type == measure["source_type"]
                and version.source_product_id == (self.product_id if measure.get("needs_product") else kpi.env["product.product"])
                and (version.unit or "").strip().casefold() == unit
                and version.direction == self._direction()
            ):
                return kpi, version
        return self.env["strategic.kpi.definition"], False

    def _create_kpi(self):
        measure = self._measure()
        product = self.product_id if measure.get("needs_product") else self.env["product.product"]
        name = measure["label"] if not product else "%s - %s" % (measure["label"].replace(" for one product", ""), product.display_name)
        ranged = self._direction() == "range"
        if ranged and self._measure().get("direction") not in (None, "range"):
            name += " (limit)"
        code = "BG-%s%s%s" % (self.measure_key.upper(), "-%s" % product.id if product else "", "-R" if ranged else "")
        return self.env["strategic.kpi.definition"].create({
            "name": name,
            "code": code,
            "company_id": self.company_id.id,
            "unit": self._unit(),
            "direction": self._direction(),
            "source_type": measure["source_type"],
            "calculation_type": self.measure_key,
            "source_product_id": product.id or False,
            "description": "Created from Business Goals for '%s'." % self.objective_id.name,
        })


    # ------------------------------------------------------------------ public API (RPC)

    @api.model
    def get_form_options(self, area=False):
        """Choices for the intake and setup forms; measures are limited to supported calculations."""
        Plan = self.env["strategic.plan"]
        plans = Plan.search([("company_id", "=", self.env.company.id), ("active", "=", True)], order="date_end, id")
        users = self.env["res.users"].search([("share", "=", False), ("company_ids", "in", self.env.company.id)], order="name")
        measures = [
            {"key": key, "label": measure["label"], "area": measure["area"], "needs_product": measure.get("needs_product", False),
             "unit": (self.env.company.currency_id.symbol if measure.get("unit") == "currency" else measure.get("unit", ""))}
            for key, measure in MEASURES.items() if (not area or measure["area"] == area) and not measure.get("template_only")
        ]
        templates = [
            {"key": t["key"], "group": t["group"], "title": t["title"], "description": t["description"], "status": t["status"],
             "how": t.get("how", ""), "needs": t.get("needs", ""), "target_label": t.get("target_label", ""), "kind": t.get("kind"),
             "needs_product": MEASURES.get(t.get("measure"), {}).get("needs_product", False), "apps": t["apps"],
             "company_only": t.get("company_only", False), "branch_only": t.get("branch_only", False), "area": t["area"],
             "goal_type": t.get("goal_type", "optimization"), "goal_type_label": dict(GOAL_TYPES)[t.get("goal_type", "optimization")],
             "monitor_source": t.get("monitor_source", False),
             "metrics": [{"key": key, "label": MEASURES[key]["label"]} for key in t.get("metrics", [])]}
            for t in TEMPLATES
        ]
        is_head = self._is_head_office()
        if is_head:
            warehouses = self.env["stock.warehouse"].search([("company_id", "=", self.env.company.id)], order="name, id")
        else:
            warehouses = self.env.user.strategic_branch_warehouse_id
        # The objective model enforces the same eligibility for Branch Managers' proposals.
        company_goals = self.env["strategic.objective"].search([
            ("company_id", "=", self.env.company.id), ("scope", "=", "organization"),
            ("state", "not in", ("closed", "archived")),
        ], order="name, id")
        return {
            "branches": [{"id": warehouse.id, "name": warehouse.display_name} for warehouse in warehouses],
            "currency_symbol": self.env.company.currency_id.symbol or self.env.company.currency_id.name,
            "company_goals": [{"id": goal.id, "name": goal.name} for goal in company_goals],
            "areas": [{"key": key, "label": label} for key, label in BUSINESS_AREAS],
            "plans": [{"id": plan.id, "name": plan.name, "date_end": fields.Date.to_string(plan.date_end) if plan.date_end else False} for plan in plans],
            # Latest deadline any active plan allows; the form warns before saving (the server still decides).
            "latest_deadline": fields.Date.to_string(max(plans.mapped("date_end"))) if plans and all(plans.mapped("date_end")) else False,
            "has_plan": bool(plans),
            "users": [{"id": user.id, "name": user.name} for user in users],
            "measures": measures,
            "templates": templates,
            "area_gaps": _AREA_GAPS,
            "is_head_office": self._is_head_office(),
            "current_user_id": self.env.user.id,
            "period_presets": [{"key": key, "label": label} for key, label in _PERIOD_PRESETS],
            "pos_categories": [{"id": category.id, "name": category.display_name}
                               for category in self.env["pos.category"].sudo().search([], order="name, id")],
            "suppliers": [{"id": partner.id, "name": partner.display_name} for partner in self.env["res.partner"].search(
                [("supplier_rank", ">", 0), ("parent_id", "=", False)], order="name, id", limit=500)],
            "product_categories": sorted(({"id": category.id, "name": category.display_name}
                                          for category in self.env["product.category"].search([])), key=lambda item: item["name"]),
        }

    @api.model
    def search_products(self, query="", limit=20, purchasable=False):
        domain = [("name", "ilike", query or "")]
        if purchasable:
            domain.append(("purchase_ok", "=", True))
        products = self.env["product.product"].search(domain, limit=limit, order="name")
        return [{"id": product.id, "name": product.display_name, "uom": product.uom_id.name or ""} for product in products]

    @api.model
    def create_goal(self, values):
        """Save a goal as a draft objective plus its setup record. Nothing is activated here."""
        name = (values.get("name") or "").strip()
        area = values.get("business_area")
        deadline = values.get("deadline")
        if not name:
            raise UserError("Describe what you want to improve.")
        if area not in _AREA_LABELS:
            raise UserError("Choose a business area for this goal.")
        if not deadline:
            raise UserError("Choose a deadline for this goal.")
        scope = values.get("scope") or "organization"
        objective_values = {
            "name": name,
            "owner_id": int(values.get("owner_id") or self.env.user.id),
            "deadline": deadline,
            "description": (values.get("reason") or "").strip() or False,
            "perspective": _AREA_PERSPECTIVE[area],
            "scope": scope,
            "business_area": area,
        }
        if not self._is_head_office() and not self.env.company.sm_bm_can_propose:
            raise UserError("Head Office has turned off objective proposals from Branch Managers.")
        if scope == "branch":
            # Existing objective rule: a branch objective is aligned under a company objective of the same plan.
            if self._is_head_office():
                warehouse = self.env["stock.warehouse"].browse(int(values.get("warehouse_id") or 0)).exists()
            else:
                # A Branch Manager's proposal is always for their own branch; a branch sent by the client is ignored.
                warehouse = self.env.user.strategic_branch_warehouse_id
                if not warehouse:
                    raise AccessError("Branch objective proposals require an assigned branch warehouse.")
            if not warehouse or warehouse.company_id != self.env.company:
                raise UserError("Choose the branch this goal applies to.")
            parent = self.env["strategic.objective"].browse(int(values.get("parent_objective_id") or 0)).exists()
            if not parent or parent.scope != "organization":
                raise UserError("A branch goal must support a company goal. Choose the company goal it contributes to.")
            if parent.deadline and parent.deadline < fields.Date.to_date(deadline):
                raise UserError("The branch goal's deadline cannot be after its company goal's deadline (%s)." % parent.deadline)
            objective_values.update({"plan_id": parent.plan_id.id, "warehouse_id": warehouse.id, "parent_objective_id": parent.id})
        elif scope == "organization":
            objective_values["plan_id"] = self._resolve_plan(deadline, values.get("plan_id")).id
        else:
            raise UserError("Choose whether the goal applies to the entire business or one branch.")
        objective = self.env["strategic.objective"].create(objective_values)
        setup = self.create({"objective_id": objective.id, "desired_result": (values.get("desired_result") or "").strip() or False})
        return setup._goal_payload(objective)

    def configure_measurement(self, values):
        """Store the owner's measure choice and re-run readiness."""
        self.ensure_one()
        objective = self.objective_id
        if objective.state != "draft" or self.setup_state in _LOCKED_STATES:
            raise UserError("The measurement is locked once a target has been prepared.")
        template_key = False
        # Areas with goal options use them; an area that also has free measures (Sales & Finance) still accepts
        # a measure chosen without an option, so goals set up that way keep working.
        free_measures = any(m["area"] == objective.business_area and not m.get("template_only") for m in MEASURES.values())
        if objective.business_area in TEMPLATE_AREAS and (values.get("template_key") or not free_measures):
            template = TEMPLATE_BY_KEY.get(values.get("template_key"))
            if not template or template["area"] != objective.business_area:
                raise UserError("Choose one of the %s goal options." % _AREA_LABELS[objective.business_area])
            if template["status"] != "available":
                raise UserError("'%s' cannot be measured yet: %s" % (template["title"], template["needs"]))
            if template.get("company_only") and objective.scope != "organization":
                raise UserError("'%s' is measured for the whole company only; create it as an entire-business goal." % template["title"])
            if template.get("branch_only") and objective.scope != "branch":
                raise UserError("'%s' is measured for one branch; create it as a branch goal." % template["title"])
            if template.get("goal_type") == "monitoring":
                return self._configure_monitoring(template, values)
            template_key, measure_key = template["key"], template["measure"]
        else:
            measure_key = values.get("measure_key")
        measure = MEASURES.get(measure_key)
        if not measure or (measure.get("template_only") and not template_key):
            raise UserError("Choose one of the supported measures.")
        if measure["area"] != objective.business_area:
            raise UserError("'%s' belongs to %s, not to this goal's area." % (measure["label"], _AREA_LABELS[measure["area"]]))
        self.write({
            "template_key": template_key,
            "measure_key": measure_key,
            "product_id": int(values["product_id"]) if measure.get("needs_product") and values.get("product_id") else False,
            "target_value": float(values.get("target_value") or 0.0),
            "period_start": values.get("period_start") or False,
            "period_end": values.get("period_end") or False,
        })
        return self.check_readiness()

    def extend_deadline(self, new_deadline):
        """Extend a draft goal's deadline with the existing objective revision (Head Office, reason recorded, plan enforced)."""
        self.ensure_one()
        self._require_head_office()
        objective = self.objective_id
        if objective.state != "draft" or self.setup_state in _LOCKED_STATES:
            raise UserError("The deadline of this goal can no longer be changed here.")
        if not new_deadline or fields.Date.to_date(new_deadline) <= objective.deadline:
            raise UserError("Choose a deadline after the current one (%s)." % _human_date(objective.deadline))
        plan = objective.plan_id
        if plan.date_end and fields.Date.to_date(new_deadline) > plan.date_end:
            raise UserError("The strategic plan '%s' ends on %s, so the deadline cannot be later. %s"
                            % (plan.name, _human_date(plan.date_end), self._plan_help()))
        objective.action_extend_deadline(new_deadline, "Extended in Business Goals to fit the measurement period.")
        return self.check_readiness() if self.measure_key else self._goal_payload(objective)

    def _readiness_checks(self):
        """Return the four plain-language readiness checks: setup, app, formula, data."""
        self.ensure_one()
        checks = []

        def add(key, label, ok, message):
            checks.append({"key": key, "label": label, "ok": bool(ok), "message": message})

        objective = self.objective_id
        measure = self._measure()
        if not measure:
            add("setup", "Measurement chosen", False, _AREA_GAPS.get(objective.business_area) if not any(
                m["area"] == objective.business_area for m in MEASURES.values()) else "Choose how this goal will be measured.")
            return checks
        if self._is_monitoring():
            return self._monitor_readiness()
        problems = []
        if measure.get("needs_product") and not self.product_id:
            problems.append("choose the product")
        rule, kind = self._rule(), self._template().get("kind")
        if self.target_value <= 0:
            problems.append("enter a target above zero")
        elif kind == "percent" and self.target_value > 100:
            problems.append("enter a percentage of at most 100")
        elif rule == "reduce_by" and self.target_value >= 100:
            problems.append("enter a reduction below 100%")
        if not self.period_start or not self.period_end:
            problems.append("choose the measurement period")
        elif self.period_start > self.period_end:
            problems.append("the period must start before it ends")
        elif objective.deadline and self.period_end > objective.deadline:
            problems.append("the period must end on or before the goal deadline (%s): choose an earlier 'Measure until' "
                            "date, or extend the goal deadline" % _human_date(objective.deadline))
        title = self._template().get("title") or measure["label"]
        # Always restate the chosen option and target, so a quantity option on a money goal is visible at once.
        chosen = "%s, target %s." % (title, self._target_label(self.target_value)) if self.target_value > 0 else "%s." % title
        add("setup", "Measurement chosen", not problems,
            chosen if not problems else "%s To finish: %s." % (chosen, "; ".join(problems)))
        if problems:
            return checks

        add("app", "Odoo apps installed", *self._apps_check(measure))

        kpi, version = self._find_kpi()
        try:
            _validate_calculation_contract(self.env, measure["source_type"], self.measure_key, self.product_id.id or False, self._unit(), self.company_id.id)
            contract_ok = True
        except ValidationError as exc:
            contract_ok, kpi = False, kpi
            add("formula", "Approved calculation", False, str(exc))
        if contract_ok:
            add("formula", "Approved calculation", True,
                "Uses the existing approved measure '%s'." % kpi.name if kpi
                else "A new measure will be created with the standard approved calculation when the target is prepared.")

        # Data: run the real collector, read-only. Once the goal's period has started, over that period up to
        # today (so the owner sees the result so far); before it, over the last period of the same length.
        company = self.company_id
        length = (self.period_end - self.period_start).days + 1
        today = self._company_today(company)
        if self.period_start <= today:
            probe_start, probe_end = self.period_start, min(self.period_end, today)
            span = "So far in this goal's period (%s to %s)" % (_human_date(probe_start), _human_date(probe_end))
        else:
            probe_end = today - timedelta(days=1)
            probe_start = probe_end - timedelta(days=length - 1)
            span = "Last %s days (%s to %s)" % (length, _human_date(probe_start), _human_date(probe_end))
        probe_version = version or SimpleNamespace(
            id=False, source_type=measure["source_type"], calculation_type=self.measure_key,
            source_product_id=self.product_id if measure.get("needs_product") else self.env["product.product"],
            unit=self._unit(), direction=self._direction(),
        )
        collector = source_collector_for(probe_version)
        if not collector:
            add("data", "Odoo data available", False, "No data source supports this measure.")
            return checks
        try:
            result = collector(_ProbeTarget(self.env, company, probe_start, probe_end, objective.scope, objective.warehouse_id), probe_version)
        except (UserError, ValidationError, AccessError) as exc:
            result = {"status": "data_quality_issue", "reason": str(exc)}
        if result.get("status") == "valid" and result.get("has_value"):
            if self._template():
                shown = self._show(result["value"])
            else:
                shown = self._money(result["value"]) if measure.get("unit") == "currency" else "%g %s" % (result["value"], self._unit())
            add("data", "Odoo data available", True, "%s: %s." % (span, shown))
        elif result.get("status") == "not_evaluated":
            # The source works but found no matching records yet; that does not block setup, and is not called "available".
            add("data", "Data source checked", True, "Data source checked — no matching records found. %s: %s"
                % (span, result.get("reason") or "no activity."))
        else:
            add("data", "Odoo data available", False, result.get("reason") or "The data source did not return a valid result.")
        return checks

    def check_readiness(self):
        self.ensure_one()
        checks = self._readiness_checks()
        values = {"readiness_json": json.dumps(checks)}
        if self.setup_state in ("needs_setup", "ready"):
            values["setup_state"] = "ready" if checks and all(check["ok"] for check in checks) and len(checks) == 4 else "needs_setup"
        self.write(values)
        return self._goal_payload(self.objective_id)

    def prepare_target(self):
        """Create the draft KPI target (reusing or creating the KPI). Approval stays a separate step."""
        self.ensure_one()
        self._require_head_office()
        if self._is_monitoring():
            raise UserError("A monitoring goal has no target; submit it for approval instead.")
        if self.setup_state != "ready":
            raise UserError("Finish the setup and pass all readiness checks before preparing the target.")
        self.check_readiness()
        if self.setup_state != "ready":
            raise UserError("Readiness changed; review the checks before preparing the target.")
        kpi, _version = self._find_kpi()
        if not kpi:
            kpi = self._create_kpi()
        objective = self.objective_id
        target_values = {
            "kpi_id": kpi.id,
            "date_start": self.period_start,
            "date_end": self.period_end,
            "scope": objective.scope,
            "warehouse_id": objective.warehouse_id.id if objective.scope == "branch" else False,
            "target_basis": "absolute",
            "target_value": self.target_value,
        }
        if self._rule() and self._direction() == "range":
            range_min, range_max = target_bounds(self._rule(), self.target_value)
            target_values.update({"target_value": 0.0, "range_min": range_min, "range_max": range_max})
        target = self.env["strategic.kpi.target"].create(target_values)
        self.write({"kpi_id": kpi.id, "target_id": target.id, "setup_state": "target_pending"})
        return self._goal_payload(self.objective_id)

    def approve_target(self):
        """Approve the target through the existing workflow, then link the OKR and required Key Result."""
        self.ensure_one()
        self._require_head_office()
        if self.setup_state != "target_pending" or not self.target_id:
            raise UserError("There is no prepared target waiting for approval.")
        self.target_id.action_approve()
        objective = self.objective_id
        okr = self.env["strategic.okr.objective"].create({
            "name": objective.name,
            "strategic_objective_id": objective.id,
            "owner_id": objective.owner_id.id,
            "date_start": self.period_start,
            "date_end": self.period_end,
        })
        key_result = self.env["strategic.key.result"].create({
            "name": ("%s: %s" % (self._template()["title"], self._target_label(self.target_value))) if self._rule()
                    else "%s reaches %g %s" % (self._measure()["label"], self.target_value, self._unit()),
            "okr_objective_id": okr.id,
            "required": True,
            "primary_kpi_id": self.kpi_id.id,
            "target_id": self.target_id.id,
        })
        self.write({"okr_id": okr.id, "key_result_id": key_result.id, "setup_state": "measured"})
        return self._goal_payload(objective)

    def submit_goal(self):
        """Submit through the existing objective approval; never activates."""
        self.ensure_one()
        if self._is_monitoring():
            # Monitoring has no target: it is submitted once its settings pass the readiness checks.
            if self.setup_state == "ready":
                self.check_readiness()
            if self.setup_state != "ready":
                raise UserError("Finish the monitoring settings and pass all readiness checks before submitting.")
        elif self.setup_state != "measured" or not self.key_result_id:
            raise UserError("The goal can be submitted only after its target is approved and linked.")
        self._check_plan_still_valid()
        self.objective_id.action_submit()
        self.write({"setup_state": "submitted"})
        return self._goal_payload(self.objective_id)

    def approve_goal(self):
        self.ensure_one()
        self._check_plan_still_valid()
        self.objective_id.action_approve()
        return self._goal_payload(self.objective_id)

    def evaluate_now(self):
        """Run the existing source evaluation for the approved target (interim results allowed by that workflow)."""
        self.ensure_one()
        if not self.target_id or self.target_id.state != "approved":
            raise UserError("The goal has no approved target to evaluate.")
        # Limits are judged on the whole period; the page shows the result so far instead of storing an interim one.
        if self._rule() and self.target_id.date_end >= self._company_today(self.company_id):
            raise ValidationError("This goal is judged on the whole period. The final result can be calculated after %s; "
                                  "until then the goal shows the result so far." % _human_date(self.target_id.date_end))
        self.target_id.action_evaluate_source()
        return self._goal_payload(self.objective_id)

    def _result_view(self, objective):
        """Result of a template goal: live 'so far' while the period is open, the stored final result after it.

        The live value runs the same collector as the KPI on the goal's own approved target (read-only);
        the final result is the evaluation linked to the Key Result. Nothing here writes records.
        """
        if not self or not self._rule() or not self.target_id:
            return False
        target = self.target_id
        company = self.company_id
        rule, kind = self._rule(), self._template()["kind"]
        if rule == "at_least":
            goal_number = target.target_value
        else:
            goal_number = target.range_max if rule in ("ceiling", "rise_at_most", "tolerance") else (target.range_min if rule == "floor" else -target.range_max)
        today = self._company_today(company)
        phase = "upcoming" if today < target.date_start else ("open" if today <= target.date_end else "closed")
        view = {
            "phase": phase, "final": False, "status": False, "tone": "neutral", "note": "",
            "period_start": fields.Date.to_string(target.date_start), "period_end": fields.Date.to_string(target.date_end),
            "target_display": self._target_label(goal_number), "value": False, "value_display": False,
            "value_label": "Current result", "remaining_display": False, "over_display": False, "used_pct": False,
            "rows": [], "rows_title": "", "count_label": "", "bar": False,
        }
        own_branch = objective.scope == "branch" and objective.warehouse_id == self.env.user.strategic_branch_warehouse_id
        if not (self._is_head_office() or own_branch):
            view["note"] = "Results for this goal are visible to Head Office and the branch's manager."
            return view
        if target.state != "approved":
            view["note"] = "Results appear once the target is approved."
            return view
        if phase == "upcoming":
            view.update(status="Starts %s" % _human_date(target.date_start), note="Measuring starts on the first day of the period.",
                        bar=_waiting_bar("Waiting for data: measuring starts %s" % _human_date(target.date_start)))
            return view

        evaluation = self.key_result_id.evaluation_id
        if phase == "closed" and evaluation:
            if evaluation.status != "valid":
                # The period is over: the final evaluation found no valid result, so nothing is pending any more.
                status = "Data unavailable" if evaluation.status == "data_quality_issue" else "Not evaluated"
                view.update(status=status, tone="bad" if evaluation.status == "data_quality_issue" else "neutral",
                            note=evaluation.reason or evaluation.source_note or "The final result could not be measured.",
                            bar=_waiting_bar(status))
                return view
            value = evaluation.actual_value
            achieved = evaluation.attainment_status in (("achieved", "exceeded") if rule == "at_least" else ("achieved",))
            view.update(final=True, note=evaluation.source_note or "")
        else:
            version = target.kpi_id._get_definition_version_for_period(target.date_start, target.date_end)
            collector = source_collector_for(version)
            result = collector(target, version) if collector else {"status": "data_quality_issue", "reason": "No data source supports this measure."}
            if result.get("status") != "valid" or not result.get("has_value"):
                # The source was queried but returned no valid value: show the target with an empty bar, not 0%.
                data_problem = result.get("status") == "data_quality_issue"
                status = "Data unavailable" if data_problem else "Waiting for data"
                view.update(status=status, tone="bad" if data_problem else "neutral", note=result.get("reason") or "",
                            bar=_waiting_bar(status))
                return view
            value = result["value"]
            achieved = (value >= target.target_value - 1e-9) if rule == "at_least" else \
                       (target.range_min - 1e-9 <= value <= target.range_max + 1e-9)
            view["note"] = result.get("reason") or ""
            if phase == "closed":
                view["note"] = "The period has ended; the final result is recorded by the next evaluation run. " + view["note"]

        bounds = (target.target_value, target.target_value) if rule == "at_least" else (target.range_min, target.range_max)
        bar = measure_bar(rule, value, *bounds)
        bar["label"] = self._bar_label(bar, value)
        view.update(value=value, value_display=self._show(value), value_label="Final result" if view["final"] else "So far", bar=bar)
        if rule == "ceiling":
            over = value - target.range_max
            # "+ 0.0" turns -0.0 (limit exactly reached) into 0.0, so nothing shows as "-0".
            view.update(remaining_display=self._show(max(0.0, -over) + 0.0), over_display=self._show(over) if over > 1e-9 else False,
                        used_pct=round(value / target.range_max * 100.0, 1) if target.range_max else False)
        view.update(self._status_text(rule, kind, value, achieved, view))
        self._add_rows(objective, target, view)
        return view

    def _bar_label(self, bar, value):
        """Plain-language text under the bar; the number is the real percentage, never capped."""
        target, rule, budget = self.target_id, self._rule(), self._is_budget()
        num = lambda x: "%g" % round(x, 1)
        if rule == "ceiling":
            over = value - target.range_max
            text = "%s%% of the %s used" % (num(bar["pct"]), "budget" if budget else "limit")
            return text + (" · %s over the %s" % (self._show(over), "budget" if budget else "limit") if over > 1e-9 else "")
        if rule == "floor":
            return "%s%% of the target reached (%s, target at least %s)" % (num(bar["pct"]), self._show(value), self._show(target.range_min))
        if rule == "at_least":
            return "%s%% of the target reached (%s of at least %s)" % (num(bar["pct"]), self._show(value), self._show(target.target_value))
        if rule == "reduce_by":
            needed = -target.range_max
            if value >= 0:
                return "Price up %s%%, a cut of at least %s%% is needed" % (num(value), num(needed))
            return "Price down %s%% of the %s%% needed" % (num(-value), num(needed))
        if rule == "tolerance":
            return "Difference of %s uses %s%% of the ±%s allowed%s" % (
                self._show(value), num(bar["pct"]), self._show(target.range_max), " · outside the tolerance" if bar["pct"] > 100 else "")
        if value <= 0:
            return "Price down %s%%, no rise" % num(-value)
        return "Price up %s%% of the %s%% allowed%s" % (num(value), num(target.range_max), " · limit exceeded" if bar["pct"] > 100 else "")

    def _status_text(self, rule, kind, value, achieved, view):
        """Plain-language status; 'Achieved' is used only for a final result."""
        budget = self._is_budget()
        if view["final"]:
            prefix = "Achieved" if achieved else "Not achieved"
            if rule == "ceiling":
                detail = ("within budget" if budget else "within the limit") if achieved else \
                         ("over budget by %s" if budget else "over the limit by %s") % view["over_display"]
            elif rule == "floor":
                detail = "%s (target %s)" % (self._show(value), view["target_display"].lower())
            elif rule == "tolerance":
                detail = "difference of %s (allowed ±%s)" % (self._show(value), self._show(self.target_id.range_max))
            elif rule == "at_least":
                detail = "%s (target %s)" % (self._show(value), view["target_display"][0].lower() + view["target_display"][1:])
            else:
                detail = "price %s %s%%" % ("down" if value < 0 else "up", abs(round(value, 1)))
            return {"status": "%s: %s" % (prefix, detail), "tone": "good" if achieved else "bad"}
        if view["phase"] == "closed":
            return {"status": "Final result pending", "tone": "neutral"}
        if rule == "ceiling":
            tone = view["bar"]["tone"]
            if tone == "exceeded":
                return {"status": ("Over budget by %s" if budget else "Over the limit by %s") % view["over_display"], "tone": "bad"}
            if tone == "bad":
                return {"status": "Near the budget limit so far" if budget else "Near the limit so far", "tone": "bad"}
            return {"status": "Within budget so far" if budget else "Within the limit so far", "tone": tone}
        if rule == "floor":
            return {"status": "%s so far (target %s)" % (self._show(value), view["target_display"].lower()), "tone": "good" if achieved else "bad"}
        if rule == "tolerance":
            return {"status": "Difference of %s so far (allowed ±%s)" % (self._show(value), self._show(self.target_id.range_max)),
                    "tone": "good" if achieved else "bad"}
        if rule == "at_least":
            return {"status": "%s so far (target %s)" % (self._show(value), view["target_display"][0].lower() + view["target_display"][1:]),
                    "tone": "good" if achieved else view["bar"]["tone"]}
        return {"status": "Price %s %s%% so far" % ("down" if value < 0 else "up", abs(round(value, 1))), "tone": "good" if achieved else "bad"}

    def _add_rows(self, objective, target, view):
        """Records behind the number, for the measures where a list helps the owner check it."""
        start_utc, end_utc, issue = reporting_period_utc(target)
        if issue:
            return
        warehouses = scope_warehouses(target)
        timezone = pytz.timezone(self.company_id.partner_id.tz or "UTC")

        def local(value):
            return pytz.UTC.localize(fields.Datetime.to_datetime(value)).astimezone(timezone).strftime("%Y-%m-%d %H:%M")

        measure = self.measure_key
        if measure == "purchase_confirmed_amount":
            _orders, rows, _total, problem = confirmed_purchase_commitments(self.env, self.company_id, start_utc, end_utc, warehouses)
            if not problem:
                view.update(rows_title="Purchase orders counted", count_label="%s orders" % len(rows), rows=[
                    {"id": r["id"], "name": r["name"], "detail": "%s · %s · confirmed %s" % (r["supplier"], r["branch"], local(r["confirmed"])),
                     "amount": self._money(r["amount"])} for r in rows])
        elif measure == "purchase_top_supplier_share_pct":
            _orders, shares, _total, problem = supplier_shares(self.env, self.company_id, start_utc, end_utc, warehouses)
            if not problem:
                view.update(rows_title="Purchasing by supplier", count_label="%s suppliers" % len(shares), rows=[
                    {"id": index, "name": s["supplier"], "detail": "%s orders · %s%% of purchasing" % (s["orders"], round(s["share"], 1)),
                     "amount": self._money(s["amount"])} for index, s in enumerate(shares)])
        elif measure == "purchase_outstanding_amount":
            now = fields.Datetime.to_datetime(fields.Datetime.now())
            _lines, rows, _total, problems = outstanding_purchases(self.env, self.company_id, min(end_utc, now), warehouses)
            if not problems:
                view.update(rows_title="Orders still waiting for delivery", count_label="%s orders still waiting for delivery" % len(rows), rows=[
                    {"id": r["id"], "name": r["name"], "detail": "%s · %s · confirmed %s" % (r["supplier"], r["branch"], local(r["confirmed"])),
                     "amount": self._money(r["amount"])} for r in rows])
        elif measure in ("inventory_waste_cost_amount", "inventory_waste_cost_to_sales_pct", "inventory_recorded_waste_cost"):
            product = self.product_id if measure == "inventory_recorded_waste_cost" else False
            moves, _total, problem = valued_waste(self.env, self.company_id, start_utc, end_utc,
                                                  target.warehouse_id if target.scope == "branch" else False, product)
            if not problem:
                view.update(rows_title="Waste counted", count_label="%s waste records" % len(moves), rows=[
                    {"id": m.id, "name": m.product_id.display_name,
                     "detail": "%g %s · %s · %s" % (m.quantity_product_uom, m.product_id.uom_id.name, ", ".join(m.scrap_reason_tag_ids.mapped("name")), local(m.date)),
                     "amount": self._money(waste_move_cost(m))} for m in moves[:100]])
        elif measure == "accounting_overdue_supplier_bills_amount":
            as_of = min(target.date_end, self._company_today(self.company_id))
            _bills, rows, _total, problems = overdue_supplier_bills(self.env, self.company_id, as_of)
            if problems:
                return
            view.update(rows_title="Overdue supplier bills", count_label="%s bills overdue" % len(rows), rows=[
                {"id": r["id"], "name": r["name"], "detail": "%s · due %s" % (r["supplier"], r["due"]), "amount": self._money(r["amount"])}
                for r in rows])

    def _okr_view(self, objective):
        """How the objective is measured: each OKR objective and its Key Results, from the real records."""
        direction_labels = {"higher": "Higher is better", "lower": "Lower is better", "range": "Within a range"}
        rule_labels = {"ceiling": "Lower is better (hard limit)", "floor": "Higher is better (minimum)",
                       "reduce_by": "Lower is better (price cut)", "rise_at_most": "Lower is better (price rise limit)",
                       "tolerance": "Close to zero (tolerance)", "at_least": "Higher is better"}
        okrs = []
        for okr in objective.okr_objective_ids:
            key_results = []
            for index, kr in enumerate(okr.key_result_ids, start=1):
                kpi, target, evaluation = kr.primary_kpi_id, kr.target_id, kr.evaluation_id
                own = self and self.key_result_id == kr and self._rule()
                if own:
                    target_text, direction = self._target_label(self.target_value), rule_labels[self._rule()]
                elif kpi.direction == "range":
                    target_text, direction = "Between %g and %g %s" % (target.range_min, target.range_max, kr.unit or ""), direction_labels["range"]
                else:
                    target_text, direction = "%g %s" % (target.target_value, kr.unit or ""), direction_labels.get(kpi.direction, "")
                if not evaluation:
                    current, readiness, tone = "Not evaluated yet", "No evaluation yet", "setup"
                elif evaluation.status == "valid" and evaluation.has_actual_value:
                    current = self._show(evaluation.actual_value) if own else "%g %s" % (evaluation.actual_value, evaluation.unit or "")
                    readiness, tone = "Evaluated %s" % _human_date(evaluation.date_end), "available"
                else:
                    current = "Data unavailable" if evaluation.status == "data_quality_issue" else "Not evaluated"
                    readiness, tone = current, "setup"
                calculation = dict(kpi._fields["calculation_type"].selection).get(kpi.calculation_type, "")
                key_results.append({
                    "id": kr.id, "label": "Key Result %s" % index, "title": kr.name, "required": kr.required,
                    "kpi": kpi.name, "calculation": calculation,
                    "source": " · ".join(filter(None, [dict(kpi._fields["source_type"].selection).get(kpi.source_type),
                                                     kpi.source_product_id.display_name])),
                    "current": current, "target": target_text, "direction": direction,
                    "readiness": readiness, "readiness_tone": tone,
                    "note": (evaluation.reason or evaluation.source_note or "") if evaluation else "",
                })
            okrs.append({"id": okr.id, "name": okr.name, "period": "%s – %s" % (_human_date(okr.date_start), _human_date(okr.date_end)),
                         "key_results": key_results})
        return okrs
