"""Branch reports (Branch Manager Phase 7): a period report a manager submits to Head Office, kept as it was submitted.

Nothing is calculated here. At each submission the server takes a snapshot of the manager's own existing figures:
Phase 6 branch operations (sales, inventory, purchasing), the objectives that apply to the branch and the task summary.
Head Office review is a decision on the report only; it never changes objectives, KPI evaluations, tasks, purchases
or stock. Notifications reuse Odoo To-Do activities and chatter.
"""

from datetime import timedelta

from markupsafe import Markup, escape

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

_HEAD = "strategic_management.group_strategic_head_office_manager"
_REVIEW_PREFIX = "Review branch report: "
_CHANGES_PREFIX = "Revise branch report: "
_BLOCKER_PREFIX = "Blocker: "
# Only the workflow writes these; a manager may edit the comment while the report is with them.
_EDITABLE_STATES = ("draft", "changes_requested")
# What the saved report shows, and whether each figure describes the period or the moment the figures were taken.
_SUMMARY = [
    ("Sales (period)", "sales", [("Net sales", "net_sales"), ("Completed orders", "orders"), ("Average order value", "average_order_value")]),
    ("Inventory", "inventory", [("Recorded waste (period)", "waste_cost"), ("Receipts completed (period)", "receipts_done"),
                                ("Below minimum stock (when taken)", "low_stock_count"), ("Expiring within 7 days (when taken)", "expiring_count")]),
    ("Purchasing", "purchasing", [("Confirmed purchases (period)", "confirmed_value"), ("Open deliveries (when taken)", "awaiting_count"),
                                  ("Overdue deliveries (when taken)", "overdue_count")]),
    ("Employee tasks (when taken)", "tasks", [("Open", "open"), ("Overdue", "overdue"), ("Awaiting review", "review")]),
]


class StrategicBranchReport(models.Model):
    _name = "strategic.branch.report"
    _description = "Branch Report"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "date_end desc, id desc"

    name = fields.Char(compute="_compute_name", store=True)
    warehouse_id = fields.Many2one("stock.warehouse", string="Branch", required=True, readonly=True, index=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True, default=lambda self: self.env.company)
    date_start = fields.Date("From", required=True, readonly=True)
    date_end = fields.Date("To", required=True, readonly=True)
    state = fields.Selection([("draft", "Draft"), ("submitted", "Submitted"), ("changes_requested", "Changes Requested"),
                              ("reviewed", "Reviewed")], default="draft", required=True, readonly=True, tracking=True)
    comment = fields.Text("Manager's comment")
    review_note = fields.Text("Head Office feedback", readonly=True)
    snapshot = fields.Json("Submitted figures", readonly=True, copy=False)
    snapshot_at = fields.Datetime("Figures taken at", readonly=True, copy=False)
    submission_count = fields.Integer("Submissions", readonly=True, copy=False)
    submitted_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    submitted_at = fields.Datetime(readonly=True, copy=False)
    reviewed_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    reviewed_at = fields.Datetime(readonly=True, copy=False)
    summary_html = fields.Html("Report", compute="_compute_summary_html", sanitize=False)

    @api.depends("warehouse_id", "date_start", "date_end")
    def _compute_name(self):
        for report in self:
            report.name = "%s · %s – %s" % (report.warehouse_id.display_name or "", report.date_start or "", report.date_end or "")

    @api.depends("snapshot")
    def _compute_summary_html(self):
        for report in self:
            snapshot = report.snapshot
            if not snapshot:
                report.summary_html = Markup("<p>Not submitted yet: the figures are saved when the report is submitted.</p>")
                continue
            parts = [Markup("<p>Figures taken %s for %s – %s.</p>") % (snapshot.get("taken_at", ""), report.date_start, report.date_end)]
            for title, section, keys in _SUMMARY:
                data = snapshot.get(section) or {}
                parts.append(Markup("<h4>%s</h4>") % title)
                if not data.get("available"):
                    parts.append(Markup("<p>Not available: %s</p>") % " ".join(data.get("messages") or []))
                    continue
                parts.append(Markup("<table class='table table-sm'>%s</table>") % Markup("").join(
                    # "is" checks: a real 0 is a figure, only False/None mean the figure is missing.
                    Markup("<tr><td>%s</td><td>%s</td></tr>") % (label, "Not available" if data.get(key) is False or data.get(key) is None else data.get(key))
                    for label, key in keys))
            objectives = snapshot.get("objectives") or {}
            parts.append(Markup("<h4>Objectives (when taken)</h4>"))
            if objectives.get("available"):
                parts.append(Markup("<ul>%s</ul>") % Markup("").join(
                    Markup("<li>%s: %s, due %s</li>") % (item["title"], item["status"], item["deadline"] or "—") for item in objectives.get("items") or [])
                    if objectives.get("items") else Markup("<p>No active objectives apply to this branch.</p>"))
            else:
                parts.append(Markup("<p>Not available.</p>"))
            report.summary_html = Markup("").join(parts)

    # ------------------------------------------------------------------ who may do what
    def _dashboard(self):
        return self.env["strategic.performance.dashboard"]

    def _require_branch_manager(self):
        """The report's own branch manager (their valid branch, same check as every branch page)."""
        branch = self._dashboard()._branch_manager_branch()
        for report in self:
            if report.warehouse_id != branch:
                raise AccessError("You can only work on your own branch's reports.")

    def _responsible_managers(self):
        """Who acts on the report now: its submitter while they still manage its branch, else the branch's current managers."""
        self.ensure_one()
        managers = self.env["strategic.kpi.target"].sudo()._get_schedule_notification_users("branch", self.company_id, warehouse=self.warehouse_id)             .filtered(lambda user: user.strategic_branch_warehouse_id == self.warehouse_id and not user.has_group(_HEAD))
        return self.submitted_by_id if self.submitted_by_id in managers else managers

    def _require_head_office(self):
        if not self.env.user.has_group(_HEAD):
            raise AccessError("Only Head Office reviews branch reports.")

    # ------------------------------------------------------------------ create and write
    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            # The branch always comes from the manager's account; figures and states only from the workflow.
            branch = self._dashboard()._branch_manager_branch()
            for vals in vals_list:
                if set(vals) - {"date_start", "date_end", "comment"}:
                    raise ValidationError("A report starts as a draft for your branch; only its period and comment can be set.")
                vals.update(warehouse_id=branch.id, company_id=self.env.company.id, state="draft")
        for vals in vals_list:
            start, end = fields.Date.to_date(vals.get("date_start")), fields.Date.to_date(vals.get("date_end"))
            if not start or not end or start > end:
                raise ValidationError("Choose a reporting period whose start is not after its end.")
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.context.get("strategic_report_workflow") and not self.env.su:
            # Chatter and activity bookkeeping stay possible; everything else goes through the workflow.
            business = {key for key in vals if not key.startswith(("message_", "activity_"))}
            if business - {"comment"}:
                raise ValidationError("A report's branch, period, figures and status change only through its workflow.")
            if business:
                self._require_branch_manager()
                if any(report.state not in _EDITABLE_STATES for report in self):
                    raise ValidationError("A submitted report can no longer be edited.")
        return super().write(vals)

    def _workflow_write(self, vals):
        return self.with_context(strategic_report_workflow=True).write(vals)

    # ------------------------------------------------------------------ snapshot (server side, from existing methods)
    def _build_snapshot(self):
        """The manager's own figures for the report period, read with their rights from the existing methods."""
        self.ensure_one()
        Dashboard = self._dashboard()
        start, end = fields.Date.to_string(self.date_start), fields.Date.to_string(self.date_end)
        ops = Dashboard.get_branch_operations(start, end)
        sales = {key: value for key, value in ops["sales"].items() if key != "trend"}

        def objectives():
            summary = Dashboard.get_branch_objective_summary()
            goals = [goal for goal in self.env["strategic.goal.setup"].get_goals()
                     if goal["state"] == "active" and (goal["scope_kind"] == "organization" or goal["warehouse_id"] == self.warehouse_id.id)]
            return {
                "available": True,
                "counts": summary["counts"],
                "items": [{"title": goal["title"], "scope": goal["scope"], "status": goal["status"], "tone": goal["tone"],
                           "deadline": goal["deadline"], "current": goal["current"], "target": goal["target"] or False} for goal in goals],
                "attention": [{"title": item["title"], "status": item["status"], "deadline": item["deadline"], "reasons": item["reasons"]}
                              for item in summary["attention"]],
            }

        def tasks():
            return {"available": True, **self.env["strategic.branch.task"].get_branch_task_summary()["counts"]}

        return {
            "branch": {"id": self.warehouse_id.id, "name": self.warehouse_id.display_name},
            "period": ops["period"],
            "taken_at": fields.Datetime.to_string(fields.Datetime.now()),
            "sales": sales,
            "inventory": ops["inventory"],
            "purchasing": ops["purchasing"],
            "objectives": Dashboard._operations_section(objectives),
            "tasks": Dashboard._operations_section(tasks),
        }

    @api.model
    def _snapshot_line(self, snapshot):
        """One readable line of the key figures, kept in chatter with each submission."""
        def show(section, key):
            data = snapshot.get(section) or {}
            value = data.get(key)
            return "Not available" if not data.get("available") or value is False or value is None else value
        return ("Net sales %s · orders %s · average %s · waste %s · confirmed purchases %s · open deliveries %s (%s overdue) · "
                "tasks open %s, overdue %s, awaiting review %s") % (
            show("sales", "net_sales"), show("sales", "orders"), show("sales", "average_order_value"), show("inventory", "waste_cost"),
            show("purchasing", "confirmed_value"), show("purchasing", "awaiting_count"), show("purchasing", "overdue_count"),
            show("tasks", "open"), show("tasks", "overdue"), show("tasks", "review"))

    # ------------------------------------------------------------------ workflow
    def action_submit(self, comment=None):
        for report in self:
            report._require_branch_manager()
            if report.state not in _EDITABLE_STATES:
                raise ValidationError("Only a draft or a report returned for changes can be submitted.")
            if comment is not None:
                report.write({"comment": (comment or "").strip() or False})
            snapshot = report._build_snapshot()
            number = report.submission_count + 1
            report._workflow_write({"state": "submitted", "snapshot": snapshot, "snapshot_at": fields.Datetime.now(),
                                    "submitted_by_id": self.env.user.id, "submitted_at": fields.Datetime.now(), "submission_count": number})
            report._close_activities(_CHANGES_PREFIX)  # the manager's "revise" To-Do is done once they resubmit
            label = "Submission %s" % number if number == 1 else "Updated submission %s (replaces submission %s)" % (number, number - 1)
            report.message_post(body=Markup("<p><b>%s</b> for %s – %s</p><p>%s</p>%s") % (
                label, report.date_start, report.date_end, report._snapshot_line(snapshot),
                Markup("<p><b>Comment:</b> %s</p>") % report.comment if report.comment else ""), subtype_xmlid="mail.mt_note")
            # Head Office gets one To-Do each (deduplicated), the same mechanism as blockers.
            self.env["strategic.kpi.target"].sudo()._create_deduplicated_activity(
                report, _REVIEW_PREFIX + report.name, "%s submitted the %s report." % (self.env.user.name, report.name),
                scope="organization", company=report.company_id, date_deadline=fields.Date.context_today(self) + timedelta(days=3))
        return True

    def _close_activities(self, prefix):
        # sudo: removes the open review To-Dos of every recipient on this report once one decision is made.
        self.activity_ids.sudo().filtered(lambda activity: (activity.summary or "").startswith(prefix)).unlink()

    def _decide(self, state, note, message, notify=True):
        self._require_head_office()
        for report in self:
            if report.state != "submitted":
                raise ValidationError("Only a submitted report can be reviewed.")
            report._workflow_write({"state": state, "review_note": note or False, "reviewed_by_id": self.env.user.id,
                                    "reviewed_at": fields.Datetime.now()})
            report._close_activities(_REVIEW_PREFIX)
            # The manager responsible now is told directly (a note addressed to them, not to every follower).
            report.message_post(body=Markup("<p><b>%s</b> by %s</p>%s") % (
                message, self.env.user.name, Markup("<p>%s</p>") % note if note else ""),
                partner_ids=report._responsible_managers().partner_id.ids if notify else [], subtype_xmlid="mail.mt_note")

    def action_mark_reviewed(self, note=None):
        self._decide("reviewed", (note or "").strip(), "Report reviewed")
        return True

    def action_request_changes(self, note=None):
        note = (note or "").strip()
        if not note:
            raise ValidationError("Explain what needs to change.")
        # The manager's To-Do below is their notification (Odoo announces it); the note is the record, not a second alert.
        self._decide("changes_requested", note, "Changes requested", notify=False)
        for report in self:
            self.env["strategic.kpi.target"].sudo()._create_deduplicated_activity(
                report, _CHANGES_PREFIX + report.name, note, recipients=report._responsible_managers(), company=report.company_id,
                date_deadline=fields.Date.context_today(self) + timedelta(days=3))
        return True

    # ------------------------------------------------------------------ data for the app
    def _row(self):
        self.ensure_one()
        return {
            "id": self.id, "name": self.name, "branch": self.warehouse_id.display_name,
            "date_start": fields.Date.to_string(self.date_start), "date_end": fields.Date.to_string(self.date_end),
            "state": self.state, "status": dict(self._fields["state"].selection)[self.state],
            "submitted_by": self.submitted_by_id.name or False, "submitted_at": fields.Datetime.to_string(self.submitted_at) if self.submitted_at else False,
            "submission_count": self.submission_count,
        }

    @api.model
    def get_branch_reports(self):
        """Branch Managers: their own branch's reports. Head Office: every branch of the company."""
        role = self._dashboard()._strategic_role()
        if role == "branch_manager":
            domain = [("warehouse_id", "=", self._dashboard()._branch_manager_branch().id)]
        elif role == "head_office":
            domain = []
        else:
            raise AccessError("Branch reports are for Branch Managers and Head Office.")
        reports = self.search(domain + [("company_id", "=", self.env.company.id)])
        return {"role": role, "reports": [report._row() for report in reports]}

    @api.model
    def get_branch_report(self, report_id):
        """One report: its saved figures, or for the manager's own editable report a live preview of what would be sent."""
        report = self.browse(int(report_id or 0)).exists()
        if not report:
            raise UserError("This report no longer exists.")
        report.check_access("read")
        role = self._dashboard()._strategic_role()
        if role == "branch_manager":
            report._require_branch_manager()
        elif role != "head_office":
            raise AccessError("Branch reports are for Branch Managers and Head Office.")
        editable = role == "branch_manager" and report.state in _EDITABLE_STATES
        return {
            **report._row(),
            "comment": report.comment or "", "review_note": report.review_note or "",
            "reviewed_by": report.reviewed_by_id.name or False,
            "reviewed_at": fields.Datetime.to_string(report.reviewed_at) if report.reviewed_at else False,
            # Saved figures stay as submitted; a draft (or a report being corrected) shows today's figures, labelled.
            "preview": editable,
            "snapshot": report._build_snapshot() if editable else report.snapshot,
            "snapshot_at": fields.Datetime.to_string(report.snapshot_at) if report.snapshot_at and not editable else False,
            "can_edit": editable,
            "can_review": role == "head_office" and report.state == "submitted",
        }

    @api.model
    def create_branch_report(self, date_start, date_end):
        return self.get_branch_report(self.create({"date_start": date_start, "date_end": date_end}).id)


class StrategicPerformanceDashboardEscalations(models.AbstractModel):
    _inherit = "strategic.performance.dashboard"

    @api.model
    def get_open_blockers(self):
        """Head Office: blockers still waiting for its follow-up (the existing blocker To-Dos, one row per blocker)."""
        if self._strategic_role() != "head_office":
            raise AccessError("Open blockers are listed for Head Office.")
        activities = self.env["mail.activity"].search([
            ("res_model", "=", "strategic.objective"), ("summary", "=like", _BLOCKER_PREFIX + "%")], order="date_deadline, id")
        rows, seen = [], set()
        today = fields.Date.context_today(self)
        for activity in activities:
            key = (activity.res_id, activity.summary)
            if key in seen:
                continue
            seen.add(key)
            objective = self.env["strategic.objective"].browse(activity.res_id).exists()
            if not objective or objective.company_id != self.env.company:
                continue
            # The blocker note on the objective names its author; their branch is the reporting branch.
            message = self.env["mail.message"].search([("model", "=", "strategic.objective"), ("res_id", "=", objective.id),
                                                       ("subject", "=", activity.summary)], order="id desc", limit=1)
            author = message.author_id.user_ids[:1]
            rows.append({
                "objective_id": objective.id, "objective": objective.name, "problem": activity.summary[len(_BLOCKER_PREFIX):],
                "branch": (author.sudo().strategic_branch_warehouse_id.display_name if author else False) or objective.warehouse_id.display_name or "",
                "reported_by": message.author_id.name or "", "reported_on": fields.Date.to_string(activity.create_date.date()),
                "due": fields.Date.to_string(activity.date_deadline), "overdue": activity.date_deadline < today,
            })
        return rows
