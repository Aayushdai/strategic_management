"""Business Goals that monitor without a target: the live view, its timeline and drill-down, and the
per-source monitors (sales, purchasing, supplier deliveries, prices, inventory). Part of strategic.goal.setup."""

from datetime import datetime, timedelta

import pytz

from odoo import fields, models
from odoo.exceptions import AccessError, UserError

from ..services.kpi_sources.inventory import (minimum_stock_rules, renamed_unit_note, stock_snapshot,
                                              unfinished_pos_deliveries)
from ..services.kpi_sources.pos import period_bounds_utc, window_sales
from ..services.kpi_sources.purchase_goals import open_purchase_lines, purchase_line_facts, received_move_facts
from ..services.kpi_sources.supplier_delivery import average_delays, delivery_performance_facts, on_time_rates
from .goal_setup import MEASURES, _PERIOD_PRESETS, _human_date, _time_label


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


class StrategicGoalSetupMonitoring(models.Model):
    _inherit = "strategic.goal.setup"

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
