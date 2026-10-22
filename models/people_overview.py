"""People section of the Strategic Management home screen.

Employees and attendances are read from the Odoo Employees / Attendances apps
when they are installed; this module does not depend on them. Expected work
times come from each employee's working schedule (``resource``), so public
holidays and approved time off recorded as calendar leaves are not counted as
absences. Staff tasks are Odoo activities. Missing sources are reported as
"Not Configured" instead of being estimated.
"""

from collections import Counter, defaultdict
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from odoo import api, fields, models


LATE_GRACE_MINUTES = 10
_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_UTC = ZoneInfo("UTC")


class StrategicPerformanceDashboardPeople(models.AbstractModel):
    _inherit = "strategic.performance.dashboard"

    @api.model
    def get_people_overview_data(self, date_start=False, date_end=False, warehouse_id=False):
        """Return staffing, attendance and task data limited to the current user's role."""
        start, end = self._resolve_period(date_start, date_end)
        today = fields.Date.context_today(self)
        role = self._strategic_role()
        hr_installed = "hr.employee" in self.env.registry.models
        attendance_installed = hr_installed and "hr.attendance" in self.env.registry.models
        result = {
            "role": role,
            "period": {"start": fields.Date.to_string(start), "end": fields.Date.to_string(end)},
            "sources": {
                "employees": "hr.employee" if hr_installed else "res.users",
                "attendance": attendance_installed,
                "shift_planning": "planning.slot" in self.env.registry.models,
            },
            "branch_options": [],
            "selected_warehouse_id": False,
            "summary": {},
            "staffing": [],
            "attendance_exceptions": [],
            "attendance_by_weekday": [],
            "workload": [],
            "attention": [],
            "actions": [],
            "insights": [],
            "messages": [],
        }
        # Head Office without a branch filter sees the whole company, including
        # staff not yet assigned to a branch; everyone else is limited to staff_users.
        organization_wide = False
        if role == "employee":
            warehouses = self.env["stock.warehouse"].browse()
            staff_users = self.env.user
        else:
            permitted, selected = self._strategic_branch_scope(role, warehouse_id)
            warehouses = selected or permitted
            result["branch_options"] = [{"id": warehouse.id, "name": warehouse.display_name} for warehouse in permitted]
            result["selected_warehouse_id"] = selected.id if selected else False
            # sudo: user names and branch assignments limited to the authorized branches
            # (Head Office without a branch filter also sees unassigned company users).
            domain = [("share", "=", False), ("company_ids", "in", self.env.company.id)]
            organization_wide = role == "head_office" and not selected
            if organization_wide:
                domain += ["|", ("strategic_branch_warehouse_id", "in", warehouses.ids), ("strategic_branch_warehouse_id", "=", False)]
            else:
                domain.append(("strategic_branch_warehouse_id", "in", warehouses.ids))
            staff_users = self.env["res.users"].sudo().search(domain)

        employees = self._people_employees(staff_users, organization_wide) if hr_installed else False
        managers = self._branch_managers(warehouses) if warehouses else {}

        # Staffing by branch: actual staff versus the configured requirement.
        staffing = []
        for warehouse in warehouses:
            if employees is not False:
                actual = len(employees.filtered(lambda employee: employee.user_id.strategic_branch_warehouse_id == warehouse))
            else:
                actual = len(staff_users.filtered(lambda user: user.strategic_branch_warehouse_id == warehouse))
            planned = warehouse.strategic_planned_headcount or False
            gap = planned - actual if planned else False
            staffing.append({
                "warehouse_id": warehouse.id,
                "name": warehouse.display_name,
                "actual": actual,
                "planned": planned,
                "gap": gap,
                "coverage_pct": round(actual * 100.0 / planned, 1) if planned else False,
                "status": "not_configured" if not planned else "understaffed" if gap > 0 else "covered",
                "manager": ", ".join(managers.get(warehouse.id, [])) or "Unassigned",
            })
        result["staffing"] = staffing

        exceptions = self._people_attendance_exceptions(employees, start, min(end, today), today) if attendance_installed and employees else []
        result["attendance_exceptions"] = exceptions
        weekday_counts = defaultdict(lambda: {"late": 0, "absent": 0})
        for item in exceptions:
            for day in item["late_dates"]:
                weekday_counts[fields.Date.to_date(day).weekday()]["late"] += 1
            for day in item["absent_dates"]:
                weekday_counts[fields.Date.to_date(day).weekday()]["absent"] += 1
        result["attendance_by_weekday"] = [
            {"day": label, **weekday_counts[index]} for index, label in enumerate(_WEEKDAYS)
        ] if attendance_installed else []

        actions, workload = self._people_tasks(staff_users, start, end, today)
        result["actions"] = actions
        result["workload"] = workload

        attention = []
        for row in staffing:
            if row["status"] == "understaffed":
                attention.append({
                    "kind": "understaffed",
                    "title": "Understaffed branch",
                    "branch": row["name"],
                    "evidence": "%s of %s planned staff (%s short)" % (row["actual"], row["planned"], row["gap"]),
                    "severity": "critical" if row["gap"] * 4 >= row["planned"] else "warning",
                    "responsible": row["manager"],
                    "res_model": "stock.warehouse", "res_id": row["warehouse_id"],
                })
        for item in exceptions:
            responsible = ", ".join(managers.get(item["warehouse_id"], [])) or "Unassigned"
            if len(item["late_dates"]) >= 2:
                attention.append({
                    "kind": "late",
                    "title": "Repeated late arrivals: %s" % item["employee"],
                    "branch": item["branch"],
                    "evidence": "%s late arrivals, average %s min after scheduled start" % (len(item["late_dates"]), item["average_late_minutes"]),
                    "severity": "critical" if len(item["late_dates"]) >= 3 else "warning",
                    "responsible": responsible,
                    "res_model": "hr.employee", "res_id": item["employee_id"],
                })
            if item["absent_dates"]:
                attention.append({
                    "kind": "absence",
                    "title": "Absence without attendance: %s" % item["employee"],
                    "branch": item["branch"],
                    "evidence": "No check-in on %s scheduled day(s): %s" % (len(item["absent_dates"]), ", ".join(item["absent_dates"][:4])),
                    "severity": "critical" if len(item["absent_dates"]) >= 2 else "warning",
                    "responsible": responsible,
                    "res_model": "hr.employee", "res_id": item["employee_id"],
                })
        for row in workload:
            if row["overdue"]:
                attention.append({
                    "kind": "overdue",
                    "title": "Overdue follow-ups: %s" % row["name"],
                    "branch": row["branch"],
                    "evidence": "%s overdue of %s open, oldest due %s" % (row["overdue"], row["open"], row["oldest_overdue"]),
                    "severity": "critical" if (today - fields.Date.to_date(row["oldest_overdue"])).days > 7 else "warning",
                    "responsible": row["name"],
                })
        attention.sort(key=lambda item: (item["severity"] != "critical", item["branch"] or ""))
        result["attention"] = attention

        employee_count = len(employees) if employees is not False else len(staff_users)
        result["summary"] = {
            "active_employees": employee_count,
            "attendance_exceptions": sum(len(item["late_dates"]) + len(item["absent_dates"]) for item in exceptions) if attendance_installed else False,
            "branches_needing_coverage": sum(1 for row in staffing if row["status"] == "understaffed") if any(row["planned"] for row in staffing) else False,
            "overdue_tasks": sum(row["overdue"] for row in workload),
        }
        result["insights"] = self._people_insights(staffing, exceptions, workload, result["attendance_by_weekday"])

        if not hr_installed:
            result["messages"].append("Employees app not installed: staff counts use internal users assigned to each branch.")
        if not attendance_installed:
            result["messages"].append("Attendance: Not Configured. Install the Odoo Attendances app to track late arrivals and absences.")
        if not result["sources"]["shift_planning"]:
            result["messages"].append("Shift-level coverage: Not Available. Odoo Community has no shift planning app; coverage uses each branch's Planned Staff setting.")
        if role != "employee" and not any(row["planned"] for row in staffing):
            result["messages"].append("Planned staffing: Not Configured. Set Planned Staff on each branch warehouse to measure coverage gaps.")
        if role == "employee":
            result["messages"].append("You are viewing your own attendance and tasks.")
        return result

    @api.model
    def _people_employees(self, staff_users, organization_wide):
        # sudo: employees are restricted to the authorized branches (or self for employees).
        Employee = self.env["hr.employee"].sudo()
        domain = [("company_id", "=", self.env.company.id)]
        if not organization_wide:
            domain.append(("user_id", "in", staff_users.ids))
        return Employee.search(domain)

    @api.model
    def _people_attendance_exceptions(self, employees, start, end, today):
        """Late arrivals and absences measured against each employee's working schedule."""
        if end < start:
            return []
        Attendance = self.env["hr.attendance"].sudo()
        start_dt = datetime.combine(start - timedelta(days=1), time.min, tzinfo=_UTC)
        end_dt = datetime.combine(end + timedelta(days=2), time.min, tzinfo=_UTC)
        expected = {}
        for calendar, group in employees.filtered("resource_calendar_id").grouped("resource_calendar_id").items():
            intervals = calendar._work_intervals_batch(start_dt, end_dt, group.resource_id._get_resources_per_tz())
            for employee in group:
                starts = {}
                for interval_start, _stop, _meta in intervals.get(employee.resource_id.id, []):
                    day = interval_start.date()
                    if start <= day <= end and (day not in starts or interval_start < starts[day]):
                        starts[day] = interval_start
                expected[employee.id] = starts
        first_check_in = {}
        attendances = Attendance.search([
            ("employee_id", "in", employees.ids),
            ("check_in", ">=", start_dt.replace(tzinfo=None)),
            ("check_in", "<", end_dt.replace(tzinfo=None)),
        ])
        for attendance in attendances:
            zone = ZoneInfo(attendance.employee_id.tz or "UTC")
            local = attendance.check_in.replace(tzinfo=_UTC).astimezone(zone)
            key = (attendance.employee_id.id, local.date())
            if key not in first_check_in or local < first_check_in[key]:
                first_check_in[key] = local
        rows = []
        for employee in employees:
            starts = expected.get(employee.id, {})
            created = fields.Datetime.context_timestamp(employee, employee.create_date).date() if employee.create_date else start
            late, late_minutes, absent = [], [], []
            for day, scheduled in sorted(starts.items()):
                check_in = first_check_in.get((employee.id, day))
                if check_in:
                    delay = (check_in - scheduled).total_seconds() / 60.0
                    if delay > LATE_GRACE_MINUTES:
                        late.append(fields.Date.to_string(day))
                        late_minutes.append(delay)
                elif created <= day < today:
                    absent.append(fields.Date.to_string(day))
            if late or absent:
                warehouse = employee.user_id.strategic_branch_warehouse_id
                rows.append({
                    "employee_id": employee.id,
                    "employee": employee.name,
                    "warehouse_id": warehouse.id,
                    "branch": warehouse.display_name or "Unassigned",
                    "late_dates": late,
                    "average_late_minutes": round(sum(late_minutes) / len(late_minutes)) if late_minutes else 0,
                    "absent_dates": absent,
                })
        return sorted(rows, key=lambda row: -(len(row["late_dates"]) + len(row["absent_dates"])))

    @api.model
    def _people_tasks(self, staff_users, start, end, today):
        """Open and recently completed activities of the staff in scope, plus workload per person."""
        # sudo: limited to activities assigned to the in-scope staff.
        Activity = self.env["mail.activity"].sudo()
        activities = Activity.search([("user_id", "in", staff_users.ids)], order="date_deadline asc, id asc")
        workload = defaultdict(lambda: {"open": 0, "overdue": 0, "oldest_overdue": False})
        actions = []
        for activity in activities:
            user = activity.user_id
            bucket = workload[user.id]
            bucket.update({"name": user.name, "branch": user.strategic_branch_warehouse_id.display_name or "Unassigned"})
            bucket["open"] += 1
            if activity.date_deadline < today:
                bucket["overdue"] += 1
                if not bucket["oldest_overdue"] or fields.Date.to_string(activity.date_deadline) < bucket["oldest_overdue"]:
                    bucket["oldest_overdue"] = fields.Date.to_string(activity.date_deadline)
            if len(actions) < 15:
                actions.append({
                    "task": activity.summary or activity.activity_type_id.name or "Follow-up",
                    "record": activity.res_name or "",
                    "assigned": user.name,
                    "branch": user.strategic_branch_warehouse_id.display_name or "Unassigned",
                    "due_date": fields.Date.to_string(activity.date_deadline),
                    "status": "Overdue" if activity.date_deadline < today else "Due today" if activity.date_deadline == today else "Planned",
                })
        # Completed activities become chatter messages carrying their activity type.
        start_utc, end_utc = self._overview_utc_bounds(start, end)
        done = self.env["mail.message"].sudo().search([
            ("mail_activity_type_id", "!=", False),
            ("author_id", "in", staff_users.partner_id.ids),
            ("date", ">=", start_utc),
            ("date", "<", end_utc),
        ], order="date desc", limit=10)
        partner_users = {user.partner_id.id: user for user in staff_users}
        for message in done:
            user = partner_users.get(message.author_id.id)
            actions.append({
                "task": message.subject or message.mail_activity_type_id.name or "Follow-up",
                "record": message.record_name or "",
                "assigned": message.author_id.name,
                "branch": user.strategic_branch_warehouse_id.display_name if user and user.strategic_branch_warehouse_id else "Unassigned",
                "due_date": fields.Date.to_string(fields.Datetime.context_timestamp(self, message.date).date()),
                "status": "Done",
            })
        workload_rows = sorted(
            ({"user_id": user_id, **values} for user_id, values in workload.items()),
            key=lambda row: (-row["overdue"], -row["open"], row["name"]),
        )
        return actions, workload_rows

    @api.model
    def _people_insights(self, staffing, exceptions, workload, by_weekday):
        """Plain-language findings computed only from the data above."""
        insights = []
        for row in staffing:
            if row["status"] == "understaffed":
                insights.append("%s is %s staff short of its planned %s." % (row["name"], row["gap"], row["planned"]))
        late_total = sum(day["late"] for day in by_weekday)
        absent_total = sum(day["absent"] for day in by_weekday)
        if late_total + absent_total:
            worst = max(by_weekday, key=lambda day: day["late"] + day["absent"])
            insights.append("%s has the most attendance exceptions (%s of %s)." % (worst["day"], worst["late"] + worst["absent"], late_total + absent_total))
        open_total = sum(row["open"] for row in workload)
        if open_total >= 4 and len(workload) > 1 and workload:
            busiest = max(workload, key=lambda row: row["open"])
            share = busiest["open"] * 100.0 / open_total
            if share >= 40:
                insights.append("%s holds %s%% of open follow-ups (%s of %s); consider rebalancing." % (busiest["name"], round(share), busiest["open"], open_total))
        branch_issues = Counter()
        for item in exceptions:
            branch_issues[item["branch"]] += len(item["late_dates"]) + len(item["absent_dates"])
        for row in workload:
            branch_issues[row["branch"]] += row["overdue"]
        for row in staffing:
            if row["status"] == "understaffed":
                branch_issues[row["name"]] += row["gap"]
        for branch, count in branch_issues.most_common(3):
            if count >= 3:
                insights.append("%s needs intervention: %s combined staffing, attendance and overdue-task issues." % (branch, count))
        return insights
