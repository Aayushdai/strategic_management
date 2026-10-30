"""Branch Manager objective execution (Phase 2): follow the objectives that apply to the branch and report blockers.

Objectives come from the existing workflow (strategic.objective, read with the user's own record rules: company-wide
objectives plus the manager's branch). Status and progress come from strategic.goal.setup.get_goals, so nothing is
calculated twice. A blocker is an internal note on the objective plus an existing Head Office To-Do activity; it never
changes the objective's state or target.
"""

import re
from datetime import timedelta

from markupsafe import Markup, escape

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import html2plaintext

_BLOCKER_PREFIX = "Blocker: "
_DUE_SOON_DAYS = 14
_EMPHASIS = re.compile(r"</?(b|strong|i|em)(\s[^>]*)?>", re.IGNORECASE)


def _plain(body):
    """Message text with its line breaks, without the *markers* html2plaintext adds for bold and italics."""
    return html2plaintext(_EMPHASIS.sub("", body or "")).strip()


class StrategicPerformanceDashboardBranchObjectives(models.AbstractModel):
    _inherit = "strategic.performance.dashboard"

    # ------------------------------------------------------------------ access
    @api.model
    def _branch_manager_branch(self):
        """The Branch Manager's own valid branch, or an AccessError (same rule as the other branch data methods)."""
        if self._strategic_role() != "branch_manager":
            raise AccessError("This is available to Branch Managers.")
        context = self.get_navigation_context()
        if context["branch_error"]:
            raise AccessError(context["branch_error"])
        return self.env["stock.warehouse"].browse(context["branch"]["id"])

    @api.model
    def _relevant_objective(self, objective_id, branch):
        """An objective the manager may follow: readable by their record rules, and company-wide or their own branch."""
        objective = self.env["strategic.objective"].browse(int(objective_id or 0)).exists()
        if not objective:
            raise UserError("This objective no longer exists.")
        objective.check_access("read")
        if objective.scope == "branch" and objective.warehouse_id != branch:
            raise AccessError("You can only follow objectives for your own branch.")
        return objective

    # ------------------------------------------------------------------ follow-up of one objective
    @api.model
    def get_objective_followup(self, objective_id):
        """Head Office instructions and blocker reports for one objective (read with the user's rights)."""
        role = self._strategic_role()
        if role == "branch_manager":
            objective = self._relevant_objective(objective_id, self._branch_manager_branch())
        elif role == "head_office":
            objective = self.env["strategic.objective"].browse(int(objective_id or 0)).exists()
            if not objective:
                raise UserError("This objective no longer exists.")
            objective.check_access("read")
        else:
            raise AccessError("Objective follow-up is available to Head Office and Branch Managers.")
        return {
            "objective_id": objective.id,
            "instructions": self._head_office_instructions(objective | objective.parent_objective_id, limit=10),
            "blockers": self._objective_blockers(objective),
            "can_report": role == "branch_manager" and objective.state == "active",
        }

    @api.model
    def _head_office_instructions(self, objectives, limit=10):
        """Messages written by Head Office on these objectives (comments and notes, not system tracking)."""
        if not objectives:
            return []
        head_office = self.env.ref("strategic_management.group_strategic_head_office_manager")
        messages = self.env["mail.message"].search([
            ("model", "=", "strategic.objective"), ("res_id", "in", objectives.ids),
            ("message_type", "=", "comment"), ("author_id.user_ids.group_ids", "in", head_office.ids),
            "|", ("subject", "=", False), ("subject", "not like", _BLOCKER_PREFIX + "%"),
        ], order="date desc, id desc", limit=limit)
        names = {objective.id: objective.name for objective in objectives}
        rows = []
        for message in messages:
            text = _plain(message.body)
            if not text:
                continue
            rows.append({"id": message.id, "objective_id": message.res_id, "objective": names.get(message.res_id, ""),
                         "author": message.author_id.name, "date": fields.Datetime.to_string(message.date), "text": text[:600]})
        return rows

    @api.model
    def _objective_blockers(self, objective):
        """Blocker reports on the objective: open while a Head Office To-Do for it is open."""
        open_summaries = set(self.env["mail.activity"].search([
            ("res_model", "=", "strategic.objective"), ("res_id", "=", objective.id), ("summary", "=like", _BLOCKER_PREFIX + "%"),
        ]).mapped("summary"))
        reports = self.env["mail.message"].search([
            ("model", "=", "strategic.objective"), ("res_id", "=", objective.id), ("message_type", "=", "comment"),
            ("subject", "=like", _BLOCKER_PREFIX + "%"),
        ], order="date desc, id desc", limit=20)
        rows = []
        for message in reports:
            text = _plain(message.body)
            problem = (message.subject or "").removeprefix(_BLOCKER_PREFIX)
            rows.append({"id": message.id, "problem": problem, "text": text[:800], "author": message.author_id.name,
                         "date": fields.Datetime.to_string(message.date),
                         "status": "Waiting for Head Office" if _BLOCKER_PREFIX + problem in open_summaries else "Closed by Head Office"})
        return rows

    @api.model
    def report_objective_blocker(self, objective_id, problem, explanation, help_needed):
        """A Branch Manager reports a blocker on an active objective; Head Office gets a To-Do. Nothing else changes."""
        branch = self._branch_manager_branch()
        objective = self._relevant_objective(objective_id, branch)
        if objective.state != "active":
            raise UserError("Blockers can be reported on active objectives.")
        problem = (problem or "").strip()
        explanation = (explanation or "").strip()
        help_needed = (help_needed or "").strip()
        if not problem or not explanation or not help_needed:
            raise UserError("Describe the problem, explain it and say what you need from Head Office.")
        problem = problem[:120]
        body = Markup(
            "<p><b>Blocker reported</b> for %s</p>"
            "<p><b>Problem:</b> %s</p><p><b>Explanation:</b> %s</p><p><b>Help needed from Head Office:</b> %s</p>"
        ) % (escape(branch.display_name), escape(problem), escape(explanation), escape(help_needed))
        # An internal note on the objective keeps an auditable record (author, date, text) with the objective.
        message = objective.message_post(body=body, subject=_BLOCKER_PREFIX + problem, message_type="comment",
                                         subtype_xmlid="mail.mt_note")
        Target = self.env["strategic.kpi.target"].sudo()
        created = Target._create_deduplicated_activity(
            objective, _BLOCKER_PREFIX + problem,
            "%s, %s: %s Help needed: %s" % (branch.display_name, self.env.user.name, explanation, help_needed),
            scope="organization", company=objective.company_id or self.env.company,
            date_deadline=fields.Date.context_today(self) + timedelta(days=2),
        )
        return {"message_id": message.id, "activities": created, "followup": self.get_objective_followup(objective.id)}

    # ------------------------------------------------------------------ dashboard
    @api.model
    def get_branch_objective_summary(self):
        """Objectives needing the Branch Manager's attention, and recent Head Office instructions, for the dashboard."""
        branch = self._branch_manager_branch()
        goals = self.env["strategic.goal.setup"].get_goals()
        today = fields.Date.context_today(self)
        soon = today + timedelta(days=_DUE_SOON_DAYS)
        relevant = [goal for goal in goals if goal["scope_kind"] == "organization" or goal["warehouse_id"] == branch.id]
        attention = []
        for goal in relevant:
            if goal["state"] != "active":
                continue
            deadline = fields.Date.to_date(goal["deadline"]) if goal["deadline"] else False
            reasons = []
            if goal["overdue"]:
                reasons.append(("overdue", "Deadline passed"))
            elif deadline and deadline <= soon:
                reasons.append(("due_soon", "Due %s" % deadline.strftime("%d %b %Y")))
            if goal["tone"] == "bad":
                reasons.append(("at_risk", goal["status"]))
            elif goal["tone"] == "warn":
                reasons.append(("attention", goal["status"]))
            # Company-wide results are shown to Head Office only (existing rule), so only the branch's own
            # objectives can be flagged for a missing measurement.
            if goal["category"] == "setup" and goal["scope_kind"] == "branch":
                reasons.append(("no_data", "No valid measurement yet"))
            if reasons:
                attention.append({
                    "objective_id": goal["objective_id"], "title": goal["title"], "scope": goal["scope"],
                    "own_branch": goal["scope_kind"] == "branch", "status": goal["status"], "tone": goal["tone"],
                    "deadline": goal["deadline"], "reasons": [label for _key, label in reasons], "keys": [key for key, _label in reasons],
                })
        order = {"overdue": 0, "at_risk": 1, "due_soon": 2, "attention": 3, "no_data": 4}
        attention.sort(key=lambda item: (min(order[key] for key in item["keys"]), item["deadline"] or "9999-12-31", item["title"]))
        objectives = self.env["strategic.objective"].browse([goal["objective_id"] for goal in relevant])
        return {
            "branch": {"id": branch.id, "name": branch.display_name},
            "counts": {
                "relevant": len(relevant),
                "own_branch": sum(1 for goal in relevant if goal["scope_kind"] == "branch" and goal["state"] == "active"),
                "company_wide": sum(1 for goal in relevant if goal["scope_kind"] == "organization" and goal["state"] == "active"),
                "proposals": sum(1 for goal in relevant if goal["scope_kind"] == "branch" and goal["state"] in ("draft", "pending_approval")),
            },
            "attention": attention,
            "instructions": self._head_office_instructions(objectives, limit=5),
        }
