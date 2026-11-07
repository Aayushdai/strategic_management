"""Hoshin countermeasures: when a Hoshin metric is off target two months running, its owner records why and what
will be done about it (cause, action, due date). Opened by the daily KPI job; edited by Head Office from the Hoshin
review or by the owner from their To-Do."""

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

_HEAD = "strategic_management.group_strategic_head_office_manager"
RED_MONTHS = 2  # off target this many months in a row opens a countermeasure


class StrategicHoshinCountermeasure(models.Model):
    _name = "strategic.hoshin.countermeasure"
    _description = "Hoshin Countermeasure"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    name = fields.Char(required=True, readonly=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True, index=True)
    kpi_id = fields.Many2one("strategic.kpi.definition", "Metric", required=True, readonly=True, index=True)
    red_months = fields.Integer("Months off target", readonly=True)
    owner_id = fields.Many2one("res.users", "Owner", required=True, tracking=True)
    cause = fields.Text(tracking=True)
    action = fields.Text("Countermeasure", tracking=True)
    due_date = fields.Date(tracking=True)
    state = fields.Selection([("open", "Open"), ("done", "Done")], required=True, default="open", tracking=True)

    def _row(self):
        self.ensure_one()
        return {"id": self.id, "state": self.state, "owner": self.owner_id.name, "red_months": self.red_months, "cause": self.cause or "",
                "action": self.action or "", "due_date": fields.Date.to_string(self.due_date) if self.due_date else "", "opened": fields.Date.to_string(self.create_date)}

    @api.model
    def _open_for_red_metrics(self):
        """Daily: one open countermeasure per Hoshin metric that is off target RED_MONTHS months running."""
        today = fields.Date.context_today(self)
        Dashboard = self.env["strategic.performance.dashboard"].sudo()
        head = self.env.ref(_HEAD)
        for company in self.env["res.company"].sudo().search([]):
            for row in Dashboard._hoshin_rows(company, today.year, today):
                if row["red_streak"] < RED_MONTHS or self.sudo().search_count([("kpi_id", "=", row["kpi_id"]), ("state", "=", "open"), ("company_id", "=", company.id)]):
                    continue
                owner = self.env["res.users"].sudo().browse(row["owner_ids"][:1]) or self.env["res.users"].sudo().search(
                    [("group_ids", "in", head.id), ("company_ids", "in", company.id), ("share", "=", False)], limit=1)
                if not owner:
                    continue
                record = self.sudo().create({"name": "%s: off target %s months" % (row["name"], row["red_streak"]), "company_id": company.id,
                                             "kpi_id": row["kpi_id"], "red_months": row["red_streak"], "owner_id": owner.id})
                self.env["strategic.kpi.target"].sudo()._create_deduplicated_activity(
                    record, "Countermeasure needed: %s" % row["name"],
                    "%s has missed its target %s months in a row. Record the cause, the countermeasure and a due date." % (row["name"], row["red_streak"]),
                    recipients=owner, company=company)

    @api.model
    def save_countermeasure(self, countermeasure_id, values, done=False):
        """Head Office, or the countermeasure's owner, records cause, action and due date; done closes it."""
        record = self.sudo().browse(int(countermeasure_id or 0)).exists()
        if not record or record.company_id != self.env.company:
            raise AccessError("This countermeasure is not in the active company.")
        if not (self.env.user.has_group(_HEAD) or record.owner_id == self.env.user):
            raise AccessError("Only Head Office or the countermeasure's owner can change it.")
        cause, action = (values.get("cause") or "").strip(), (values.get("action") or "").strip()
        due = values.get("due_date") or False
        try:
            due = fields.Date.to_date(due) if due else False
        except ValueError:
            raise ValidationError("Enter a valid due date.")
        if done and not (cause and action):
            raise ValidationError("Record the cause and the countermeasure before closing it.")
        record.write({"cause": cause or False, "action": action or False, "due_date": due, "state": "done" if done else "open"})
        if done:
            record.activity_ids.action_feedback(feedback="Countermeasure recorded.")
        return record._row()
