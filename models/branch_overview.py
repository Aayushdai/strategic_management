"""Branches section of the Strategic Management home screen.

Figures come from existing Odoo records (POS, Inventory, Purchase receipts) and
from approved Strategic KPI targets and objectives. Nothing is inferred: when a
measurement or target is missing the branch is reported as "Not Evaluated".
"""

from collections import defaultdict

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .business_overview import _LIST_LIMIT
from .kpi_source_adapter import source_collector_for
from ..services.kpi_sources.pos import POS_SALE_STATES


_POS_STATES = POS_SALE_STATES  # Odoo 20 has no "invoiced" POS state; same population as the sales KPIs
_BELOW_TARGET = {"not_achieved", "almost_met"}
_MEETS_TARGET = {"achieved", "exceeded"}
_STATUS = {
    "meeting_target": ("Meeting target", "o_sm_br_status_good"),
    "below_target": ("Below target", "o_sm_br_status_critical"),
    "not_evaluated": ("Not Evaluated", "o_sm_br_status_info"),
}
_LATE_DELIVERY_CRITICAL_DAYS = 7


class StrategicPerformanceDashboardBranches(models.AbstractModel):
    _inherit = "strategic.performance.dashboard"

    @api.model
    def get_branches_overview_data(self, date_start=False, date_end=False, warehouse_id=False):
        """Return branch comparison, status and attention items for the current user's scope."""
        start, end = self._resolve_period(date_start, date_end)
        company = self.env.company
        role = self._strategic_role()
        currency = company.currency_id
        result = {
            "available": False,
            "role": role,
            "period": {"start": fields.Date.to_string(start), "end": fields.Date.to_string(end)},
            "currency": {
                "name": currency.name,
                "symbol": currency.symbol or currency.name,
                "position": currency.position or "before",
                "decimal_places": currency.decimal_places,
            },
            "branch_options": [],
            "selected_warehouse_id": False,
            "summary": {"active_branches": 0, "meeting_target": 0, "below_target": 0, "not_evaluated": 0, "critical_issues": 0},
            "branches": [],
            "attention": [],
            "messages": [],
        }
        if role == "employee":
            result["messages"] = ["Branch comparisons are available to Head Office and Branch Managers."]
            return result

        permitted, selected = self._strategic_branch_scope(role, warehouse_id)
        warehouses = selected or permitted
        result.update({
            "available": True,
            "branch_options": [{"id": warehouse.id, "name": warehouse.display_name} for warehouse in permitted],
            "selected_warehouse_id": selected.id if selected else False,
        })

        today = fields.Date.context_today(self)
        managers = self._branch_managers(warehouses)
        sales = self._branch_pos_figures(start, end, warehouses)
        kpis = self._branch_kpi_results(start, end, warehouses, today)
        variances = self._branch_stock_variances(warehouses)
        attention = []
        rows = []
        totals = {"net": 0.0, "sales": 0.0, "cost": 0.0, "waste": 0.0, "cost_known": sales is not False, "waste_known": True}
        for warehouse in warehouses:
            kpi_rows = kpis.get(warehouse.id, [])
            valid = [row for row in kpi_rows if row["status"] == "valid"]
            if not valid:
                status = "not_evaluated"
            elif any(row["attainment_status"] in _BELOW_TARGET for row in valid):
                status = "below_target"
            else:
                status = "meeting_target"
            attainment = self._mean([row["attainment"] for row in valid])
            pos = sales.get(warehouse.id) if sales else False
            manager_names = ", ".join(managers.get(warehouse.id, [])) or "Unassigned"
            label, css = _STATUS[status]
            waste_cost = self._overview_waste_cost(start, end, warehouse)
            contribution = round(pos["net"] - pos["cost"] - waste_cost, 2) if pos and pos["cost"] is not False and waste_cost is not False else False
            totals["net"] += pos["net"] if pos else 0.0
            totals["sales"] += pos["sales"] if pos else 0.0
            totals["cost_known"] &= not (pos and pos["net"] and pos["cost"] is False)
            totals["cost"] += pos["cost"] if pos and pos["cost"] else 0.0
            totals["waste_known"] &= waste_cost is not False
            totals["waste"] += waste_cost or 0.0
            rows.append({
                "warehouse_id": warehouse.id,
                "name": warehouse.display_name,
                "manager": manager_names,
                "sales": pos["sales"] if pos else False,
                "orders": pos["orders"] if pos else False,
                "gross_margin": pos["gross_margin"] if pos else False,
                "gross_margin_pct": pos["gross_margin_pct"] if pos else False,
                "food_cost_pct": pos["food_cost_pct"] if pos else False,
                "waste_cost": waste_cost,
                "waste_pct_of_sales": round(waste_cost * 100.0 / pos["sales"], 1) if pos and pos["sales"] and waste_cost is not False else False,
                # Branch contribution: net sales - cost of goods sold - valued waste. Not net
                # profit (rent, wages and overheads are not attributed to branches).
                "contribution": contribution,
                "contribution_pct": round(contribution * 100.0 / pos["net"], 1) if contribution is not False and pos["net"] else False,
                "stock_variance_count": variances.get(warehouse.id, False) if variances is not False else False,
                "attainment": attainment,
                "kpi_total": len(kpi_rows),
                "kpi_evaluated": len(valid),
                "kpi_met": sum(1 for row in valid if row["attainment_status"] in _MEETS_TARGET),
                "status": status,
                "status_label": label,
                "status_class": css,
            })
            for row in kpi_rows:
                if row["status"] == "valid" and row["attainment_status"] in _BELOW_TARGET:
                    attention.append({
                        "kind": "waste" if "waste" in (row["calculation_type"] or "") else "kpi",
                        "title": ("Excessive waste" if "waste" in (row["calculation_type"] or "") else "KPI below target") + ": " + row["kpi_name"],
                        "branch": warehouse.display_name,
                        "evidence": "Actual %s %s vs target %s %s (%s%%)%s" % (
                            self._fmt(row["actual_value"]), row["unit"] or "", self._fmt(row["target_value"]), row["unit"] or "",
                            self._fmt(row["attainment"]), " · period in progress" if row["live"] else "",
                        ),
                        "severity": "critical" if row["attainment_status"] == "not_achieved" else "warning",
                        "responsible": manager_names,
                        "deadline": row["date_end"],
                        "action_status": row["action_status"],
                        "res_model": "strategic.kpi.target", "res_id": row["target_id"],
                    })
        attention.extend(self._branch_late_deliveries(warehouses, managers))
        attention.extend(self._branch_objectives_below_target(warehouses))
        severity_order = {"critical": 0, "warning": 1}
        attention.sort(key=lambda item: (severity_order.get(item["severity"], 2), item["deadline"] or "9999-12-31"))

        contribution_known = totals["cost_known"] and totals["waste_known"]
        result["branches"] = rows
        result["attention"] = attention[:_LIST_LIMIT]
        result["summary"] = {
            "active_branches": len(rows),
            "meeting_target": sum(1 for row in rows if row["status"] == "meeting_target"),
            "below_target": sum(1 for row in rows if row["status"] == "below_target"),
            "not_evaluated": sum(1 for row in rows if row["status"] == "not_evaluated"),
            "critical_issues": sum(1 for item in attention if item["severity"] == "critical"),
            # Scope-wide ratios are weighted by sales, never averaged across branches.
            "food_cost_pct": round(totals["cost"] * 100.0 / totals["net"], 1) if totals["cost_known"] and totals["net"] else False,
            "waste_pct_of_sales": round(totals["waste"] * 100.0 / totals["sales"], 1) if totals["waste_known"] and totals["sales"] else False,
            "contribution": round(totals["net"] - totals["cost"] - totals["waste"], 2) if contribution_known and totals["net"] else False,
            "contribution_pct": round((totals["net"] - totals["cost"] - totals["waste"]) * 100.0 / totals["net"], 1) if contribution_known and totals["net"] else False,
        }
        if sales is False:
            result["messages"].append("POS sales could not be read with your access rights.")
        result["messages"].append(
            "Net profit by branch is Not Evaluated: posted accounting lines have no branch dimension. "
            "Food cost % uses the cost of goods recorded on POS sales; it shows Not Evaluated while sales lack a recorded cost "
            "(set each product's Cost, or its recipe ingredients' costs for Kits)."
        )
        return result

    @api.model
    def _strategic_role(self):
        user = self.env.user
        if user.has_group("strategic_management.group_strategic_head_office_manager"):
            return "head_office"
        if user.has_group("strategic_management.group_strategic_branch_manager"):
            return "branch_manager"
        return "employee"

    @api.model
    def get_navigation_context(self):
        """The current user's role and own branch assignment, for the app's navigation only (no business data).

        The role is known even when a Branch Manager's branch is missing or invalid, so the app can show that
        problem instead of falling back to another role's navigation; data methods keep refusing as before.
        """
        role = self._strategic_role()
        context = {"role": role, "branch": False, "branch_error": False}
        if role == "branch_manager":
            # sudo only to validate the user's own assignment; a branch in another company may not be readable.
            warehouse = self.env.user.sudo().strategic_branch_warehouse_id
            if not warehouse or (warehouse.company_id and warehouse.company_id != self.env.company):
                context["branch_error"] = "Assign this Branch Manager to a warehouse in the active company."
            else:
                context["branch"] = {"id": warehouse.id, "name": warehouse.display_name}
        return context

    @api.model
    def _strategic_branch_scope(self, role, warehouse_id=False):
        """Return (permitted, selected) warehouses; Branch Managers are pinned to their own branch."""
        company = self.env.company
        Warehouse = self.env["stock.warehouse"]
        if role == "head_office":
            permitted = Warehouse.search([("company_id", "=", company.id)], order="name, id")
            selected = Warehouse.browse()
            if warehouse_id:
                try:
                    requested_id = int(warehouse_id)
                except (TypeError, ValueError):
                    raise ValidationError("Choose a valid branch.")
                selected = permitted.filtered(lambda warehouse: warehouse.id == requested_id)[:1]
                if not selected:
                    raise AccessError("You cannot view the selected branch.")
            return permitted, selected
        selected = self.env.user.strategic_branch_warehouse_id
        if not selected or (selected.company_id and selected.company_id != company):
            raise AccessError("Assign this Branch Manager to a warehouse in the active company.")
        return selected, selected

    @api.model
    def _fmt(self, value):
        return ("%.2f" % value).rstrip("0").rstrip(".") if isinstance(value, (int, float)) else "—"

    @api.model
    def _branch_managers(self, warehouses):
        group = self.env.ref("strategic_management.group_strategic_branch_manager", raise_if_not_found=False)
        head = self.env.ref("strategic_management.group_strategic_head_office_manager", raise_if_not_found=False)
        if not group:
            return {}
        # sudo: only names of the users assigned to the authorized branches are returned.
        users = self.env["res.users"].sudo().search([
            ("strategic_branch_warehouse_id", "in", warehouses.ids),
            ("all_group_ids", "in", group.id),
        ], order="name")
        managers = defaultdict(list)
        for manager in users:
            if head and head in manager.all_group_ids:
                continue
            managers[manager.strategic_branch_warehouse_id.id].append(manager.name)
        return managers

    @api.model
    def _branch_pos_figures(self, start, end, warehouses):
        """POS sales (same basis as the Overview) and gross margin per branch, read as the user."""
        configs = self._overview_safe(lambda: self.env["pos.config"].search([
            ("company_id", "=", self.env.company.id),
            ("picking_type_id.warehouse_id", "in", warehouses.ids),
        ]))
        if configs is False:
            return False
        config_branch = {config.id: config.picking_type_id.warehouse_id.id for config in configs}
        if not config_branch:
            return {}
        start_utc, end_utc = self._overview_utc_bounds(start, end)
        domain = [
            ("company_id", "=", self.env.company.id),
            ("state", "in", _POS_STATES),
            ("date_order", ">=", fields.Datetime.to_string(start_utc)),
            ("date_order", "<", fields.Datetime.to_string(end_utc)),
            ("config_id", "in", list(config_branch)),
        ]
        orders = self._overview_safe(lambda: self.env["pos.order"].read_group(
            domain, groupby=["config_id", "is_refund"], aggregates=["amount_total:sum", "amount_tax:sum", "id:count"],
        ))
        line_domain = [("order_id." + leaf[0], leaf[1], leaf[2]) for leaf in domain]
        lines = self._overview_safe(lambda: self._overview_pos_line_groups(
            line_domain, groupby=["order_id.config_id", "is_total_cost_computed"],
            aggregates=["price_subtotal:sum", "total_cost:sum"],
        ))
        # Sold lines with revenue but no recorded cost mean product costs are not set up.
        uncosted = self._overview_safe(lambda: self.env["pos.order.line"].read_group(
            line_domain + [("total_cost", "=", 0), ("price_subtotal", "!=", 0)],
            groupby=["order_id.config_id"], aggregates=["id:count"],
        ))
        if orders is False or lines is False or uncosted is False:
            return False
        figures = defaultdict(lambda: {"sales": 0.0, "orders": 0, "net": 0.0, "cost": 0.0, "cost_missing": False})
        # Same basis as Sales & Finance: excluding tax, refunds net, refund orders not counted.
        for config, is_refund, amount, tax, count in orders:
            bucket = figures[config_branch.get(getattr(config, "id", config))]
            bucket["sales"] += float(amount or 0.0) - float(tax or 0.0)
            bucket["orders"] += 0 if is_refund else int(count or 0)
        for config, cost_computed, subtotal, cost in lines:
            bucket = figures[config_branch.get(getattr(config, "id", config))]
            bucket["net"] += float(subtotal or 0.0)
            bucket["cost"] += float(cost or 0.0)
            if not cost_computed:
                bucket["cost_missing"] = True
        result = {}
        for config, count in uncosted:
            if count:
                figures[config_branch.get(getattr(config, "id", config))]["cost_missing"] = True
        for warehouse in warehouses:
            bucket = figures.get(warehouse.id)
            if not bucket:
                result[warehouse.id] = {"sales": 0.0, "orders": 0, "gross_margin": False, "gross_margin_pct": False,
                                        "food_cost_pct": False, "net": 0.0, "cost": False}
                continue
            # A margin is only shown when every sold line has a recorded, non-zero cost.
            margin_ok = not bucket["cost_missing"] and bucket["net"]
            result[warehouse.id] = {
                "sales": round(bucket["sales"], 2),
                "orders": bucket["orders"],
                "gross_margin": round(bucket["net"] - bucket["cost"], 2) if margin_ok else False,
                "gross_margin_pct": round((bucket["net"] - bucket["cost"]) * 100.0 / bucket["net"], 1) if margin_ok else False,
                # Food cost %: recorded cost of goods sold over net POS sales (both excl. tax).
                "food_cost_pct": round(bucket["cost"] * 100.0 / bucket["net"], 1) if margin_ok else False,
                "net": bucket["net"],
                "cost": bucket["cost"] if margin_ok else False,
            }
        return result

    @api.model
    def _branch_stock_variances(self, warehouses):
        """Count counted-but-unapplied stock quants whose count differs from system quantity."""
        Quant = self.env["stock.quant"]
        if not {"inventory_quantity_set", "inventory_diff_quantity"}.issubset(Quant._fields):
            return False
        counts = {}
        for warehouse in warehouses:
            count = self._overview_safe(lambda warehouse=warehouse: Quant.search_count([
                ("location_id", "child_of", warehouse.view_location_id.id),
                ("location_id.usage", "=", "internal"),
                ("inventory_quantity_set", "=", True),
                ("inventory_diff_quantity", "!=", 0),
            ])) if warehouse.view_location_id else 0
            if count is False:
                return False
            counts[warehouse.id] = count
        return counts

    @api.model
    def _branch_kpi_results(self, start, end, warehouses, today, include_organization=False):
        """Approved branch KPI targets overlapping the period, with their current result.

        ``include_organization`` adds organization-wide targets (keyed by False).

        Closed periods use the stored current evaluation. Periods still in progress
        are measured live from the operational source with the same attainment
        rules, without creating evaluation records.
        """
        # sudo: strictly limited to the company and the user's authorized branches.
        Target = self.env["strategic.kpi.target"].sudo()
        Evaluation = self.env["strategic.kpi.evaluation"].sudo()
        scope_domain = ["&", ("scope", "=", "branch"), ("warehouse_id", "in", warehouses.ids)]
        if include_organization:
            scope_domain = ["|", ("scope", "=", "organization")] + scope_domain
        targets = Target.search([
            ("company_id", "=", self.env.company.id),
            ("state", "=", "approved"),
            ("date_start", "<=", end),
            ("date_end", ">=", start),
        ] + scope_domain, order="date_end, id")
        stored = {}
        for row in Evaluation.search([("target_id", "in", targets.ids), ("superseded_by_id", "=", False)], order="evaluated_at desc, id desc"):
            stored.setdefault(row.target_id.id, row)
        results = defaultdict(list)
        for target in targets:
            values = False
            live = False
            evaluation = stored.get(target.id)
            if target.date_end < today or not evaluation:
                version = target.kpi_id._get_definition_version_for_period(target.date_start, target.date_end)
                collector = source_collector_for(version)
                if collector and (target.date_end >= today or not evaluation):
                    try:
                        with self.env.cr.savepoint():
                            values = Evaluation._prepare_source_adapter_values(target, collector(target, version))
                        live = target.date_end >= today
                    except Exception:
                        values = False
            if not values and evaluation:
                values = {
                    "status": evaluation.status, "attainment_status": evaluation.attainment_status,
                    "attainment": evaluation.attainment, "actual_value": evaluation.actual_value,
                    "target_value": evaluation.target_value, "unit": evaluation.unit,
                }
            if not values:
                values = {"status": "not_evaluated", "attainment_status": "not_evaluated", "attainment": 0.0,
                          "actual_value": False, "target_value": target.target_value, "unit": False}
            activities = target.activity_ids
            results[target.warehouse_id.id].append({
                "target_id": target.id,
                "kpi_name": target.kpi_id.display_name,
                "calculation_type": target.kpi_id.calculation_type,
                "date_end": fields.Date.to_string(target.date_end),
                "status": values["status"],
                "attainment_status": values["attainment_status"],
                "attainment": values["attainment"] if values["status"] == "valid" else False,
                "actual_value": values.get("actual_value"),
                "target_value": values.get("target_value"),
                "unit": values.get("unit"),
                "live": live,
                "action_status": self._action_status(activities, today),
            })
        return results

    @api.model
    def _action_status(self, activities, today):
        if not activities:
            return "No action recorded"
        due = min(activities.mapped("date_deadline"))
        return ("Action overdue since %s" if due < today else "Action planned, due %s") % fields.Date.to_string(due)

    @api.model
    def _branch_late_deliveries(self, warehouses, managers):
        """Incoming supplier receipts past their scheduled date and not yet completed."""
        now = fields.Datetime.now()
        today = fields.Date.context_today(self)
        pickings = self._overview_safe(lambda: self.env["stock.picking"].search([
            ("company_id", "=", self.env.company.id),
            ("picking_type_id.code", "=", "incoming"),
            ("picking_type_id.warehouse_id", "in", warehouses.ids),
            ("state", "not in", ["done", "cancel"]),
            ("scheduled_date", "<", now),
        ], order="scheduled_date asc", limit=20))
        items = []
        for picking in pickings or []:
            warehouse = picking.picking_type_id.warehouse_id
            days_late = (now - picking.scheduled_date).days
            items.append({
                "kind": "delivery",
                "title": "Delayed supplier delivery: %s" % (picking.origin or picking.name),
                "branch": warehouse.display_name,
                "evidence": "%s · %s · scheduled %s, %s day(s) late" % (
                    picking.name, picking.partner_id.display_name or "Unknown supplier",
                    fields.Date.to_string(picking.scheduled_date.date()), days_late,
                ),
                "severity": "critical" if days_late > _LATE_DELIVERY_CRITICAL_DAYS else "warning",
                "responsible": picking.user_id.name or ", ".join(managers.get(warehouse.id, [])) or "Unassigned",
                "deadline": fields.Date.to_string(picking.scheduled_date.date()),
                "action_status": self._action_status(picking.activity_ids, today),
                "res_model": "stock.picking", "res_id": picking.id,
            })
        return items

    @api.model
    def _branch_objectives_below_target(self, warehouses):
        """Active branch objectives whose computed score is below target."""
        today = fields.Date.context_today(self)
        # sudo: limited to active objectives of the user's authorized branches.
        objectives = self.env["strategic.objective"].sudo().search([
            ("company_id", "=", self.env.company.id),
            ("state", "=", "active"),
            ("scope", "=", "branch"),
            ("warehouse_id", "in", warehouses.ids),
        ], order="deadline, id")
        items = []
        for objective in objectives:
            if objective.scoring_status not in _BELOW_TARGET:
                continue
            items.append({
                "kind": "objective",
                "title": "Objective below target: %s" % objective.name,
                "branch": objective.warehouse_id.display_name,
                "evidence": "Score %s%% (%s)%s" % (
                    self._fmt(objective.score),
                    dict(objective._fields["scoring_status"].selection).get(objective.scoring_status),
                    " · overdue" if objective.overdue else "",
                ),
                "severity": "critical" if objective.scoring_status == "not_achieved" or objective.overdue else "warning",
                "responsible": objective.owner_id.name or "Unassigned",
                "deadline": fields.Date.to_string(objective.deadline),
                "action_status": self._action_status(objective.activity_ids, today),
                "res_model": "strategic.objective", "res_id": objective.id,
            })
        return items
