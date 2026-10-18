"""Server-side data contract for the Head Office performance dashboard.

All queries run as the requesting user. Existing model ACLs and record rules
remain the source of truth; no sudo is used in this dashboard service.
"""

from collections import defaultdict
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


_HEAD_OFFICE_GROUP = "strategic_management.group_strategic_head_office_manager"
_ATTAINMENT_LABELS = {
    "not_evaluated": "Not Evaluated",
    "data_quality_issue": "Data Quality Issue",
    "pending_review": "Pending Review",
    "not_achieved": "Not Achieved",
    "almost_met": "Almost Met",
    "achieved": "Achieved",
    "exceeded": "Exceeded",
}
_OBJECTIVE_STATE_LABELS = {
    "draft": "Draft",
    "pending_approval": "Pending Approval",
    "active": "Active",
    "completed": "Completed",
    "closed": "Closed",
    "archived": "Archived",
}
_PERSPECTIVE_LABELS = {
    "financial": "Financial",
    "customer": "Customer",
    "internal_process": "Internal Process",
    "learning_growth": "Learning & Growth",
}


class StrategicPerformanceDashboard(models.AbstractModel):
    """Provide permission-checked summary data for the Head Office dashboard."""

    _name = "strategic.performance.dashboard"
    _description = "Strategic Performance Dashboard Data"

    @api.model
    def get_head_office_data(self, date_start=False, date_end=False):
        """Return current KPI snapshots, objective progress, branch comparisons and trends.

        The KPI average is the arithmetic mean of current valid target evaluations;
        attainment percentages are never summed. Invalid or missing values remain
        exceptions/not-evaluated instead of being represented as zero performance.
        """
        self._require_head_office()
        period_start, period_end = self._resolve_period(date_start, date_end)
        company_id = self.env.company.id
        start_text = fields.Date.to_string(period_start)
        end_text = fields.Date.to_string(period_end)

        Evaluation = self.env["strategic.kpi.evaluation"]
        raw_evaluations = Evaluation.search_read(
            [
                ("company_id", "=", company_id),
                ("superseded_by_id", "=", False),
                ("date_start", "<=", end_text),
                ("date_end", ">=", start_text),
            ],
            [
                "name", "target_id", "kpi_id", "date_start", "date_end", "scope",
                "warehouse_id", "user_id", "status", "attainment_status", "attainment",
                "actual_value", "has_actual_value", "target_value", "unit", "reason",
                "source_note", "evaluated_at",
            ],
            order="evaluated_at desc, id desc",
            limit=5000,
        )
        current_evaluations = self._latest_per_target(raw_evaluations)
        valid_evaluations = [
            row for row in current_evaluations if row.get("status") == "valid"
        ]
        exception_evaluations = [
            row for row in current_evaluations if row.get("status") != "valid"
        ]

        average_attainment = self._mean(
            [row.get("attainment") for row in valid_evaluations]
        )
        branch_comparison = self._branch_comparison(valid_evaluations)
        employee_kpi_results, employee_kpi_summary, employee_kpi_results_truncated = (
            self._employee_kpi_results(current_evaluations)
        )

        monthly_trend = self._monthly_trend(
            valid_evaluations, period_start, period_end
        )

        Objective = self.env["strategic.objective"]
        raw_objectives = Objective.search_read(
            [
                ("company_id", "=", company_id),
                ("state", "in", ["active", "pending_approval", "completed"]),
            ],
            [
                "name", "state", "scope", "warehouse_id", "deadline", "score",
                "scoring_status", "perspective", "overdue", "is_primary",
            ],
            order="deadline asc, name asc",
            limit=2000,
        )
        objectives = [self._format_objective(row) for row in raw_objectives]
        active_objectives = [row for row in objectives if row["state"] == "active"]
        overdue_objectives = [row for row in active_objectives if row["overdue"]]
        pending_count = sum(
            1 for row in objectives if row["state"] == "pending_approval"
        )
        completed_count = sum(1 for row in objectives if row["state"] == "completed")

        active_objectives.sort(
            key=lambda row: (
                not row["overdue"],
                row["deadline"] or "9999-12-31",
                row["name"].casefold(),
            )
        )

        formatted_exceptions = [
            self._format_exception(row) for row in exception_evaluations[:6]
        ]
        formatted_overdue = [
            {
                "id": row["id"],
                "name": row["name"],
                "deadline": row["deadline"],
                "perspective": row["perspective_label"],
            }
            for row in overdue_objectives[:6]
        ]

        return {
            "period": {"start": start_text, "end": end_text},
            "kpi_summary": {
                "average_attainment": average_attainment,
                "valid_evaluation_count": len(valid_evaluations),
                "exception_count": len(exception_evaluations),
            },
            "objective_summary": {
                "active_count": len(active_objectives),
                "overdue_count": len(overdue_objectives),
                "pending_approval_count": pending_count,
                "completed_count": completed_count,
                "evaluated_count": sum(
                    1 for row in objectives if row["progress_pct"] is not False
                ),
            },
            "branch_comparison": branch_comparison,
            "employee_kpi_results": employee_kpi_results,
            "employee_kpi_summary": employee_kpi_summary,
            "monthly_trend": monthly_trend,
            "objectives": active_objectives[:8],
            "attention": {
                "overdue_objectives": formatted_overdue,
                "kpi_exceptions": formatted_exceptions,
            },
            "limits": {
                "evaluations_truncated": len(raw_evaluations) == 5000,
                "employee_kpi_results_truncated": employee_kpi_results_truncated,
                "objectives_truncated": len(raw_objectives) == 2000,
            },
        }

    @api.model
    def get_branch_data(self, date_start=False, date_end=False):
        """Return KPI and objective outcomes for the logged-in manager's branch only.

        The warehouse is resolved from the user's assigned branch; no client-supplied
        warehouse or arbitrary branch ID is accepted. Existing ACLs and record rules
        continue to apply because all searches run as the requesting user.
        """
        warehouse = self._require_branch_manager()
        period_start, period_end = self._resolve_period(date_start, date_end)
        company_id = self.env.company.id
        start_text = fields.Date.to_string(period_start)
        end_text = fields.Date.to_string(period_end)

        Evaluation = self.env["strategic.kpi.evaluation"]
        base_domain = [
            ("company_id", "=", company_id),
            ("superseded_by_id", "=", False),
            ("date_start", "<=", end_text),
            ("date_end", ">=", start_text),
        ]
        evaluation_fields = [
            "name", "target_id", "kpi_id", "date_start", "date_end", "scope",
            "warehouse_id", "user_id", "status", "attainment_status", "attainment",
            "actual_value", "has_actual_value", "target_value", "unit", "reason",
            "source_note", "evaluated_at",
        ]
        branch_rows = Evaluation.search_read(
            base_domain + [("scope", "=", "branch"), ("warehouse_id", "=", warehouse.id)],
            evaluation_fields, order="evaluated_at desc, id desc", limit=3000,
        )
        employee_rows = Evaluation.search_read(
            base_domain + [
                ("scope", "=", "user"),
                ("user_id.strategic_branch_warehouse_id", "=", warehouse.id),
            ],
            evaluation_fields, order="evaluated_at desc, id desc", limit=3000,
        )
        raw_evaluations = sorted(
            branch_rows + employee_rows,
            key=lambda row: (row.get("evaluated_at") or "", row.get("id") or 0),
            reverse=True,
        )
        current_evaluations = self._latest_per_target(raw_evaluations)
        valid_evaluations = [row for row in current_evaluations if row.get("status") == "valid"]
        exception_evaluations = [row for row in current_evaluations if row.get("status") != "valid"]
        active_average = self._mean([row.get("attainment") for row in valid_evaluations])

        Objective = self.env["strategic.objective"]
        raw_objectives = Objective.search_read(
            [
                ("company_id", "=", company_id),
                ("scope", "=", "branch"),
                ("warehouse_id", "=", warehouse.id),
                ("state", "in", ["active", "pending_approval", "completed"]),
            ],
            [
                "name", "state", "scope", "warehouse_id", "deadline", "score",
                "scoring_status", "perspective", "overdue", "is_primary",
            ],
            order="deadline asc, name asc", limit=1000,
        )
        objectives = [self._format_objective(row) for row in raw_objectives]
        active_objectives = [row for row in objectives if row["state"] == "active"]
        overdue_objectives = [row for row in active_objectives if row["overdue"]]
        pending_count = sum(1 for row in objectives if row["state"] == "pending_approval")
        completed_count = sum(1 for row in objectives if row["state"] == "completed")
        active_objectives.sort(
            key=lambda row: (
                not row["overdue"], row["deadline"] or "9999-12-31", row["name"].casefold()
            )
        )

        formatted_kpis = []
        for row in current_evaluations:
            kpi = row.get("kpi_id")
            user = row.get("user_id")
            status = row.get("attainment_status") or "not_evaluated"
            formatted_kpis.append({
                "id": row["id"],
                "name": kpi[1] if kpi else (row.get("name") or "KPI evaluation"),
                "scope": row.get("scope") or "branch",
                "scope_label": "Employee" if row.get("scope") == "user" else "Branch",
                "user_name": user[1] if user else False,
                "date_start": row.get("date_start") or False,
                "date_end": row.get("date_end") or False,
                "actual_value": row.get("actual_value") if row.get("has_actual_value") else False,
                "has_actual_value": bool(row.get("has_actual_value")),
                "target_value": row.get("target_value"),
                "unit": row.get("unit") or "",
                "attainment": row.get("attainment") if row.get("status") == "valid" else False,
                "status": row.get("status") or "not_evaluated",
                "attainment_status": status,
                "attainment_status_label": _ATTAINMENT_LABELS.get(
                    status, status.replace("_", " ").title()
                ),
            })
        formatted_kpis.sort(key=lambda row: (row["status"] != "valid", row["name"].casefold(), row["date_start"] or ""))
        formatted_exceptions = [self._format_exception(row) for row in exception_evaluations[:10]]
        return {
            "company_id": company_id,
            "branch": {"warehouse_id": warehouse.id, "name": warehouse.display_name},
            "period": {"start": start_text, "end": end_text},
            "kpi_summary": {
                "average_attainment": active_average,
                "valid_evaluation_count": len(valid_evaluations),
                "exception_count": len(exception_evaluations),
            },
            "objective_summary": {
                "active_count": len(active_objectives),
                "overdue_count": len(overdue_objectives),
                "pending_approval_count": pending_count,
                "completed_count": completed_count,
                "evaluated_count": sum(1 for row in objectives if row["progress_pct"] is not False),
            },
            "monthly_trend": self._monthly_trend(valid_evaluations, period_start, period_end),
            "objectives": active_objectives[:10],
            "kpis": formatted_kpis[:100],
            "attention": {"kpi_exceptions": formatted_exceptions},
            "limits": {
                "branch_evaluations_truncated": len(branch_rows) == 3000,
                "employee_evaluations_truncated": len(employee_rows) == 3000,
                "objectives_truncated": len(raw_objectives) == 1000,
            },
        }

    @api.model
    def _require_branch_manager(self):
        if not self.env.user.has_group("strategic_management.group_strategic_branch_manager"):
            raise AccessError("Only Branch Managers can access a branch performance dashboard.")
        warehouse = self.env.user.strategic_branch_warehouse_id
        if not warehouse:
            raise AccessError("Assign this Branch Manager to a branch warehouse before opening the dashboard.")
        if warehouse.company_id and warehouse.company_id.id != self.env.company.id:
            raise AccessError("Your assigned branch does not belong to the active company.")
        return warehouse

    @api.model
    def get_employee_data(self, date_start=False, date_end=False):
        """Return only the signed-in employee's personal KPI results and submissions."""
        user = self._require_employee_dashboard_access()
        period_start, period_end = self._resolve_period(date_start, date_end)
        start_text = fields.Date.to_string(period_start)
        end_text = fields.Date.to_string(period_end)

        Evaluation = self.env["strategic.kpi.evaluation"]
        raw_evaluations = Evaluation.search_read(
            [
                ("company_id", "=", self.env.company.id),
                ("scope", "=", "user"),
                ("user_id", "=", user.id),
                ("superseded_by_id", "=", False),
                ("date_start", "<=", end_text),
                ("date_end", ">=", start_text),
            ],
            [
                "name", "target_id", "kpi_id", "date_start", "date_end", "scope",
                "user_id", "status", "attainment_status", "attainment", "actual_value",
                "has_actual_value", "target_value", "unit", "reason", "source_note",
                "evaluated_at",
            ],
            order="evaluated_at desc, id desc",
            limit=1000,
        )
        current_evaluations = self._latest_per_target(raw_evaluations)
        valid_evaluations = [
            row for row in current_evaluations if row.get("status") == "valid"
        ]
        exception_evaluations = [
            row for row in current_evaluations if row.get("status") != "valid"
        ]

        kpis = []
        for row in current_evaluations:
            kpi_ref = row.get("kpi_id") or False
            status = row.get("status") or "not_evaluated"
            attainment_status = row.get("attainment_status") or "not_evaluated"
            actual = row.get("actual_value") if row.get("has_actual_value") else False
            kpis.append({
                "id": row["id"],
                "name": kpi_ref[1] if kpi_ref else (row.get("name") or "KPI evaluation"),
                "kpi_id": kpi_ref[0] if kpi_ref else False,
                "scope": row.get("scope") or "user",
                "user_id": user.id,
                "date_start": row.get("date_start") or False,
                "date_end": row.get("date_end") or False,
                "actual_value": actual,
                "has_actual_value": bool(row.get("has_actual_value")),
                "target_value": row.get("target_value"),
                "unit": row.get("unit") or "",
                "attainment": row.get("attainment") if status == "valid" else False,
                "status": status,
                "attainment_status": attainment_status,
                "attainment_status_label": _ATTAINMENT_LABELS.get(
                    attainment_status, attainment_status.replace("_", " ").title()
                ),
            })
        kpis.sort(key=lambda item: (
            item["date_start"] or "",
            item["date_end"] or "",
            item["id"],
        ), reverse=True)

        valid_attainments = [row.get("attainment") for row in valid_evaluations]
        below_target_count = sum(
            1 for row in valid_evaluations
            if row.get("attainment_status") in ("not_achieved", "almost_met")
        )

        Measurement = self.env["strategic.kpi.measurement"]
        raw_measurements = Measurement.search_read(
            [
                ("company_id", "=", self.env.company.id),
                ("scope", "=", "user"),
                ("user_id", "=", user.id),
                ("date_start", "<=", end_text),
                ("date_end", ">=", start_text),
            ],
            [
                "name", "kpi_id", "target_id", "date_start", "date_end", "actual_value",
                "has_actual_value", "state", "submitted_at", "reviewed_at", "review_note",
                "submitter_note", "submission_version", "corrects_measurement_id",
            ],
            order="date_start desc, id desc",
            limit=500,
        )
        state_labels = {
            "draft": "Draft",
            "submitted": "Submitted for Review",
            "returned": "Returned for Correction",
            "approved": "Approved",
            "rejected": "Rejected",
        }
        measurements = []
        for row in raw_measurements:
            kpi_ref = row.get("kpi_id") or False
            target_ref = row.get("target_id") or False
            state = row.get("state") or "draft"
            measurements.append({
                "id": row["id"],
                "name": row.get("name") or "KPI measurement",
                "kpi_id": kpi_ref[0] if kpi_ref else False,
                "kpi_name": kpi_ref[1] if kpi_ref else "KPI",
                "target_id": target_ref[0] if target_ref else False,
                "date_start": row.get("date_start") or False,
                "date_end": row.get("date_end") or False,
                "actual_value": row.get("actual_value") if row.get("has_actual_value") else False,
                "has_actual_value": bool(row.get("has_actual_value")),
                "state": state,
                "state_label": state_labels.get(state, state.replace("_", " ").title()),
                "submitted_at": row.get("submitted_at") or False,
                "reviewed_at": row.get("reviewed_at") or False,
                "review_note": row.get("review_note") or False,
                "submitter_note": row.get("submitter_note") or False,
                "submission_version": row.get("submission_version") or 0,
                "is_correction": bool(row.get("corrects_measurement_id")),
            })
        measurement_summary = {
            "total_count": len(measurements),
            "draft_count": sum(1 for row in measurements if row["state"] == "draft"),
            "submitted_count": sum(1 for row in measurements if row["state"] == "submitted"),
            "returned_count": sum(1 for row in measurements if row["state"] == "returned"),
            "approved_count": sum(1 for row in measurements if row["state"] == "approved"),
            "rejected_count": sum(1 for row in measurements if row["state"] == "rejected"),
        }
        return {
            "user_id": user.id,
            "user_name": user.name,
            "period": {"start": start_text, "end": end_text},
            "kpi_summary": {
                "average_attainment": self._mean(valid_attainments),
                "evaluation_count": len(current_evaluations),
                "valid_evaluation_count": len(valid_evaluations),
                "exception_count": len(exception_evaluations),
                "below_target_count": below_target_count,
            },
            "kpis": kpis[:250],
            "monthly_trend": self._monthly_trend(
                valid_evaluations, period_start, period_end
            ),
            "measurement_summary": measurement_summary,
            "measurement_submissions": measurements[:100],
            "limits": {
                "evaluations_truncated": len(raw_evaluations) == 1000,
                "measurement_submissions_truncated": len(raw_measurements) == 500,
            },
        }

    @api.model
    def _require_employee_dashboard_access(self):
        """Reject managers as well as users without the Employee role."""
        user = self.env.user
        if (
            not user.has_group("strategic_management.group_strategic_employee")
            or user.has_group(_HEAD_OFFICE_GROUP)
            or user.has_group("strategic_management.group_strategic_branch_manager")
        ):
            raise AccessError(
                "Only employees can access the personal KPI dashboard."
            )
        return user

    @api.model
    def _require_head_office(self):
        if not self.env.user.has_group(_HEAD_OFFICE_GROUP):
            raise AccessError(
                "Only Head Office Managers can access the organization performance dashboard."
            )

    @api.model
    def _resolve_period(self, date_start, date_end):
        today = fields.Date.context_today(self)
        default_end = today
        default_start = today.replace(day=1) - relativedelta(months=5)
        try:
            start = fields.Date.to_date(date_start) if date_start else default_start
            end = fields.Date.to_date(date_end) if date_end else default_end
        except (TypeError, ValueError):
            raise ValidationError("Choose valid reporting start and end dates.")
        if not start or not end:
            raise ValidationError("Choose valid reporting start and end dates.")
        if start > end:
            raise ValidationError("The reporting start date cannot be after the end date.")
        return start, end

    @api.model
    def _latest_per_target(self, rows):
        """Keep the newest unsuperseded row per target; don't merge snapshots."""
        current = {}
        for row in rows:
            target = row.get("target_id")
            target_id = target[0] if target else False
            key = ("target", target_id) if target_id else ("row", row["id"])
            if key not in current:
                current[key] = row
        return list(current.values())

    @api.model
    def _mean(self, values):
        numeric = [float(value) for value in values if isinstance(value, (int, float))]
        if not numeric:
            return False
        return round(sum(numeric) / len(numeric), 2)

    @api.model
    def _branch_comparison(self, valid_evaluations):
        buckets = defaultdict(list)
        labels = {}
        for row in valid_evaluations:
            if row.get("scope") != "branch" or not row.get("warehouse_id"):
                continue
            warehouse_id, warehouse_name = row["warehouse_id"]
            buckets[warehouse_id].append(row.get("attainment"))
            labels[warehouse_id] = warehouse_name
        values = []
        for warehouse_id, attainments in buckets.items():
            mean = self._mean(attainments)
            if mean is False:
                continue
            values.append({
                "warehouse_id": warehouse_id,
                "branch_name": labels.get(warehouse_id) or "Branch",
                "average_attainment": mean,
                "evaluation_count": len(attainments),
            })
        return sorted(
            values,
            key=lambda item: (-item["average_attainment"], item["branch_name"].casefold()),
        )[:12]

    @api.model
    def _employee_kpi_results(self, current_evaluations):
        """Expose the same current employee-scoped evaluations to Head Office.

        Employee evaluations do not necessarily carry warehouse_id themselves. Their
        branch is resolved from the employee user's strategic_branch_warehouse_id,
        matching the branch-scope rule used by the Branch Manager dashboard.
        """
        employee_rows = [
            row for row in current_evaluations
            if row.get("scope") == "user" and row.get("user_id")
        ]
        user_ids = sorted({row["user_id"][0] for row in employee_rows if row.get("user_id")})
        users_by_id = {}
        if user_ids:
            users = self.env["res.users"].search_read(
                [("id", "in", user_ids)],
                ["name", "strategic_branch_warehouse_id"],
                limit=len(user_ids),
            )
            users_by_id = {user["id"]: user for user in users}

        results = []
        for row in employee_rows:
            user_ref = row.get("user_id") or False
            employee_id = user_ref[0] if user_ref else False
            employee = users_by_id.get(employee_id, {})
            warehouse_ref = employee.get("strategic_branch_warehouse_id") or False
            kpi_ref = row.get("kpi_id") or False
            status = row.get("status") or "not_evaluated"
            attainment_status = row.get("attainment_status") or status
            results.append({
                "id": row["id"],
                "employee_id": employee_id,
                "employee_name": employee.get("name") or (user_ref[1] if user_ref else "Unknown employee"),
                "branch_warehouse_id": warehouse_ref[0] if warehouse_ref else False,
                "branch_name": warehouse_ref[1] if warehouse_ref else "Unassigned branch",
                "kpi_id": kpi_ref[0] if kpi_ref else False,
                "kpi_name": kpi_ref[1] if kpi_ref else (row.get("name") or "KPI evaluation"),
                "period_start": row.get("date_start") or False,
                "period_end": row.get("date_end") or False,
                "actual_value": row.get("actual_value") if row.get("has_actual_value") else False,
                "has_actual_value": bool(row.get("has_actual_value")),
                "target_value": row.get("target_value"),
                "unit": row.get("unit") or "",
                "attainment": row.get("attainment") if status == "valid" else False,
                "status": status,
                "attainment_status": attainment_status,
                "attainment_status_label": _ATTAINMENT_LABELS.get(
                    attainment_status, attainment_status.replace("_", " ").title()
                ),
            })

        results.sort(key=lambda item: (
            (item["branch_name"] or "").casefold(),
            (item["employee_name"] or "").casefold(),
            (item["kpi_name"] or "").casefold(),
            item["period_start"] or "",
            item["id"],
        ))
        valid_results = [item for item in results if item["status"] == "valid"]
        summary = {
            "evaluation_count": len(results),
            "valid_evaluation_count": len(valid_results),
            "exception_count": len(results) - len(valid_results),
            "employee_count": len({item["employee_id"] for item in results if item["employee_id"]}),
            "average_attainment": self._mean([item["attainment"] for item in valid_results]),
        }
        return results[:250], summary, len(results) > 250

    @api.model
    def _monthly_trend(self, valid_evaluations, period_start, period_end):
        buckets = defaultdict(list)
        for row in valid_evaluations:
            value = row.get("date_start")
            if not value:
                continue
            try:
                start = fields.Date.to_date(value)
            except (TypeError, ValueError):
                continue
            # A yearly/long-range target should not be plotted as a monthly point
            # before its own start. It can still contribute to the selected-period KPI.
            if start < period_start or start > period_end:
                continue
            key = start.strftime("%Y-%m")
            buckets[key].append(row.get("attainment"))

        result = []
        for key in sorted(buckets):
            month_date = date.fromisoformat(key + "-01")
            mean = self._mean(buckets[key])
            if mean is not False:
                result.append({
                    "month": key,
                    "label": month_date.strftime("%b %Y"),
                    "average_attainment": mean,
                    "evaluation_count": len(buckets[key]),
                })
        return result

    @api.model
    def _format_objective(self, row):
        state = row.get("state") or "draft"
        scoring_status = row.get("scoring_status") or "not_evaluated"
        usable_score = scoring_status in {
            "not_achieved", "almost_met", "achieved", "exceeded"
        }
        return {
            "id": row["id"],
            "name": row.get("name") or "Untitled objective",
            "state": state,
            "state_label": _OBJECTIVE_STATE_LABELS.get(state, state.replace("_", " ").title()),
            "scope": row.get("scope") or "organization",
            "warehouse_id": self._relation_id(row.get("warehouse_id")),
            "deadline": row.get("deadline") or False,
            "overdue": bool(row.get("overdue")),
            "is_primary": bool(row.get("is_primary")),
            "perspective": row.get("perspective") or False,
            "perspective_label": _PERSPECTIVE_LABELS.get(
                row.get("perspective"), "Strategic"
            ),
            "scoring_status": scoring_status,
            "scoring_status_label": _ATTAINMENT_LABELS.get(
                scoring_status, scoring_status.replace("_", " ").title()
            ),
            "progress_pct": round(float(row.get("score") or 0.0), 2) if usable_score else False,
        }

    @api.model
    def _format_exception(self, row):
        kpi = row.get("kpi_id")
        warehouse = row.get("warehouse_id")
        status = row.get("status") or "not_evaluated"
        return {
            "id": row["id"],
            "name": kpi[1] if kpi else (row.get("name") or "KPI evaluation"),
            "period_start": row.get("date_start") or False,
            "period_end": row.get("date_end") or False,
            "scope": row.get("scope") or "organization",
            "branch_name": warehouse[1] if warehouse else False,
            "status": status,
            "status_label": {
                "not_evaluated": "Not Evaluated",
                "data_quality_issue": "Data Quality Issue",
                "pending_review": "Pending Review",
            }.get(status, status.replace("_", " ").title()),
            "reason": row.get("reason") or row.get("source_note") or "No reason was recorded.",
        }

    @api.model
    def _relation_id(self, value):
        return value[0] if value else False
