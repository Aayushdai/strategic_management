"""Objective Requests on the Overview: branch objectives submitted for Head Office review, and the review history.

The requests are the existing strategic.objective records in "pending_approval"; decisions go through the objective's
own action_approve / action_reject (via the goal setup's approve_goal when one exists), which check the role and state
and append to strategic.objective.review.
"""

from odoo import api, fields, models
from odoo.exceptions import UserError

_HISTORY_LIMIT = 200


class StrategicPerformanceDashboardObjectiveRequests(models.AbstractModel):
    _inherit = "strategic.performance.dashboard"

    @api.model
    def get_objective_requests(self, warehouse_id=False):
        """Pending branch proposals and decided reviews within the user's branch scope."""
        role = self._strategic_role()
        result = {
            "available": False,
            "role": role,
            "can_decide": False,
            "branch_options": [],
            "selected_warehouse_id": False,
            "pending": [],
            "history": [],
            "messages": [],
        }
        if role == "employee":
            result["messages"] = ["Objective requests are available to Head Office and Branch Managers."]
            return result

        permitted, selected = self._strategic_branch_scope(role, warehouse_id)
        warehouses = selected or permitted
        company = self.env.company
        result.update({
            "available": True,
            "can_decide": role == "head_office",
            "branch_options": [{"id": warehouse.id, "name": warehouse.display_name} for warehouse in permitted],
            "selected_warehouse_id": selected.id if selected else False,
        })

        pending = self.env["strategic.objective"].search(
            [
                ("company_id", "=", company.id),
                ("scope", "=", "branch"),
                ("state", "=", "pending_approval"),
                ("warehouse_id", "in", warehouses.ids),
            ],
            order="submitted_at desc, id desc",
        )
        result["pending"] = [self._objective_request_row(objective) for objective in pending]

        reviews = self.env["strategic.objective.review"].search(
            [
                ("company_id", "=", company.id),
                ("scope", "=", "branch"),
                ("warehouse_id", "in", warehouses.ids),
            ],
            limit=_HISTORY_LIMIT,
        )
        result["history"] = [self._objective_review_row(review) for review in reviews]
        return result

    @api.model
    def decide_objective_request(self, objective_id, decision, reason=False, warehouse_id=False):
        """Approve or reject one pending proposal, then return the refreshed requests."""
        objective = self.env["strategic.objective"].browse(int(objective_id)).exists()
        if not objective or objective.scope != "branch":
            raise UserError("This objective request no longer exists.")
        if decision == "approve":
            setup = objective.goal_setup_ids[:1]
            # A goal with a setup is approved through its own step, which also re-checks the plan dates.
            if setup:
                setup.approve_goal()
            else:
                objective.action_approve()
        elif decision == "reject":
            objective.action_reject(reason)
        else:
            raise UserError("Choose Approve or Reject.")
        return self.get_objective_requests(warehouse_id)

    def _objective_request_row(self, objective):
        setup = objective.goal_setup_ids[:1]
        goal = setup._goal_payload(objective) if setup else {}
        return {
            "objective_id": objective.id,
            "name": objective.name,
            "description": objective.description or goal.get("desired_result") or "",
            "branch": objective.warehouse_id.display_name,
            "warehouse_id": objective.warehouse_id.id,
            "parent_objective": objective.parent_objective_id.display_name or False,
            "plan": objective.plan_id.display_name,
            "proposer": objective.proposer_id.name or objective.create_uid.name,
            "submitted_at": fields.Datetime.to_string(objective.submitted_at) if objective.submitted_at else False,
            "deadline": fields.Date.to_string(objective.deadline) if objective.deadline else False,
            "proposed_target": objective._review_target_summary() or False,
            "measure": goal.get("measure_label") or False,
            "target": goal.get("target") or False,
            "period_start": goal.get("period_start") or False,
            "period_end": goal.get("period_end") or False,
            "goal_type": goal.get("goal_type_label") or False,
            "area": goal.get("area_label") or False,
            "readiness": [
                {"label": step.get("label"), "ok": bool(step.get("ok")), "message": step.get("message") or ""}
                for step in goal.get("readiness") or []
            ],
            "status": "Pending Review",
        }

    def _objective_review_row(self, review):
        objective = review.objective_id
        return {
            "review_id": review.id,
            "objective_id": objective.id or False,
            "objective_state": objective.state or False,
            "name": review.objective_name,
            "branch": review.warehouse_id.display_name or "",
            "proposer": review.proposer_id.name or "",
            "submitted_at": fields.Datetime.to_string(review.submitted_at) if review.submitted_at else False,
            "proposed_target": review.proposed_target or False,
            "decision": review.decision,
            "status": dict(review._fields["decision"].selection)[review.decision],
            "reason": review.reason or "",
            "reviewer": review.reviewer_id.name,
            "decided_at": fields.Datetime.to_string(review.decided_at),
        }
