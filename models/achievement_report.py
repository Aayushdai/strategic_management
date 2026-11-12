"""Achievement reports: when Head Office completes an objective, what was achieved is frozen in a report.

The report keeps the objective's key results as they were confirmed (target, actual, attainment and where each
figure came from), the branch tasks completed for it and who signed it off. The owner and the branch's managers are
notified. Reports are never edited; they are read on the Reports page and printed from there (views and the
printable page in views/achievement_report_views.xml).
"""

from markupsafe import Markup

from odoo import api, fields, models

_GROUP_BRANCH = "strategic_management.group_strategic_branch_manager"


class StrategicAchievementReport(models.Model):
    _name = "strategic.achievement.report"
    _description = "Objective Achievement Report"
    _inherit = ["mail.thread"]
    _order = "completed_at desc, id desc"

    name = fields.Char(required=True, readonly=True)
    objective_id = fields.Many2one("strategic.objective", required=True, readonly=True, index=True, ondelete="cascade")
    company_id = fields.Many2one("res.company", required=True, readonly=True, index=True)
    warehouse_id = fields.Many2one("stock.warehouse", string="Branch", readonly=True, index=True)
    owner_id = fields.Many2one("res.users", string="Owner", readonly=True, index=True)
    completed_by_id = fields.Many2one("res.users", string="Confirmed by", readonly=True)
    completed_at = fields.Datetime("Confirmed on", required=True, readonly=True)
    score = fields.Float("Overall attainment (%)", readonly=True)
    result = fields.Char(readonly=True)
    snapshot = fields.Json("Achieved results", readonly=True, copy=False)
    summary_html = fields.Html("Report", compute="_compute_summary_html", sanitize=False)

    @api.depends("snapshot")
    def _compute_summary_html(self):
        for report in self:
            report.summary_html = report._render_summary(report.snapshot or {})

    @api.model
    def _create_for(self, objective):
        """Freeze one completed objective's results. Called as the system after Head Office completes it."""
        snapshot = self._build_snapshot(objective)
        report = self.create({
            "name": "%s · achieved" % objective.name,
            "objective_id": objective.id,
            "company_id": objective.company_id.id,
            "warehouse_id": objective.warehouse_id.id or False,
            "owner_id": objective.owner_id.id or False,
            "completed_by_id": objective.completed_by_id.id or self.env.uid,
            "completed_at": fields.Datetime.now(),
            "score": snapshot["score"],
            "result": snapshot["result"],
            "snapshot": snapshot,
        })
        recipients = objective.owner_id
        if objective.warehouse_id:
            recipients |= self.env["res.users"].search([
                ("active", "=", True), ("share", "=", False), ("strategic_branch_warehouse_id", "=", objective.warehouse_id.id),
                ("group_ids", "in", self.env.ref(_GROUP_BRANCH).ids)])
        report.message_post(
            body=Markup("<p><b>%s</b> was completed.</p><p>Result: %s</p>") % (objective.name, snapshot["result"]),
            partner_ids=recipients.partner_id.ids, subtype_xmlid="mail.mt_comment")
        objective.message_post(body=Markup("<p>Achievement report created: %s</p>") % snapshot["result"], subtype_xmlid="mail.mt_note")
        return report

    @api.model
    def _build_snapshot(self, objective):
        statuses = dict(objective._fields["scoring_status"].selection)
        scope = dict(objective._fields["scope"].selection).get(objective.scope, objective.scope)
        key_results = []
        for kr in objective.okr_objective_ids.key_result_ids.sorted(lambda kr: (kr.sequence, kr.id)):
            evaluation = kr.evaluation_id
            key_results.append({
                "name": kr.name,
                "kpi": kr.primary_kpi_id.display_name or "",
                "required": kr.required,
                "target": evaluation.target_value if evaluation else (kr.target_id.target_value if kr.target_id else False),
                "actual": evaluation.actual_value if evaluation and evaluation.has_actual_value else False,
                "unit": evaluation.unit or kr.unit or "",
                "attainment": round(kr.attainment, 1),
                "status": statuses.get(kr.attainment_status) or statuses.get(kr.scoring_status) or "",
                "period": "%s – %s" % (kr.date_start or "", kr.date_end or ""),
                "evidence": (evaluation.source_note or "").strip() if evaluation else "",
                "measured_on": fields.Datetime.to_string(evaluation.evaluated_at) if evaluation else "",
            })
        objectives = objective | objective.child_objective_ids
        tasks = self.env["strategic.branch.task"].search([("objective_id", "in", objectives.ids), ("state", "=", "done")],
                                                        order="reviewed_at, id")
        score = round(objective.score, 1)
        status = statuses.get(objective.scoring_status, "")
        return {
            "objective": objective.name,
            "description": objective.description or "",
            "scope": scope,
            "branch": objective.warehouse_id.display_name or "",
            "owner": objective.owner_id.name or "",
            "plan": objective.plan_id.display_name or "",
            "deadline": fields.Date.to_string(objective.deadline) if objective.deadline else "",
            "completed_by": (objective.completed_by_id or self.env.user).name,
            "completed_on": fields.Date.to_string(fields.Date.context_today(self)),
            "score": score,
            "result": "%s (%.0f%%)" % (status, score) if status else "%.0f%%" % score,
            "key_results": key_results,
            "tasks": [{"name": task.name, "assignee": task.assigned_user_id.name or "", "branch": task.warehouse_id.display_name or "",
                       "done_on": fields.Date.to_string(task.reviewed_at.date()) if task.reviewed_at else ""} for task in tasks],
        }

    @api.model
    def _render_summary(self, snap):
        if not snap:
            return False

        def number(value, unit=""):
            return "—" if value is False or value is None else ("%s %s" % ("{:,.2f}".format(value).rstrip("0").rstrip("."), unit)).strip()

        rows = Markup("").join(
            Markup("<tr><td>%s%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s%%</td><td>%s</td></tr>") % (
                kr["name"], Markup(" <small>(optional)</small>") if not kr["required"] else "", kr["kpi"],
                number(kr["target"], kr["unit"]), number(kr["actual"], kr["unit"]), kr["attainment"], kr["status"])
            for kr in snap.get("key_results", []))
        tasks = Markup("").join(Markup("<li>%s · %s%s</li>") % (task["name"], task["assignee"], " · %s" % task["done_on"] if task["done_on"] else "")
                                for task in snap.get("tasks", []))
        return Markup(
            "<p><b>Result:</b> %s</p><p>%s · %s%s · Owner %s · Plan %s · Due %s</p><p>Confirmed by %s on %s</p>"
            "<h4>Key results</h4><table class='table table-sm'><thead><tr><th>Key result</th><th>KPI</th><th>Target</th>"
            "<th>Actual</th><th>Attainment</th><th>Status</th></tr></thead><tbody>%s</tbody></table>"
            "<h4>Tasks completed</h4>%s"
        ) % (snap["result"], snap["objective"], snap["scope"], " · %s" % snap["branch"] if snap["branch"] else "", snap["owner"] or "—",
             snap["plan"], snap["deadline"], snap["completed_by"], snap["completed_on"], rows,
             Markup("<ul>%s</ul>") % tasks if tasks else Markup("<p>No branch tasks were linked to this objective.</p>"))


class StrategicObjectiveAchievement(models.Model):
    _inherit = "strategic.objective"

    achievement_report_ids = fields.One2many("strategic.achievement.report", "objective_id", readonly=True)

    def action_complete(self):
        result = super().action_complete()
        Report = self.env["strategic.achievement.report"].sudo()
        for objective in self.filtered(lambda objective: objective.state == "completed" and objective.company_id.sm_achievement_reports):
            Report._create_for(objective)
        return result

