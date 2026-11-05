"""What the Business Goals screen reads: the goal list, each goal's payload, workflow steps and status.
Part of strategic.goal.setup."""

import json

from odoo import api, fields, models

from .goal_templates import GOAL_TYPES, attainment_tone
from .goal_setup import _AREA_LABELS, _LOCKED_STATES, _human_date, _time_label, _waiting_bar


_MONITOR_NEXT_STEP = {
    "needs_setup": "Choose what to monitor, the reporting period and the daily time window, then check readiness.",
    "ready": "All checks passed. Submit the goal for approval; its sales are already shown above.",
}


class StrategicGoalSetupPayload(models.Model):
    _inherit = "strategic.goal.setup"

    # ------------------------------------------------------------------ read model

    @api.model
    def get_goals(self, area=False):
        """Goals visible to the user; the same records serve Business Goals and every area page."""
        objectives = self.env["strategic.objective"].search(
            [("company_id", "=", self.env.company.id), ("state", "!=", "archived")], order="deadline, id"
        )
        goals = []
        for objective in objectives:
            if area and area not in objective._goal_areas():
                continue
            setup = objective.goal_setup_ids[:1]
            goals.append(setup._goal_payload(objective) if setup else self._goal_payload(objective))
        return goals

    def _workflow_steps(self, objective, result):
        """Steps after setup, computed from the linked records (never stored, never part of the 'ready' decision):
        target and Key Result, approval, result. Each is ok, pending or a problem."""
        if not self or not self.measure_key:
            return []
        steps = []

        def step(key, label, state, message):
            steps.append({"key": key, "label": label, "ok": state == "ok", "state": state, "message": message})

        if self._is_monitoring():
            approved = objective.state in ("active", "completed", "closed")
            step("approval", "Goal approved", "ok" if approved else "pending", "Approved by Head Office." if approved else (
                "Waiting for Head Office approval." if objective.state == "pending_approval" else "Submit the goal for Head Office approval."))
            return steps

        kr, target = self.key_result_id, self.target_id
        if kr and target.state == "approved":
            step("linked", "Target and Key Result linked", "ok",
                 "Target %s approved; Key Result '%s' measures it with '%s'." % (self._target_label(self.target_value), kr.name, kr.primary_kpi_id.name))
        else:
            step("linked", "Target and Key Result linked", "pending", {
                "needs_setup": "Created once the setup checks pass and Head Office prepares and approves the target.",
                "ready": "Head Office prepares the target next.",
                "target_pending": "The target is waiting for Head Office approval; the Key Result is created then.",
            }.get(self.setup_state, "Not linked yet."))
            return steps
        state = objective.state
        if state == "active" or state in ("completed", "closed"):
            step("approval", "Goal approved", "ok", "Approved by Head Office.")
        elif state == "pending_approval":
            step("approval", "Goal approved", "pending", "Waiting for Head Office approval.")
        else:
            step("approval", "Goal approved", "pending", "Submit the goal for Head Office approval.")
        evaluation = kr.evaluation_id
        if evaluation and evaluation.status == "valid" and evaluation.has_actual_value:
            step("result", "Result evaluated", "ok", "Final result recorded for %s to %s." % (_human_date(evaluation.date_start), _human_date(evaluation.date_end)))
        elif result and result.get("value_display"):
            step("result", "Result evaluated", "pending", "%s so far; the final result is recorded after %s." % (
                result["value_display"], _human_date(result["period_end"])))
        elif result and result.get("status") == "Data unavailable":
            step("result", "Result evaluated", "fail", result.get("note") or "The data source returned a problem.")
        else:
            step("result", "Result evaluated", "pending", (result or {}).get("note") or "Waiting for data.")
        return steps

    def _goal_payload(self, objective):
        setup = self[:1]
        is_head = self._is_head_office()
        state = objective.state
        status, tone = self._status(objective, setup)
        key_results = objective.okr_objective_ids.key_result_ids.filtered("required")
        current, current_note = "Not measured", "No Key Result is linked to this goal yet."
        if setup and not key_results:
            # The Key Result is created when Head Office approves the target; say which step comes next.
            current_note = {
                "needs_setup": "No Key Result yet: choose what to measure and click 'Save and check readiness'.",
                "ready": "No Key Result yet: Head Office prepares the target next; the Key Result is created when it is approved.",
                "target_pending": "No Key Result yet: it is created when Head Office approves the target.",
            }.get(setup.setup_state, current_note)
        if len(key_results) == 1:
            evaluation = key_results.evaluation_id
            if evaluation and evaluation.status == "valid" and evaluation.has_actual_value:
                current, current_note = "%g %s" % (evaluation.actual_value, evaluation.unit or ""), "Evaluated %s to %s." % (evaluation.date_start, evaluation.date_end)
            else:
                current, current_note = "Not evaluated", (evaluation.reason if evaluation else "No evaluation has been recorded for the target period yet.")
        elif key_results:
            current, current_note = "%s Key Results" % len(key_results), "See the objective for each Key Result."
        target_display = False
        if setup and setup.measure_key and setup.target_value:
            target_display = setup._target_label(setup.target_value)
        elif len(key_results) == 1:
            target_display = "%g %s" % (key_results.target_id.target_value, key_results.unit or "")
        # Baseline: only the one Head Office recorded on the Key Result's target, in that target's unit.
        baseline = key_results.target_id.baseline_id if len(key_results) == 1 else False
        baseline_display = ("%g %s" % (baseline.baseline_value, key_results.unit or "")).strip() if baseline else False
        scored = objective.scoring_status not in ("not_evaluated", "data_quality_issue")
        result = setup._result_view(objective) if setup else False
        monitor = setup._monitor_view(objective) if setup else False
        if monitor and monitor["status"] == "ok":
            # Time breakdown for the card, only when the goal's own figure is valid.
            monitor["timeline"] = setup._monitor_timeline(objective)
        if monitor:
            # Monitoring: the live value, no target; never "on track", "at risk", achieved or failed.
            current = monitor["value_display"] or {"empty": {"pos": "No POS data", "purchase": "No purchases"}.get(setup._monitor_source(), "Nothing to show"),
                                                   "data_problem": "Data unavailable"}.get(
                monitor["status"], "Not shown")
            current_note = monitor["note"]
            target_display = "No target (monitoring)"
            scored = False
            if state == "active":
                status, tone = "Monitoring", "neutral"
        if result and result["status"]:
            # A template goal reports its value against the target; it is never shown as achieved before the period ends.
            if result["value_display"]:
                current = result["value_display"]
                current_note = "%s. %s" % (result["value_label"], result["note"])
            if state == "active":
                status, tone = result["status"], result["tone"]
            scored = False
        # Performance panels: 'measured' (ranked by progress), 'missed' (final result below target after the
        # period), or 'setup' (no valid measurement yet). Missing data is never ranked as 0% or counted as missed.
        today = fields.Date.context_today(self)
        bar, category, reason = False, "setup", ""
        if result and result["value"] is not False:
            bar = result["bar"]
            category = "missed" if result["final"] and result["tone"] == "bad" else "measured"
        elif result and result["bar"]:
            bar = result["bar"]
        elif not result and scored:
            # Objectives scored by Key Result attainment (100 = target met), higher is better.
            score = objective.score
            bar = {"kind": "attainment", "pct": round(score, 1), "bar": round(max(0.0, min(100.0, score)), 1),
                   "tone": attainment_tone(score), "margin": round(score - 100.0, 2), "label": "%g%% of the target reached" % round(score, 1)}
            missed = objective.scoring_status == "not_achieved" and objective.deadline and objective.deadline < today
            category = "missed" if missed else "measured"
        elif not result and setup and setup.key_result_id and setup.target_id.state == "approved":
            bar = _waiting_bar("Waiting for data")
        if category == "setup":
            failing = next((check["message"] for check in (json.loads(setup.readiness_json) if setup and setup.readiness_json else [])
                            if not check["ok"]), "")
            if state == "pending_approval":
                reason = "Waiting for Head Office approval."
            elif result and (result["note"] or result["status"]):
                reason = result["note"] or result["status"]
            elif setup and state == "draft":
                reason = failing or {
                    "needs_setup": "Choose how this goal will be measured, then check readiness.",
                    "ready": "Head Office prepares the target next.",
                    "target_pending": "Head Office must approve the target.",
                    "measured": "Submit the goal for approval.",
                }.get(setup.setup_state, "")
            else:
                reason = current_note
        if monitor:
            if state in ("active", "completed"):
                category, reason = "monitoring", ""
            elif state == "draft":
                reason = _MONITOR_NEXT_STEP.get(setup.setup_state, reason) if setup.setup_state != "needs_setup" else reason
        # Head Office's latest decision on this objective (the review log is read within the user's own access).
        review = objective.review_ids[:1]
        review_feedback = {
            "decision": review.decision, "status": dict(review._fields["decision"].selection)[review.decision],
            "reason": review.reason or "", "reviewer": review.reviewer_id.name,
            "decided_at": fields.Date.to_string(fields.Datetime.context_timestamp(self, review.decided_at).date()) if review.decided_at else False,
        } if review else False
        if state == "draft" and review.decision == "rejected":
            # A rejection returns the proposal to draft so its proposer can revise and resubmit it.
            status, tone = "Returned for changes", "warn"
            reason = "Returned by Head Office: %s" % review.reason if category == "setup" else reason
        return {
            "setup_id": setup.id or False,
            "objective_id": objective.id,
            "title": objective.name,
            "areas": objective._goal_areas(),
            "area_label": ", ".join(_AREA_LABELS[a] for a in objective._goal_areas()) or "No business area",
            "description": objective.description or "",
            "desired_result": setup.desired_result or "",
            "deadline": fields.Date.to_string(objective.deadline) if objective.deadline else False,
            "owner": objective.owner_id.name,
            "scope": "Entire business" if objective.scope == "organization" else (objective.warehouse_id.display_name or "Branch"),
            "plan": objective.plan_id.name,
            "scope_kind": objective.scope,
            "warehouse_id": objective.warehouse_id.id or False,
            "parent_objective": objective.parent_objective_id.display_name or False,
            "overdue": bool(objective.overdue),
            "baseline": baseline_display,
            "review_feedback": review_feedback,
            "state": state,
            "status": status,
            "tone": tone,
            "setup_state": setup.setup_state or False,
            "measure_key": setup.measure_key or False,
            "measure_label": (setup._template().get("title") or dict(self._fields["measure_key"].selection).get(setup.measure_key)) if setup.measure_key else False,
            "product": {"id": setup.product_id.id, "name": setup.product_id.display_name, "uom": setup.product_id.uom_id.name or ""} if setup.product_id else False,
            "target_value": setup.target_value if setup else False,
            "period_start": fields.Date.to_string(setup.period_start) if setup.period_start else False,
            "period_end": fields.Date.to_string(setup.period_end) if setup.period_end else False,
            "kpi": key_results.primary_kpi_id[:1].name or (setup.kpi_id.name if setup else False) or False,
            "target": target_display,
            "current": current,
            "current_note": current_note,
            "bar": bar,
            "margin": bar["margin"] if bar else False,
            "category": category,
            "reason": reason,
            "readiness": (json.loads(setup.readiness_json) if setup.readiness_json else []) + setup._workflow_steps(objective, result),
            "result": result,
            "okr": setup._okr_view(objective),
            "template_key": setup.template_key or False,
            "template_title": setup._template().get("title", False) if setup else False,
            "can_configure": bool(setup) and state == "draft" and setup.setup_state not in _LOCKED_STATES,
            "goal_type": objective.goal_type or "optimization",
            "goal_type_label": dict(GOAL_TYPES).get(objective.goal_type or "optimization"),
            "monitor": monitor,
            "next_step": _MONITOR_NEXT_STEP.get(setup.setup_state, False) if monitor else False,
            "period_preset": setup.period_preset or "" if monitor else "",
            "window_start": _time_label(setup.window_start) if monitor else "",
            "window_end": _time_label(setup.window_end) if monitor else "",
            "pos_category": {"id": setup.pos_category_id.id, "name": setup.pos_category_id.display_name} if setup.pos_category_id else False,
            "partner": {"id": setup.partner_id.id, "name": setup.partner_id.display_name} if setup.partner_id else False,
            "product_category": {"id": setup.product_category_id.id, "name": setup.product_category_id.display_name} if setup.product_category_id else False,
            "can_prepare_target": bool(setup) and is_head and setup.setup_state == "ready" and not monitor,
            "can_approve_target": bool(setup) and is_head and setup.setup_state == "target_pending",
            "can_submit": bool(setup) and state == "draft" and setup.setup_state == ("ready" if monitor else "measured"),
            "can_approve_goal": is_head and state == "pending_approval",
            "can_evaluate": bool(setup) and is_head and bool(setup.target_id) and setup.target_id.state == "approved"
                            and (not result or result["phase"] == "closed"),
        }

    @api.model
    def _status(self, objective, setup):
        """Owner-facing status derived only from server state; 'On track' requires a valid evaluation."""
        state = objective.state
        if state == "draft":
            return {
                "needs_setup": ("Needs setup", "warn"),
                "ready": ("Ready for target", "neutral"),
                "target_pending": ("Target awaiting approval", "neutral"),
                "measured": ("Ready to submit", "neutral"),
            }.get(setup.setup_state, ("Draft", "neutral"))
        if state == "pending_approval":
            return ("Pending approval", "neutral")
        if state == "active":
            return {
                "achieved": ("On track", "good"),
                "exceeded": ("On track", "good"),
                "almost_met": ("Needs attention", "warn"),
                "not_achieved": ("At risk", "bad"),
                "data_quality_issue": ("Data problem", "bad"),
            }.get(objective.scoring_status, ("Active · not evaluated yet", "neutral"))
        return ({"completed": "Completed", "closed": "Closed"}.get(state, state.title()), "good" if state == "completed" else "neutral")
