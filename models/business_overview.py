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

from ..services.kpi_sources.pos import recipe_consumption


_logger = logging.getLogger(__name__)

_INCOME_TYPES = {"income", "income_other"}
_EXPENSE_TYPES = {"expense", "expense_other", "expense_direct_cost", "expense_depreciation"}
_POS_STATES = ["paid", "done", "invoiced"]


class StrategicPerformanceDashboardBusinessOverview(models.AbstractModel):
    _inherit = "strategic.performance.dashboard"

    @api.model
    def get_business_overview_data(self, date_start=False, date_end=False, warehouse_id=False, trend_granularity="month", top_product_limit=5):
        """Return business-facing figures, scoped to the current user's role.

        The method respects the requesting user's ACLs and record rules. Unavailable
        source data is returned as ``False`` so the UI can show "Not available".
        """
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
        is_branch_manager = user.has_group("strategic_management.group_strategic_branch_manager") and not is_head_office
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
        branch_sales = sorted(
            [dict(branch_id=key[0] if isinstance(key, tuple) else key, **values) for key, values in branch_sales_map.items()],
            key=lambda row: row.get("sales", 0), reverse=True,
        )[:8]
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
        branch_count = len(branch_sales)
        return {
            "role": "head_office" if is_head_office else "branch_manager",
            "scope_label": selected_warehouse.display_name if selected_warehouse else "All branches",
            "period": {"start": fields.Date.to_string(start), "end": fields.Date.to_string(end)},
            "currency": {
                "name": company.currency_id.name,
                "symbol": company.currency_id.symbol,
                "position": company.currency_id.position,
                "decimal_places": company.currency_id.decimal_places,
            },
            "branch_options": branch_options,            "currency": {
                "name": company.currency_id.name,
                "symbol": company.currency_id.symbol or company.currency_id.name,
                "position": company.currency_id.position or "before",
                "decimal_places": company.currency_id.decimal_places,
            },
            "selected_warehouse_id": selected_warehouse.id if selected_warehouse else False,
            "sales": round(sales_total, 2) if sales_total is not False else False,
            "order_count": order_count if sales_total is not False else False,
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
    def get_purchasing_overview_data(self, date_start=False, date_end=False, warehouse_id=False):
        """Return Purchasing dashboard figures from actual Purchase and Stock records."""
        start, end = self._resolve_period(date_start, date_end)
        company = self.env.company
        user = self.env.user
        is_head_office = user.has_group("strategic_management.group_strategic_head_office_manager")
        is_branch_manager = user.has_group("strategic_management.group_strategic_branch_manager") and not is_head_office
        currency = company.currency_id
        currency_data = {
            "name": currency.name if currency else "",
            "symbol": currency.symbol if currency else "",
            "position": currency.position if currency else "before",
            "decimal_places": currency.decimal_places if currency else 2,
        }
        empty = {
            "available": False,
            "period": {"start": fields.Date.to_string(start), "end": fields.Date.to_string(end)},
            "currency": currency_data,
            "branch_options": [],
            "selected_warehouse_id": False,
            "purchase_order_count": False,
            "purchase_order_value": False,
            "pending_delivery_count": False,
            "pending_delivery_value": False,
            "overdue_delivery_count": False,
            "overdue_delivery_value": False,
            "on_time_delivery_rate": False,
            "on_time_delivery_available": False,
            "expenditure_trend": [],
            "purchase_categories": [],
            "supplier_performance": [],
            "orders_attention": [],
            "recent_orders": [],
            "price_months": [],
            "ingredient_price_trend": [],
            "messages": [],
        }
        if not (is_head_office or is_branch_manager):
            empty["messages"] = ["Purchasing totals are available to Head Office and Branch Managers."]
            return empty
        if "purchase.order" not in self.env.registry.models:
            empty["messages"] = ["The Odoo Purchase application is not installed."]
            return empty

        Warehouse = self.env["stock.warehouse"]
        if is_head_office:
            warehouses = Warehouse.search([("company_id", "=", company.id)], order="name, id")
            selected_warehouse = False
            if warehouse_id:
                try:
                    requested_id = int(warehouse_id)
                except (TypeError, ValueError):
                    raise ValidationError("Choose a valid branch.")
                selected_warehouse = warehouses.filtered(lambda item: item.id == requested_id)[:1]
                if not selected_warehouse:
                    raise AccessError("You cannot view the selected branch.")
            branch_options = [{"id": item.id, "name": item.display_name} for item in warehouses]
        else:
            selected_warehouse = user.strategic_branch_warehouse_id
            if not selected_warehouse or (selected_warehouse.company_id and selected_warehouse.company_id != company):
                raise AccessError("Assign this Branch Manager to a warehouse in the active company.")
            branch_options = [{"id": selected_warehouse.id, "name": selected_warehouse.display_name}]

        warehouse_ids = [selected_warehouse.id] if selected_warehouse else [item["id"] for item in branch_options]
        PurchaseOrder = self.env["purchase.order"]
        if not {"company_id", "date_order", "state", "amount_untaxed", "partner_id"}.issubset(PurchaseOrder._fields):
            empty["messages"] = ["The Purchase order model does not expose the fields required for this report."]
            empty["branch_options"] = branch_options
            return empty

        company_domain = [("company_id", "=", company.id)]
        if warehouse_ids:
            if "picking_type_id" in PurchaseOrder._fields:
                company_domain.append(("picking_type_id.warehouse_id", "in", warehouse_ids))
            elif selected_warehouse:
                empty["messages"] = ["Branch-level purchase filtering is unavailable because purchase orders are not linked to a warehouse."]
                empty["branch_options"] = branch_options
                empty["selected_warehouse_id"] = selected_warehouse.id
                return empty

        # In Odoo 20 ``date_order`` is the editable "Order Deadline" and may lie in
        # the future; the actual order date of a confirmed PO is ``date_approve``.
        # RFQs that were never approved fall back to ``date_order``. This matches
        # the KPI purchase source, which also reports confirmed orders by approval.
        has_date_approve = "date_approve" in PurchaseOrder._fields

        def effective_datetime(order):
            return (order.date_approve if has_date_approve else False) or order.date_order

        def period_domain(start_value, end_value):
            start_text = fields.Datetime.to_string(start_value)
            end_text = fields.Datetime.to_string(end_value)
            order_date_range = [("date_order", ">=", start_text), ("date_order", "<", end_text)]
            if not has_date_approve:
                return order_date_range
            return [
                "|",
                "&", "&", ("date_approve", "!=", False), ("date_approve", ">=", start_text), ("date_approve", "<", end_text),
                "&", ("date_approve", "=", False), "&", *order_date_range,
            ]

        def sort_recent(records):
            return records.sorted(key=lambda order: (effective_datetime(order) or datetime.min, order.id), reverse=True)

        start_utc, end_utc = self._overview_utc_bounds(start, end)
        order_domain = company_domain + period_domain(start_utc, end_utc)
        orders = sort_recent(PurchaseOrder.search(order_domain))
        confirmed = orders.filtered(lambda order: order.state in ("purchase", "done"))
        company_currency = company.currency_id

        def to_company_currency(amount, source_currency, date_value):
            amount = float(amount or 0.0)
            if not source_currency or not company_currency or source_currency == company_currency:
                return amount
            rate_date = fields.Date.to_date(date_value) if date_value else end
            return source_currency._convert(amount, company_currency, company, rate_date or end)

        def order_date(order):
            if not effective_datetime(order):
                return False
            order_datetime = fields.Datetime.to_datetime(effective_datetime(order))
            if not order_datetime:
                return False
            timezone = pytz.timezone(user.tz or "UTC")
            localized = pytz.UTC.localize(order_datetime).astimezone(timezone)
            return localized.date()

        def order_branch(order):
            picking_type = order.picking_type_id if "picking_type_id" in order._fields else False
            warehouse = picking_type.warehouse_id if picking_type and "warehouse_id" in picking_type._fields else False
            return warehouse.display_name if warehouse else "Unassigned"

        def order_status(order):
            mapping = {
                "draft": ("RFQ", "o_sm_pur_status_info"),
                "sent": ("Sent", "o_sm_pur_status_info"),
                "to approve": ("To approve", "o_sm_pur_status_warn"),
                "purchase": ("Confirmed", "o_sm_pur_status_good"),
                "done": ("Locked", "o_sm_pur_status_good"),
                "cancel": ("Cancelled", "o_sm_pur_status_bad"),
            }
            return mapping.get(order.state, (str(order.state or "Unknown").replace("_", " ").title(), "o_sm_pur_status_info"))

        def lines_for(order):
            lines = order.order_line if "order_line" in order._fields else self.env["purchase.order.line"].browse()
            return lines.filtered(lambda line: bool(line.product_id) and (not getattr(line, "display_type", False)))

        def line_remaining(line):
            ordered = float(getattr(line, "product_qty", 0.0) or 0.0)
            received = float(getattr(line, "qty_received", 0.0) or 0.0)
            return max(0.0, ordered - received)

        purchase_order_value = sum(
            to_company_currency(order.amount_untaxed, order.currency_id if "currency_id" in order._fields else company_currency, order.date_order)
            for order in confirmed
        )
        pending_delivery_value = 0.0
        outstanding_by_order = {}
        overdue_line_order_ids = set()
        now = fields.Datetime.to_datetime(fields.Datetime.now())
        for order in confirmed:
            outstanding = 0.0
            for line in lines_for(order):
                remaining = line_remaining(line)
                if remaining <= 0:
                    continue
                source_currency = line.order_id.currency_id if "currency_id" in line.order_id._fields else company_currency
                outstanding += to_company_currency(remaining * float(getattr(line, "price_unit", 0.0) or 0.0), source_currency, order.date_order)
                planned = getattr(line, "date_planned", False)
                planned_dt = fields.Datetime.to_datetime(planned) if planned else False
                if planned_dt and planned_dt < now:
                    overdue_line_order_ids.add(order.id)
            outstanding_by_order[order.id] = outstanding
            pending_delivery_value += outstanding

        open_receipts = self.env["stock.picking"].browse()
        overdue_receipts = self.env["stock.picking"].browse()
        receipts_available = "picking_ids" in PurchaseOrder._fields and "stock.picking" in self.env.registry.models
        if receipts_available:
            all_receipts = orders.mapped("picking_ids")
            incoming_receipts = all_receipts.filtered(
                lambda picking: picking.picking_type_id
                and picking.picking_type_id.code == "incoming"
                and picking.state not in ("done", "cancel")
            )
            open_receipts = incoming_receipts
            overdue_receipts = incoming_receipts.filtered(
                lambda picking: bool(picking.scheduled_date)
                and fields.Datetime.to_datetime(picking.scheduled_date) < now
            )
        overdue_order_ids = set(overdue_line_order_ids)
        if receipts_available:
            overdue_order_ids.update(
                picking.purchase_id.id for picking in overdue_receipts
                if "purchase_id" in picking._fields and picking.purchase_id
            )
            if "purchase_id" not in self.env["stock.picking"]._fields:
                for order in orders:
                    if order.picking_ids & overdue_receipts:
                        overdue_order_ids.add(order.id)
        overdue_delivery_value = sum(outstanding_by_order.get(order_id, 0.0) for order_id in overdue_order_ids)

        # Monthly spend buckets for the selected months and the preceding equal number of calendar months.
        cursor = start.replace(day=1)
        month_starts = []
        while cursor <= end.replace(day=1):
            month_starts.append(cursor)
            cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)

        def shifted_month(month_start, offset):
            total = month_start.year * 12 + (month_start.month - 1) + offset
            return month_start.replace(year=total // 12, month=(total % 12) + 1, day=1)

        previous_start = shifted_month(month_starts[0], -len(month_starts)) if month_starts else start.replace(day=1)
        previous_end = month_starts[0] - timedelta(days=1) if month_starts else start
        previous_start_utc, previous_end_utc = self._overview_utc_bounds(previous_start, previous_end) if month_starts else (False, False)
        previous_orders = PurchaseOrder.search(
            company_domain + period_domain(previous_start_utc, previous_end_utc), order="id asc"
        ) if month_starts else PurchaseOrder.browse()
        previous_confirmed = previous_orders.filtered(lambda order: order.state in ("purchase", "done"))
        current_by_month = defaultdict(float)
        previous_by_month = defaultdict(float)
        for order in confirmed:
            dt = order_date(order)
            if dt:
                key = dt.strftime("%Y-%m")
                current_by_month[key] += to_company_currency(order.amount_untaxed, order.currency_id if "currency_id" in order._fields else company_currency, order.date_order)
        for order in previous_confirmed:
            dt = order_date(order)
            if dt:
                previous_by_month[dt.strftime("%Y-%m")] += to_company_currency(order.amount_untaxed, order.currency_id if "currency_id" in order._fields else company_currency, order.date_order)
        expenditure_trend = []
        for index, month_start in enumerate(month_starts):
            prior_month = shifted_month(month_starts[0], index - len(month_starts))
            key = month_start.strftime("%Y-%m")
            expenditure_trend.append({
                "key": key,
                "label": month_start.strftime("%b"),
                "current": round(current_by_month.get(key, 0.0), 2),
                "previous": round(previous_by_month.get(prior_month.strftime("%Y-%m"), 0.0), 2),
            })

        categories = defaultdict(float)
        product_totals = defaultdict(float)
        product_labels = {}
        product_units = {}
        monthly_product_price = defaultdict(lambda: {"qty": 0.0, "total": 0.0})
        month_keys = [month.strftime("%Y-%m") for month in month_starts]
        month_label_by_key = {month.strftime("%Y-%m"): month.strftime("%b") for month in month_starts}
        for order in confirmed:
            dt = order_date(order)
            month_key = dt.strftime("%Y-%m") if dt else False
            source_currency = order.currency_id if "currency_id" in order._fields else company_currency
            for line in lines_for(order):
                line_amount = to_company_currency(getattr(line, "price_subtotal", 0.0), source_currency, order.date_order)
                product = line.product_id
                category = product.categ_id.display_name if product and product.categ_id else "Uncategorized"
                categories[category] += line_amount
                product_totals[product.id] += line_amount
                product_labels[product.id] = product.display_name
                uom = getattr(line, "product_uom", False)
                product_units[product.id] = uom.name if uom else ""
                qty = float(getattr(line, "product_qty", 0.0) or 0.0)
                if month_key and month_key in month_label_by_key and qty > 0:
                    unit_price = to_company_currency(getattr(line, "price_unit", 0.0), source_currency, order.date_order)
                    bucket = monthly_product_price[(product.id, month_key)]
                    bucket["qty"] += qty
                    bucket["total"] += unit_price * qty

        sorted_categories = sorted(categories.items(), key=lambda pair: pair[1], reverse=True)
        category_rows = [{"name": name, "value": round(value, 2)} for name, value in sorted_categories[:5]]
        if len(sorted_categories) > 5:
            category_rows.append({"name": "Other", "value": round(sum(value for _, value in sorted_categories[5:]), 2)})
        category_total = sum(row["value"] for row in category_rows)
        for row in category_rows:
            row["share"] = round((row["value"] / category_total) * 100.0, 1) if category_total else 0.0
        total_order_count = len(confirmed)

        supplier_stats = defaultdict(lambda: {"name": "", "eligible": 0, "on_time": 0})
        orders_attention = []
        for order in orders:
            partner = order.partner_id
            supplier_name = partner.display_name if partner else "Unknown supplier"
            branch_name = order_branch(order)
            order_lines = lines_for(order)
            remaining_lines = [line for line in order_lines if line_remaining(line) > 0]
            overdue_lines = [line for line in remaining_lines if getattr(line, "date_planned", False) and fields.Datetime.to_datetime(line.date_planned) < now]
            order_picks = order.picking_ids if "picking_ids" in order._fields else self.env["stock.picking"].browse()
            incoming_order_picks = order_picks.filtered(lambda picking: picking.picking_type_id and picking.picking_type_id.code == "incoming")
            done_picks = incoming_order_picks.filtered(lambda picking: picking.state == "done" and picking.date_done)
            open_order_picks = incoming_order_picks.filtered(lambda picking: picking.state not in ("done", "cancel"))
            expected_dates = [fields.Datetime.to_datetime(line.date_planned) for line in order_lines if getattr(line, "date_planned", False)]
            actual_dates = [fields.Datetime.to_datetime(picking.date_done) for picking in done_picks if picking.date_done]
            if order.state in ("purchase", "done") and expected_dates and actual_dates and not open_order_picks:
                stats = supplier_stats[partner.id if partner else 0]
                stats["name"] = supplier_name
                stats["eligible"] += 1
                if max(actual_dates) <= max(expected_dates):
                    stats["on_time"] += 1

            issue = False
            expected = False
            status_label, status_class = order_status(order)
            if order.state == "to approve":
                issue = "Awaiting approval"
            elif order.state in ("draft", "sent"):
                issue = "Awaiting confirmation"
            elif order.id in overdue_order_ids or overdue_lines or open_order_picks.filtered(lambda picking: picking.scheduled_date and fields.Datetime.to_datetime(picking.scheduled_date) < now):
                issue = "Overdue delivery"
                status_label, status_class = "Overdue", "o_sm_pur_status_bad"
            elif remaining_lines and any(float(getattr(line, "qty_received", 0.0) or 0.0) > 0 for line in order_lines):
                issue = "Partial delivery"
                status_label, status_class = "Partial", "o_sm_pur_status_warn"
            if issue:
                dates = [fields.Date.to_date(line.date_planned) for line in order_lines if getattr(line, "date_planned", False)]
                if open_order_picks:
                    dates.extend(fields.Date.to_date(picking.scheduled_date) for picking in open_order_picks if picking.scheduled_date)
                orders_attention.append({
                    "name": order.name,
                    "supplier": supplier_name,
                    "branch": branch_name,
                    "issue": issue,
                    "expected_date": fields.Date.to_string(min(dates)) if dates else "",
                    "status": status_label,
                    "status_class": status_class,
                    "date_order": fields.Date.to_string(order_date(order)) if order_date(order) else "",
                })

        supplier_performance = []
        for stats in supplier_stats.values():
            if stats["eligible"]:
                supplier_performance.append({
                    "name": stats["name"],
                    "orders": stats["eligible"],
                    "on_time": stats["on_time"],
                    "rate": round(stats["on_time"] * 100.0 / stats["eligible"], 1),
                })
        supplier_performance.sort(key=lambda row: (row["orders"], row["rate"]), reverse=True)
        supplier_performance = supplier_performance[:5]

        price_trends = []
        top_product_ids = [product_id for product_id, _ in sorted(product_totals.items(), key=lambda item: item[1], reverse=True)[:5]]
        for product_id in top_product_ids:
            monthly_prices = []
            available_prices = []
            for key in month_keys:
                bucket = monthly_product_price.get((product_id, key))
                value = round(bucket["total"] / bucket["qty"], 2) if bucket and bucket["qty"] else False
                monthly_prices.append({"key": key, "label": month_label_by_key[key], "value": value})
                if value is not False:
                    available_prices.append(value)
            if len(available_prices) < 2:
                trend_label = "Insufficient history"
            elif available_prices[-1] > available_prices[0]:
                trend_label = "Rising"
            elif available_prices[-1] < available_prices[0]:
                trend_label = "Falling"
            else:
                trend_label = "Stable"
            price_trends.append({
                "product_id": product_id,
                "name": product_labels.get(product_id, "Product"),
                "unit": product_units.get(product_id, ""),
                "monthly_prices": monthly_prices,
                "trend": trend_label,
            })

        recent_orders = []
        for order in orders[:10]:
            status_label, status_class = order_status(order)
            recent_orders.append({
                "id": order.id,
                "name": order.name,
                "supplier": order.partner_id.display_name if order.partner_id else "Unknown supplier",
                "branch": order_branch(order),
                "date": fields.Date.to_string(order_date(order)) if order_date(order) else "",
                "amount": round(to_company_currency(order.amount_untaxed, order.currency_id if "currency_id" in order._fields else company_currency, order.date_order), 2),
                "status": status_label,
                "status_class": status_class,
            })

        return {
            "available": True,
            "period": {"start": fields.Date.to_string(start), "end": fields.Date.to_string(end)},
            "currency": currency_data,
            "branch_options": branch_options,
            "selected_warehouse_id": selected_warehouse.id if selected_warehouse else False,
            "purchase_order_count": len(confirmed),
            "purchase_order_value": round(purchase_order_value, 2),
            "pending_delivery_count": len(open_receipts) if receipts_available else False,
            "pending_delivery_value": round(pending_delivery_value, 2),
            "overdue_delivery_count": len(overdue_receipts) if receipts_available else False,
            "overdue_delivery_value": round(overdue_delivery_value, 2),
            "on_time_delivery_rate": round(sum(row["on_time"] for row in supplier_stats.values()) * 100.0 / sum(row["eligible"] for row in supplier_stats.values()), 1) if sum(row["eligible"] for row in supplier_stats.values()) else False,
            "on_time_delivery_available": bool(sum(row["eligible"] for row in supplier_stats.values())),
            "expenditure_trend": expenditure_trend,
            "purchase_categories": category_rows,
            "purchase_category_total": round(category_total, 2),
            "supplier_performance": supplier_performance,
            "orders_attention": sorted(orders_attention, key=lambda row: (row["status"] != "Overdue", row["expected_date"] or "9999-12-31"))[:8],
            "recent_orders": recent_orders,
            "price_months": [{"key": key, "label": month_label_by_key[key]} for key in month_keys],
            "ingredient_price_trend": price_trends,
            "messages": [] if orders else ["No purchase orders match the selected period and branch."],
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
    def _overview_safe(self, function):
        """Run one optional source query without making the whole dashboard fail."""
        try:
            return function()
        except (AccessError, UserError, ValidationError, KeyError, TypeError, ValueError):
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
    def _overview_month_buckets(self, start, end):
        return self._overview_period_buckets(start, end, "month")

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
    def get_inventory_waste_data(self, date_start=False, date_end=False, warehouse_id=False, product_id=False):
        """Return actual stock, expiry, scrap and movement data for authorized warehouses."""
        start, end = self._resolve_period(date_start, date_end)
        today = fields.Date.context_today(self)
        if end > today:
            end = today
        if start > today:
            start = today
        if start > end:
            start = end

        company = self.env.company
        user = self.env.user
        is_head_office = user.has_group("strategic_management.group_strategic_head_office_manager")
        is_branch_manager = user.has_group("strategic_management.group_strategic_branch_manager") and not is_head_office
        currency = company.currency_id
        currency_data = {
            "name": currency.name if currency else "USD",
            "symbol": currency.symbol if currency else "$",
            "position": currency.position if currency else "before",
            "decimal_places": int(currency.decimal_places) if currency and "decimal_places" in currency._fields else 2,
        }

        empty_response = {
            "available": False,
            "role": "head_office" if is_head_office else "branch_manager" if is_branch_manager else "employee",
            "period": {"start": fields.Date.to_string(start), "end": fields.Date.to_string(end)},
            "branch_options": [],
            "selected_warehouse_id": False,
            "currency": currency_data,
            "summary": {"low_stock_count": False, "expiring_soon_count": False, "waste_cost": False, "discrepancy_count": False},
            "stock_health": [], "waste_by_product": [], "inventory_products": [],
            "selected_product_id": False, "movement": False, "expiry_tracking_available": False, "expiring_stock": [],
            "stock_discrepancies": [], "attention": [], "branch_comparison": [], "messages": [],
        }
        if not is_head_office and not is_branch_manager:
            empty_response["messages"] = ["Inventory-wide figures are available to authorized managers."]
            return empty_response

        Warehouse = self.env["stock.warehouse"]
        if is_head_office:
            permitted_warehouses = Warehouse.search([("company_id", "=", company.id)], order="name, id")
        else:
            assigned = user.strategic_branch_warehouse_id
            if not assigned or (assigned.company_id and assigned.company_id != company):
                raise AccessError("Assign this Branch Manager to a warehouse in the active company.")
            permitted_warehouses = assigned

        selected_warehouse = False
        if is_head_office and warehouse_id:
            try:
                requested_id = int(warehouse_id)
            except (TypeError, ValueError):
                raise ValidationError("Choose a valid branch.")
            selected_warehouse = permitted_warehouses.filtered(lambda wh: wh.id == requested_id)[:1]
            if not selected_warehouse:
                raise AccessError("You cannot view the selected branch.")
        elif is_branch_manager:
            selected_warehouse = permitted_warehouses[:1]

        scoped_warehouses = selected_warehouse or permitted_warehouses
        branch_options = [{"id": wh.id, "name": wh.display_name} for wh in permitted_warehouses]
        warehouse_ids = scoped_warehouses.ids
        empty_response.update({
            "role": "head_office" if is_head_office else "branch_manager",
            "branch_options": branch_options,
            "selected_warehouse_id": selected_warehouse.id if selected_warehouse else False,
        })

        Location = self.env["stock.location"]
        location_ids_by_warehouse = {}
        location_to_warehouse = {}
        location_usage = {}
        location_names = {}
        location_query_failed = False
        for warehouse in scoped_warehouses:
            if not warehouse.view_location_id:
                location_ids_by_warehouse[warehouse.id] = set()
                continue
            locations = self._overview_safe(lambda warehouse=warehouse: Location.search([
                ("id", "child_of", warehouse.view_location_id.id), ("usage", "=", "internal")
            ]))
            if locations is False:
                location_query_failed = True
                location_ids_by_warehouse[warehouse.id] = set()
                continue
            ids = set(locations.ids)
            location_ids_by_warehouse[warehouse.id] = ids
            for location in locations:
                location_to_warehouse[location.id] = warehouse.id
                location_usage[location.id] = location.usage
                location_names[location.id] = location.display_name
        scoped_location_ids = sorted({location_id for ids in location_ids_by_warehouse.values() for location_id in ids})

        Quant = self.env["stock.quant"]
        quant_field_candidates = [
            "product_id", "location_id", "quantity", "reserved_quantity", "available_quantity", "lot_id",
            "inventory_quantity_set", "inventory_quantity", "inventory_diff_quantity", "expiration_date",
        ]
        quant_fields = [name for name in quant_field_candidates if name in Quant._fields]
        if not scoped_location_ids:
            quant_rows = []
        else:
            quant_rows = self._overview_safe(lambda: Quant.search_read(
                [("location_id", "in", scoped_location_ids)], quant_fields, order="product_id, location_id, id", limit=50000
            ))
        if quant_rows is False:
            empty_response["messages"] = ["Stock quantities could not be read with the current access rights."]
            return empty_response

        product_ids = set()
        product_labels = {}
        product_wh = defaultdict(lambda: {
            "quantity": 0.0, "available_quantity": 0.0, "quant_rows": 0,
            "counted_rows": 0, "physical_count": 0.0, "variance": 0.0,
        })
        lot_quantity_by_branch = defaultdict(float)
        lot_info = {}
        discrepancy_delta = defaultdict(float)
        for row in quant_rows:
            product_ref = row.get("product_id")
            location_ref = row.get("location_id")
            if not product_ref or not location_ref:
                continue
            product_id_row = product_ref[0]
            location_id_row = location_ref[0]
            warehouse_ref = location_to_warehouse.get(location_id_row)
            if not warehouse_ref:
                continue
            product_ids.add(product_id_row)
            product_labels.setdefault(product_id_row, product_ref[1])
            quantity = float(row.get("quantity") or 0.0)
            reserved = float(row.get("reserved_quantity") or 0.0)
            available = float(row.get("available_quantity") or 0.0) if "available_quantity" in quant_fields else quantity - reserved
            bucket = product_wh[(product_id_row, warehouse_ref)]
            bucket["quantity"] += quantity
            bucket["available_quantity"] += available
            bucket["quant_rows"] += 1
            if row.get("inventory_quantity_set"):
                bucket["counted_rows"] += 1
                bucket["physical_count"] += float(row.get("inventory_quantity") or 0.0)
                bucket["variance"] += float(row.get("inventory_diff_quantity") or 0.0)
                delta = float(row.get("inventory_diff_quantity") or 0.0)
                if abs(delta) > 0.000001:
                    discrepancy_delta[(product_id_row, warehouse_ref)] += delta
            lot_ref = row.get("lot_id")
            expiry_value = row.get("expiration_date") if "expiration_date" in quant_fields else False
            if lot_ref:
                lot_key = (lot_ref[0], warehouse_ref)
                lot_quantity_by_branch[lot_key] += quantity
                lot_info.setdefault(lot_key, {
                    "lot_id": lot_ref[0], "lot_name": lot_ref[1], "product_id": product_id_row,
                    "expiry": expiry_value, "warehouse_id": warehouse_ref,
                })
                if expiry_value and not lot_info[lot_key].get("expiry"):
                    lot_info[lot_key]["expiry"] = expiry_value

        Orderpoint = self.env["stock.warehouse.orderpoint"]
        required_orderpoint_fields = {"product_id", "product_min_qty", "qty_forecast", "warehouse_id"}
        orderpoint_available = required_orderpoint_fields.issubset(Orderpoint._fields)
        orderpoint_rows = False
        if orderpoint_available and warehouse_ids:
            domain = [("warehouse_id", "in", warehouse_ids), ("product_min_qty", ">", 0)]
            if "company_id" in Orderpoint._fields:
                domain.append(("company_id", "=", company.id))
            fields_to_read = [name for name in ("product_id", "product_min_qty", "qty_forecast", "warehouse_id", "product_max_qty") if name in Orderpoint._fields]
            orderpoint_rows = self._overview_safe(lambda: Orderpoint.search_read(domain, fields_to_read, order="id asc", limit=2000))
            if orderpoint_rows is not False:
                for row in orderpoint_rows:
                    if row.get("product_id"):
                        product_ids.add(row["product_id"][0])
                        product_labels.setdefault(row["product_id"][0], row["product_id"][1])

        Product = self.env["product.product"]
        product_meta = {}
        if product_ids:
            product_read_fields = [name for name in ("display_name", "uom_id", "categ_id") if name in Product._fields]
            product_rows = self._overview_safe(lambda: Product.browse(sorted(product_ids)).read(product_read_fields)) or []
            for row in product_rows:
                product_meta[row["id"]] = {
                    "name": row.get("display_name") or product_labels.get(row["id"]) or "Product",
                    "uom": row.get("uom_id", [False, ""])[1] if row.get("uom_id") else "",
                    "category": row.get("categ_id", [False, "Uncategorized"])[1] if row.get("categ_id") else "Uncategorized",
                }
            if "standard_price" in Product._fields:
                cost_rows = self._overview_safe(lambda: Product.browse(sorted(product_ids)).read(["standard_price"])) or []
                for row in cost_rows:
                    product_meta.setdefault(row["id"], {"name": product_labels.get(row["id"]) or "Product", "uom": "", "category": "Uncategorized"})
                    product_meta[row["id"]]["standard_price"] = float(row.get("standard_price") or 0.0)
        for pid, label in product_labels.items():
            product_meta.setdefault(pid, {"name": label or "Product", "uom": "", "category": "Uncategorized", "standard_price": 0.0})

        low_stock_all = []
        if orderpoint_rows is not False and orderpoint_rows is not None:
            for row in orderpoint_rows:
                forecast = float(row.get("qty_forecast") or 0.0)
                minimum = float(row.get("product_min_qty") or 0.0)
                if forecast >= minimum:
                    continue
                product_ref = row.get("product_id") or [False, "Product"]
                warehouse_ref = row.get("warehouse_id") or [False, "Unassigned warehouse"]
                pid = product_ref[0]
                wid = warehouse_ref[0]
                quantities = product_wh.get((pid, wid), {})
                available = float(quantities.get("available_quantity", 0.0))
                meta = product_meta.get(pid, {"name": product_ref[1], "uom": "", "category": "Uncategorized"})
                low_stock_all.append({
                    "product_id": pid, "name": meta.get("name") or product_ref[1], "category": meta.get("category") or "Uncategorized",
                    "warehouse_id": wid, "branch_name": warehouse_ref[1], "available_quantity": round(available, 4),
                    "forecast_quantity": round(forecast, 4), "minimum_quantity": round(minimum, 4),
                    "unit": meta.get("uom") or "", "status": "critical" if forecast <= 0 else "low",
                    "status_label": "Reorder" if forecast <= 0 else "Low stock",
                    "stock_percent": round(max(0.0, min(100.0, (available / minimum * 100.0) if minimum else 0.0)), 1),
                })
        low_stock_all.sort(key=lambda item: (item["forecast_quantity"] / item["minimum_quantity"] if item["minimum_quantity"] else 0, item["name"]))
        low_stock_count = len(low_stock_all) if orderpoint_rows is not False and orderpoint_rows else (0 if orderpoint_rows is not False and orderpoint_rows is not None and len(orderpoint_rows) else False)
        stock_health = low_stock_all[:10]

        inventory_products = [{"id": pid, "name": meta.get("name") or product_labels.get(pid) or "Product", "unit": meta.get("uom") or ""}
                              for pid, meta in product_meta.items()]
        inventory_products.sort(key=lambda item: item["name"].casefold())
        inventory_products = inventory_products[:200]
        valid_product_ids = {item["id"] for item in inventory_products}
        try:
            requested_product_id = int(product_id) if product_id else False
        except (TypeError, ValueError):
            requested_product_id = False
        selected_product_id = requested_product_id if requested_product_id in valid_product_ids else (inventory_products[0]["id"] if inventory_products else False)

        # Expiry data is sourced only from tracked lot quantities with a configured expiry date.
        expiry_available = "expiration_date" in quant_fields
        if not expiry_available and "stock.lot" in self.env.registry.models:
            Lot = self.env["stock.lot"]
            expiry_available = "expiration_date" in Lot._fields
            if expiry_available:
                lot_ids = sorted({key[0] for key in lot_quantity_by_branch})
                if lot_ids:
                    lot_rows = self._overview_safe(lambda: Lot.browse(lot_ids).read([name for name in ("name", "product_id", "expiration_date") if name in Lot._fields])) or []
                    lot_rows_by_id = {row["id"]: row for row in lot_rows}
                    for key, info in lot_info.items():
                        lot_row = lot_rows_by_id.get(info["lot_id"], {})
                        info["expiry"] = lot_row.get("expiration_date") or info.get("expiry")
                        if lot_row.get("name"):
                            info["lot_name"] = lot_row["name"]
        expiring_stock = []
        today_value = fields.Date.to_date(today)
        if expiry_available:
            for key, info in lot_info.items():
                quantity = float(lot_quantity_by_branch.get(key, 0.0))
                if quantity <= 0 or not info.get("expiry"):
                    continue
                try:
                    expiry_date = fields.Date.to_date(str(info["expiry"])[:10])
                except (TypeError, ValueError):
                    continue
                days_left = (expiry_date - today_value).days
                if days_left > 7:
                    continue
                pid = info["product_id"]
                wid = info["warehouse_id"]
                product = product_meta.get(pid, {"name": product_labels.get(pid) or "Product", "uom": "", "standard_price": 0.0})
                warehouse = scoped_warehouses.filtered(lambda wh: wh.id == wid)[:1]
                cost = float(product.get("standard_price") or 0.0) * quantity
                if days_left < 0:
                    status = "Expired"
                elif days_left == 0:
                    status = "Expires today"
                else:
                    status = "In %s days" % days_left
                expiring_stock.append({
                    "lot_id": info["lot_id"], "lot_name": info.get("lot_name") or "Tracked lot",
                    "product_id": pid, "product": product.get("name") or "Product",
                    "branch": warehouse.display_name if warehouse else "Unknown branch",
                    "warehouse_id": wid, "expiry_date": fields.Date.to_string(expiry_date),
                    "days_left": days_left, "quantity": round(quantity, 4), "unit": product.get("uom") or "",
                    "value_at_cost": round(cost, 2), "status": status,
                })
        expiring_stock.sort(key=lambda item: (item["expiry_date"], item["product"]))
        expiring_soon_count = sum(1 for item in expiring_stock if 0 <= item["days_left"] <= 7) if expiry_available else False

        stock_discrepancies = []
        for (pid, wid), delta in discrepancy_delta.items():
            meta = product_meta.get(pid, {"name": product_labels.get(pid) or "Product", "uom": ""})
            warehouse = scoped_warehouses.filtered(lambda wh: wh.id == wid)[:1]
            stock_discrepancies.append({
                "product_id": pid, "product": meta.get("name") or "Product", "warehouse_id": wid,
                "branch": warehouse.display_name if warehouse else "Unknown branch", "variance": round(delta, 4),
                "unit": meta.get("uom") or "",
            })
        stock_discrepancies.sort(key=lambda item: abs(item["variance"]), reverse=True)
        discrepancy_count = len(stock_discrepancies) if {"inventory_quantity_set", "inventory_diff_quantity"}.issubset(Quant._fields) else False

        start_utc, end_utc = self._overview_utc_bounds(start, end)
        waste_cost = self._overview_waste_cost(start, end, selected_warehouse)
        waste_by_product = []
        waste_breakdown_available = waste_cost is not False
        Move = self.env["stock.move"]
        def _inventory_move_qty(move):
            if "quantity_product_uom" in Move._fields:
                return float(move.quantity_product_uom or 0.0)
            quantity = float(move.quantity or 0.0) if "quantity" in Move._fields else float(move.product_uom_qty or 0.0)
            if "product_uom" in Move._fields and move.product_uom and move.product_id and move.product_id.uom_id and move.product_uom != move.product_id.uom_id:
                try:
                    quantity = move.product_uom._compute_quantity(quantity, move.product_id.uom_id)
                except (UserError, ValidationError, TypeError, ValueError):
                    pass
            return quantity

        waste_required = {"is_scrap", "date", "state", "company_id", "scrap_reason_tag_ids", "location_id", "product_id"}
        if waste_breakdown_available and waste_required.issubset(Move._fields) and scoped_location_ids:
            waste_domain = [
                ("company_id", "=", company.id), ("state", "=", "done"), ("is_scrap", "=", True),
                ("date", ">=", fields.Datetime.to_string(start_utc)), ("date", "<", fields.Datetime.to_string(end_utc)),
                ("location_id", "in", scoped_location_ids),
            ]
            scrap_moves = self._overview_safe(lambda: Move.search(waste_domain, order="date desc, id desc"))
            if scrap_moves is False:
                waste_breakdown_available = False
            elif scrap_moves:
                allowed_tags = {"spoilage/expiry", "damaged goods"}
                for move in scrap_moves:
                    tags = {(tag.name or "").strip().casefold() for tag in move.scrap_reason_tag_ids}
                    if not tags or tags.difference(allowed_tags):
                        waste_breakdown_available = False
                        break
                cost_by_move = self._overview_scrap_costs(scrap_moves) if waste_breakdown_available else False
                if cost_by_move is False:
                    waste_breakdown_available = False
                else:
                    waste_by_product_map = defaultdict(lambda: {"cost": 0.0, "quantity": 0.0, "product": "Product", "category": "Uncategorized", "unit": ""})
                    for move in scrap_moves:
                        pid = move.product_id.id
                        meta = product_meta.get(pid, {"name": move.product_id.display_name, "category": move.product_id.categ_id.display_name if move.product_id.categ_id else "Uncategorized", "uom": move.product_id.uom_id.name if move.product_id.uom_id else ""})
                        qty = _inventory_move_qty(move)
                        bucket = waste_by_product_map[pid]
                        bucket["product"] = meta.get("name") or move.product_id.display_name
                        bucket["category"] = meta.get("category") or "Uncategorized"
                        bucket["unit"] = meta.get("uom") or ""
                        bucket["quantity"] += qty
                        bucket["cost"] += cost_by_move.get(move.id, 0.0)
                    waste_by_product = [dict(product_id=pid, cost=round(values["cost"], 2), quantity=round(values["quantity"], 4), product=values["product"], category=values["category"], unit=values["unit"])
                                         for pid, values in waste_by_product_map.items() if values["cost"] > 0]
                    waste_by_product.sort(key=lambda item: item["cost"], reverse=True)

        # Reconstruct period movement from actual completed stock moves and current quants.
        movement = False
        if selected_product_id and scoped_location_ids:
            current_bucket = [values for (pid, _wid), values in product_wh.items() if pid == selected_product_id]
            current_quantity = sum(float(values["quantity"]) for values in current_bucket)
            current_counted = bool(current_bucket) and all(values["counted_rows"] == values["quant_rows"] and values["quant_rows"] > 0 for values in current_bucket)
            physical_count = sum(float(values["physical_count"]) for values in current_bucket) if current_counted else False
            physical_variance = sum(float(values["variance"]) for values in current_bucket) if current_counted else False
            movement_fields_ok = {"company_id", "state", "product_id", "date", "location_id", "location_dest_id"}.issubset(Move._fields)
            movement_rows = False
            if movement_fields_ok:
                movement_domain = [
                    ("company_id", "=", company.id), ("state", "=", "done"), ("product_id", "=", selected_product_id),
                    # No upper bound: every completed move up to now explains current stock;
                    # a "< now" bound drops moves done in the current second.
                    ("date", ">=", fields.Datetime.to_string(start_utc)),
                    "|", ("location_id", "in", scoped_location_ids), ("location_dest_id", "in", scoped_location_ids),
                ]
                movement_rows = self._overview_safe(lambda: Move.search(movement_domain, order="date asc, id asc", limit=30000))
            if movement_rows is not False:
                net_since_start = 0.0
                period_net = 0.0
                received_qty = 0.0
                consumption_qty = 0.0
                adjustment_qty = 0.0
                waste_qty = 0.0
                tags_field = "scrap_reason_tag_ids" in Move._fields
                allowed_tags = {"spoilage/expiry", "damaged goods"}
                for move in movement_rows:
                    source_id = move.location_id.id
                    dest_id = move.location_dest_id.id
                    source_in = source_id in location_to_warehouse
                    dest_in = dest_id in location_to_warehouse
                    if dest_in and not source_in:
                        net_delta = 1.0
                    elif source_in and not dest_in:
                        net_delta = -1.0
                    else:
                        net_delta = 0.0
                    qty = _inventory_move_qty(move)
                    delta = net_delta * qty
                    net_since_start += delta
                    if move.date and move.date < end_utc:
                        period_net += delta
                        if net_delta > 0 and move.location_id.usage == "supplier":
                            received_qty += qty
                        # Actual consumption: completed issues to customers (POS/sales) or production.
                        counterpart = move.location_dest_id if net_delta < 0 else move.location_id
                        if net_delta < 0 and counterpart.usage in ("customer", "production"):
                            consumption_qty += qty
                        elif net_delta and counterpart.usage == "inventory" and not ("is_scrap" in Move._fields and move.is_scrap):
                            adjustment_qty += delta
                        if net_delta < 0 and "is_scrap" in Move._fields and move.is_scrap and tags_field:
                            tags = {(tag.name or "").strip().casefold() for tag in move.scrap_reason_tag_ids}
                            if tags and not tags.difference(allowed_tags):
                                waste_qty += qty
                opening_quantity = current_quantity - net_since_start
                expected_closing = opening_quantity + period_net
                other_net = period_net - received_qty + consumption_qty + waste_qty - adjustment_qty
                opening_value = round(opening_quantity, 4) if opening_quantity >= -0.0001 else False
                expected_value = round(expected_closing, 4) if opening_quantity is not False and expected_closing >= -0.0001 else False
                physical_applicable = end >= today
                meta = product_meta.get(selected_product_id, {"name": product_labels.get(selected_product_id) or "Product", "uom": ""})
                movement = {
                    "product_id": selected_product_id, "product": meta.get("name") or "Product", "unit": meta.get("uom") or "",
                    "opening_stock": opening_value, "received_quantity": round(received_qty, 4),
                    "consumption_quantity": round(consumption_qty, 4), "adjustment_quantity": round(adjustment_qty, 4) or 0.0,
                    "waste_quantity": round(waste_qty, 4), "other_net_quantity": round(other_net, 4) or 0.0,
                    "expected_closing_stock": expected_value,
                    "physical_count": physical_count if physical_applicable else False,
                    "variance": physical_variance if physical_applicable else False,
                    "message": "Opening stock is reconstructed from completed movements since the selected period began. Physical count compares pending inventory counts with system stock.",
                }
                recipe_value, recipe_note = self._overview_recipe_estimate(start_utc, end_utc, selected_product_id, scoped_warehouses)
                movement.update({"recipe_consumption_quantity": recipe_value, "recipe_consumption_note": recipe_note})
                if opening_value is False or expected_value is False:
                    movement["message"] = "Opening or expected closing stock could not be reconciled from the accessible movement history."
            else:
                movement = {"product_id": selected_product_id, "product": product_meta.get(selected_product_id, {}).get("name") or "Product", "unit": product_meta.get(selected_product_id, {}).get("uom") or "", "message": "Completed stock movement history is unavailable for this product and warehouse scope."}

        discrepancy_rows_by_warehouse = defaultdict(int)
        for item in stock_discrepancies:
            discrepancy_rows_by_warehouse[item["warehouse_id"]] += 1
        low_stock_by_warehouse = defaultdict(int)
        for item in low_stock_all:
            low_stock_by_warehouse[item["warehouse_id"]] += 1
        branch_comparison = []
        for warehouse in scoped_warehouses:
            branch_waste = self._overview_waste_cost(start, end, warehouse)
            branch_comparison.append({
                "warehouse_id": warehouse.id, "branch": warehouse.display_name,
                "low_stock_count": low_stock_by_warehouse.get(warehouse.id, 0) if orderpoint_rows is not False and orderpoint_rows else False,
                "waste_cost": branch_waste,
                "variance_count": discrepancy_rows_by_warehouse.get(warehouse.id, 0) if discrepancy_count is not False else False,
            })

        attention = []
        for item in low_stock_all[:4]:
            attention.append({"kind": "stock", "severity": "high" if item["status"] == "critical" else "medium", "title": item["status_label"], "detail": "%s - %s - %s %s available" % (item["branch_name"], item["name"], item["available_quantity"], item["unit"]), "date": "", "res_model": "product.product", "res_id": item["product_id"]})
        for item in expiring_stock[:4]:
            if item["days_left"] <= 7:
                attention.append({"kind": "expiry", "severity": "high" if item["days_left"] < 0 else "medium", "title": item["status"], "detail": "%s - %s - %s %s" % (item["branch"], item["product"], item["quantity"], item["unit"]), "date": item["expiry_date"], "res_model": "stock.lot", "res_id": item["lot_id"]})
        for item in stock_discrepancies[:3]:
            attention.append({"kind": "variance", "severity": "review", "title": "Unreconciled stock count", "detail": "%s - %s - variance %s %s" % (item["branch"], item["product"], item["variance"], item["unit"]), "date": "", "res_model": "product.product", "res_id": item["product_id"]})
        attention = attention[:10]

        messages = []
        if location_query_failed:
            messages.append("Some warehouse locations could not be read; figures may be incomplete.")
        if not orderpoint_available or orderpoint_rows is False or not orderpoint_rows:
            messages.append("Low-stock classification requires readable replenishment rules for the selected warehouses.")
        if not expiry_available:
            messages.append("Expiry tracking is not enabled on the available stock data; the expiring-stock panel cannot be calculated.")
        manufacturing_recipes = self._overview_safe(lambda: self._overview_pos_recipes_not_kits(company))
        if manufacturing_recipes:
            messages.append(
                "Ingredients are not deducted by POS sales for: %s. Their recipe is a manufacturing BoM; set BoM Type to Kit "
                "so each sale consumes its ingredients from branch stock." % ", ".join(manufacturing_recipes[:5]))
        if waste_cost is False:
            messages.append("Waste cost is unavailable because the required recorded scrap reasons or valuation data are missing.")
        if not waste_by_product and waste_cost not in (False, None, 0, 0.0):
            messages.append("Waste-by-product breakdown is unavailable for the selected period.")

        return {
            "available": True,
            "role": "head_office" if is_head_office else "branch_manager",
            "period": {"start": fields.Date.to_string(start), "end": fields.Date.to_string(end)},
            "branch_options": branch_options,
            "selected_warehouse_id": selected_warehouse.id if selected_warehouse else False,
            "currency": currency_data,
            "summary": {
                "low_stock_count": low_stock_count,
                "expiring_soon_count": expiring_soon_count,
                "waste_cost": waste_cost,
                "discrepancy_count": discrepancy_count,
            },
            "stock_health": stock_health,
            "waste_by_product": waste_by_product if waste_breakdown_available else [],
            "waste_breakdown_available": waste_breakdown_available,
            "inventory_products": inventory_products,
            "selected_product_id": selected_product_id or False,
            "movement": movement,
            "expiry_tracking_available": expiry_available,
            "expiring_stock": expiring_stock,
            "stock_discrepancies": stock_discrepancies,
            "attention": attention,
            "branch_comparison": branch_comparison,
            "messages": messages,
        }
    @api.model
    def _overview_waste_cost(self, start, end, warehouse=False):
        Move = self.env["stock.move"]
        required_fields = {"is_scrap", "date", "state", "company_id", "scrap_reason_tag_ids", "location_id"}
        if not required_fields.issubset(Move._fields):
            return False
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
    def _overview_pos_recipes_not_kits(self, company):
        """POS products whose only recipe is a manufacturing BoM, so sales never move ingredient stock."""
        if "mrp.bom" not in self.env.registry.models:
            return []
        # sudo: reads recipe configuration only; returns product names.
        boms = self.env["mrp.bom"].sudo().search([
            ("company_id", "in", [False, company.id]), ("type", "in", ["normal", "phantom"]),
            ("product_tmpl_id.available_in_pos", "=", True),
        ])
        kits = boms.filtered(lambda bom: bom.type == "phantom").product_tmpl_id
        return sorted(set((boms.product_tmpl_id - kits).mapped("display_name")))

    @api.model
    def _overview_recipe_estimate(self, start_utc, end_utc, product_id, warehouses):
        """Estimated ingredient use from completed POS sales and effective recipes.

        Kept separate from recorded stock moves and waste: it is an estimate.
        Returns (quantity or False, explanation).
        """
        if "mrp.bom" not in self.env.registry.models:
            return False, "Recipe consumption: Not Configured (Manufacturing app for recipes is not installed)."
        company = self.env.company
        # sudo: limited to POS registers of the user's authorized branch warehouses;
        # only an aggregated quantity is returned.
        configs = self.env["pos.config"].sudo().search([
            ("company_id", "=", company.id), ("picking_type_id.warehouse_id", "in", warehouses.ids),
        ])
        if not configs:
            return False, "Recipe consumption: Not Available (no POS register is mapped to the selected branch)."
        orders = self.env["pos.order"].sudo().search([
            ("company_id", "=", company.id), ("config_id", "in", configs.ids), ("state", "in", _POS_STATES),
            ("date_order", ">=", fields.Datetime.to_string(start_utc)), ("date_order", "<", fields.Datetime.to_string(end_utc)),
        ])
        if not orders:
            return False, "Recipe consumption: Not Available (no completed POS sales in this period)."
        status, value, reason = recipe_consumption(self.env, company, orders, self.env["product.product"].browse(product_id))[:3]
        if status == "valid":
            return round(value, 4), "Estimated recipe consumption (not included above): " + reason
        return False, "Recipe consumption: Not Available. " + reason

    @api.model
    def _overview_scrap_costs(self, moves):
        """Return {move id: stock value lost} for scrap moves, or False if any is unvalued.

        Odoo 19+ stores valuation on the move itself (``stock.move.value``);
        older versions use ``stock.valuation.layer``. A scrap without valuation
        makes the total incomplete, so no partial figure is returned.
        """
        Move = self.env["stock.move"]
        if "value" in Move._fields and "is_valued" in Move._fields:
            if not all(move.is_valued for move in moves):
                return False
            # Outgoing moves carry a negative value; the cost is the value lost.
            return {move.id: abs(float(move.value or 0.0)) for move in moves}
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

        Orderpoint = self.env["stock.warehouse.orderpoint"]
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
        rows = self._overview_safe(lambda: Activity.search_read(domain, fields_to_read, order="date_deadline asc, id asc", limit=5))
        if not rows:
            return []
        return [{
            "name": row.get("summary") or row.get("res_name") or "Follow-up",
            "responsible": row.get("user_id", [False, "Unassigned"])[1] if row.get("user_id") else "Unassigned",
            "record": row.get("res_name") or row.get("res_model") or "Business record",
            "due_date": row.get("date_deadline") or "",
        } for row in rows]
