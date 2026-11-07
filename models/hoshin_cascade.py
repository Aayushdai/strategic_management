"""Hoshin cascade (catchball) for a Branch Manager: the company's annual objectives for the year, and under each
the branch's own objectives and their tasks.

Company-level items are shown by name only (no results: company-wide figures stay with Head Office). The branch's
objectives and tasks are read with the manager's own access rights, and the branch always comes from the server.
"""

from odoo import api, fields, models


class StrategicPerformanceDashboardHoshinCascade(models.AbstractModel):
    _inherit = "strategic.performance.dashboard"

    @api.model
    def get_branch_cascade(self, year=False):
        branch = self._branch_manager_branch()
        today = fields.Date.context_today(self)
        year = self._hoshin_year(year, today)
        result = {"year": year, "years": list(range(today.year - 2, today.year + 2)), "branch": branch.display_name, "plan": False, "annuals": []}
        plan = self._hoshin_plan(self.env.company, year)
        if not plan:
            return result
        result["plan"] = plan.name
        annuals = self.env["strategic.objective"].sudo().search([
            ("plan_id", "=", plan.id), ("scope", "=", "organization"), ("hoshin_level", "=", "annual"),
            ("state", "in", ("active", "completed"))], order="deadline, name")
        links = self.env["strategic.xmatrix.alignment"].sudo().search([
            ("state", "=", "active"), ("annual_objective_id", "in", annuals.ids), ("link_type", "in", ("B>A", "I>A"))])
        mine = self.env["strategic.objective"].search([
            ("scope", "=", "branch"), ("warehouse_id", "=", branch.id), ("parent_objective_id", "in", annuals.ids),
            ("state", "!=", "archived")], order="deadline, name")
        tasks = self.env["strategic.branch.task"].search([
            ("warehouse_id", "=", branch.id), ("objective_id", "in", (mine | annuals).ids)], order="date_deadline, id")
        goals = {goal["objective_id"]: goal for goal in self.env["strategic.goal.setup"].get_goals() if goal["objective_id"] in mine.ids}
        objective_states = dict(self.env["strategic.objective"]._fields["state"].selection)
        task_states = dict(self.env["strategic.branch.task"]._fields["state"].selection)

        def task_rows(objective):
            return [{"id": task.id, "name": task.name, "assignee": task.assigned_user_id.name, "state": task.state,
                     "state_label": task_states.get(task.state), "deadline": fields.Date.to_string(task.date_deadline),
                     "overdue": task.is_overdue} for task in tasks if task.objective_id == objective]

        for annual in annuals:
            own = links.filtered(lambda link: link.annual_objective_id == annual)
            result["annuals"].append({
                "id": annual.id, "name": annual.name, "deadline": fields.Date.to_string(annual.deadline),
                "breakthrough": own.breakthrough_objective_id.mapped("name"),
                "initiatives": [{"name": initiative.name, "owner": initiative.owner_id.name or ""} for initiative in own.initiative_id],
                "tasks": task_rows(annual),
                "branch_objectives": [{
                    "id": objective.id, "name": objective.name, "state": objective.state, "state_label": objective_states.get(objective.state),
                    "deadline": fields.Date.to_string(objective.deadline), "status": goals.get(objective.id, {}).get("status") or "",
                    "tone": goals.get(objective.id, {}).get("tone") or "neutral", "tasks": task_rows(objective),
                } for objective in mine if objective.parent_objective_id == annual],
            })
        result["uncovered"] = sum(1 for annual in result["annuals"] if not annual["branch_objectives"])
        return result
