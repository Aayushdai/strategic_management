"""Inventory & Waste page data: stock health, waste, recipes, counts and expiring lots for a period and branch.
Part of strategic.performance.dashboard; shared helpers live in business_overview.py."""

from collections import defaultdict

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ..services.kpi_sources.pos import recipe_consumption
from .business_overview import _LIST_LIMIT, _POS_STATES


class StrategicPerformanceDashboardInventoryOverview(models.AbstractModel):
    _inherit = "strategic.performance.dashboard"

    @api.model
    def get_inventory_waste_data(self, date_start=False, date_end=False, warehouse_id=False, product_id=False):
        """Return actual stock, expiry, scrap and movement data for authorized warehouses."""
        self._strategic_role()  # Admin stops here.
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
        is_branch_manager = (user.has_group("strategic_management.group_strategic_branch_manager") and not is_head_office) or self._department_read_active()
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
            "stock_health": [], "stock_levels": [], "waste_by_product": [], "inventory_products": [],
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
        lot_quant_ids = defaultdict(list)
        discrepancy_quant_ids = []
        discrepancy_quant_ids_by_warehouse = defaultdict(list)  # for each branch's drill
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
                    discrepancy_quant_ids.append(row["id"])
                    discrepancy_quant_ids_by_warehouse[warehouse_ref].append(row["id"])
            lot_ref = row.get("lot_id")
            expiry_value = row.get("expiration_date") if "expiration_date" in quant_fields else False
            if lot_ref:
                lot_key = (lot_ref[0], warehouse_ref)
                lot_quantity_by_branch[lot_key] += quantity
                lot_quant_ids[lot_key].append(row["id"])
                lot_info.setdefault(lot_key, {
                    "lot_id": lot_ref[0], "lot_name": lot_ref[1], "product_id": product_id_row,
                    "expiry": expiry_value, "warehouse_id": warehouse_ref,
                })
                if expiry_value and not lot_info[lot_key].get("expiry"):
                    lot_info[lot_key]["expiry"] = expiry_value

        # Reorder rules are readable only by Sales/Purchase/Inventory users. A Branch Manager reads them for their own
        # verified branch (warehouse_ids is pinned above, never taken from the request) and company only.
        Orderpoint = self.env["stock.warehouse.orderpoint"].sudo() if is_branch_manager else self.env["stock.warehouse.orderpoint"]
        required_orderpoint_fields = {"product_id", "product_min_qty", "qty_forecast", "warehouse_id"}
        orderpoint_available = required_orderpoint_fields.issubset(Orderpoint._fields)
        orderpoint_rows = False
        if orderpoint_available and warehouse_ids:
            domain = [("warehouse_id", "in", warehouse_ids), ("product_min_qty", ">", 0), ("company_id", "=", company.id)]
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
                    "orderpoint_id": row["id"],
                    "product_id": pid, "name": meta.get("name") or product_ref[1], "category": meta.get("category") or "Uncategorized",
                    "warehouse_id": wid, "branch_name": warehouse_ref[1], "available_quantity": round(available, 4),
                    "forecast_quantity": round(forecast, 4), "minimum_quantity": round(minimum, 4),
                    "unit": meta.get("uom") or "", "status": "critical" if forecast <= 0 else "low",
                    "status_label": "Reorder" if forecast <= 0 else "Low stock",
                    "stock_percent": round(max(0.0, min(100.0, (available / minimum * 100.0) if minimum else 0.0)), 1),
                    "drill": self._overview_drill("stock.warehouse.orderpoint", None, "Minimum stock rule: %s" % (meta.get("name") or product_ref[1]), ids=[row["id"]]),
                })
        low_stock_all.sort(key=lambda item: (item["forecast_quantity"] / item["minimum_quantity"] if item["minimum_quantity"] else 0, item["name"]))
        low_stock_count = len(low_stock_all) if orderpoint_rows is not False and orderpoint_rows else (0 if orderpoint_rows is not False and orderpoint_rows is not None and len(orderpoint_rows) else False)
        stock_health = low_stock_all[:_LIST_LIMIT]

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
                    "quant_ids": lot_quant_ids.get(key, []),
                    "lot_id": info["lot_id"], "lot_name": info.get("lot_name") or "Tracked lot",
                    "product_id": pid, "product": product.get("name") or "Product",
                    "branch": warehouse.display_name if warehouse else "Unknown branch",
                    "warehouse_id": wid, "expiry_date": fields.Date.to_string(expiry_date),
                    "days_left": days_left, "quantity": round(quantity, 4), "unit": product.get("uom") or "",
                    "value_at_cost": round(cost, 2), "status": status,
                    "drill": self._overview_drill("stock.quant", None, "Stock of lot %s" % (info.get("lot_name") or ""), ids=lot_quant_ids.get(key, [])),
                })
        expiring_stock.sort(key=lambda item: (item["expiry_date"], item["product"]))
        expiring_window = self.env.company.sm_expiring_days or 7  # Settings: expiring stock window
        expiring_soon_count = sum(1 for item in expiring_stock if 0 <= item["days_left"] <= expiring_window) if expiry_available else False

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

        waste_by_reason_map = defaultdict(float)
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
                        # Value by recorded waste reason; quantities stay per product because units differ.
                        reason = " + ".join(sorted({(tag.name or "").strip() for tag in move.scrap_reason_tag_ids}))
                        waste_by_reason_map[reason] += cost_by_move.get(move.id, 0.0)
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
                    for item in waste_by_product:
                        item["drill"] = self._overview_drill("stock.move", None, "Recorded waste: %s" % item["product"],
                                                             ids=scrap_moves.filtered(lambda move, pid=item["product_id"]: move.product_id.id == pid).ids)

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
                movement["ordered_quantity"] = self._overview_ordered_quantity(start_utc, end_utc, selected_product_id, scoped_warehouses)
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
                "drill": self._overview_drills(
                    self._overview_drill("stock.warehouse.orderpoint", None, "Low stock at %s" % warehouse.display_name,
                                         ids=[item["orderpoint_id"] for item in low_stock_all if item["warehouse_id"] == warehouse.id]),
                    self._overview_drill("stock.move", self._overview_waste_domain(start, end, warehouse), "Recorded waste at %s" % warehouse.display_name)
                    if branch_waste else False,
                    self._overview_drill("stock.quant", None, "Count differences at %s" % warehouse.display_name,
                                         ids=discrepancy_quant_ids_by_warehouse.get(warehouse.id, []))),
            })

        attention = []
        for item in low_stock_all:
            attention.append({"kind": "stock", "severity": "high" if item["status"] == "critical" else "medium", "title": item["status_label"], "detail": "%s - %s - %s %s available" % (item["branch_name"], item["name"], item["available_quantity"], item["unit"]), "date": "", "res_model": "product.product", "res_id": item["product_id"]})
        for item in expiring_stock:
            if item["days_left"] <= expiring_window:
                attention.append({"kind": "expiry", "severity": "high" if item["days_left"] < 0 else "medium", "title": item["status"], "detail": "%s - %s - %s %s" % (item["branch"], item["product"], item["quantity"], item["unit"]), "date": item["expiry_date"], "res_model": "stock.lot", "res_id": item["lot_id"]})
        for item in stock_discrepancies:
            attention.append({"kind": "variance", "severity": "review", "title": "Unreconciled stock count", "detail": "%s - %s - variance %s %s" % (item["branch"], item["product"], item["variance"], item["unit"]), "date": "", "res_model": "product.product", "res_id": item["product_id"]})
        severity_rank = {"high": 0, "medium": 1, "review": 2}
        attention.sort(key=lambda item: severity_rank.get(item["severity"], 3))
        attention = attention[:_LIST_LIMIT]

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
            "drill": {
                "low_stock": self._overview_drill("stock.warehouse.orderpoint", None, "Products below their minimum stock", ids=[item["orderpoint_id"] for item in low_stock_all]),
                "expiring": self._overview_drill("stock.quant", None, "Stock expiring within %s days" % expiring_window, ids=[qid for item in expiring_stock if 0 <= item["days_left"] <= expiring_window for qid in item["quant_ids"]]) if expiry_available else False,
                "waste": self._overview_drill("stock.move", self._overview_waste_domain(start, end, selected_warehouse), "Recorded waste") if waste_cost is not False else False,
                "issues": self._overview_drill("stock.quant", None, "Counted stock that differs from the system", ids=discrepancy_quant_ids),
            },
            "summary": {
                "low_stock_count": low_stock_count,
                "expiring_soon_count": expiring_soon_count,
                "waste_cost": waste_cost,
                "discrepancy_count": discrepancy_count,
            },
            "stock_health": stock_health,
            # Current balances per product, from the same quants as the figures above.
            "stock_levels": sorted(
                ({"product_id": pid, "name": product_meta.get(pid, {}).get("name") or product_labels.get(pid) or "Product",
                  "unit": product_meta.get(pid, {}).get("uom") or "", "on_hand": round(values["quantity"], 4),
                  "available": round(values["available_quantity"], 4)}
                 for (pid, _wid), values in product_wh.items() if values["quant_rows"]),
                key=lambda row: row["name"].casefold())[:_LIST_LIMIT],
            "waste_by_product": waste_by_product if waste_breakdown_available else [],
            "waste_by_reason": [{"reason": reason, "cost": round(cost, 2)} for reason, cost in sorted(waste_by_reason_map.items())]
                               if waste_breakdown_available else [],
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
    def _overview_ordered_quantity(self, start_utc, end_utc, product_id, warehouses):
        """Quantity of the product on purchase orders confirmed in the period for the branches, in the product's unit.

        Same population as the Purchasing page (confirmed orders, dated by their approval, branch from the receipt type).
        Ordered is not received: receipts come from completed stock moves. False when purchases cannot be read.
        """
        if "purchase.order.line" not in self.env.registry.models:
            return False
        lines = self._overview_safe(lambda: self.env["purchase.order.line"].search([
            ("product_id", "=", product_id), ("order_id.company_id", "=", self.env.company.id),
            ("order_id.state", "in", ("purchase", "done")),
            ("order_id.picking_type_id.warehouse_id", "in", warehouses.ids),
            ("order_id.date_approve", ">=", fields.Datetime.to_string(start_utc)),
            ("order_id.date_approve", "<", fields.Datetime.to_string(end_utc)),
        ]))
        if lines is False:
            return False
        return round(sum(lines.mapped("product_uom_qty")), 4)

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
