"""Monthly company report (Head Office): when a month ends, that month's figures for every branch in one report.

The figures come from the same Head Office methods as the Sales, Inventory and Purchasing pages, read as a Head Office
user, so the report never shows more or less than Head Office sees there. It is saved as it was taken and never
edited. Head Office is notified with the PDF attached (emailed when email is set up) and finds it under
Reports > Company. Created by the daily job in the first days of the month (period_reports.py); Head Office can also
create last month's report itself.
"""

import logging
from datetime import timedelta

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import format_amount

_logger = logging.getLogger(__name__)

_HEAD = "strategic_management.group_strategic_head_office_manager"
_ADMIN = "strategic_management.group_strategic_admin"
_MONEY = {"net_sales", "average_order_value", "waste_cost", "purchases"}
# How active objectives are counted: by how their latest result reads (the same colours as the goal cards).
_TONES = {"good": "on track", "warn": "close", "bad": "off track"}
# (label, key) of each figure, in the order the report shows them.
_FIGURES = [("Net sales", "net_sales"), ("Orders", "orders"), ("Average order", "average_order_value"),
            ("Recorded waste", "waste_cost"), ("Confirmed purchases", "purchases"), ("Overdue deliveries (when taken)", "overdue_deliveries")]


class StrategicCompanyReport(models.Model):
    _name = "strategic.company.report"
    _description = "Monthly Company Report"
    _inherit = ["mail.thread"]
    _order = "date_start desc, id desc"

    name = fields.Char(required=True, readonly=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True, index=True)
    currency_id = fields.Many2one(related="company_id.currency_id")
    date_start = fields.Date("From", required=True, readonly=True)
    date_end = fields.Date("To", required=True, readonly=True)
    taken_at = fields.Datetime("Figures taken at", readonly=True)
    net_sales = fields.Monetary("Net sales", readonly=True)
    snapshot = fields.Json("Figures", readonly=True, copy=False)
    summary_html = fields.Html("Report", compute="_compute_summary_html", sanitize=False)

    _company_month_unique = models.Constraint("UNIQUE(company_id, date_start)", "There is already a company report for this month.")

    @api.depends("snapshot")
    def _compute_summary_html(self):
        for report in self:
            report.summary_html = report._render_summary(report.snapshot or {})

    # ------------------------------------------------------------------ creating
    @api.model
    def _last_month(self, today):
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end

    @api.model
    def _reader(self, company):
        """The Head Office user whose view the figures are read with (Admin-role users see no business data)."""
        users = self.env["res.users"].sudo().search([
            ("active", "=", True), ("share", "=", False), ("group_ids", "in", self.env.ref(_HEAD).ids),
            ("company_ids", "in", company.id)], order="id")
        return users.filtered(lambda user: not user.has_group(_ADMIN))[:1]

    @api.model
    def _create_for_month(self, company, start, end):
        """Take the month's figures and save the report; False when it exists or no Head Office user can read them."""
        Report = self.sudo()
        if Report.search_count([("company_id", "=", company.id), ("date_start", "=", start)]):
            return False
        reader = self._reader(company)
        if not reader:
            _logger.warning("Company report for %s %s not created: no Head Office user to read the figures as.", company.name, start)
            return False
        snapshot = self._build_snapshot(company, start, end, reader)
        report = Report.create({
            "name": "%s · %s" % (company.name, start.strftime("%B %Y")),
            "company_id": company.id, "date_start": start, "date_end": end,
            "taken_at": fields.Datetime.now(), "net_sales": snapshot["totals"].get("net_sales") or 0.0, "snapshot": snapshot,
        })
        report._notify_head_office()
        return report

    @api.model
    def _build_snapshot(self, company, start, end, reader):
        Dashboard = self.env["strategic.performance.dashboard"].with_user(reader).with_company(company)
        first, last = fields.Date.to_string(start), fields.Date.to_string(end)

        def figures(warehouse_id):
            sales = Dashboard.get_business_overview_data(first, last, warehouse_id, "month")
            inventory = Dashboard.get_inventory_waste_data(first, last, warehouse_id)
            purchasing = Dashboard.get_purchasing_overview_data(first, last, warehouse_id)
            net, orders = sales.get("sales"), sales.get("order_count")
            # False means the figure could not be read, never zero.
            return {
                "net_sales": net,
                "orders": orders if net is not False else False,
                "average_order_value": round(net / orders, 2) if net is not False and orders else False,
                "waste_cost": inventory["summary"].get("waste_cost") if inventory.get("available") else False,
                "purchases": purchasing["purchase_order_value"] if purchasing.get("available") else False,
                "overdue_deliveries": purchasing["overdue_delivery_count"] if purchasing.get("available") else False,
            }

        reports = {report.warehouse_id.id: dict(report._fields["state"].selection)[report.state]
                   for report in self.env["strategic.branch.report"].sudo().search([
                       ("company_id", "=", company.id), ("date_start", "=", start), ("date_end", "=", end)])}
        branches = [{"id": branch.id, "name": branch.display_name, "branch_report": reports.get(branch.id, "Not opened"), **figures(branch.id)}
                    for branch in self.env["stock.warehouse"].with_user(reader).search([("company_id", "=", company.id)], order="name, id")]
        selling = [row for row in branches if row["net_sales"] is not False]
        previous = self.sudo().search([("company_id", "=", company.id), ("date_end", "<", start)], limit=1)
        totals = figures(False)
        return {
            "company": company.name,
            "period": {"start": first, "end": last, "label": start.strftime("%B %Y")},
            "taken_at": fields.Datetime.to_string(fields.Datetime.now()),
            "read_as": reader.name,
            "totals": totals,
            "previous": {"label": previous.date_start.strftime("%B %Y"), "net_sales": previous.snapshot["totals"].get("net_sales")}
                        if previous and previous.snapshot else False,
            "branches": branches,
            "best": max(selling, key=lambda row: row["net_sales"])["name"] if len(selling) > 1 else False,
            "lowest": min(selling, key=lambda row: row["net_sales"])["name"] if len(selling) > 1 else False,
            "objectives": self._objectives_snapshot(company, start, end, reader),
        }

    @api.model
    def _objectives_snapshot(self, company, start, end, reader):
        goals = [goal for goal in self.env["strategic.goal.setup"].with_user(reader).with_company(company).get_goals() if goal["state"] == "active"]
        statuses = {}
        for goal in goals:
            label = _TONES.get(goal["tone"], "not measured yet")
            statuses[label] = statuses.get(label, 0) + 1
        start_at, end_at = fields.Datetime.to_datetime(start), fields.Datetime.to_datetime(end + timedelta(days=1))
        completed = self.env["strategic.achievement.report"].sudo().search([
            ("company_id", "=", company.id), ("completed_at", ">=", start_at), ("completed_at", "<", end_at)], order="completed_at, id")
        return {
            "active": len(goals),
            "by_status": [{"status": label, "count": statuses[label]} for label in (*_TONES.values(), "not measured yet") if label in statuses],
            "attention": [{"title": goal["title"], "status": goal["status"], "deadline": goal["deadline"] or ""}
                          for goal in goals if goal["tone"] == "bad"],
            "completed": [{"name": report.objective_id.name, "result": report.result or "", "branch": report.warehouse_id.display_name or ""}
                          for report in completed],
        }

    def _notify_head_office(self):
        """One note to every Head Office user, with the PDF attached when it can be made."""
        self.ensure_one()
        users = self.env["res.users"].sudo().search([("active", "=", True), ("share", "=", False),
                                                     ("group_ids", "in", self.env.ref(_HEAD).ids), ("company_ids", "in", self.company_id.id)])
        attachments = []
        try:
            pdf, _kind = self.env["ir.actions.report"].sudo()._render_qweb_pdf("strategic_management.action_report_strategic_company", self.ids)
            attachments = [("%s.pdf" % self.name, pdf)]
        except (UserError, OSError) as error:  # no PDF engine on this server: the note still goes out
            _logger.warning("Company report %s sent without PDF: %s", self.name, error)
        totals = (self.snapshot or {}).get("totals") or {}
        self.message_post(
            body=Markup("<p><b>Monthly company report: %s</b></p><p>Net sales %s · orders %s · waste %s.</p><p>Open it in Reports &gt; Company.</p>") % (
                self.snapshot["period"]["label"], self._value("net_sales", totals.get("net_sales")), self._value("orders", totals.get("orders")),
                self._value("waste_cost", totals.get("waste_cost"))),
            partner_ids=users.partner_id.ids, attachments=attachments, subtype_xmlid="mail.mt_comment")

    def action_create_last_month(self):
        """Head Office: create last month's report now instead of waiting for the daily job (list button, so it gets records)."""
        if not self.env.user.has_group(_HEAD):
            raise AccessError("Company reports are created by Head Office.")
        start, end = self._last_month(fields.Date.context_today(self))
        report = self._create_for_month(self.env.company, start, end)
        if not report:
            report = self.search([("company_id", "=", self.env.company.id), ("date_start", "=", start)], limit=1)
            if not report:
                raise UserError("The report could not be created: no Head Office user can read the figures.")
        return {"type": "ir.actions.act_window", "res_model": self._name, "res_id": report.id, "view_mode": "form", "target": "current"}

    # ------------------------------------------------------------------ showing
    def _value(self, key, value):
        if value is False or value is None:
            return "Not available"
        return format_amount(self.env, value, self.company_id.currency_id) if key in _MONEY else value

    def _render_summary(self, snap):
        if not snap:
            return False
        totals = snap.get("totals") or {}
        rows = Markup("").join(Markup("<tr><td>%s</td><td>%s</td></tr>") % (label, self._value(key, totals.get(key))) for label, key in _FIGURES)
        previous = snap.get("previous")
        change = ""
        if previous and previous.get("net_sales") and totals.get("net_sales") is not False:
            pct = (totals["net_sales"] - previous["net_sales"]) / previous["net_sales"] * 100
            change = Markup("<p>Net sales %s%.0f%% compared with %s.</p>") % ("+" if pct >= 0 else "", pct, previous["label"])
        head = Markup("").join(Markup("<th>%s</th>") % label for label, _key in _FIGURES)
        branch_rows = Markup("").join(
            Markup("<tr><td>%s</td>%s<td>%s</td></tr>") % (
                row["name"], Markup("").join(Markup("<td>%s</td>") % self._value(key, row.get(key)) for _label, key in _FIGURES), row["branch_report"])
            for row in snap.get("branches") or [])
        ranking = Markup("<p>Highest sales: <b>%s</b> · lowest: <b>%s</b>.</p>") % (snap["best"], snap["lowest"]) if snap.get("best") else ""
        objectives = snap.get("objectives") or {}
        completed = Markup("").join(Markup("<li>%s%s: %s</li>") % (item["name"], " (%s)" % item["branch"] if item["branch"] else "", item["result"])
                                    for item in objectives.get("completed") or [])
        attention = Markup("").join(Markup("<li>%s: %s, due %s</li>") % (item["title"], item["status"], item["deadline"] or "—")
                                    for item in objectives.get("attention") or [])
        statuses = ", ".join("%s %s" % (item["count"], item["status"]) for item in objectives.get("by_status") or [])
        return Markup(
            "<p>%s · figures taken %s.</p>"
            "<h4>Company</h4><table class='table table-sm'>%s</table>%s"
            "<h4>Branches</h4><table class='table table-sm'><thead><tr><th>Branch</th>%s<th>Branch report</th></tr></thead><tbody>%s</tbody></table>%s"
            "<h4>Objectives</h4><p>Active: %s%s</p>"
            "<h5>Completed this month</h5>%s<h5>Needing attention</h5>%s"
        ) % (snap["period"]["label"], snap.get("taken_at", ""), rows, change, head, branch_rows, ranking,
             objectives.get("active", 0), " (%s)" % statuses if statuses else "",
             Markup("<ul>%s</ul>") % completed if completed else Markup("<p>None.</p>"),
             Markup("<ul>%s</ul>") % attention if attention else Markup("<p>None.</p>"))
