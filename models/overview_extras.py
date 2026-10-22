"""Head Office Overview: comparisons, section summaries, strategy, alerts, activity.

Composes the existing section services (Sales, Inventory, Purchasing, Branches,
People) so the Overview shows exactly the figures of those pages. Each section
is isolated: one the user cannot read is reported as unavailable instead of
failing the whole Overview.
"""

import logging
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


_logger = logging.getLogger(__name__)
_ON_TRACK = {"achieved", "exceeded"}
_AT_RISK = {"not_achieved", "almost_met"}
_SEVERITY = {"critical": "critical", "high": "critical", "warning": "warning", "medium": "warning"}
# Records a dashboard issue can point to; follow-ups are Odoo activities on them.
_FOLLOWUP_MODELS = {
    "strategic.kpi.target", "strategic.objective", "stock.picking", "stock.warehouse",
    "product.product", "stock.lot", "hr.employee",
}


class StrategicPerformanceDashboardOverview(models.AbstractModel):
    _inherit = "strategic.performance.dashboard"

    @api.model
    def get_overview_extras(self, date_start=False, date_end=False, warehouse_id=False):
        start, end = self._resolve_period(date_start, date_end)
        role = self._strategic_role()
        result = {
            "role": role, "comparison": False, "inventory": False, "purchasing": False, "branches": False,
            "people": False, "strategy": False, "attention": [], "activities": [], "messages": [],
        }
        if role == "employee":
            result["people"] = self._overview_section(lambda: self._overview_people(start, end, warehouse_id))
            return result

        permitted, selected = self._strategic_branch_scope(role, warehouse_id)
        warehouses = selected or permitted
        args = (fields.Date.to_string(start), fields.Date.to_string(end), warehouse_id)
        result["comparison"] = self._overview_section(lambda: self._overview_previous_period(start, end, warehouse_id))
        inventory = self._overview_section(lambda: self.get_inventory_waste_data(*args))
        purchasing = self._overview_section(lambda: self.get_purchasing_overview_data(*args))
        branches = self._overview_section(lambda: self.get_branches_overview_data(*args))
        people = self._overview_section(lambda: self.get_people_overview_data(*args))
        if inventory and inventory.get("available"):
            result["inventory"] = dict(inventory["summary"], attention_count=len(inventory["attention"]))
        if purchasing and purchasing.get("available"):
            result["purchasing"] = {key: purchasing[key] for key in (
                "purchase_order_count", "purchase_order_value", "pending_delivery_count",
                "overdue_delivery_count", "on_time_delivery_rate", "currency")}
        if branches and branches.get("available"):
            result["branches"] = {"summary": branches["summary"], "rows": [
                {key: row[key] for key in ("warehouse_id", "name", "manager", "sales", "attainment", "status", "status_label", "status_class")}
                for row in branches["branches"]]}
        if people:
            result["people"] = {"summary": people["summary"], "sources": people["sources"]}
        result["strategy"] = self._overview_section(lambda: self._overview_strategy(start, end, warehouses, include_organization=not selected))
        result["attention"] = self._overview_attention(inventory, branches, people)
        result["activities"] = self._overview_activities(start, end, warehouses)
        return result

    @api.model
    def _overview_section(self, function):
        try:
            with self.env.cr.savepoint():
                return function()
        except (AccessError, UserError, ValidationError, KeyError, TypeError, ValueError):
            _logger.info("Overview section unavailable for user %s", self.env.uid, exc_info=True)
            return False

    @api.model
    def _overview_people(self, start, end, warehouse_id):
        data = self.get_people_overview_data(fields.Date.to_string(start), fields.Date.to_string(end), warehouse_id)
        return {"summary": data["summary"], "sources": data["sources"]}

    @api.model
    def _overview_previous_period(self, start, end, warehouse_id):
        """Same-length period immediately before the selected one, from the same Sales service."""
        length = (end - start).days + 1
        previous_end = start - timedelta(days=1)
        previous_start = previous_end - timedelta(days=length - 1)
        data = self.get_business_overview_data(
            fields.Date.to_string(previous_start), fields.Date.to_string(previous_end), warehouse_id, "month", 5,
        )
        return {
            "start": fields.Date.to_string(previous_start), "end": fields.Date.to_string(previous_end),
            "sales": data.get("sales"), "order_count": data.get("order_count"),
            "net_profit": data.get("net_profit") if data.get("financial_available") else False,
            "waste_cost": data.get("waste_cost"),
        }

    @api.model
    def _overview_strategy(self, start, end, warehouses, include_organization):
        """On Track / At Risk / Not Evaluated for KPI targets and active objectives."""
        today = fields.Date.context_today(self)
        kpi_rows = [row for rows in self._branch_kpi_results(start, end, warehouses, today, include_organization).values() for row in rows]
        domain = [("company_id", "=", self.env.company.id), ("state", "=", "active")]
        branch_domain = ["&", ("scope", "=", "branch"), ("warehouse_id", "in", warehouses.ids)]
        domain += (["|", ("scope", "=", "organization")] + branch_domain) if include_organization else branch_domain
        # sudo: limited to active objectives of the company or the user's authorized branches.
        objectives = self.env["strategic.objective"].sudo().search(domain, order="deadline, id")

        def bucket(status, valid=True):
            if valid and status in _ON_TRACK:
                return "on_track"
            if valid and status in _AT_RISK:
                return "at_risk"
            return "not_evaluated"

        kpis = {"on_track": 0, "at_risk": 0, "not_evaluated": 0}
        for row in kpi_rows:
            kpis[bucket(row["attainment_status"], row["status"] == "valid")] += 1
        objective_counts = {"on_track": 0, "at_risk": 0, "not_evaluated": 0}
        at_risk = []
        for objective in objectives:
            status = bucket(objective.scoring_status)
            objective_counts[status] += 1
            if status == "at_risk" or objective.overdue:
                at_risk.append({
                    "name": objective.name,
                    "branch": objective.warehouse_id.display_name or "Organization",
                    "score": round(objective.score, 1),
                    "deadline": fields.Date.to_string(objective.deadline),
                    "owner": objective.owner_id.name or "Unassigned",
                    "overdue": objective.overdue,
                })
        valid = [row["attainment"] for row in kpi_rows if row["status"] == "valid"]
        return {
            "kpis": kpis, "kpi_total": len(kpi_rows),
            "kpi_average_attainment": self._mean(valid),
            "objectives": objective_counts, "objective_total": len(objectives),
            "objectives_at_risk": at_risk[:5],
        }

    @api.model
    def _overview_attention(self, inventory, branches, people):
        """Merge each section's measurable issues into one prioritized list."""
        items = []
        keys = ("title", "branch", "evidence", "responsible", "deadline", "res_model", "res_id")
        for item in (branches or {}).get("attention", []):
            items.append({**{key: item.get(key) for key in keys},
                          "source": "Branches", "severity": _SEVERITY.get(item["severity"], "warning")})
        for item in (people or {}).get("attention", []):
            items.append({**{key: item.get(key) for key in keys},
                          "deadline": False, "source": "People", "severity": _SEVERITY.get(item["severity"], "warning")})
        for item in (inventory or {}).get("attention", []):
            items.append({"title": item["title"], "branch": False, "evidence": item["detail"], "responsible": False,
                          "deadline": item.get("date") or False, "source": "Inventory",
                          "res_model": item.get("res_model"), "res_id": item.get("res_id"),
                          "severity": _SEVERITY.get(item["severity"], "warning")})
        items.sort(key=lambda item: (item["severity"] != "critical", item["deadline"] or "9999-12-31"))
        items = items[:10]
        today = fields.Date.context_today(self)
        Activity = self.env["mail.activity"].sudo()
        for item in items:
            record = self._followup_record(item.get("res_model"), item.get("res_id"))
            # Search activities directly: not every linked model has the activity mixin.
            item["action_status"] = self._action_status(
                Activity.search([("res_model", "=", record._name), ("res_id", "=", record.id)]), today,
            ) if record else False
        return items

    @api.model
    def _followup_record(self, res_model, res_id):
        if res_model not in _FOLLOWUP_MODELS or res_model not in self.env.registry.models or not res_id:
            return False
        # sudo: existence and activity status only; access is checked before any action.
        return self.env[res_model].sudo().browse(int(res_id)).exists()

    @api.model
    def _followup_warehouse(self, record):
        if record._name == "stock.warehouse":
            return record
        if record._name == "stock.picking":
            return record.picking_type_id.warehouse_id
        if record._name == "hr.employee":
            return record.user_id.strategic_branch_warehouse_id
        return record.warehouse_id if "warehouse_id" in record._fields else self.env["stock.warehouse"]

    @api.model
    def create_attention_followup(self, res_model, res_id, title, evidence=False, deadline=False):
        """Assign a follow-up activity on the record behind a dashboard issue.

        The requester must be able to read the record, and Branch Managers only act
        on their own branch. The record's own owner is assigned when it has one;
        otherwise the branch's managers (or Head Office) via the KPI routing rule.
        """
        role = self._strategic_role()
        if role == "employee":
            raise AccessError("Only Head Office and Branch Managers can assign follow-ups.")
        record = self._followup_record(res_model, res_id)
        if not record:
            raise ValidationError("This issue is not linked to a record that can be followed up.")
        record.with_user(self.env.user).check_access("read")
        warehouse = self._followup_warehouse(record)
        if role == "branch_manager" and warehouse != self.env.user.strategic_branch_warehouse_id:
            raise AccessError("Branch Managers can only assign follow-ups for their own branch.")
        owner = record.owner_id if "owner_id" in record._fields else record.user_id if record._name == "stock.picking" else False
        Target = self.env["strategic.kpi.target"].sudo()
        recipients = owner or Target._get_schedule_notification_users(
            "branch" if warehouse else "organization", self.env.company, warehouse=warehouse or None)
        today = fields.Date.context_today(self)
        due = fields.Date.to_date(deadline) if deadline else False
        created = Target._create_deduplicated_activity(
            record, "Follow-up: %s" % title, evidence or title, company=self.env.company,
            recipients=recipients, date_deadline=due if due and due > today else today + timedelta(days=2),
        )
        return {"created": created, "assigned_to": recipients.mapped("name")}

    @api.model
    def _overview_activities(self, start, end, warehouses):
        """Latest real records in scope, newest first, each source read with the user's rights."""
        start_utc, end_utc = (fields.Datetime.to_string(value) for value in self._overview_utc_bounds(start, end))
        company = self.env.company
        activities = []

        def add(kind, title, detail, branch, when):
            if when:
                activities.append({"kind": kind, "title": title, "detail": detail, "branch": branch or "—",
                                   "date": fields.Datetime.to_string(when)})

        # Same rule as the Sales service: no POS configuration access, no POS figures.
        pos_readable = self.env["pos.config"].has_access("read") and self.env["pos.order"].has_access("read")
        orders = pos_readable and self._overview_safe(lambda: self.env["pos.order"].search([
            ("company_id", "=", company.id), ("state", "in", ["paid", "done"]),
            ("date_order", ">=", start_utc), ("date_order", "<", end_utc),
            ("config_id.picking_type_id.warehouse_id", "in", warehouses.ids),
        ], order="date_order desc", limit=5)) or []
        for order in orders:
            add("pos", "POS %s %s" % ("refund" if order.is_refund else "order", order.name),
                "%s %s excl. tax" % (order.currency_id.symbol or "", "%.2f" % (order.amount_total - order.amount_tax)),
                order.config_id.picking_type_id.warehouse_id.display_name, order.date_order)
        if "purchase.order" in self.env.registry.models:
            purchases = self._overview_safe(lambda: self.env["purchase.order"].search([
                ("company_id", "=", company.id), ("write_date", ">=", start_utc), ("write_date", "<", end_utc),
                ("picking_type_id.warehouse_id", "in", warehouses.ids),
            ], order="write_date desc", limit=5)) or []
            for order in purchases:
                add("purchase", "Purchase order %s" % order.name,
                    "%s · %s" % (order.partner_id.display_name or "Supplier", dict(order._fields["state"].selection).get(order.state)),
                    order.picking_type_id.warehouse_id.display_name, order.write_date)
        adjustments = self._overview_safe(lambda: self.env["stock.move"].search([
            ("company_id", "=", company.id), ("state", "=", "done"), ("is_inventory", "=", True),
            ("date", ">=", start_utc), ("date", "<", end_utc),
            "|", ("location_id.warehouse_id", "in", warehouses.ids), ("location_dest_id.warehouse_id", "in", warehouses.ids),
        ], order="date desc", limit=5)) or []
        for move in adjustments:
            sign = 1 if move.location_dest_id.warehouse_id in warehouses else -1
            add("inventory", "Inventory adjustment: %s" % move.product_id.display_name,
                "%+g %s" % (sign * move.quantity, move.uom_id.name or ""),
                (move.location_dest_id.warehouse_id or move.location_id.warehouse_id).display_name, move.date)
        if "hr.attendance" in self.env.registry.models:
            # sudo: attendances of employees assigned to the authorized branches only.
            attendances = self.env["hr.attendance"].sudo().search([
                ("employee_id.user_id.strategic_branch_warehouse_id", "in", warehouses.ids),
                ("check_in", ">=", start_utc), ("check_in", "<", end_utc),
            ], order="write_date desc", limit=5)
            for attendance in attendances:
                add("attendance", "Attendance: %s" % attendance.employee_id.name,
                    "Checked out" if attendance.check_out else "Checked in",
                    attendance.employee_id.user_id.strategic_branch_warehouse_id.display_name, attendance.write_date)
        # sudo: objectives of the company or authorized branches only.
        objectives = self.env["strategic.objective"].sudo().search([
            ("company_id", "=", company.id), ("write_date", ">=", start_utc), ("write_date", "<", end_utc),
            "|", ("scope", "=", "organization"), ("warehouse_id", "in", warehouses.ids),
        ], order="write_date desc", limit=5)
        for objective in objectives:
            add("objective", "Objective updated: %s" % objective.name,
                dict(objective._fields["state"].selection).get(objective.state),
                objective.warehouse_id.display_name or "Organization", objective.write_date)
        activities.sort(key=lambda item: item["date"], reverse=True)
        return activities[:12]
