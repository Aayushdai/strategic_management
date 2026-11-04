"""Branch Manager Phase 6: the branch's sales, inventory and purchasing at a glance.

Nothing is calculated here that the Sales & Finance, Inventory & Waste and Purchasing pages do not already calculate:
this composes their existing, user-scoped methods for the manager's own branch and keeps a few figures and short lists.
A source the manager cannot read is reported as unavailable, never as zero.
"""

from odoo import api, fields, models
from odoo.exceptions import AccessError

_LIST = 5


class StrategicPerformanceDashboardBranchOperations(models.AbstractModel):
    _inherit = "strategic.performance.dashboard"

    @api.model
    def get_branch_operations(self, date_start=False, date_end=False):
        """Sales, inventory and purchasing for the Branch Manager's own branch over the reporting period."""
        role = self._strategic_role()
        if role != "branch_manager":
            raise AccessError("Branch operations are shown to Branch Managers for their own branch.")
        # The branch always comes from the manager's account; an invalid assignment is refused here.
        branch = self._strategic_branch_scope(role)[1]
        start, end = self._resolve_period(date_start, date_end)
        result = self._operations_header(branch, start, end)
        result.update({
            "sales": self._operations_section(lambda: self._operations_sales(start, end, result["granularity"])),
            "inventory": self._operations_section(lambda: self._operations_inventory(start, end, branch)),
            "purchasing": self._operations_section(lambda: self._operations_purchasing(start, end)),
        })
        return result

    @api.model
    def _operations_header(self, branch, start, end):
        days = (end - start).days + 1
        return {
            "branch": {"id": branch.id, "name": branch.display_name},
            "period": {"start": fields.Date.to_string(start), "end": fields.Date.to_string(end)},
            "granularity": "day" if days <= 31 else "week" if days <= 183 else "month",
        }

    @api.model
    def _operations_section(self, build):
        """A section the manager's access rights do not allow is unavailable, with the reason; other errors are real errors."""
        try:
            return build()
        except AccessError as error:
            return {"available": False, "messages": [str(error.args[0]) if error.args else "Not available with your access rights."]}

    @api.model
    def _operations_sales(self, start, end, granularity):
        data = self.get_business_overview_data(fields.Date.to_string(start), fields.Date.to_string(end), False, granularity)
        sales, orders = data.get("sales"), data.get("order_count")
        if sales is False:
            return {"available": False, "messages": ["POS sales could not be read with your access rights."]}
        return {
            "available": True,
            "currency": data["currency"],
            # Paid POS sales excluding tax, net of refunds; refund orders are not counted as orders.
            "net_sales": sales,
            "orders": orders,
            "drill": data.get("drill") or {},
            # Per completed (non-refund) order; no orders means no average, not zero.
            "average_order_value": round(sales / orders, 2) if orders else False,
            "trend": [{"key": row["month_key"], "label": row["label"], "sales": round(row["sales"], 2)} for row in data.get("sales_trend") or []],
            "messages": [message for message in data.get("messages") or [] if "currency" in message],
        }

    @api.model
    def _operations_inventory(self, start, end, branch, product_id=False):
        data = self.get_inventory_waste_data(fields.Date.to_string(start), fields.Date.to_string(end), False, product_id)
        if not data.get("available"):
            return {"available": False, "messages": data.get("messages") or ["Inventory could not be read with your access rights."]}
        summary = data["summary"]
        expiring = [row for row in data.get("expiring_stock") or [] if row.get("days_left", 99) <= 7]
        receipts = self._operations_receipts_done(start, end, branch)
        return {
            "available": True,
            "currency": data["currency"],
            # Current balances (not period figures): reorder rules and lots as of now.
            "stock_levels": data.get("stock_levels") or [],
            "products": data.get("inventory_products") or [],
            "selected_product_id": data.get("selected_product_id"),
            "movement": data.get("movement"),
            "warnings": [{"title": row["title"], "detail": row["detail"], "severity": row["severity"]} for row in data.get("attention") or []],
            "low_stock_count": summary.get("low_stock_count"),
            "low_stock": [{"product_id": row["product_id"], "name": row["name"], "available": row["available_quantity"],
                           "minimum": row["minimum_quantity"], "unit": row["unit"], "status": row["status_label"]}
                          for row in (data.get("stock_health") or [])[:_LIST]],
            "expiry_tracking_available": data.get("expiry_tracking_available"),
            "expiring_count": summary.get("expiring_soon_count"),
            "expiring": [{"lot_id": row.get("lot_id"), "product": row["product"], "quantity": row["quantity"], "unit": row["unit"],
                          "expiry_date": row["expiry_date"], "days_left": row["days_left"]} for row in expiring[:_LIST]],
            # Period figures: recorded waste (Spoilage/Expiry, Damaged Goods) and completed supplier receipts.
            "waste_cost": summary.get("waste_cost"),
            "waste_by_reason": data.get("waste_by_reason") or [],
            "waste_by_product": [{"product": row["product"], "quantity": row["quantity"], "unit": row["unit"], "cost": row["cost"]}
                                 for row in (data.get("waste_by_product") or [])[:_LIST]],
            "waste_breakdown_available": data.get("waste_breakdown_available", False),
            "receipts_done": receipts,
            "drill": dict(data.get("drill") or {}, receipts=self._overview_drill("stock.picking", self._operations_receipts_domain(start, end, branch), "Supplier receipts completed")),
            "messages": data.get("messages") or [],
        }

    @api.model
    def _operations_receipts_domain(self, start, end, branch):
        start_utc, end_utc = self._overview_utc_bounds(start, end)
        return [("picking_type_id.code", "=", "incoming"), ("picking_type_id.warehouse_id", "=", branch.id), ("state", "=", "done"),
                ("date_done", ">=", fields.Datetime.to_string(start_utc)), ("date_done", "<", fields.Datetime.to_string(end_utc))]

    @api.model
    def _operations_receipts_done(self, start, end, branch):
        return self._overview_safe(lambda: self.env["stock.picking"].search_count(self._operations_receipts_domain(start, end, branch)))

    @api.model
    def _operations_purchasing(self, start, end):
        data = self.get_purchasing_overview_data(fields.Date.to_string(start), fields.Date.to_string(end))
        if not data.get("available"):
            return {"available": False, "messages": data.get("messages") or ["Purchases could not be read with your access rights."]}
        return {
            "available": True,
            "currency": data["currency"],
            # Confirmed orders in the period (draft, sent, waiting approval and cancelled excluded).
            "confirmed_count": data["purchase_order_count"],
            "confirmed_value": data["purchase_order_value"],
            # Current obligations: open receipts of every confirmed order of the branch, whenever it was placed; overdue is
            # the subset already past its scheduled date (outstanding is not overdue until then).
            "awaiting_count": data["pending_delivery_count"],
            "awaiting_value": data["pending_delivery_value"],
            "overdue_count": data["overdue_delivery_count"],
            "overdue_value": data["overdue_delivery_value"],
            "drill": data.get("drill") or {},
            # No links to purchase orders: Odoo's Purchase screens are not branch-restricted, so they stay Head Office only.
            "period_orders": [{"name": row["name"], "supplier": row["supplier"], "lines": row["lines"]} for row in (data.get("recent_orders") or [])[:_LIST]],
            "deliveries": [{"name": row["name"], "supplier": row["supplier"], "status": row["status"], "expected_date": row["expected_date"], "lines": row["lines"],
                            "outstanding_value": row["outstanding_value"], "date_order": row["date_order"]}
                           for row in (data.get("open_deliveries") or [])[:_LIST]],
            "messages": data.get("messages") or [],
        }
