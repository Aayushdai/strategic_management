"""Business Goals: owner-facing setup that drives the existing objective -> OKR -> KPI chain.

A business goal is a normal ``strategic.objective`` (draft until approved). This module
only stores the owner's setup choices and walks the existing governed steps:
measure -> readiness -> draft target -> target approval -> OKR/Key Result -> submit.
It never activates an objective itself and never invents measurement values.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
import json

import pytz

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import format_amount

from .goal_templates import GOAL_TYPES, TEMPLATE_AREAS, TEMPLATE_BY_KEY, TEMPLATES, attainment_tone, measure_bar, target_bounds
from .kpi import _validate_calculation_contract
from .kpi_source_adapter import source_collector_for
from ..services.kpi_sources.accounting import overdue_supplier_bills
from ..services.kpi_sources.common import reporting_period_utc, unit_price_unit
from ..services.kpi_sources.inventory import (minimum_stock_rules, renamed_unit_note, stock_snapshot, unfinished_pos_deliveries,
                                              valued_waste, waste_move_cost)
from ..services.kpi_sources.pos import period_bounds_utc, window_sales
from ..services.kpi_sources.purchase import confirmed_purchase_commitments, scope_warehouses
from ..services.kpi_sources.purchase_goals import (open_purchase_lines, outstanding_purchases, purchase_line_facts,
                                                   received_move_facts, supplier_shares)
from ..services.kpi_sources.supplier_delivery import average_delays, delivery_performance_facts, on_time_rates

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
_MONITOR_NEXT_STEP = {
    "needs_setup": "Choose what to monitor, the reporting period and the daily time window, then check readiness.",
    "ready": "All checks passed. Submit the goal for approval; its sales are already shown above.",
}


def _parse_time(value):
    """'17:30' (or 17.5) -> 17.5 hours of the day; '24:00' is the end of the day."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        hours = float(value)
    else:
        try:
            hour, minute = (value or "").strip().split(":")[:2]
            hour, minute = int(hour), int(minute)
        except (ValueError, AttributeError):
            raise UserError("Enter the times of day as HH:MM, for example 17:00 and 21:00.")
        if not 0 <= minute < 60:
            raise UserError("Enter the times of day as HH:MM, for example 17:00 and 21:00.")
        hours = hour + minute / 60.0
    if not 0.0 <= hours <= 24.0:
        raise UserError("Times of day run from 00:00 to 24:00.")
    return hours


def _bar_rows(values):
    """[(key, label, number or None, display)] -> rows with a bar width relative to the largest number."""
    peak = max((abs(number) for _key, _label, number, _display in values if number is not None), default=0.0)
    return [{"key": str(key), "label": label, "display": display,
             "bar": round(abs(number) / peak * 100.0, 1) if number is not None and peak else 0.0}
            for key, label, number, display in values]


_TIMELINE_STEPS = ("hour", "day", "week", "month")
_TIMELINE_RECORDS = 100


def _timeline_starts(start, end, granularity):
    """Interval starts (naive local datetimes) covering [start, end) at the given granularity."""
    if granularity == "hour":
        current, step = start.replace(minute=0, second=0, microsecond=0), lambda value: value + timedelta(hours=1)
    elif granularity == "day":
        current, step = start.replace(hour=0, minute=0, second=0, microsecond=0), lambda value: value + timedelta(days=1)
    elif granularity == "week":
        day = start.replace(hour=0, minute=0, second=0, microsecond=0)
        current, step = day - timedelta(days=day.weekday()), lambda value: value + timedelta(days=7)
    else:
        current = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        step = lambda value: (value.replace(day=28) + timedelta(days=4)).replace(day=1)
    while current < end:
        following = step(current)
        yield current, following
        current = following


def _interval_label(start, end, granularity, short=False):
    """'17:00', 'Mon 6 Oct', '6 – 12 Oct', 'Oct 2026'; long form adds the date and the end."""
    last = end - timedelta(seconds=1)
    if granularity == "hour":
        return start.strftime("%H:%M") if short else "%s, %s – %s" % (start.strftime("%a %d %b %Y"), start.strftime("%H:%M"), end.strftime("%H:%M"))
    if granularity == "day":
        return start.strftime("%a %d") if short else start.strftime("%a %d %b %Y")
    if granularity == "week":
        if short:
            return start.strftime("%d %b")
        return "%s – %s" % (start.strftime("%d %b"), last.strftime("%d %b %Y"))
    return start.strftime("%b") if short else start.strftime("%B %Y")


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

    # ------------------------------------------------------------------ monitoring goals (no target)

    def _is_monitoring(self):
        return self._template().get("goal_type") == "monitoring"

    def _monitor_period(self):
        """Dates a monitoring goal covers (company time zone), never past today; (False, False) when not chosen."""
        today = self._company_today(self.company_id)
        if self.period_preset == "today":
            return today, today
        if self.period_preset == "this_week":
            return today - timedelta(days=today.weekday()), today
        if self.period_preset == "this_month":
            return today.replace(day=1), today
        if self.period_preset == "custom" and self.period_start and self.period_end:
            return self.period_start, min(self.period_end, today)
        return False, False

    def _monitor_value(self, metric, totals):
        """(number or None, display) of one metric from window_sales totals; None when there is no single value."""
        if metric == "revenue":
            return totals["revenue"], self._money(totals["revenue"])
        if metric == "orders":
            return totals["orders"], "%d order%s" % (totals["orders"], "" if totals["orders"] == 1 else "s")
        if metric == "aov":
            if not totals["orders"]:
                return None, "No orders"
            value = totals["revenue"] / totals["orders"]
            return value, self._money(value)
        return self._units_value(totals["quantity"])

    def _monitor_filters_label(self):
        parts = [self.product_id.display_name if self.product_id else ""]
        parts.append("category %s" % self.pos_category_id.display_name if self.pos_category_id else "")
        parts.append("supplier %s" % self.partner_id.display_name if self.partner_id else "")
        parts.append("product category %s" % self.product_category_id.display_name if self.product_category_id else "")
        return " and ".join(part for part in parts if part) or "All products"

    def _monitor_view(self, objective):
        """Live result of a monitoring goal: values, daily trend, previous equivalent period, branches and products.

        Monitoring has no target, so nothing is called achieved or failed. Recalculated from Odoo's POS orders each
        time the goal is shown (new, refunded or cancelled orders appear at once). Nothing here writes records.
        """
        if not self or not self._is_monitoring() or not self.measure_key:
            return False
        metric = self._measure()["monitor"]
        pos = self._monitor_source() == "pos"
        window = "%s–%s" % (_time_label(self.window_start), _time_label(self.window_end)) if pos else ""
        view = {
            "status": "ok", "note": "", "metric_label": self._measure()["label"], "window_label": window,
            "title": "%s, %s each day" % (self._measure()["label"], window) if pos else self._measure()["label"], "period_label": "",
            "filters_label": self._monitor_filters_label(), "value_display": False,
            "cards": [], "trend": [], "previous": False, "branches": [], "products": [], "blocks": [], "table": False,
            "warnings": [],
        }
        own_branch = objective.scope == "branch" and objective.warehouse_id == self.env.user.strategic_branch_warehouse_id
        if not (self._is_head_office() or own_branch):
            view.update(status="no_access", note="%s for this goal are visible to Head Office and the branch's manager."
                        % {"pos": "Sales", "purchase": "Purchasing figures"}.get(self._monitor_source(), "Stock figures"))
            return view
        if self._monitor_source() == "inventory":
            return self._inventory_monitor(view, objective)
        date_from, date_to = self._monitor_period()
        if pos and not 0.0 <= self.window_start < self.window_end <= 24.0:
            view.update(status="setup", note="Choose a daily time window that starts before it ends, e.g. 17:00 to 21:00.")
            return view
        if not date_from:
            view.update(status="setup", note="Choose the reporting period.")
            return view
        if date_from > date_to:
            view.update(status="upcoming", note="The reporting period starts on %s." % _human_date(date_from))
            return view
        view["period_label"] = "%s – %s" % (_human_date(date_from), _human_date(date_to))
        if not pos:
            return self._purchase_monitor(view, objective, date_from, date_to)
        warehouse = objective.warehouse_id if objective.scope == "branch" else False
        settings = (self.window_start, self.window_end, warehouse, self.product_id, self.pos_category_id)
        data = window_sales(self.env, self.company_id, date_from, date_to, *settings)
        if data["problem"]:
            view.update(status="data_problem", note=data["problem"])
            return view
        if not data["orders_in_period"]:
            view.update(status="empty", note=(
                "No paid POS orders were recorded in this period at any time of day, so there is nothing to show yet; "
                "this is not a zero-sales result. If the POS was in use, check that it is linked to %s."
                % (warehouse.display_name if warehouse else "a branch")))
            return view
        totals = data["total"]
        for key, label in (("revenue", "Sales (excl. tax)"), ("orders", "Orders"), ("aov", "Average order value"),
                           ("quantity", "Quantity sold")):
            view["cards"].append({"key": key, "label": label, "value_display": self._monitor_value(key, totals)[1],
                                  "primary": key == metric})
        value, view["value_display"] = self._monitor_value(metric, totals)
        if not (totals["orders"] or totals["revenue"] or totals["quantity"]):
            view["note"] = "POS orders exist in this period, but none between %s and %s%s." % (
                _time_label(self.window_start), _time_label(self.window_end),
                "" if view["filters_label"] == "All products" else " for %s" % view["filters_label"])
        else:
            view["note"] = "Recalculated from Odoo's POS orders each time this goal is opened."

        def rows(items):
            return _bar_rows([(key, label, *self._monitor_value(metric, bucket)) for key, label, bucket in items])

        view["trend"] = rows([(day, _human_date(day), bucket) for day, bucket in sorted(data["days"].items())])
        if self._is_head_office() and objective.scope == "organization":
            view["branches"] = rows([(key, bucket["name"], bucket) for key, bucket in
                                     sorted(data["branches"].items(), key=lambda item: item[1]["name"])])
        view["products"] = [{"key": str(index), "name": item["name"], "display": "%g %s" % (round(item["qty"], 4), item["unit"]),
                             "amount": self._money(item["revenue"])} for index, item in enumerate(data["products"][:10])]
        view["blocks"] = [{"title": "Trend by day", "rows": view["trend"]}, {"title": "By branch", "rows": view["branches"]}]
        view["table"] = {"headers": ["Products sold in the window", "Quantity", "Sales"],
                         "rows": [{"key": item["key"], "cells": [item["name"], item["display"], item["amount"]]} for item in view["products"]]}
        # Previous equivalent period: same length, window and filters, immediately before.
        previous_to = date_from - timedelta(days=1)
        previous_from = previous_to - (date_to - date_from)
        previous = window_sales(self.env, self.company_id, previous_from, previous_to, *settings)
        label = "%s – %s" % (_human_date(previous_from), _human_date(previous_to))
        if previous["problem"]:
            view["previous"] = {"label": label, "display": "Unavailable", "change": previous["problem"]}
        elif not previous["orders_in_period"]:
            view["previous"] = {"label": label, "display": "No POS orders", "change": "Nothing to compare with."}
        else:
            previous_value, previous_display = self._monitor_value(metric, previous["total"])
            change = ""
            if value is not None and previous_value:
                change = "%+.1f%% compared with the previous period" % ((value - previous_value) / abs(previous_value) * 100.0)
            view["previous"] = {"label": label, "display": previous_display, "change": change}
        return view

    def _monitor_source(self):
        return self._template().get("monitor_source")

    # ------------------------------------------------------------------ monitoring timeline and drill-down

    def _monitor_access(self, objective):
        own_branch = objective.scope == "branch" and objective.warehouse_id == self.env.user.strategic_branch_warehouse_id
        return self._is_head_office() or own_branch

    def _monitor_points(self, objective):
        """The facts behind a monitoring goal, each with the local time it belongs to and its source record.

        Same sources, filters, branch scope and rules as _monitor_view, so intervals add up to (or, for averages and
        rates, are recalculated exactly like) the goal's own figure. Stock goals are snapshots and have no points.
        """
        metric = self._measure()["monitor"]
        source = self._monitor_source()
        company = self.company_id
        tz_name = company.partner_id.tz or "UTC"
        info = {"status": "ok", "metric": metric, "timezone": tz_name, "hours": (0.0, 24.0), "timed": True, "kind": "flow",
                "points": [], "note": ""}
        if source == "inventory":
            return dict(info, status="snapshot")
        date_from, date_to = self._monitor_period()
        if not date_from or date_from > date_to:
            return dict(info, status="unavailable", note="There is no reporting period to break down yet.")
        timezone = pytz.timezone(tz_name)
        local = lambda value: pytz.UTC.localize(fields.Datetime.to_datetime(value)).astimezone(timezone).replace(tzinfo=None)
        midnight = lambda day: datetime(day.year, day.month, day.day)
        info.update(start=midnight(date_from), end=midnight(date_to) + timedelta(days=1))
        branch_scope = objective.warehouse_id if objective.scope == "branch" else False
        points = info["points"]

        if source == "pos":
            data = window_sales(self.env, company, date_from, date_to, self.window_start, self.window_end, branch_scope,
                                self.product_id, self.pos_category_id)
            if data["problem"]:
                return dict(info, status="unavailable", note=data["problem"])
            if not data["orders_in_period"]:
                return dict(info, status="unavailable", note="No POS orders were recorded in this period, so there is nothing to break down (this is not zero sales).")

            def value_of(facts):
                totals = {"revenue": 0.0, "orders": 0, "quantity": {}}
                for fact in facts:
                    totals["revenue"] += fact["revenue"]
                    totals["orders"] += fact["orders"]
                    for unit, qty in fact["quantity"].items():
                        totals["quantity"][unit] = totals["quantity"].get(unit, 0.0) + qty
                return self._monitor_value(metric, totals)

            for fact in data["facts"]:
                order = fact["order"]
                points.append({"at": fact["local"], "fact": fact, "record": ("pos.order", order.id, order.name),
                               "detail": "%s%s" % (fact["branch"], " · refund" if order.is_refund else "")})
            info.update(value_of=value_of, hours=(self.window_start, self.window_end), kind="rate" if metric == "aov" else "flow",
                        basis="order time")
            return info

        warehouses = objective.warehouse_id if objective.scope == "branch" else \
            self.env["stock.warehouse"].sudo().search([("company_id", "=", company.id)])
        start_utc, end_utc, problem = period_bounds_utc(company, date_from, date_to)
        if not problem and not warehouses:
            problem = "No branch warehouse is configured, so purchase orders cannot be assigned to a branch."
        if problem:
            return dict(info, status="unavailable", note=problem)
        filters = (self.partner_id, self.product_id, self.product_category_id)

        if metric in ("on_time", "delay"):
            facts, _excluded, problem = delivery_performance_facts(self.env, company, date_from, date_to, warehouses, *filters)
            if problem:
                return dict(info, status="unavailable", note=problem)
            for fact in facts:
                points.append({"at": midnight(fact["expected"]), "fact": fact, "record": ("purchase.order", fact["order"].id, fact["order"].name),
                               "detail": "%s · %s" % (fact["supplier"], fact["product"])})
            info.update(value_of=lambda facts: self._delivery_value(metric, facts), timed=False, kind="rate", basis="promised day")
            return info

        if metric in ("outstanding", "overdue"):
            now = fields.Datetime.to_datetime(fields.Datetime.now())
            as_of = min(end_utc, now)
            _lines, items, problems = open_purchase_lines(self.env, company, as_of, warehouses, *filters)
            if problems:
                return dict(info, status="unavailable", note="; ".join(problems[:5]))
            as_of_day = local(as_of).date()
            chosen = items if metric == "outstanding" else [i for i in items if i["expected"] and local(i["expected"]).date() < as_of_day]
            orders = {}
            for item in chosen:
                row = orders.setdefault(item["order"].id, {"order": item["order"], "expected": item["expected"], "amount": 0.0})
                row["amount"] += item["amount"]
                if item["expected"] and (not row["expected"] or item["expected"] < row["expected"]):
                    row["expected"] = item["expected"]
            undated = 0
            for row in orders.values():
                if not row["expected"]:
                    undated += 1
                    continue
                order = row["order"]
                points.append({"at": midnight(local(row["expected"]).date()), "fact": row, "record": ("purchase.order", order.id, order.name),
                               "detail": "%s · %s" % (order.partner_id.display_name or "No supplier", self._money(row["amount"]))})
            if not points:
                return dict(info, status="unavailable", note="No dated order is %s, so there is nothing to place on a timeline."
                            % ("awaiting delivery" if metric == "outstanding" else "overdue"))
            days = [point["at"] for point in points]
            info.update(start=min(days), end=max(days) + timedelta(days=1), timed=False, kind="position", basis="expected arrival day",
                        value_of=lambda facts: (len(facts), "%d order%s" % (len(facts), "" if len(facts) == 1 else "s")),
                        note=("Position %s, spread by each order's expected arrival day: not activity over time."
                              % ("now" if as_of == now else "at the end of %s" % _human_date(date_to)))
                        + (" %d order%s without an expected date %s not shown." % (undated, "" if undated == 1 else "s", "is" if undated == 1 else "are") if undated else ""))
            return info

        if metric == "received":
            facts = received_move_facts(self.env, company, start_utc, end_utc, warehouses, *filters)[1]
            for fact in facts:
                picking = fact["move"].picking_id
                record = ("stock.picking", picking.id, picking.name) if picking else ("purchase.order", fact["order"].id, fact["order"].name)
                points.append({"at": local(fact["date"]), "fact": fact, "record": record,
                               "detail": "%s · %s%s" % (fact["supplier"], fact["product"], " · return" if fact["quantity"] < 0 else "")})
            info.update(value_of=lambda facts: self._purchase_value(metric, facts), basis="receipt time")
            return info

        if metric == "price_comparison":
            _lines, facts, problems = purchase_line_facts(self.env, company, start_utc, end_utc, warehouses, self.partner_id, self.product_id)
            value_of = lambda facts: self._purchase_value("unit_price", facts)
        else:
            _lines, facts, problems = purchase_line_facts(self.env, company, start_utc, end_utc, warehouses, *filters)
            value_of = lambda facts: self._purchase_value(metric, facts)
        if problems:
            return dict(info, status="unavailable", note="; ".join(problems[:5]))
        for fact in facts:
            points.append({"at": local(fact["date"]), "fact": fact, "record": ("purchase.order", fact["order"].id, fact["order"].name),
                           "detail": "%s · %s" % (fact["supplier"], fact["product"])})
        info.update(value_of=value_of, kind="rate" if metric in ("unit_price", "price_comparison") else "flow", basis="confirmation time")
        return info

    def _timeline_granularity(self, info, start, end):
        days = (end - start).days or 1
        if days <= 1:
            return "hour" if info["timed"] else "day"
        if days <= 31:
            return "day"
        return "week" if days <= 120 else "month"

    def _timeline_mixed_units(self, info):
        """Quantities in different units cannot share one bar scale: such goals size bars by their number of records."""
        return info["kind"] == "flow" and info["value_of"]([point["fact"] for point in info["points"]])[0] is None

    def _timeline_buckets(self, info, start, end, granularity):
        """Intervals of [start, end) with their value; future time is 'not yet', never zero."""
        now = pytz.UTC.localize(fields.Datetime.now()).astimezone(pytz.timezone(info["timezone"])).replace(tzinfo=None)
        first_hour, last_hour = info["hours"]
        buckets = []
        for bucket_start, bucket_end in _timeline_starts(start, end, granularity):
            low, high = max(bucket_start, start), min(bucket_end, end)
            if granularity == "hour":
                # Only hours inside the daily time window exist for this goal.
                window_low = low.replace(hour=0, minute=0) + timedelta(hours=first_hour)
                window_high = low.replace(hour=0, minute=0) + timedelta(hours=last_hour)
                low, high = max(low, window_low), min(high, window_high)
                if low >= high:
                    continue
            group = [point["fact"] for point in info["points"] if low <= point["at"] < high]
            future = low >= now
            number, display = (None, "Not yet") if future else info["value_of"](group)
            buckets.append({
                "key": low.strftime("%Y-%m-%dT%H:%M"), "start": low.strftime("%Y-%m-%dT%H:%M"), "end": high.strftime("%Y-%m-%dT%H:%M"),
                "label": _interval_label(low, high, granularity, short=True), "range_label": _interval_label(low, high, granularity),
                "value": number, "display": display, "future": future,
                "records": len({point["record"] for point in info["points"] if low <= point["at"] < high}),
            })
        if self._timeline_mixed_units(info):
            peak = max((b["records"] for b in buckets), default=0)
            for bucket in buckets:
                bucket["height"] = round(bucket["records"] / peak * 100.0, 1) if peak and not bucket["future"] else 0.0
                bucket["negative"], bucket["empty"] = False, False
            return buckets
        peak = max((abs(b["value"]) for b in buckets if b["value"] is not None), default=0.0)
        for bucket in buckets:
            bucket["height"] = round(abs(bucket["value"]) / peak * 100.0, 1) if bucket["value"] is not None and peak else 0.0
            # Below zero is flagged only for totals (refunds, returns); an early delivery is not a bad result.
            bucket["negative"] = bool(info["kind"] == "flow" and bucket["value"] is not None and bucket["value"] < 0)
            bucket["empty"] = bucket["value"] is None and not bucket["future"]
        return buckets

    def _finer(self, info, granularity):
        finer = {"month": "day", "week": "day", "day": "hour" if info["timed"] else False}.get(granularity, False)
        return finer

    def _monitor_timeline(self, objective):
        """Compact time breakdown shown on the monitoring card; values come from the same facts as the goal's figure."""
        info = self._monitor_points(objective)
        caption_units = {"hour": "per hour", "day": "per day", "week": "per week", "month": "per month"}
        if info["status"] == "snapshot":
            return {"status": "snapshot", "note": "Stock is a snapshot of Odoo now; past balances are not kept, so no history is drawn."}
        if info["status"] != "ok":
            return {"status": "unavailable", "note": info["note"]}
        granularity = self._timeline_granularity(info, info["start"], info["end"])
        buckets = self._timeline_buckets(info, info["start"], info["end"], granularity)
        kind_note = {"rate": "Each interval is its own average, not a share of a total.",
                     "position": info["note"],
                     "flow": "Intervals add up to the total shown."}[info["kind"]]
        if info["kind"] == "flow" and self._measure()["monitor"] in ("quantity", "ordered", "received"):
            kind_note = "Intervals add up to the total shown; different units are listed separately, never added."
            if self._timeline_mixed_units(info):
                kind_note += " Units differ, so bar height shows the number of records, not a quantity."
        return {
            "status": "ok", "kind": info["kind"], "granularity": granularity, "drill": self._finer(info, granularity),
            "caption": "%s %s by %s · times in %s" % (self._measure()["label"], caption_units[granularity], info["basis"], info["timezone"]),
            "note": kind_note, "buckets": buckets,
        }

    def get_monitor_interval(self, start, end, granularity):
        """One interval of a monitoring goal: its value, the records behind it and finer intervals to drill into.

        Access, branch, filters and period come from the goal on the server; the request only chooses a time slice
        inside the goal's own period, so it cannot reach other branches or other data.
        """
        self.ensure_one()
        objective = self.objective_id
        if not self._is_monitoring() or not self.measure_key:
            raise UserError("Only monitoring goals have a time breakdown.")
        if not self._monitor_access(objective):
            raise AccessError("Monitoring figures for this goal are visible to Head Office and the branch's manager.")
        if granularity not in _TIMELINE_STEPS:
            raise UserError("Choose an hour, day, week or month.")
        try:
            low, high = (datetime.strptime(value, "%Y-%m-%dT%H:%M") for value in (start, end))
        except (TypeError, ValueError):
            raise UserError("Choose a valid interval.")
        info = self._monitor_points(objective)
        if info["status"] != "ok":
            return {"status": info["status"], "note": info.get("note", "")}
        low, high = max(low, info["start"]), min(high, info["end"])
        if low >= high:
            raise UserError("This interval is outside the goal's reporting period.")
        inside = [point for point in info["points"] if low <= point["at"] < high]
        number, display = info["value_of"]([point["fact"] for point in inside])
        records = {}
        for point in sorted(inside, key=lambda point: point["at"]):
            item = records.setdefault(point["record"], {"points": [], "at": point["at"], "detail": point["detail"]})
            item["points"].append(point["fact"])
        rows = []
        for (model, record_id, name), item in records.items():
            rows.append({"key": "%s:%s" % (model, record_id), "model": model, "id": record_id, "name": name,
                         "when": item["at"].strftime("%d %b %Y %H:%M") if info["timed"] else item["at"].strftime("%d %b %Y"),
                         "value": info["value_of"](item["points"])[1], "detail": item["detail"]})
        finer = self._finer(info, granularity)
        objective_branch = objective.warehouse_id.display_name if objective.scope == "branch" else "All branches"
        return {
            "status": "ok", "granularity": granularity, "start": low.strftime("%Y-%m-%dT%H:%M"), "end": high.strftime("%Y-%m-%dT%H:%M"),
            "range_label": _interval_label(low, high, granularity), "value": number, "display": display,
            "record_count": len(rows), "records": rows[:_TIMELINE_RECORDS], "more": max(0, len(rows) - _TIMELINE_RECORDS),
            "children": self._timeline_buckets(info, low, high, finer) if finer else [], "child_granularity": finer,
            "scope": {"branch": objective_branch, "filters": self._monitor_filters_label(), "timezone": info["timezone"],
                      "window": "%s–%s" % (_time_label(self.window_start), _time_label(self.window_end)) if self._monitor_source() == "pos" else ""},
        }

    def _units_value(self, units):
        """(number or None, display) of quantities by unit: one unit gives a number; different units are listed, never added."""
        if not units:
            return 0.0, ("0 %s" % (self.product_id.uom_id.name or "")).strip()
        if len(units) > 1:
            return None, " · ".join("%g %s" % (round(qty, 4), unit) for unit, qty in sorted(units.items()))
        (unit, qty), = units.items()
        return qty, "%g %s" % (round(qty, 4), unit)

    def _purchase_value(self, metric, facts):
        """(number or None, display) of a purchasing flow metric over order-line or receipt facts."""
        if metric == "value":
            total = sum(fact["amount"] for fact in facts)
            return total, self._money(total)
        if metric == "unit_price":
            # Weighted by quantity: total paid / total quantity, both in the product's base unit and company currency.
            priced = [fact for fact in facts if fact["quantity"] and fact["quantity"] > 0]
            quantity = sum(fact["quantity"] for fact in priced)
            if quantity <= 1e-9:
                return None, "No purchases"
            price = sum(fact["amount"] for fact in priced) / quantity
            return price, "%s/%s" % (self._money(price), priced[0]["unit"])
        units = {}
        for fact in facts:
            if fact["quantity"] is not None:
                units[fact["unit"]] = units.get(fact["unit"], 0.0) + fact["quantity"]
        return self._units_value(units)

    def _delivery_value(self, metric, facts):
        """(number or None, display) of the on-time rate or average delay; None when there is no single value."""
        values = on_time_rates(facts) if metric == "on_time" else average_delays(facts)
        if not values:
            return None, "No deliveries due" if not facts else "Nothing received yet"

        def show(value):
            if metric == "on_time":
                return "%.1f%%" % value
            if abs(value) < 0.05:
                return "0.0 days (on time)"
            return "%+.1f days (%s)" % (value, "late" if value > 0 else "early")

        if len(values) == 1:
            (_unit, value), = values.items()
            return value, show(value)
        # Different units are never weighted against each other.
        return None, " · ".join("%s: %s" % (unit, show(value)) for unit, value in sorted(values.items()))

    def _supplier_delivery_monitor(self, view, objective, metric, date_from, date_to, warehouses, filters, compare_branches):
        """On-time delivery rate or average delivery delay of the order lines due in the period (supplier_delivery)."""
        company = self.company_id
        facts, excluded, problem = delivery_performance_facts(self.env, company, date_from, date_to, warehouses, *filters)
        if problem:
            view.update(status="data_problem", note=problem)
            return view
        value_of = lambda group: self._delivery_value(metric, group)

        def units(key, group):
            totals = {}
            for fact in group:
                totals[fact["unit"]] = totals.get(fact["unit"], 0.0) + fact[key]
            return self._units_value({unit: qty for unit, qty in totals.items()})[1] if totals else "0"

        value, view["value_display"] = value_of(facts)
        not_judged = ""
        if excluded:
            not_judged = " %d order line%s due in this period %s not judged: %s." % (
                len(excluded), "" if len(excluded) == 1 else "s", "is" if len(excluded) == 1 else "are",
                "; ".join("%s %s (%s)" % (item["order"], item["product"], item["reason"]) for item in excluded[:5]))
        if not facts:
            view.update(status="empty", note="No order line was due in this period, or its due day has not passed yet, so there "
                                             "is no delivery record to judge (this is not 0%%).%s" % not_judged)
            return view
        view["cards"] = [
            {"key": "on_time", "label": "Delivered on time", "value_display": self._delivery_value("on_time", facts)[1]},
            {"key": "delay", "label": "Average delivery delay", "value_display": self._delivery_value("delay", facts)[1]},
            {"key": "due", "label": "Quantity due", "value_display": units("due", facts)},
            {"key": "undelivered", "label": "Due but not delivered", "value_display": units("undelivered", facts)},
            {"key": "lines", "label": "Order lines judged", "value_display": str(len(facts))},
        ]
        if excluded:
            view["cards"].append({"key": "excluded", "label": "Order lines not judged", "value_display": str(len(excluded))})
        for card in view["cards"]:
            card["primary"] = card["key"] == metric
        view["note"] = (
            "Quantity received by the promised day / quantity due x 100, judged per shipment: a partial delivery counts "
            "on time only for what arrived by the promised day, and quantity not delivered yet counts as not on time."
            if metric == "on_time" else
            "Days between the promised day and each receipt, weighted by the quantity received (negative = early). "
            "Quantity not delivered yet has no delay and is shown under 'Due but not delivered'."
        ) + " Promised day = the promised date recorded when the order was confirmed." + not_judged
        days = {}
        for fact in facts:
            days.setdefault(fact["expected"], []).append(fact)
        view["trend"] = _bar_rows([(str(day), "Due %s" % _human_date(day), *value_of(group)) for day, group in sorted(days.items())])

        def grouped(key):
            buckets = {}
            for fact in facts:
                buckets.setdefault(fact[key], []).append(fact)
            values = [(name, name, *value_of(group)) for name, group in buckets.items()]
            values.sort(key=lambda item: (item[2] is None, item[2] if metric == "delay" else -(item[2] or 0.0), item[1]))
            return _bar_rows(values)

        view["blocks"] = [{"title": "By promised day", "rows": view["trend"]}, {"title": "By supplier", "rows": grouped("supplier")},
                          {"title": "By product", "rows": grouped("product")}]
        if compare_branches:
            view["blocks"].append({"title": "By branch", "rows": grouped("branch")})
        rows = []
        for fact in sorted(facts, key=lambda f: (f["expected"], f["order"].name)):
            shipments = ", ".join("%g %s on %s (%+d d)" % (round(qty, 4), fact["unit"], _human_date(day), days_late)
                                  for day, qty, days_late in fact["receipts"]) or "nothing received"
            undelivered = " · %g %s not delivered" % (round(fact["undelivered"], 4), fact["unit"]) if fact["undelivered"] > 1e-9 else ""
            rows.append({"key": str(fact["line"].id), "cells": [
                fact["order"].name, "%s · %s · due %s" % (fact["supplier"], fact["product"], _human_date(fact["expected"])),
                "%g of %g %s on time · %s%s" % (round(fact["on_time"], 4), round(fact["due"], 4), fact["unit"], shipments, undelivered)]})
        for index, item in enumerate(excluded):
            rows.append({"key": "x%d" % index, "cells": [item["order"], item["product"], "Not judged: %s" % item["reason"]]})
        view["table"] = {"headers": ["Purchase order", "Supplier · product · promised day", "Deliveries"], "rows": rows[:60]}
        previous_to = date_from - timedelta(days=1)
        previous_from = previous_to - (date_to - date_from)
        previous, _excluded, problem = delivery_performance_facts(self.env, company, previous_from, previous_to, warehouses, *filters)
        previous_value, previous_display = value_of(previous) if not problem else (None, "Unavailable")
        change = ""
        if value is not None and previous_value is not None:
            change = ("%+.1f percentage points" if metric == "on_time" else "%+.1f days") % (value - previous_value) + \
                     " compared with the previous period"
        view["previous"] = {"label": "%s – %s" % (_human_date(previous_from), _human_date(previous_to)),
                            "display": previous_display, "change": change}
        return view

    def _price_comparison_monitor(self, view, objective, start_utc, end_utc, warehouses):
        """Weighted-average price actually paid for one product, per supplier (purchase_line_facts): not a ranking."""
        company, product = self.company_id, self.product_id
        _lines, facts, problems = purchase_line_facts(self.env, company, start_utc, end_utc, warehouses, self.partner_id, product)
        if problems:
            view.update(status="data_problem", note="Prices cannot be compared reliably: %s." % "; ".join(problems[:10]))
            return view
        priced = [fact for fact in facts if fact["quantity"] and fact["quantity"] > 0]
        if not priced:
            view.update(status="empty", note="Nothing was bought of %s in this period, so there are no prices to compare." % product.display_name)
            return view
        unit, currency = product.uom_id.name, company.currency_id.name
        suppliers = {}
        for fact in priced:
            suppliers.setdefault(fact["supplier"], []).append(fact)
        rows, prices = [], []
        for name, group in suppliers.items():
            quantity = sum(fact["quantity"] for fact in group)
            price = sum(fact["amount"] for fact in group) / quantity
            line_prices = [fact["amount"] / fact["quantity"] for fact in group]
            original = sorted({fact["order"].currency_id.name for fact in group})
            prices.append(price)
            rows.append((price, name, quantity, len(group), min(line_prices), max(line_prices), original))
        rows.sort(key=lambda row: (row[0], row[1]))
        per_unit = lambda amount: "%s/%s" % (self._money(amount), unit)
        lowest, highest = min(prices), max(prices)
        overall = sum(fact["amount"] for fact in priced) / sum(fact["quantity"] for fact in priced)
        view["value_display"] = (per_unit(lowest) if len(rows) == 1 else
                                 "%s – %s" % (per_unit(lowest), per_unit(highest)))
        view["cards"] = [
            {"key": "suppliers", "label": "Suppliers compared", "value_display": str(len(rows)), "primary": False},
            {"key": "lowest", "label": "Lowest supplier price", "value_display": per_unit(lowest), "primary": True},
            {"key": "highest", "label": "Highest supplier price", "value_display": per_unit(highest), "primary": True},
            {"key": "difference", "label": "Difference", "primary": False, "value_display": "%s (%.1f%%)" % (
                per_unit(highest - lowest), (highest - lowest) / lowest * 100.0 if lowest else 0.0)},
            {"key": "overall", "label": "Average price paid, all suppliers", "value_display": per_unit(overall), "primary": False},
        ]
        view["blocks"] = [{"title": "Average price paid per supplier", "rows": _bar_rows(
            [(row[1], row[1], row[0], per_unit(row[0])) for row in rows])}]
        view["table"] = {"headers": ["Supplier", "Average price paid", "Quantity · purchases", "Lowest – highest order price", "Order currency"],
                         "rows": [{"key": row[1], "cells": [row[1], per_unit(row[0]), "%g %s · %d" % (round(row[2], 4), unit, row[3]),
                                                            "%s – %s" % (per_unit(row[4]), per_unit(row[5])), ", ".join(row[6])]}
                                  for row in rows]}
        view["note"] = ("Price actually paid on confirmed orders for %s: line amounts excluding tax, after discounts, converted to %s "
                        "at each order's confirmation date, divided by the quantity in %s. Not the suppliers' price lists. Price "
                        "is one measure on its own, not a supplier ranking: compare delivery performance separately."
                        % (product.display_name, currency, unit))
        return view

    def _inventory_monitor(self, view, objective):
        """Inventory monitoring: Odoo's current stock, a snapshot now (never a balance for a past period)."""
        metric = self._measure()["monitor"]
        company = self.company_id
        warehouses = objective.warehouse_id if objective.scope == "branch" else \
            self.env["stock.warehouse"].sudo().search([("company_id", "=", company.id)])
        if not warehouses:
            view.update(status="data_problem", note="No branch warehouse is configured, so stock cannot be shown per branch.")
            return view
        timezone = pytz.timezone(company.partner_id.tz or "UTC")
        now = pytz.UTC.localize(fields.Datetime.now()).astimezone(timezone)
        view["period_label"] = "Current snapshot, %s" % now.strftime("%d %b %Y %H:%M")
        filters = (self.product_id, self.product_category_id)
        rows = stock_snapshot(self.env, company, warehouses, *filters)
        pos_rows = unfinished_pos_deliveries(self.env, company, warehouses, *filters)
        compare_branches = self._is_head_office() and objective.scope == "organization"
        quantity = lambda value, unit: "%g %s" % (round(value, 4) + 0.0, unit)

        def units(key, subset):
            totals = {}
            for row in subset:
                totals[row["unit"]] = totals.get(row["unit"], 0.0) + row[key]
            return self._units_value(totals)

        if not rows:
            view.update(status="empty", note="No storable product matches these filters, so there is no stock to show.")
            return view
        view["warnings"] = [note for note in [renamed_unit_note(self.env, self.env["product.product"].union(*[r["product"] for r in rows]))] if note]
        held = {}
        for row in pos_rows:
            held.setdefault(row["delivery"], row)
        if pos_rows and metric in ("on_hand", "available", "forecast", "pos_reserved"):
            deliveries = "%d deliver%s (%s)" % (len(held), "y" if len(held) == 1 else "ies", ", ".join(list(held)[:5]))
            if any(row["reserved"] > 1e-9 for row in pos_rows):
                view["warnings"].insert(0, "Stock may be reserved by unfinished POS deliveries: %s still hold %s reserved, which "
                                        "lowers available stock until they are completed or cancelled." % (deliveries, units("reserved", pos_rows)[1]))
            else:
                view["warnings"].insert(0, "Unfinished POS deliveries: %s reserve nothing now, but %s still to take out of stock "
                                        "lowers forecast stock until they are completed or cancelled." % (deliveries, units("to_deliver", pos_rows)[1]))
            if metric == "on_hand":
                view["warnings"][0] += " On-hand stock is not reduced until they are completed."
        snapshot = "Current snapshot: Odoo's stock now, not a balance for a past period. "
        key = {"on_hand": "on_hand", "available": "available", "forecast": "forecast"}.get(metric)
        if key:
            value, view["value_display"] = units(key, rows)
            negatives = [row for row in rows if row["on_hand"] < -1e-9]
            labels = {"on_hand": "On hand", "available": "Available", "forecast": "Forecast"}
            view["cards"] = [{"key": key, "label": labels[key], "value_display": view["value_display"], "primary": True},
                             {"key": "products", "label": "Products monitored", "value_display": str(len({r["product"].id for r in rows})), "primary": False},
                             {"key": "negative", "label": "Negative balances", "value_display": str(len(negatives)), "primary": False},
                             {"key": "pos", "label": "Unfinished POS deliveries", "value_display": str(len(held)), "primary": False}]
            columns = {
                "on_hand": (["Product", "Branch", "On hand"], lambda r: [quantity(r["on_hand"], r["unit"]) + (" (negative)" if r["on_hand"] < -1e-9 else "")]),
                "available": (["Product", "Branch", "On hand", "Reserved", "Available"],
                              lambda r: [quantity(r["on_hand"], r["unit"]), quantity(r["reserved"], r["unit"]), quantity(r["available"], r["unit"])]),
                "forecast": (["Product", "Branch", "On hand", "Incoming", "Outgoing", "Forecast"],
                             lambda r: [quantity(r["on_hand"], r["unit"]), quantity(r["incoming"], r["unit"]), quantity(r["outgoing"], r["unit"]),
                                        quantity(r["forecast"], r["unit"])]),
            }[key]
            view["table"] = {"headers": columns[0], "rows": [{"key": "%s-%s" % (r["warehouse"].id, r["product"].id),
                                                               "cells": [r["name"], r["branch"]] + columns[1](r)} for r in rows[:150]]}
            view["note"] = snapshot + {
                "on_hand": "Products with no stock record in a branch are listed at 0.",
                "available": "Available = on hand minus quantities reserved for pending deliveries; it is not physical stock.",
                "forecast": "Forecast = on hand plus incoming minus outgoing pending moves, whatever their date. It is not a "
                            "demand forecast, and unfinished deliveries lower it.",
            }[key]
            if compare_branches:
                by_branch = {}
                for row in rows:
                    by_branch.setdefault(row["branch"], []).append(row)
                view["blocks"] = [{"title": "By branch", "rows": _bar_rows([(name, name, *units(key, group)) for name, group in by_branch.items()])}]
            return view
        if metric == "below_minimum":
            rules = minimum_stock_rules(self.env, company, warehouses, *filters)
            without_rule = len({(r["warehouse"].id, r["product"].id) for r in rows} - {(r["warehouse"].id, r["product"].id) for r in rules})
            if not rules:
                view.update(status="empty", note=snapshot + "No minimum-stock (reordering) rule exists for these products and branches, "
                            "so nothing can be judged below minimum; this does not mean stock is sufficient. Rules are set in "
                            "Inventory > Configuration > Reordering Rules.")
                return view
            below = [rule for rule in rules if rule["below"]]
            view["value_display"] = "%d below minimum" % len(below) if below else "None below minimum"
            view["cards"] = [{"key": "below", "label": "Below minimum", "value_display": str(len(below)), "primary": True},
                             {"key": "rules", "label": "Rules checked", "value_display": str(len(rules)), "primary": False},
                             {"key": "without", "label": "Products without a rule (not judged)", "value_display": str(without_rule), "primary": False}]
            view["table"] = {"headers": ["Product", "Branch · location", "On hand", "Forecast", "Minimum", "Shortfall"],
                             "rows": [{"key": str(r["rule"].id), "cells": [r["name"], "%s · %s" % (r["branch"], r["location"]),
                                                                           quantity(r["on_hand"], r["unit"]), quantity(r["forecast"], r["unit"]),
                                                                           quantity(r["minimum"], r["unit"]), quantity(r["shortfall"], r["unit"])]}
                                      for r in below]}
            view["note"] = snapshot + ("Odoo's reordering-rule test: the rule's forecast (within its lead time, including draft "
                                       "purchase orders) is under its minimum. Shortfall = minimum - forecast. %d of %d rules are at "
                                       "or above their minimum; %d monitored products have no rule and are not judged."
                                       % (len(rules) - len(below), len(rules), without_rule))
            return view
        if metric == "negative":
            negatives = [row for row in rows if row["on_hand"] < -1e-9]
            view["value_display"] = "%d negative balance%s" % (len(negatives), "" if len(negatives) == 1 else "s") if negatives else "No negative stock"
            view["cards"] = [{"key": "negative", "label": "Negative balances", "value_display": str(len(negatives)), "primary": True},
                             {"key": "checked", "label": "Product-branch balances checked", "value_display": str(len(rows)), "primary": False}]
            view["table"] = {"headers": ["Product", "Branch", "On hand", "Warning"],
                             "rows": [{"key": "%s-%s" % (r["warehouse"].id, r["product"].id), "cells": [
                                 r["name"], r["branch"], quantity(r["on_hand"], r["unit"]),
                                 "More stock left than Odoo recorded arriving; investigate. Not adjusted automatically."]} for r in negatives]}
            view["note"] = snapshot + ("Negative recorded stock usually means stock was moved out (sold, scrapped or delivered) "
                                       "beyond what Odoo had recorded; it does not by itself show the cause." if negatives else
                                       "No product has negative recorded stock in these branches.")
            return view
        # Unfinished POS deliveries
        view["value_display"] = "%d unfinished POS deliver%s" % (len(held), "y" if len(held) == 1 else "ies") if held else "No unfinished POS deliveries"
        view["cards"] = [{"key": "deliveries", "label": "Unfinished POS deliveries", "value_display": str(len(held)), "primary": True},
                         {"key": "reserved", "label": "Reserved by them", "value_display": units("reserved", pos_rows)[1] if pos_rows else "0", "primary": False},
                         {"key": "to_deliver", "label": "Still to take out of stock", "value_display": units("to_deliver", pos_rows)[1] if pos_rows else "0",
                          "primary": False}]
        view["table"] = {"headers": ["Branch", "Product", "Reserved", "Still to deliver", "Delivery · state", "POS order"],
                         "rows": [{"key": str(r["move"].id), "cells": [r["branch"], r["name"], quantity(r["reserved"], r["unit"]),
                                                                      quantity(r["to_deliver"], r["unit"]),
                                                                      "%s · %s%s" % (r["delivery"], r["state"], " · %s" % r["hint"] if r["hint"] else ""),
                                                                      r["order"]]} for r in pos_rows[:150]]}
        view["note"] = snapshot + ("POS sales whose stock delivery is not completed. Until each is completed or cancelled in "
                                   "Inventory, its stock still counts as on hand, its reserved quantity lowers available stock and "
                                   "its full quantity lowers forecast stock. Some may simply be waiting; nothing is changed here."
                                   if pos_rows else "Every POS sale in these branches has completed its stock delivery.")
        return view

    def _purchase_monitor(self, view, objective, date_from, date_to):
        """Purchasing monitoring. Each figure uses its own business date: confirmation for order value, quantities
        ordered and prices; receipt completion for quantities received; the position at the end of the period (or now)
        for orders awaiting delivery and overdue orders."""
        metric = self._measure()["monitor"]
        company = self.company_id
        warehouses = objective.warehouse_id if objective.scope == "branch" else \
            self.env["stock.warehouse"].sudo().search([("company_id", "=", company.id)])
        start_utc, end_utc, problem = period_bounds_utc(company, date_from, date_to)
        if not problem and not warehouses:
            problem = "No branch warehouse is configured, so purchase orders cannot be assigned to a branch."
        if problem:
            view.update(status="data_problem", note=problem)
            return view
        timezone = pytz.timezone(company.partner_id.tz)

        def day(value):
            return pytz.UTC.localize(fields.Datetime.to_datetime(value)).astimezone(timezone).date()

        filters = (self.partner_id, self.product_id, self.product_category_id)
        compare_branches = self._is_head_office() and objective.scope == "organization"

        def grouped(facts, key, value_of):
            buckets = {}
            for fact in facts:
                buckets.setdefault(fact[key], []).append(fact)
            values = [(name, name, *value_of(group)) for name, group in buckets.items()]
            values.sort(key=lambda item: (item[2] is None, -(item[2] or 0.0), item[1]))
            return _bar_rows(values)

        def orders_of(items):
            return len({item["order"].id for item in items})

        if metric in ("on_time", "delay"):
            return self._supplier_delivery_monitor(view, objective, metric, date_from, date_to, warehouses, filters, compare_branches)
        if metric == "price_comparison":
            return self._price_comparison_monitor(view, objective, start_utc, end_utc, warehouses)

        if metric in ("outstanding", "overdue"):
            now = fields.Datetime.to_datetime(fields.Datetime.now())
            as_of = min(end_utc, now)
            _lines, items, problems = open_purchase_lines(self.env, company, as_of, warehouses, *filters)
            if problems:
                view.update(status="data_problem", note="Orders awaiting delivery cannot be measured reliably: %s." % "; ".join(problems[:10]))
                return view
            # Overdue: the expected arrival day (company time zone) has fully passed and goods are still missing.
            as_of_day = day(as_of)
            overdue = [item for item in items if item["expected"] and day(item["expected"]) < as_of_day]
            chosen = items if metric == "outstanding" else overdue
            count = orders_of(chosen)
            view["value_display"] = "%d order%s" % (count, "" if count == 1 else "s")
            view["cards"] = [
                {"key": "open", "label": "Orders awaiting delivery", "value_display": str(orders_of(items)), "primary": metric == "outstanding"},
                {"key": "open_value", "label": "Value awaiting delivery", "value_display": self._money(sum(i["amount"] for i in items)), "primary": False},
                {"key": "overdue", "label": "Overdue orders", "value_display": str(orders_of(overdue)), "primary": metric == "overdue"},
                {"key": "overdue_value", "label": "Overdue value", "value_display": self._money(sum(i["amount"] for i in overdue)), "primary": False},
                {"key": "remaining", "label": "Quantity still to receive", "value_display": self._purchase_value(
                    "quantity", [dict(item, quantity=item["remaining"]) for item in chosen])[1], "primary": False},
            ]
            position = "now" if as_of == now else "at the end of %s" % _human_date(date_to)
            if not chosen:
                view["note"] = ("Nothing is waiting for delivery %s." if metric == "outstanding" else "No order is overdue %s.") % position
            else:
                view["note"] = ("Position %s: goods on confirmed orders not yet received in full (a partial receipt leaves the "
                                "rest open). An order is overdue only when its expected arrival day has passed; awaiting "
                                "delivery is not the same as late." % position)
            money_of = lambda group: (sum(i["amount"] for i in group), self._money(sum(i["amount"] for i in group)))
            view["blocks"] = [{"title": "By supplier", "rows": grouped(chosen, "supplier", money_of)}]
            if compare_branches:
                view["blocks"].append({"title": "By branch", "rows": grouped(chosen, "branch", money_of)})
            by_order = {}
            for item in chosen:
                row = by_order.setdefault(item["order"].id, {"order": item["order"], "expected": item["expected"], "amount": 0.0})
                row["amount"] += item["amount"]
                if item["expected"] and (not row["expected"] or item["expected"] < row["expected"]):
                    row["expected"] = item["expected"]
            rows = []
            for row in sorted(by_order.values(), key=lambda r: (r["expected"] or fields.Datetime.now(), r["order"].name)):
                order, expected = row["order"], row["expected"]
                late = (as_of_day - day(expected)).days if expected and day(expected) < as_of_day else 0
                detail = "%s · %s · expected %s%s" % (order.partner_id.display_name or "No supplier",
                                                      order.picking_type_id.warehouse_id.display_name or "",
                                                      _human_date(day(expected)) if expected else "(no date)",
                                                      " · %d days late" % late if late else "")
                rows.append({"key": str(order.id), "cells": [order.name, detail, self._money(row["amount"])]})
            view["table"] = {"headers": ["Purchase order", "Supplier · branch · expected arrival", "Value awaiting delivery"], "rows": rows[:50]}
            return view

        def facts_for(start, end):
            if metric == "received":
                return received_move_facts(self.env, company, start, end, warehouses, *filters)[1], []
            _lines, facts, problems = purchase_line_facts(self.env, company, start, end, warehouses, *filters)
            return facts, problems

        facts, problems = facts_for(start_utc, end_utc)
        if problems:
            view.update(status="data_problem", note="Purchasing cannot be measured reliably: %s." % "; ".join(problems[:10]))
            return view
        value_of = lambda group: self._purchase_value(metric, group)
        value, view["value_display"] = value_of(facts)
        if metric == "received":
            ordered = purchase_line_facts(self.env, company, start_utc, end_utc, warehouses, *filters)[1]
            view["cards"] = [
                {"key": "received", "label": "Received (net of returns)", "value_display": view["value_display"], "primary": True},
                {"key": "returned", "label": "Returned to suppliers", "primary": False, "value_display": self._purchase_value(
                    "quantity", [dict(f, quantity=-f["quantity"]) for f in facts if f["quantity"] < 0])[1]},
                {"key": "receipts", "label": "Receipts", "primary": False,
                 "value_display": str(len({f["move"].picking_id.id for f in facts if f["quantity"] > 0}))},
                {"key": "ordered", "label": "Ordered in this period", "primary": False,
                 "value_display": self._purchase_value("ordered", ordered)[1]},
            ]
        else:
            cards = [
                ("value", "Confirmed order value", self._purchase_value("value", facts)[1]),
                ("ordered", "Quantity ordered", self._purchase_value("ordered", facts)[1]),
                ("orders", "Purchase orders", str(orders_of(facts))),
                ("suppliers", "Suppliers", str(len({f["supplier"] for f in facts}))),
            ]
            if metric == "ordered":
                received = received_move_facts(self.env, company, start_utc, end_utc, warehouses, *filters)[1]
                cards.append(("received", "Received in this period (net)", self._purchase_value("received", received)[1]))
            if metric == "unit_price":
                prices = [f["amount"] / f["quantity"] for f in facts if f["quantity"] and f["quantity"] > 0]
                cards = [("unit_price", "Average price paid", view["value_display"])] + cards[:1] + cards[1:3]
                if prices:
                    cards.append(("range", "Lowest – highest order price", "%s – %s" % (self._money(min(prices)), self._money(max(prices)))))
            view["cards"] = [{"key": key, "label": label, "value_display": display, "primary": key == metric}
                             for key, label, display in cards]
        empty = {
            "value": "No purchase orders were confirmed in this period%s, so the value is zero.",
            "ordered": "No purchase orders were confirmed in this period%s, so nothing was ordered.",
            "received": "Nothing was received from suppliers in this period%s.",
            "unit_price": "Nothing was bought in this period%s, so there is no price.",
        }[metric]
        filtered = "" if view["filters_label"] == "All products" else " for %s" % view["filters_label"]
        if not facts:
            if metric == "unit_price":
                view.update(status="empty", note=empty % filtered)
                return view
            view["note"] = empty % filtered
        elif metric == "unit_price":
            view["note"] = ("The price actually paid on confirmed orders (quantity-weighted, excluding tax), not the "
                            "supplier's current price list.")
        else:
            view["note"] = "Recalculated from Odoo's purchase orders and receipts each time this goal is opened."
        days = {}
        for fact in facts:
            days.setdefault(day(fact["date"]), []).append(fact)
        view["trend"] = _bar_rows([(str(date), _human_date(date), *value_of(days.get(date, [])))
                                   for date in (date_from + timedelta(days=n) for n in range((date_to - date_from).days + 1))])
        group_value = value_of
        if metric == "value" and value:
            # Supplier, category and branch rows also show their share of the confirmed value.
            def group_value(group):
                amount = sum(fact["amount"] for fact in group)
                return amount, "%s · %.1f%%" % (self._money(amount), amount / value * 100.0)
        view["blocks"] = [{"title": "Trend by day", "rows": view["trend"]},
                          {"title": "By supplier", "rows": grouped(facts, "supplier", group_value)}]
        if metric != "unit_price":
            view["blocks"].append({"title": "By product category", "rows": grouped(facts, "category", group_value)})
        if compare_branches:
            view["blocks"].append({"title": "By branch", "rows": grouped(facts, "branch", group_value)})
        products = {}
        for fact in facts:
            item = products.setdefault(fact["product"], {"facts": []})
            item["facts"].append(fact)
        table_rows = []
        for name, item in sorted(products.items(), key=lambda pair: -abs(sum(f["quantity"] or 0.0 for f in pair[1]["facts"]))):
            quantity = self._purchase_value("ordered", item["facts"])[1]
            amount = self._purchase_value("value", item["facts"])[1] if metric != "received" else "—"
            table_rows.append({"key": name, "cells": [name, quantity, amount]})
        view["table"] = {"headers": ["Product", "Quantity received (net)" if metric == "received" else "Quantity ordered",
                                     "Value excl. tax"], "rows": table_rows[:20]}
        # Previous equivalent period: same length and filters, immediately before.
        previous_to = date_from - timedelta(days=1)
        previous_from = previous_to - (date_to - date_from)
        previous_start, previous_end, _problem = period_bounds_utc(company, previous_from, previous_to)
        previous_facts, previous_problems = facts_for(previous_start, previous_end)
        label = "%s – %s" % (_human_date(previous_from), _human_date(previous_to))
        if previous_problems:
            view["previous"] = {"label": label, "display": "Unavailable", "change": "; ".join(previous_problems[:3])}
        else:
            previous_value, previous_display = value_of(previous_facts)
            change = ""
            if value is not None and previous_value:
                change = "%+.1f%% compared with the previous period" % ((value - previous_value) / abs(previous_value) * 100.0)
            view["previous"] = {"label": label, "display": previous_display, "change": change}
        return view

    def _monitor_readiness(self):
        """Readiness of a monitoring goal: settings, apps, calculation, and the live POS data."""
        checks = []

        def add(key, label, ok, message):
            checks.append({"key": key, "label": label, "ok": bool(ok), "message": message})

        objective = self.objective_id
        measure = self._measure()
        problems = []
        pos = self._monitor_source() == "pos"
        if measure["monitor"] in ("unit_price", "price_comparison") and not self.product_id:
            problems.append("choose the product whose purchase price is shown")
        if pos and not 0.0 <= self.window_start < self.window_end <= 24.0:
            problems.append("choose a daily time window that starts before it ends on the same day, e.g. 17:00 to 21:00 "
                            "(windows across midnight, such as 22:00 to 02:00, are not supported yet)")
        if measure["monitor"] == "aov" and (self.product_id or self.pos_category_id):
            problems.append("average order value covers whole orders, so remove the product and category filters")
        snapshot = self._monitor_source() == "inventory"
        if not self.period_preset and not snapshot:
            problems.append("choose the reporting period")
        elif self.period_preset == "custom" and not snapshot:
            if not self.period_start or not self.period_end:
                problems.append("choose both dates of the reporting period")
            elif self.period_start > self.period_end:
                problems.append("the period must start before it ends")
            elif objective.deadline and self.period_end > objective.deadline:
                problems.append("the period must end on or before the goal deadline (%s)" % _human_date(objective.deadline))
        period = "current snapshot" if snapshot else dict(_PERIOD_PRESETS).get(self.period_preset, "")
        if self.period_preset == "custom" and self.period_start and self.period_end and not snapshot:
            period = "%s – %s" % (_human_date(self.period_start), _human_date(self.period_end))
        summary = "%s, %s%s, %s." % (measure["label"], "%s–%s each day, " % (_time_label(self.window_start), _time_label(
            self.window_end)) if pos else "", period or "no period", self._monitor_filters_label())
        add("setup", "Measurement chosen", not problems, summary if not problems else "%s To finish: %s." % (summary, "; ".join(problems)))
        if problems:
            return checks
        add("app", "Odoo apps installed", *self._apps_check(measure))
        add("formula", "Approved calculation", True,
            "Monitoring: values and trends without a target, so the goal is never marked achieved or failed.")
        view = self._monitor_view(objective)
        if view["status"] == "data_problem":
            add("data", "Odoo data available", False, view["note"])
        elif view["status"] == "ok":
            add("data", "Odoo data available", True, "%s%s: %s." % (view["period_label"], ", %s" % view["window_label"] if pos else "",
                                                                  view["value_display"]))
        else:
            add("data", "Data source checked", True, "Data source checked — %s" % view["note"])
        return checks

    def _configure_monitoring(self, template, values):
        metric = values.get("measure_key")
        if metric not in template["metrics"]:
            raise UserError("Choose what to monitor: %s." % ", ".join(MEASURES[key]["label"] for key in template["metrics"]))
        snapshot = template.get("monitor_source") == "inventory"
        preset = False if snapshot else values.get("period_preset")
        if not snapshot and preset not in dict(_PERIOD_PRESETS):
            raise UserError("Choose the reporting period: today, this week, this month or custom dates.")
        pos = template.get("monitor_source") == "pos"
        category = self.env["pos.category"].sudo().browse(int(values.get("pos_category_id") or 0)).exists() if pos else False
        supplier = self.env["res.partner"].browse(int(values.get("partner_id") or 0)).exists() if template.get("monitor_source") == "purchase" else False
        product_category = self.env["product.category"].browse(int(values.get("product_category_id") or 0)).exists() if not pos else False
        self.write({
            "partner_id": supplier.id if supplier else False,
            "product_category_id": product_category.id if product_category else False,
            "template_key": template["key"],
            "measure_key": metric,
            "product_id": int(values["product_id"]) if values.get("product_id") else False,
            "pos_category_id": category.id if category else False,
            "target_value": 0.0,
            "period_preset": preset,
            "period_start": (values.get("period_start") or False) if preset == "custom" else False,
            "period_end": (values.get("period_end") or False) if preset == "custom" else False,
            "window_start": _parse_time(values.get("window_start")) if pos else 0.0,
            "window_end": _parse_time(values.get("window_end")) if pos else 0.0,
        })
        return self.check_readiness()

    def _apps_check(self, measure):
        apps = {app["id"]: app for app in self.env["strategic.performance.dashboard"].get_app_dependencies_data()["apps"]}
        needed = [apps.get(app_id, {"name": app_id}) for app_id in (self._template().get("apps") or [measure["app"]])]
        missing = [app["name"] for app in needed if not app.get("installed")]
        return not missing, ("%s installed." % ", ".join(app["name"] for app in needed) if not missing
                             else "%s not installed; this goal cannot be measured." % ", ".join(missing))

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

    # ------------------------------------------------------------------ read model

    @api.model
    def get_goals(self, area=False):
        """Goals visible to the user; the same records serve Business Goals and every area page."""
        objectives = self.env["strategic.objective"].search(
            [("company_id", "=", self.env.company.id), ("state", "!=", "archived")], order="deadline, id"
        )
        goals = []
        for objective in objectives:
            if area and area not in objective._goal_areas():
                continue
            setup = objective.goal_setup_ids[:1]
            goals.append(setup._goal_payload(objective) if setup else self._goal_payload(objective))
        return goals

    def _workflow_steps(self, objective, result):
        """Steps after setup, computed from the linked records (never stored, never part of the 'ready' decision):
        target and Key Result, approval, result. Each is ok, pending or a problem."""
        if not self or not self.measure_key:
            return []
        steps = []

        def step(key, label, state, message):
            steps.append({"key": key, "label": label, "ok": state == "ok", "state": state, "message": message})

        if self._is_monitoring():
            approved = objective.state in ("active", "completed", "closed")
            step("approval", "Goal approved", "ok" if approved else "pending", "Approved by Head Office." if approved else (
                "Waiting for Head Office approval." if objective.state == "pending_approval" else "Submit the goal for Head Office approval."))
            return steps

        kr, target = self.key_result_id, self.target_id
        if kr and target.state == "approved":
            step("linked", "Target and Key Result linked", "ok",
                 "Target %s approved; Key Result '%s' measures it with '%s'." % (self._target_label(self.target_value), kr.name, kr.primary_kpi_id.name))
        else:
            step("linked", "Target and Key Result linked", "pending", {
                "needs_setup": "Created once the setup checks pass and Head Office prepares and approves the target.",
                "ready": "Head Office prepares the target next.",
                "target_pending": "The target is waiting for Head Office approval; the Key Result is created then.",
            }.get(self.setup_state, "Not linked yet."))
            return steps
        state = objective.state
        if state == "active" or state in ("completed", "closed"):
            step("approval", "Goal approved", "ok", "Approved by Head Office.")
        elif state == "pending_approval":
            step("approval", "Goal approved", "pending", "Waiting for Head Office approval.")
        else:
            step("approval", "Goal approved", "pending", "Submit the goal for Head Office approval.")
        evaluation = kr.evaluation_id
        if evaluation and evaluation.status == "valid" and evaluation.has_actual_value:
            step("result", "Result evaluated", "ok", "Final result recorded for %s to %s." % (_human_date(evaluation.date_start), _human_date(evaluation.date_end)))
        elif result and result.get("value_display"):
            step("result", "Result evaluated", "pending", "%s so far; the final result is recorded after %s." % (
                result["value_display"], _human_date(result["period_end"])))
        elif result and result.get("status") == "Data unavailable":
            step("result", "Result evaluated", "fail", result.get("note") or "The data source returned a problem.")
        else:
            step("result", "Result evaluated", "pending", (result or {}).get("note") or "Waiting for data.")
        return steps

    def _goal_payload(self, objective):
        setup = self[:1]
        is_head = self._is_head_office()
        state = objective.state
        status, tone = self._status(objective, setup)
        key_results = objective.okr_objective_ids.key_result_ids.filtered("required")
        current, current_note = "Not measured", "No Key Result is linked to this goal yet."
        if setup and not key_results:
            # The Key Result is created when Head Office approves the target; say which step comes next.
            current_note = {
                "needs_setup": "No Key Result yet: choose what to measure and click 'Save and check readiness'.",
                "ready": "No Key Result yet: Head Office prepares the target next; the Key Result is created when it is approved.",
                "target_pending": "No Key Result yet: it is created when Head Office approves the target.",
            }.get(setup.setup_state, current_note)
        if len(key_results) == 1:
            evaluation = key_results.evaluation_id
            if evaluation and evaluation.status == "valid" and evaluation.has_actual_value:
                current, current_note = "%g %s" % (evaluation.actual_value, evaluation.unit or ""), "Evaluated %s to %s." % (evaluation.date_start, evaluation.date_end)
            else:
                current, current_note = "Not evaluated", (evaluation.reason if evaluation else "No evaluation has been recorded for the target period yet.")
        elif key_results:
            current, current_note = "%s Key Results" % len(key_results), "See the objective for each Key Result."
        target_display = False
        if setup and setup.measure_key and setup.target_value:
            target_display = setup._target_label(setup.target_value)
        elif len(key_results) == 1:
            target_display = "%g %s" % (key_results.target_id.target_value, key_results.unit or "")
        # Baseline: only the one Head Office recorded on the Key Result's target, in that target's unit.
        baseline = key_results.target_id.baseline_id if len(key_results) == 1 else False
        baseline_display = ("%g %s" % (baseline.baseline_value, key_results.unit or "")).strip() if baseline else False
        scored = objective.scoring_status not in ("not_evaluated", "data_quality_issue")
        result = setup._result_view(objective) if setup else False
        monitor = setup._monitor_view(objective) if setup else False
        if monitor and monitor["status"] == "ok":
            # Time breakdown for the card, only when the goal's own figure is valid.
            monitor["timeline"] = setup._monitor_timeline(objective)
        if monitor:
            # Monitoring: the live value, no target; never "on track", "at risk", achieved or failed.
            current = monitor["value_display"] or {"empty": {"pos": "No POS data", "purchase": "No purchases"}.get(setup._monitor_source(), "Nothing to show"),
                                                   "data_problem": "Data unavailable"}.get(
                monitor["status"], "Not shown")
            current_note = monitor["note"]
            target_display = "No target (monitoring)"
            scored = False
            if state == "active":
                status, tone = "Monitoring", "neutral"
        if result and result["status"]:
            # A template goal reports its value against the target; it is never shown as achieved before the period ends.
            if result["value_display"]:
                current = result["value_display"]
                current_note = "%s. %s" % (result["value_label"], result["note"])
            if state == "active":
                status, tone = result["status"], result["tone"]
            scored = False
        # Performance panels: 'measured' (ranked by progress), 'missed' (final result below target after the
        # period), or 'setup' (no valid measurement yet). Missing data is never ranked as 0% or counted as missed.
        today = fields.Date.context_today(self)
        bar, category, reason = False, "setup", ""
        if result and result["value"] is not False:
            bar = result["bar"]
            category = "missed" if result["final"] and result["tone"] == "bad" else "measured"
        elif result and result["bar"]:
            bar = result["bar"]
        elif not result and scored:
            # Objectives scored by Key Result attainment (100 = target met), higher is better.
            score = objective.score
            bar = {"kind": "attainment", "pct": round(score, 1), "bar": round(max(0.0, min(100.0, score)), 1),
                   "tone": attainment_tone(score), "margin": round(score - 100.0, 2), "label": "%g%% of the target reached" % round(score, 1)}
            missed = objective.scoring_status == "not_achieved" and objective.deadline and objective.deadline < today
            category = "missed" if missed else "measured"
        elif not result and setup and setup.key_result_id and setup.target_id.state == "approved":
            bar = _waiting_bar("Waiting for data")
        if category == "setup":
            failing = next((check["message"] for check in (json.loads(setup.readiness_json) if setup and setup.readiness_json else [])
                            if not check["ok"]), "")
            if state == "pending_approval":
                reason = "Waiting for Head Office approval."
            elif result and (result["note"] or result["status"]):
                reason = result["note"] or result["status"]
            elif setup and state == "draft":
                reason = failing or {
                    "needs_setup": "Choose how this goal will be measured, then check readiness.",
                    "ready": "Head Office prepares the target next.",
                    "target_pending": "Head Office must approve the target.",
                    "measured": "Submit the goal for approval.",
                }.get(setup.setup_state, "")
            else:
                reason = current_note
        if monitor:
            if state in ("active", "completed"):
                category, reason = "monitoring", ""
            elif state == "draft":
                reason = _MONITOR_NEXT_STEP.get(setup.setup_state, reason) if setup.setup_state != "needs_setup" else reason
        # Head Office's latest decision on this objective (the review log is read within the user's own access).
        review = objective.review_ids[:1]
        review_feedback = {
            "decision": review.decision, "status": dict(review._fields["decision"].selection)[review.decision],
            "reason": review.reason or "", "reviewer": review.reviewer_id.name,
            "decided_at": fields.Date.to_string(fields.Datetime.context_timestamp(self, review.decided_at).date()) if review.decided_at else False,
        } if review else False
        if state == "draft" and review.decision == "rejected":
            # A rejection returns the proposal to draft so its proposer can revise and resubmit it.
            status, tone = "Returned for changes", "warn"
            reason = "Returned by Head Office: %s" % review.reason if category == "setup" else reason
        return {
            "setup_id": setup.id or False,
            "objective_id": objective.id,
            "title": objective.name,
            "areas": objective._goal_areas(),
            "area_label": ", ".join(_AREA_LABELS[a] for a in objective._goal_areas()) or "No business area",
            "description": objective.description or "",
            "desired_result": setup.desired_result or "",
            "deadline": fields.Date.to_string(objective.deadline) if objective.deadline else False,
            "owner": objective.owner_id.name,
            "scope": "Entire business" if objective.scope == "organization" else (objective.warehouse_id.display_name or "Branch"),
            "plan": objective.plan_id.name,
            "scope_kind": objective.scope,
            "warehouse_id": objective.warehouse_id.id or False,
            "parent_objective": objective.parent_objective_id.display_name or False,
            "overdue": bool(objective.overdue),
            "baseline": baseline_display,
            "review_feedback": review_feedback,
            "state": state,
            "status": status,
            "tone": tone,
            "setup_state": setup.setup_state or False,
            "measure_key": setup.measure_key or False,
            "measure_label": (setup._template().get("title") or dict(self._fields["measure_key"].selection).get(setup.measure_key)) if setup.measure_key else False,
            "product": {"id": setup.product_id.id, "name": setup.product_id.display_name, "uom": setup.product_id.uom_id.name or ""} if setup.product_id else False,
            "target_value": setup.target_value if setup else False,
            "period_start": fields.Date.to_string(setup.period_start) if setup.period_start else False,
            "period_end": fields.Date.to_string(setup.period_end) if setup.period_end else False,
            "kpi": key_results.primary_kpi_id[:1].name or (setup.kpi_id.name if setup else False) or False,
            "target": target_display,
            "current": current,
            "current_note": current_note,
            "bar": bar,
            "margin": bar["margin"] if bar else False,
            "category": category,
            "reason": reason,
            "readiness": (json.loads(setup.readiness_json) if setup.readiness_json else []) + setup._workflow_steps(objective, result),
            "result": result,
            "okr": setup._okr_view(objective),
            "template_key": setup.template_key or False,
            "template_title": setup._template().get("title", False) if setup else False,
            "can_configure": bool(setup) and state == "draft" and setup.setup_state not in _LOCKED_STATES,
            "goal_type": objective.goal_type or "optimization",
            "goal_type_label": dict(GOAL_TYPES).get(objective.goal_type or "optimization"),
            "monitor": monitor,
            "next_step": _MONITOR_NEXT_STEP.get(setup.setup_state, False) if monitor else False,
            "period_preset": setup.period_preset or "" if monitor else "",
            "window_start": _time_label(setup.window_start) if monitor else "",
            "window_end": _time_label(setup.window_end) if monitor else "",
            "pos_category": {"id": setup.pos_category_id.id, "name": setup.pos_category_id.display_name} if setup.pos_category_id else False,
            "partner": {"id": setup.partner_id.id, "name": setup.partner_id.display_name} if setup.partner_id else False,
            "product_category": {"id": setup.product_category_id.id, "name": setup.product_category_id.display_name} if setup.product_category_id else False,
            "can_prepare_target": bool(setup) and is_head and setup.setup_state == "ready" and not monitor,
            "can_approve_target": bool(setup) and is_head and setup.setup_state == "target_pending",
            "can_submit": bool(setup) and state == "draft" and setup.setup_state == ("ready" if monitor else "measured"),
            "can_approve_goal": is_head and state == "pending_approval",
            "can_evaluate": bool(setup) and is_head and bool(setup.target_id) and setup.target_id.state == "approved"
                            and (not result or result["phase"] == "closed"),
        }

    @api.model
    def _status(self, objective, setup):
        """Owner-facing status derived only from server state; 'On track' requires a valid evaluation."""
        state = objective.state
        if state == "draft":
            return {
                "needs_setup": ("Needs setup", "warn"),
                "ready": ("Ready for target", "neutral"),
                "target_pending": ("Target awaiting approval", "neutral"),
                "measured": ("Ready to submit", "neutral"),
            }.get(setup.setup_state, ("Draft", "neutral"))
        if state == "pending_approval":
            return ("Pending approval", "neutral")
        if state == "active":
            return {
                "achieved": ("On track", "good"),
                "exceeded": ("On track", "good"),
                "almost_met": ("Needs attention", "warn"),
                "not_achieved": ("At risk", "bad"),
                "data_quality_issue": ("Data problem", "bad"),
            }.get(objective.scoring_status, ("Active · not evaluated yet", "neutral"))
        return ({"completed": "Completed", "closed": "Closed"}.get(state, state.title()), "good" if state == "completed" else "neutral")
