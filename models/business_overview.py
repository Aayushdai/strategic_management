"""Business-facing overview data for the Strategic Management home screen.

This service reads existing Odoo business records. It deliberately does not
invent missing figures or infer branch-level accounting totals without a
verified accounting-to-branch mapping.
"""

from collections import defaultdict
from datetime import datetime, time, timedelta

import logging

import pytz
from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ..services.kpi_sources.inventory import waste_move_cost
from ..services.kpi_sources.pos import POS_SALE_STATES


_logger = logging.getLogger(__name__)

_INCOME_TYPES = {"income", "income_other"}
_EXPENSE_TYPES = {"expense", "expense_other", "expense_direct_cost", "expense_depreciation"}
_POS_STATES = POS_SALE_STATES  # Odoo 20 has no "invoiced" POS state; same population as the sales KPIs
# Upper bound for dashboard record lists; the UI pages them five rows at a time.
_LIST_LIMIT = 100


class StrategicPerformanceDashboardBusinessOverview(models.AbstractModel):
    _inherit = "strategic.performance.dashboard"

    @api.model
    def get_business_overview_data(self, date_start=False, date_end=False, warehouse_id=False, trend_granularity="month", top_product_limit=5):
        """Return business-facing figures, scoped to the current user's role.

        The method respects the requesting user's ACLs and record rules. Unavailable
        source data is returned as ``False`` so the UI can show "Not available".
        """
        self._strategic_role()  # Admin stops here.
        if trend_granularity not in ("day", "week", "month"):
            trend_granularity = "month"
        try:
            top_product_limit = int(top_product_limit)
        except (TypeError, ValueError):
            top_product_limit = 5
        if top_product_limit not in (5, 10):
            top_product_limit = 5

        start, end = self._resolve_period(date_start, date_end)
        company = self.env.company
        user = self.env.user
        is_head_office = user.has_group("strategic_management.group_strategic_head_office_manager")
        is_branch_manager = (user.has_group("strategic_management.group_strategic_branch_manager") and not is_head_office) or self._department_read_active()
        is_employee = not is_head_office and not is_branch_manager

        Warehouse = self.env["stock.warehouse"]
        all_warehouses = (self._overview_safe(lambda: Warehouse.search([("company_id", "=", company.id)], order="name, id")) or Warehouse.browse()) if is_head_office else Warehouse.browse()
        selected_warehouse = False
        if is_branch_manager:
            selected_warehouse = user.strategic_branch_warehouse_id
            if not selected_warehouse or (selected_warehouse.company_id and selected_warehouse.company_id != company):
                raise AccessError("Assign this Branch Manager to a warehouse in the active company.")
        elif is_head_office and warehouse_id:
            try:
                requested_id = int(warehouse_id)
            except (TypeError, ValueError):
                raise ValidationError("Choose a valid branch.")
            selected_warehouse = all_warehouses.filtered(lambda warehouse: warehouse.id == requested_id)[:1]
            if not selected_warehouse:
                raise AccessError("You cannot view the selected branch.")

        # Employees may open the app, but must not receive company or branch financials.
        if is_employee:
            return {
                "role": "employee",
                "scope_label": "Personal workspace",
                "period": {"start": fields.Date.to_string(start), "end": fields.Date.to_string(end)},
                "branch_options": [],
                "selected_warehouse_id": False,
                "sales": False,
                "net_profit": False,
                "expenses": False,
                "waste_cost": False,
                "financial_available": False,
                "sales_trend": [],
                "branch_sales": [],
                "expense_breakdown": [],
                "top_products": [],
                "alerts": [],
                "tasks": self._overview_tasks(end, own_only=True),
                "branch_performance": [],
                "open_issues": 0,
                "messages": ["Financial and branch-wide figures are available to managers. Your assigned follow-ups are shown here."],
            }

        if is_branch_manager:
            branch_options = [{"id": selected_warehouse.id, "name": selected_warehouse.display_name}]
        else:
            branch_options = [{"id": warehouse.id, "name": warehouse.display_name} for warehouse in all_warehouses]

        scope_warehouse_ids = [selected_warehouse.id] if selected_warehouse else [row["id"] for row in branch_options]
        PosConfig = self.env["pos.config"]
        config_domain = [("company_id", "=", company.id)]
        if scope_warehouse_ids:
            config_domain.append(("picking_type_id.warehouse_id", "in", scope_warehouse_ids))
        configs = self._overview_safe(lambda: PosConfig.search(config_domain))
        # Unreadable POS configuration means sales are unknown, not zero.
        pos_readable = configs is not False
        configs = configs or PosConfig.browse()
        config_ids = configs.ids

        start_utc, end_utc = self._overview_utc_bounds(start, end)
        order_domain = [
            ("company_id", "=", company.id),
            ("state", "in", _POS_STATES),
            ("date_order", ">=", fields.Datetime.to_string(start_utc)),
            ("date_order", "<", fields.Datetime.to_string(end_utc)),
        ]
        if config_ids:
            order_domain.append(("config_id", "in", config_ids))
        else:
            # Do not accidentally fall back to all company POS orders if no POS
            # configuration can be mapped to an authorized warehouse.
            order_domain.append(("config_id", "=", 0))

        PosOrder = self.env["pos.order"]
        order_groups = self._overview_safe(lambda: [
    {
        "config_id": (config, self.env["pos.config"].browse(config).display_name) if config else False,
        # Sales are reported excluding tax, like product, category and margin figures.
        "amount_total": (amount or 0.0) - (tax or 0.0),
        # Refund orders reduce net sales but are not customer orders.
        "__count": 0 if is_refund else count,
    }
    for config, is_refund, amount, tax, count in PosOrder.read_group(
        order_domain,
        groupby=["config_id", "is_refund"],
        aggregates=["amount_total:sum", "amount_tax:sum", "id:count"],
    )
])
        sales_total = False
        order_count = 0
        branch_sales_map = defaultdict(lambda: {"sales": 0.0, "orders": 0})
        config_to_branch = {}
        config_rows = self._overview_safe(lambda: configs.read(["name", "picking_type_id"])) or []
        picking_type_ids = [row["picking_type_id"][0] for row in config_rows if row.get("picking_type_id")]
        picking_types = self._overview_safe(lambda: self.env["stock.picking.type"].browse(picking_type_ids).read(["warehouse_id"])) or []
        picking_type_to_warehouse = {}
        for picking_type in picking_types:
            wh = picking_type.get("warehouse_id")
            picking_type_to_warehouse[picking_type["id"]] = (wh[0], wh[1]) if wh else (False, "Unassigned POS")
        for row in config_rows:
            picking = row.get("picking_type_id")
            config_to_branch[row["id"]] = picking_type_to_warehouse.get(picking[0], (False, row["name"])) if picking else (False, row["name"])

        if order_groups is not False and pos_readable:
            sales_total = 0.0
            for row in order_groups:
                amount = float(row.get("amount_total") or 0.0)
                count = int(row.get("__count") or 0)
                sales_total += amount
                order_count += count
                config_ref = row.get("config_id")
                config_id = config_ref[0] if config_ref else False
                branch_id, branch_name = config_to_branch.get(config_id, (False, config_ref[1] if config_ref else "Unassigned POS"))
                branch_sales_map[branch_id or ("pos", config_id)] ["sales"] += amount
                branch_sales_map[branch_id or ("pos", config_id)] ["orders"] += count
                branch_sales_map[branch_id or ("pos", config_id)]["name"] = branch_name

        # Customer invoices count as sales too. POS-generated invoices are left out: their POS order is already counted.
        invoice_branch = False if (is_head_office and not selected_warehouse) else (selected_warehouse.ids if selected_warehouse else scope_warehouse_ids)
        invoices = self._overview_safe(lambda: self._overview_invoices(start, end, invoice_branch))
        invoice_keys = {}  # invoice id -> its branch key in branch_sales_map, for the branch drills
        if invoices:
            sales_total = sales_total or 0.0
            for invoice in invoices:
                amount = invoice.amount_untaxed_signed  # negative for credit notes
                counted = 1 if invoice.move_type == "out_invoice" else 0
                sales_total += amount
                order_count += counted
                warehouse = invoice.invoice_line_ids.sale_line_ids.order_id.warehouse_id[:1]
                key = warehouse.id or ("invoice", 0)
                invoice_keys[invoice.id] = key
                branch_sales_map[key]["sales"] += amount
                branch_sales_map[key]["orders"] += counted
                branch_sales_map[key]["name"] = warehouse.display_name if warehouse else "Invoices without a branch"

        trend_groupby = "date_order:month" if trend_granularity == "month" else "date_order:day"
        trend_groups = self._overview_safe(lambda: [
            {"period_value": period_value, "amount_total": (amount or 0.0) - (tax or 0.0)}
            for period_value, amount, tax in PosOrder.read_group(
                order_domain,
                groupby=[trend_groupby],
                aggregates=["amount_total:sum", "amount_tax:sum"],
            )
        ])
        trend_sales = self._overview_period_buckets(start, end, trend_granularity)
        if trend_groups is not False:
            for row in trend_groups:
                group_date = self._overview_group_date(row.get("period_value"))
                if not group_date:
                    continue
                if trend_granularity == "week":
                    group_date -= timedelta(days=group_date.weekday())
                    period_key = group_date.isoformat()
                elif trend_granularity == "day":
                    period_key = group_date.isoformat()
                else:
                    period_key = group_date.strftime("%Y-%m")
                if period_key in trend_sales:
                    trend_sales[period_key]["sales"] += float(row.get("amount_total") or 0.0)
        for invoice in invoices or []:
            day = invoice.invoice_date
            period_key = (day - timedelta(days=day.weekday())).isoformat() if trend_granularity == "week" else day.isoformat() if trend_granularity == "day" else day.strftime("%Y-%m")
            if period_key in trend_sales:
                trend_sales[period_key]["sales"] += invoice.amount_untaxed_signed

        # Aggregate POS sales by hour-of-day, reusing the same authorized order domain.
        # Odoo groups each calendar date/hour separately; combine those hourly buckets
        # into 24 hour-of-day totals for the dashboard chart.
        hourly_groups = self._overview_safe(lambda: [
            (hour_bucket, (amount or 0.0) - (tax or 0.0))
            for hour_bucket, amount, tax in PosOrder.read_group(
                order_domain,
                groupby=["date_order:hour"],
                aggregates=["amount_total:sum", "amount_tax:sum"],
            )
        ])
        sales_by_hour = []
        if hourly_groups is not False:
            hourly_totals = [0.0] * 24
            for hour_bucket, amount in hourly_groups:
                if not hour_bucket:
                    continue
                if isinstance(hour_bucket, datetime):
                    hour_index = hour_bucket.hour
                else:
                    bucket_text = str(hour_bucket).strip()
                    # Odoo can format hour groups as "HH:00 dd MMM"; otherwise
                    # accept a normal server datetime string.
                    if (len(bucket_text) >= 5 and bucket_text[:2].isdigit()
                            and bucket_text[2:5] == ":00"):
                        hour_index = int(bucket_text[:2])
                    else:
                        try:
                            bucket_datetime = fields.Datetime.to_datetime(bucket_text)
                        except (TypeError, ValueError):
                            continue
                        if not bucket_datetime:
                            continue
                        hour_index = bucket_datetime.hour
                if 0 <= hour_index < 24:
                    hourly_totals[hour_index] += float(amount or 0.0)

            for hour_index, amount in enumerate(hourly_totals):
                label = f"{hour_index % 12 or 12} {'AM' if hour_index < 12 else 'PM'}"
                sales_by_hour.append({
                    "hour": hour_index,
                    "label": label if hour_index % 2 == 0 else "",
                    "full_label": label,
                    "sales": round(amount, 2),
                })
        product_domain = [
            ("order_id.company_id", "=", company.id),
            ("order_id.state", "in", _POS_STATES),
            ("order_id.date_order", ">=", fields.Datetime.to_string(start_utc)),
            ("order_id.date_order", "<", fields.Datetime.to_string(end_utc)),
        ]
        if config_ids:
            product_domain.append(("order_id.config_id", "in", config_ids))
        else:
            product_domain.append(("order_id.config_id", "=", 0))
        product_groups = self._overview_safe(lambda: [
    {
        "product_id": (product, self.env["product.product"].browse(product).display_name) if product else False,
        "qty": quantity,
        "price_subtotal": subtotal,
    }
    for product, quantity, subtotal in self._overview_pos_line_groups(
        product_domain,
        groupby=["product_id"],
        aggregates=["qty:sum", "price_subtotal:sum"],
    )
])
        discounts_refunds = self._overview_safe(lambda: self._overview_discounts_refunds(order_domain, product_domain))
        credit_notes = (invoices or self.env["account.move"]).filtered(lambda invoice: invoice.move_type == "out_refund")
        if discounts_refunds and credit_notes:
            discounts_refunds["refund_total"] = round(discounts_refunds["refund_total"] - sum(credit_notes.mapped("amount_untaxed_signed")), 2)
            discounts_refunds["refund_count"] += len(credit_notes)
        sales_by_category = []
        if product_groups is not False:
            grouped_product_ids = sorted({
                int(row["product_id"][0])
                for row in product_groups
                if row.get("product_id")
            })
            product_rows = self._overview_safe(lambda: self.env["product.product"].browse(grouped_product_ids).read(["product_tmpl_id"]))
            if product_rows is not False:
                template_ids = sorted({
                    int(row["product_tmpl_id"][0])
                    for row in product_rows
                    if row.get("product_tmpl_id")
                })
                template_rows = self._overview_safe(lambda: self.env["product.template"].browse(template_ids).read(["categ_id"]))
                if template_rows is not False:
                    template_category = {
                        row["id"]: row.get("categ_id")
                        for row in template_rows
                    }
                    product_template = {
                        row["id"]: row["product_tmpl_id"][0]
                        for row in product_rows
                        if row.get("product_tmpl_id")
                    }
                    category_sales_map = {}
                    for row in product_groups:
                        product_ref = row.get("product_id")
                        if not product_ref:
                            continue
                        template_id = product_template.get(product_ref[0])
                        category_ref = template_category.get(template_id)
                        category_id = category_ref[0] if category_ref else False
                        category_name = category_ref[1] if category_ref else "Uncategorized"
                        key = category_id if category_id else False
                        item = category_sales_map.setdefault(key, {
                            "category_id": category_id,
                            "name": category_name,
                            "sales": 0.0,
                        })
                        item["sales"] += float(row.get("price_subtotal") or 0.0)
                    positive_category_total = sum(max(0.0, item["sales"]) for item in category_sales_map.values())
                    sales_by_category = [
                        {
                            "drill": self._overview_drill("pos.order.line", product_domain + [("product_id.categ_id", "=", item["category_id"])],
                                                          "POS sales in %s" % item["name"]),
                            "category_id": item["category_id"],
                            "name": item["name"],
                            "sales": round(item["sales"], 2),
                            "percentage": round(max(0.0, item["sales"]) * 100.0 / positive_category_total, 1) if positive_category_total else 0.0,
                        }
                        for item in sorted(category_sales_map.values(), key=lambda value: value["sales"], reverse=True)
                        if item["sales"] != 0.0
                    ]
        top_products = []
        if product_groups is not False:
            for row in sorted(product_groups, key=lambda item: float(item.get("price_subtotal") or 0), reverse=True)[:top_product_limit]:
                product = row.get("product_id")
                if product:
                    top_products.append({
                        "drill": self._overview_drill("pos.order.line", product_domain + [("product_id", "=", product[0])], "POS sales of %s" % product[1]),
                        "product_id": product[0],
                        "name": product[1],
                        "quantity": float(row.get("qty") or 0.0),
                        "revenue": float(row.get("price_subtotal") or 0.0),
                    })

        # Branches must not see consolidated P&L figures as if they were their own.
        financial = (self._overview_safe(lambda: self._overview_financials(start, end)) or False) if is_head_office and not selected_warehouse else False
        if financial and financial.get("monthly_profit"):
            for month_key, profit in financial["monthly_profit"].items():
                if month_key in trend_sales:
                    trend_sales[month_key]["profit"] = profit
        waste_cost = self._overview_waste_cost(start, end, selected_warehouse)
        alerts = self._overview_alerts(end, selected_warehouse, waste_cost)
        tasks = self._overview_tasks(end, own_only=(not is_head_office or bool(selected_warehouse)))
        def branch_drill(key):
            configs_of_key = [config for config, (branch, _name) in config_to_branch.items() if (branch or ("pos", config)) == key]
            pos_domain = [leaf for leaf in order_domain if leaf[0] != "config_id"] + [("config_id", "in", configs_of_key)]
            return self._overview_drills(
                self._overview_drill("pos.order", pos_domain, "POS orders") if configs_of_key else False,
                self._overview_drill("account.move", None, "Customer invoices", ids=[i for i, k in invoice_keys.items() if k == key]))

        top_branches = sorted(branch_sales_map.items(), key=lambda item: item[1].get("sales", 0), reverse=True)[:8]
        branch_sales = [dict(branch_id=key[0] if isinstance(key, tuple) else key, drill=branch_drill(key), **values) for key, values in top_branches]
        # Hide unrelated POS locations that could not be mapped to an authorized branch.
        if not is_head_office or selected_warehouse:
            authorized_ids = set(scope_warehouse_ids)
            branch_sales = [row for row in branch_sales if row.get("branch_id") in authorized_ids]
        for row in branch_sales:
            count = row.get("orders", 0)
            row["sales"] = round(row.get("sales", 0.0), 2)
            row["average_ticket"] = row["sales"] / count if count else 0.0

        trend = list(trend_sales.values())
        trend.sort(key=lambda row: row["month_key"])
        for row in trend:
            row["drill"] = self._overview_period_drill(row, trend_granularity, start, end, order_domain, invoices) if row["sales"] else False
        branch_count = len(branch_sales)
        return {
            "role": "head_office" if is_head_office else "branch_manager",
            "scope_label": selected_warehouse.display_name if selected_warehouse else "All branches",
            "period": {"start": fields.Date.to_string(start), "end": fields.Date.to_string(end)},
            "branch_options": branch_options,
            "currency": {
                "name": company.currency_id.name,
                "symbol": company.currency_id.symbol or company.currency_id.name,
                "position": company.currency_id.position or "before",
                "decimal_places": company.currency_id.decimal_places,
            },
            "selected_warehouse_id": selected_warehouse.id if selected_warehouse else False,
            "sales": round(sales_total, 2) if sales_total is not False else False,
            "order_count": order_count if sales_total is not False else False,
            "drill": {
                "sales": self._overview_drills(self._overview_drill("pos.order", order_domain, "POS orders"),
                                               self._overview_drill("account.move", None, "Customer invoices", ids=(invoices or self.env["account.move"]).ids)),
                "orders": self._overview_drills(self._overview_drill("pos.order", order_domain + [("is_refund", "=", False)], "POS orders"),
                                                self._overview_drill("account.move", None, "Customer invoices", ids=(invoices or self.env["account.move"]).filtered(lambda invoice: invoice.move_type == "out_invoice").ids)),
                "discounts_refunds": self._overview_drills(self._overview_drill("pos.order", order_domain + ["|", ("is_refund", "=", True), ("lines.discount", ">", 0)], "Discounted and refund POS orders"),
                                                           self._overview_drill("account.move", None, "Credit notes", ids=credit_notes.ids)),
            } if sales_total is not False else {},
            "net_profit": financial.get("net_profit") if financial else False,
            "expenses": financial.get("expenses") if financial else False,
            "financial_available": bool(financial),
            "expense_breakdown": financial.get("expense_breakdown", []) if financial else [],
            "waste_cost": waste_cost,
            "sales_trend": trend,
            "sales_by_hour": sales_by_hour,
            "branch_sales": branch_sales,
            "top_products": top_products,
            "discounts_refunds": discounts_refunds,
            "sales_by_category": sales_by_category,
            "alerts": alerts,
            "tasks": tasks,
            "branch_performance": branch_sales,
            "open_issues": len(alerts),
            "branch_count": branch_count,
            "messages": ["Some POS orders are in a currency other than %s; POS totals are summed without conversion." % company.currency_id.name]
            if discounts_refunds and discounts_refunds["mixed_currency"] else [],
        }


    @api.model
    def _overview_pos_line_groups(self, domain, groupby, aggregates):
        """``read_group`` on POS lines with line prices signed by quantity.

        Odoo stores POS line prices as positive amounts even on refund lines
        ("Line prices are always positive"), so negative-quantity lines are
        subtracted to keep product, category and margin figures net of refunds.
        """
        Line = self.env["pos.order.line"]
        signed = [index for index, aggregate in enumerate(aggregates) if aggregate.split(":")[0].startswith("price_subtotal")]
        totals = {}
        for sign, quantity_leaf in ((1, ("qty", ">=", 0)), (-1, ("qty", "<", 0))):
            for row in Line.read_group(domain + [quantity_leaf], groupby=groupby, aggregates=aggregates):
                keys, values = tuple(row[:len(groupby)]), list(row[len(groupby):])
                for index in signed:
                    values[index] = sign * float(values[index] or 0.0)
                current = totals.get(keys)
                totals[keys] = values if current is None else [(left or 0) + (right or 0) for left, right in zip(current, values)]
        return [(*keys, *values) for keys, values in totals.items()]

    @api.model
    def _overview_discounts_refunds(self, order_domain, line_domain):
        """POS discounts and refunds for the same orders as Total Sales, excluding tax."""
        PosOrder = self.env["pos.order"]
        refund_rows = PosOrder.read_group(
            order_domain + [("is_refund", "=", True)], groupby=[], aggregates=["amount_total:sum", "amount_tax:sum", "id:count"],
        )
        refund_total, refund_tax, refund_count = refund_rows[0] if refund_rows else (0.0, 0.0, 0)
        refund_amount = (refund_total or 0.0) - (refund_tax or 0.0)
        # Discount = undiscounted price minus what was charged (both excluding tax),
        # on sale orders only: a refund is reported once, as the refunded amount.
        lines = self.env["pos.order.line"].search(line_domain + [("discount", ">", 0), ("order_id.is_refund", "=", False)])
        discount_total = 0.0
        for line in lines:
            order = line.order_id
            taxes = line.tax_ids_after_fiscal_position.compute_all(
                line.price_unit, order.currency_id, line.qty, product=line.product_id, partner=order.partner_id,
            )
            discount_total += taxes["total_excluded"] - line.price_subtotal
        mixed_currency = bool(PosOrder.search_count(order_domain + [("config_id.currency_id", "!=", self.env.company.currency_id.id)], limit=1))
        return {
            "discount_total": round(discount_total, 2),
            "discounted_lines": len(lines),
            "refund_total": round(-float(refund_amount or 0.0), 2) or 0.0,
            "refund_count": int(refund_count or 0),
            "mixed_currency": mixed_currency,
        }

    @api.model
    def _overview_invoices(self, start, end, warehouse_ids=False):
        """Posted customer invoices and credit notes dated in the period, except those made by the POS.

        An invoice belongs to the branch of the sales order it came from; one without a sales order counts only in the
        company-wide total. Read with sudo: the period and branch here come from the server, never the browser.
        """
        if "account.move" not in self.env.registry.models:
            return self.env["account.move"]
        Move = self.env["account.move"].sudo()
        domain = [("company_id", "=", self.env.company.id), ("move_type", "in", ["out_invoice", "out_refund"]), ("state", "=", "posted"),
                  ("invoice_date", ">=", start), ("invoice_date", "<=", end)]
        if "pos_order_ids" in Move._fields:
            domain.append(("pos_order_ids", "=", False))
        if warehouse_ids is not False:
            if "sale_line_ids" not in self.env["account.move.line"]._fields:
                return Move.browse()
            domain.append(("invoice_line_ids.sale_line_ids.order_id.warehouse_id", "in", list(warehouse_ids)))
        return Move.search(domain, order="invoice_date, id")

    @api.model
    def _overview_period_drill(self, row, granularity, start, end, order_domain, invoices):
        """The POS orders and invoices behind one point of the sales trend (its day, week or month, within the period)."""
        first = fields.Date.to_date(row["month_key"] + ("-01" if granularity == "month" else ""))
        if granularity == "week":
            last = first + timedelta(days=6)
        elif granularity == "month":
            last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        else:
            last = first
        first, last = max(first, start), min(last, end)
        start_utc, end_utc = self._overview_utc_bounds(first, last)
        domain = [leaf for leaf in order_domain if leaf[0] != "date_order"] + [
            ("date_order", ">=", fields.Datetime.to_string(start_utc)), ("date_order", "<", fields.Datetime.to_string(end_utc))]
        invoice_ids = [invoice.id for invoice in invoices or [] if first <= invoice.invoice_date <= last]
        return self._overview_drills(self._overview_drill("pos.order", domain, "POS orders, %s" % row["label"]),
                                     self._overview_drill("account.move", None, "Customer invoices, %s" % row["label"], ids=invoice_ids))

    @api.model
    def _overview_drills(self, *drills):
        """One card backed by several kinds of record: the user picks which list to open."""
        drills = [drill for drill in drills if drill]
        return drills[0] if len(drills) == 1 else drills or False

    @api.model
    def _overview_drill(self, model, domain, name, ids=None):
        """What a summary card opens: Odoo's own list of exactly the records behind its number.

        Offered only when this user can open that list themselves; Odoo's own access rules still filter it, so a Branch
        Manager sees their branch only. Never offered from sudo reads (an Employee's department page) or for purchase
        orders outside Head Office, whose Odoo screens are not branch-restricted.
        """
        if self.env.su or model not in self.env.registry.models:
            return False
        # Purchase lists: Head Office, and Branch Managers, whose read access is limited to their own branch (ir.access.csv).
        if model in ("purchase.order", "purchase.order.line") and not (
                self.env.user.has_group("strategic_management.group_strategic_head_office_manager")
                or self.env.user.has_group("strategic_management.group_strategic_branch_manager")):
            return False
        try:
            self.env[model].check_access("read")
        except AccessError:
            return False
        if ids is not None:
            domain = [("id", "in", list(ids))]  # an empty list opens an empty list, never everything
        # Nothing to show, nothing to click: Odoo fills an empty list with made-up sample rows.
        try:
            count = self.env[model].search_count(domain)
        except AccessError:  # the filter reaches a model this user cannot read (POS order lines): no link, the section stays
            return False
        if not count:
            return False
        return {"model": model, "domain": domain, "name": name, "count": count}

    @api.model
    def _overview_safe(self, function):
        """Run one optional source query without making the whole dashboard fail."""
        try:
            return function()
        except AccessError as error:
            # Expected for users without that app's rights: the section stays empty; not a server fault.
            _logger.info("Business overview section skipped, no access: %s", str(error).splitlines()[0])
            return False
        except (UserError, ValidationError, KeyError, TypeError, ValueError):
            _logger.exception("Business overview query failed (callable=%r)", function)
            return False

    @api.model
    def _overview_utc_bounds(self, start, end):
        timezone_name = self.env.company.partner_id.tz or self.env.user.tz or "UTC"
        try:
            timezone = pytz.timezone(timezone_name)
            local_start = timezone.localize(datetime.combine(start, time.min), is_dst=None)
            local_end = timezone.localize(datetime.combine(end + timedelta(days=1), time.min), is_dst=None)
            return (
                local_start.astimezone(pytz.UTC).replace(tzinfo=None),
                local_end.astimezone(pytz.UTC).replace(tzinfo=None),
            )
        except (pytz.UnknownTimeZoneError, pytz.InvalidTimeError, ValueError, TypeError):
            return datetime.combine(start, time.min), datetime.combine(end + timedelta(days=1), time.min)

    @api.model
    def _overview_period_buckets(self, start, end, granularity):
        result = {}
        if granularity == "day":
            cursor = start
            while cursor <= end:
                key = cursor.isoformat()
                result[key] = {"month_key": key, "label": cursor.strftime("%d %b"), "sales": 0.0, "profit": False}
                cursor += timedelta(days=1)
        elif granularity == "week":
            cursor = start - timedelta(days=start.weekday())
            while cursor <= end:
                key = cursor.isoformat()
                result[key] = {"month_key": key, "label": cursor.strftime("%d %b"), "sales": 0.0, "profit": False}
                cursor += timedelta(days=7)
        else:
            cursor = start.replace(day=1)
            while cursor <= end:
                key = cursor.strftime("%Y-%m")
                result[key] = {"month_key": key, "label": cursor.strftime("%b"), "sales": 0.0, "profit": False}
                cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        return result

    @api.model
    def _overview_group_date(self, value):
        if not value:
            return False
        if isinstance(value, datetime):
            return value.date()
        try:
            return fields.Date.to_date(value)
        except (TypeError, ValueError):
            text = str(value)
            for fmt in ("%B %Y", "%b %Y"):
                try:
                    return datetime.strptime(text, fmt).date()
                except ValueError:
                    pass
            return False

    @api.model
    def _overview_month_key(self, value):
        if not value:
            return False
        if isinstance(value, (datetime,)):
            return value.strftime("%Y-%m")
        text = str(value)
        try:
            return fields.Date.to_date(text[:10]).strftime("%Y-%m")
        except (AttributeError, TypeError, ValueError):
            for fmt in ("%B %Y", "%b %Y"):
                try:
                    return datetime.strptime(text, fmt).strftime("%Y-%m")
                except ValueError:
                    pass
        return False

    @api.model
    def _overview_financials(self, start, end):
        Line = self.env["account.move.line"]
        domain = [
            ("company_id", "=", self.env.company.id),
            ("date", ">=", fields.Date.to_string(start)),
            ("date", "<=", fields.Date.to_string(end)),
            ("move_id.state", "=", "posted"),
            ("account_id.account_type", "in", sorted(_INCOME_TYPES | _EXPENSE_TYPES)),
        ]
        groups = [
    {
        "account_id": (account, self.env["account.account"].browse(account).display_name) if account else False,
        "date:month": month_value,
        "balance": balance,
    }
    for account, month_value, balance in Line.read_group(
        domain,
        groupby=["account_id", "date:month"],
        aggregates=["balance:sum"],
    )
]
        account_ids = list({row["account_id"][0] for row in groups if row.get("account_id")})
        accounts = self.env["account.account"].browse(account_ids).read(["account_type", "name"])
        types_by_id = {account["id"]: account.get("account_type") for account in accounts}
        names_by_id = {account["id"]: account.get("name") for account in accounts}
        total_income = 0.0
        total_expenses = 0.0
        monthly = defaultdict(lambda: {"income": 0.0, "expenses": 0.0})
        expenses_by_account = defaultdict(float)
        for row in groups:
            account = row.get("account_id")
            if not account:
                continue
            account_id = account[0]
            account_type = types_by_id.get(account_id)
            balance = float(row.get("balance") or 0.0)
            month_key = self._overview_month_key(row.get("date:month") or row.get("date"))
            if account_type in _INCOME_TYPES:
                income = -balance
                total_income += income
                if month_key:
                    monthly[month_key]["income"] += income
            elif account_type in _EXPENSE_TYPES:
                total_expenses += balance
                expenses_by_account[account_id] += balance
                if month_key:
                    monthly[month_key]["expenses"] += balance
        monthly_profit = {
            key: round(values["income"] - values["expenses"], 2)
            for key, values in monthly.items()
        }
        expense_breakdown = [
            {"name": names_by_id.get(account_id) or "Expense", "value": round(value, 2)}
            for account_id, value in sorted(expenses_by_account.items(), key=lambda item: item[1], reverse=True)[:5]
            if value > 0
        ]
        if not any(month["income"] != 0 for month in monthly.values()) or not any(month["expenses"] != 0 for month in monthly.values()):
            return False
        return {
            "net_profit": round(total_income - total_expenses, 2),
            "expenses": round(total_expenses, 2),
            "monthly_profit": monthly_profit,
            "expense_breakdown": expense_breakdown,
        }

    @api.model
    def _overview_waste_domain(self, start, end, warehouse=False):
        start_utc, end_utc = self._overview_utc_bounds(start, end)
        domain = [
            ("company_id", "=", self.env.company.id),
            ("state", "=", "done"),
            ("is_scrap", "=", True),
            ("date", ">=", fields.Datetime.to_string(start_utc)),
            ("date", "<", fields.Datetime.to_string(end_utc)),
        ]
        if warehouse:
            domain.append(("location_id", "child_of", warehouse.view_location_id.id))
        return domain

    def _overview_waste_cost(self, start, end, warehouse=False):
        Move = self.env["stock.move"]
        required_fields = {"is_scrap", "date", "state", "company_id", "scrap_reason_tag_ids", "location_id"}
        if not required_fields.issubset(Move._fields):
            return False
        domain = self._overview_waste_domain(start, end, warehouse)
        moves = self._overview_safe(lambda: Move.search(domain))
        if moves is False:
            return False
        if not moves:
            return 0.0
        # Follow the module's existing waste data contract: only Spoilage/Expiry
        # and Damaged Goods are valid waste categories. Missing or unsupported tags
        # make the cost incomplete, so do not display a misleading partial total.
        allowed_tags = {"spoilage/expiry", "damaged goods"}
        for move in moves:
            tags = {(tag.name or "").strip().casefold() for tag in move.scrap_reason_tag_ids}
            if not tags or tags.difference(allowed_tags):
                return False
        costs = self._overview_scrap_costs(moves)
        return round(sum(costs.values()), 2) if costs is not False else False


    @api.model
    def _overview_scrap_costs(self, moves):
        """Return {move id: stock value lost} for scrap moves, or False if any is unvalued.

        Odoo 19+ stores valuation on the move itself (``stock.move.value``);
        older versions use ``stock.valuation.layer``. A scrap without valuation
        makes the total incomplete, so no partial figure is returned.
        """
        Move = self.env["stock.move"]
        if "value" in Move._fields and "is_out" in Move._fields:
            # Same rule as the waste-cost KPI: valued outgoing moves only, sign-checked, never zero-cost stock.
            costs = {move.id: waste_move_cost(move) for move in moves}
            if any(cost is None or (move.quantity_product_uom > 0 and not cost) for move, cost in zip(moves, costs.values())):
                return False
            return costs
        if "stock.valuation.layer" in self.env.registry.models:
            layers = self._overview_safe(lambda: self.env["stock.valuation.layer"].search([("stock_move_id", "in", moves.ids)]))
            if not layers:
                return False
            costs = defaultdict(float)
            for layer in layers:
                costs[layer.stock_move_id.id] -= float(layer.value or 0.0)
            return {move_id: max(0.0, value) for move_id, value in costs.items()}
        return False

    @api.model
    def _overview_alerts(self, date_end, warehouse=False, waste_cost=False):
        alerts = []
        company = self.env.company
        now = fields.Datetime.now()
        Picking = self.env["stock.picking"]
        if {"scheduled_date", "picking_type_id", "state", "company_id"}.issubset(Picking._fields):
            domain = [
                ("company_id", "=", company.id),
                ("picking_type_id.code", "=", "incoming"),
                ("state", "not in", ["done", "cancel"]),
                ("scheduled_date", "<", now),
            ]
            if warehouse:
                domain.append(("picking_type_id.warehouse_id", "=", warehouse.id))
            pickings = self._overview_safe(lambda: Picking.search_read(domain, ["name", "partner_id", "origin", "scheduled_date"], order="scheduled_date asc", limit=5))
            if pickings:
                for picking in pickings:
                    partner = picking.get("partner_id")
                    alerts.append({
                        "severity": "medium",
                        "icon": "fa-truck",
                        "title": "Supplier delivery overdue",
                        "detail": "%s%s" % ((partner[1] + " · ") if partner else "", picking.get("origin") or picking.get("name") or "Incoming delivery"),
                        "date": picking.get("scheduled_date") or "",
                    })

        user = self.env.user
        # Same rule as the inventory page: a Branch Manager reads only their own pinned branch's reorder rules.
        own_branch = (bool(warehouse) and user.has_group("strategic_management.group_strategic_branch_manager")
                      and not user.has_group("strategic_management.group_strategic_head_office_manager")
                      and warehouse == user.strategic_branch_warehouse_id)
        Orderpoint = self.env["stock.warehouse.orderpoint"].sudo() if own_branch else self.env["stock.warehouse.orderpoint"]
        required = {"product_id", "product_min_qty", "qty_forecast", "warehouse_id"}
        if required.issubset(Orderpoint._fields):
            domain = [("company_id", "=", company.id), ("product_min_qty", ">", 0)]
            if warehouse:
                domain.append(("warehouse_id", "=", warehouse.id))
            orderpoints = self._overview_safe(lambda: Orderpoint.search_read(domain, ["product_id", "product_min_qty", "qty_forecast", "warehouse_id"], limit=1000))
            if orderpoints:
                low_stock = [row for row in orderpoints if float(row.get("qty_forecast") or 0) < float(row.get("product_min_qty") or 0)]
                for row in sorted(low_stock, key=lambda item: float(item.get("qty_forecast") or 0))[:5]:
                    product = row.get("product_id")
                    warehouse_ref = row.get("warehouse_id")
                    alerts.append({
                        "severity": "medium",
                        "icon": "fa-exclamation-triangle",
                        "title": "Low stock level",
                        "detail": "%s%s" % ((warehouse_ref[1] + " · ") if warehouse_ref else "", product[1] if product else "Product"),
                        "date": "",
                    })

        if waste_cost is not False and waste_cost > 0:
            alerts.append({
                "severity": "low",
                "icon": "fa-recycle",
                "title": "Inventory scrap recorded",
                "detail": "Recorded scrap valuation for the selected period",
                "date": fields.Date.to_string(date_end),
                "amount": waste_cost,
            })
        return alerts[:8]

    @api.model
    def _overview_tasks(self, date_end, own_only=False):
        Activity = self.env["mail.activity"]
        if not {"date_deadline", "user_id", "summary"}.issubset(Activity._fields):
            return []
        domain = [("date_deadline", "<=", fields.Date.to_string(date_end))]
        if own_only:
            domain.append(("user_id", "=", self.env.user.id))
        fields_to_read = [name for name in ("summary", "date_deadline", "user_id", "res_name", "res_model", "res_id", "activity_type_id") if name in Activity._fields]
        rows = self._overview_safe(lambda: Activity.search_read(domain, fields_to_read, order="date_deadline asc, id asc", limit=_LIST_LIMIT))
        if not rows:
            return []
        return [{
            "name": row.get("summary") or row.get("res_name") or "Follow-up",
            "responsible": row.get("user_id", [False, "Unassigned"])[1] if row.get("user_id") else "Unassigned",
            "record": row.get("res_name") or row.get("res_model") or "Business record",
            "due_date": row.get("date_deadline") or "",
        } for row in rows]
