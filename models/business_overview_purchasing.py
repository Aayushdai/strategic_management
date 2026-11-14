"""Purchasing page data: orders, deliveries, suppliers, prices and spending for a period and branch.
Part of strategic.performance.dashboard; shared helpers live in business_overview.py."""

from collections import defaultdict
from datetime import datetime, timedelta

import pytz
from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.kpi_sources.purchase import confirmed_purchase_commitments
from .business_overview import _LIST_LIMIT


class StrategicPerformanceDashboardPurchasingOverview(models.AbstractModel):
    _inherit = "strategic.performance.dashboard"

    @api.model
    def get_purchasing_overview_data(self, date_start=False, date_end=False, warehouse_id=False):
        """Return Purchasing dashboard figures from actual Purchase and Stock records."""
        self._strategic_role()  # Admin stops here.
        user = self.env.user
        start, end = self._resolve_period(date_start, date_end)
        company = self.env.company
        is_head_office = user.has_group("strategic_management.group_strategic_head_office_manager")
        is_branch_manager = (user.has_group("strategic_management.group_strategic_branch_manager") and not is_head_office) or self._department_read_active()
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
            "open_deliveries": [],
            "recent_orders": [],
            "price_months": [],
            "ingredient_price_trend": [],
            "daily_spend": [],
            "previous_period": False,
            "spend_by_branch": [],
            "largest_order": False,
            "top_supplier": False,
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
        if not PurchaseOrder.has_access("read"):
            # Purchasing figures stay with Odoo's Purchase rights: say so plainly instead of failing.
            empty["messages"] = ["Purchasing figures need Odoo's Purchase access. Ask Head Office to give your account Purchase rights."]
            empty["branch_options"] = branch_options
            if selected_warehouse:
                empty["selected_warehouse_id"] = selected_warehouse.id
            return empty
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

        def received_lines(order):
            # Per product, in the line's own unit: quantities in different units cannot be added up.
            return [{"product": line.product_id.display_name, "unit": line.uom_id.name, "ordered": float(line.product_qty or 0.0),
                     "received": float(line.qty_received or 0.0)} for line in lines_for(order)]

        def line_remaining(line):
            ordered = float(getattr(line, "product_qty", 0.0) or 0.0)
            received = float(getattr(line, "qty_received", 0.0) or 0.0)
            return max(0.0, ordered - received)

        # Same calculation as the purchase_confirmed_amount KPI, so the page and goals agree.
        _committed_orders, _committed_rows, purchase_order_value, committed_issue = confirmed_purchase_commitments(
            self.env, company, start_utc, end_utc, Warehouse.browse(warehouse_ids)
        )
        # Open deliveries are current obligations, whenever the order was placed: every confirmed order in scope whose
        # receipts are not all done or cancelled (Odoo's receipt status). The period only scopes spend and order lists.
        if "receipt_status" in PurchaseOrder._fields:
            open_delivery_orders = PurchaseOrder.search(company_domain + [
                ("state", "in", ("purchase", "done")), ("receipt_status", "in", ("pending", "partial"))])
        else:
            open_delivery_orders = confirmed
        pending_delivery_value = 0.0
        outstanding_by_order = {}
        overdue_line_order_ids = set()
        now = fields.Datetime.to_datetime(fields.Datetime.now())
        for order in open_delivery_orders:
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
            all_receipts = open_delivery_orders.mapped("picking_ids")
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
                for order in open_delivery_orders:
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
        month_order_ids = defaultdict(list)  # the orders behind each trend month, for its drill
        for order in confirmed:
            dt = order_date(order)
            if dt:
                key = dt.strftime("%Y-%m")
                month_order_ids[key].append(order.id)
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
                "drill": self._overview_drill("purchase.order", None, "Purchase orders, %s" % month_start.strftime("%b %Y"), ids=month_order_ids[key])
                if month_order_ids[key] else False,
            })

        categories = defaultdict(float)
        category_line_ids = defaultdict(list)  # the purchase lines behind each category and product, for their drills
        product_line_ids = defaultdict(list)
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
                category_line_ids[category].append(line.id)
                product_line_ids[product.id].append(line.id)
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
            names = [name for name, _value in sorted_categories[5:]] if row["name"] == "Other" and len(sorted_categories) > 5 else [row["name"]]
            row["drill"] = self._overview_drill("purchase.order.line", None, "Purchases in %s" % row["name"],
                                                ids=[line_id for name in names for line_id in category_line_ids[name]])

        supplier_stats = defaultdict(lambda: {"name": "", "eligible": 0, "on_time": 0, "order_ids": []})
        on_time_eligible = PurchaseOrder.browse()
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
                stats["order_ids"].append(order.id)
                on_time_eligible |= order
                if max(actual_dates) <= max(expected_dates):
                    stats["on_time"] += 1

            issue = False
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

        # Every order with a receipt still to come (any order date), overdue first; one row per order.
        open_deliveries = []
        for order in open_delivery_orders:
            open_picks = (order.picking_ids if "picking_ids" in order._fields else self.env["stock.picking"].browse()).filtered(
                lambda picking: picking.picking_type_id.code == "incoming" and picking.state not in ("done", "cancel"))
            if not open_picks and not outstanding_by_order.get(order.id):
                continue
            dates = [fields.Date.to_date(picking.scheduled_date) for picking in open_picks if picking.scheduled_date]
            received_any = any(float(getattr(line, "qty_received", 0.0) or 0.0) > 0 for line in lines_for(order))
            status = "Overdue" if order.id in overdue_order_ids else "Partial" if received_any else "Awaiting"
            open_deliveries.append({
                "name": order.name,
                "lines": received_lines(order),
                "supplier": order.partner_id.display_name if order.partner_id else "Unknown supplier",
                "branch": order_branch(order),
                "status": status,
                "expected_date": fields.Date.to_string(min(dates)) if dates else "",
                "outstanding_value": round(outstanding_by_order.get(order.id, 0.0), 2),
                "date_order": fields.Date.to_string(order_date(order)) if order_date(order) else "",
                "days_late": (fields.Date.context_today(self) - min(dates)).days if status == "Overdue" and dates else 0,
            })
        open_deliveries.sort(key=lambda row: (row["status"] != "Overdue", row["expected_date"] or "9999-12-31", row["name"]))

        supplier_performance = []
        for stats in supplier_stats.values():
            if stats["eligible"]:
                supplier_performance.append({
                    "name": stats["name"],
                    "orders": stats["eligible"],
                    "on_time": stats["on_time"],
                    "rate": round(stats["on_time"] * 100.0 / stats["eligible"], 1),
                    "drill": self._overview_drill("purchase.order", None, "Completed orders from %s" % stats["name"], ids=stats["order_ids"]),
                })
        supplier_performance.sort(key=lambda row: (row["orders"], row["rate"]), reverse=True)
        supplier_performance = supplier_performance[:_LIST_LIMIT]

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
                "drill": self._overview_drill("purchase.order.line", None, "Purchases of %s" % product_labels.get(product_id, "product"),
                                              ids=product_line_ids[product_id]),
            })

        def order_amount(order):
            return to_company_currency(order.amount_untaxed, order.currency_id if "currency_id" in order._fields else company_currency, order.date_order)

        spend_by_day = defaultdict(float)
        orders_by_day = defaultdict(int)
        spend_by_branch = defaultdict(float)
        spend_by_supplier = defaultdict(float)
        for order in confirmed:
            amount = order_amount(order)
            day = order_date(order)
            if day:
                spend_by_day[day] += amount
                orders_by_day[day] += 1
            spend_by_branch[order_branch(order)] += amount
            spend_by_supplier[order.partner_id.display_name if order.partner_id else "Unknown supplier"] += amount
        daily_spend = []
        day = start
        while day <= end:
            daily_spend.append({"date": fields.Date.to_string(day), "amount": round(spend_by_day.get(day, 0.0), 2), "orders": orders_by_day.get(day, 0)})
            day += timedelta(days=1)
        # Same length, immediately before; measured the same way as the headline (the purchase KPI's calculation).
        previous_last = start - timedelta(days=1)
        previous_first = previous_last - (end - start)
        prev_start_utc, prev_end_utc = self._overview_utc_bounds(previous_first, previous_last)
        previous_committed, _previous_rows, previous_value, _previous_issue = confirmed_purchase_commitments(
            self.env, company, prev_start_utc, prev_end_utc, Warehouse.browse(warehouse_ids))
        previous_period = {
            "start": fields.Date.to_string(previous_first), "end": fields.Date.to_string(previous_last),
            "spend": round(previous_value, 2) if previous_value is not None else False,
            "orders": len(previous_committed) if previous_committed is not None else False,
        }
        branch_rows = {row["name"]: 0.0 for row in branch_options} if not selected_warehouse else {}
        branch_rows.update(spend_by_branch)
        largest = max(confirmed, key=order_amount) if confirmed else False
        top_supplier = max(spend_by_supplier.items(), key=lambda pair: pair[1]) if spend_by_supplier else False

        recent_orders = []
        for order in orders[:_LIST_LIMIT]:
            status_label, status_class = order_status(order)
            recent_orders.append({
                "id": order.id,
                "lines": received_lines(order),
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
            "drill": {
                "purchase_orders": self._overview_drill("purchase.order", None, "Confirmed purchase orders", ids=confirmed.ids),
                "pending": self._overview_drill("stock.picking", None, "Pending deliveries", ids=open_receipts.ids) if receipts_available else False,
                "overdue": self._overview_drill("stock.picking", None, "Overdue deliveries", ids=overdue_receipts.ids) if receipts_available else False,
                "on_time": self._overview_drill("purchase.order", None, "Completed purchase orders measured for on-time delivery", ids=on_time_eligible.ids),
            },
            "purchase_order_value": round(purchase_order_value, 2) if purchase_order_value is not None else False,
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
            "orders_attention": sorted(orders_attention, key=lambda row: (row["status"] != "Overdue", row["expected_date"] or "9999-12-31"))[:_LIST_LIMIT],
            "open_deliveries": open_deliveries[:_LIST_LIMIT],
            "recent_orders": recent_orders,
            "price_months": [{"key": key, "label": month_label_by_key[key]} for key in month_keys],
            "ingredient_price_trend": price_trends,
            "daily_spend": daily_spend,
            "previous_period": previous_period,
            "spend_by_branch": sorted(({"name": name, "value": round(value, 2)} for name, value in branch_rows.items()),
                                      key=lambda row: (-row["value"], row["name"])),
            "largest_order": {"name": largest.name, "amount": round(order_amount(largest), 2)} if largest else False,
            "top_supplier": {"name": top_supplier[0], "amount": round(top_supplier[1], 2)} if top_supplier else False,
            "messages": ([] if orders else ["No purchase orders match the selected period and branch."]) + ([committed_issue] if committed_issue else []),
        }
