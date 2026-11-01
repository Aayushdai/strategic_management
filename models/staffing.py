"""Staffing requests, employee registration and task-based performance.

A Branch Manager asks for staff for their own branch; Head Office decides and registers real hr.employee records against
an approved request. A login is a separate step. Until an employee has a login their branch is kept on the employee; once
they have one, the user's branch is the only source (tasks, Employee Management and access rules already read it).

Performance comes only from strategic.branch.task records. Branch-wide sales or stock results are never attributed to a
person. A task belongs to a period by its due date, once; cancelled tasks are left out. On time is the result kept at
submission against the deadline then in force, so editing a task later cannot change it.
"""

import logging

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import email_normalize
from odoo.tools.sql import escape_like_value

_logger = logging.getLogger(__name__)

_HEAD = "strategic_management.group_strategic_head_office_manager"
_MANAGER = "strategic_management.group_strategic_branch_manager"
_EMPLOYEE = "strategic_management.group_strategic_employee"
_CRITERIA = (
    ("work_quality", "Work quality", "Follows recipes and procedures; work rarely has to be redone."),
    ("teamwork", "Teamwork", "Helps colleagues, shares the load and works well across shifts."),
    ("communication", "Communication", "Keeps the manager and colleagues informed clearly and on time."),
    ("responsibility", "Responsibility", "Is punctual, keeps commitments and owns mistakes."),
    ("problem_solving", "Problem solving", "Spots problems early, fixes what they can and raises the rest."),
    ("customer_service", "Customer service", "Is courteous and attentive, and handles complaints well."),
)
_RATINGS = tuple(criterion[0] for criterion in _CRITERIA)
_SCALE = (
    (1, "Unacceptable", "Regularly falls short of the basic standard; immediate improvement is required."),
    (2, "Needs improvement", "Reaches the standard only with frequent reminders or correction."),
    (3, "Meets expectations", "Reliably reaches the standard without close supervision."),
    (4, "Exceeds expectations", "Often goes beyond the standard and helps others reach it."),
    (5, "Outstanding", "Consistently sets the example for the branch; others learn from this work."),
)
_RETURNED_MARK = "Changes requested:"
_BLOCKER_PREFIX = "Blocker: "


def _is_head_office(env):
    return env.user.has_group(_HEAD)


def _manager_branch(env):
    """The signed-in Branch Manager's valid branch, or an AccessError; never taken from a request."""
    return env["strategic.performance.dashboard"]._branch_manager_branch()


class HrEmployee(models.Model):
    _inherit = "hr.employee"

    strategic_staffing_request_id = fields.Many2one("strategic.staffing.request", "Staffing Request", readonly=True, copy=False, index=True, groups="hr.group_hr_user")
    # Only for employees without a login: with one, the user's branch is the source.
    strategic_branch_warehouse_id = fields.Many2one("stock.warehouse", "Branch", copy=False, index=True, groups="hr.group_hr_user")
    strategic_invitation_state = fields.Selection([("sent", "Invitation sent"), ("failed", "Invitation not sent")], "Invitation", readonly=True, copy=False, groups="hr.group_hr_user")
    strategic_invitation_error = fields.Char("Invitation problem", readonly=True, copy=False, groups="hr.group_hr_user")

    # Backs up the check in register_employee for parallel registrations; other employees' existing data is not touched.
    _strategic_staffing_email_unique = models.UniqueIndex("(lower(work_email)) WHERE strategic_staffing_request_id IS NOT NULL", "An employee with this work email already exists.")

    def _strategic_branch(self):
        self.ensure_one()
        employee = self.sudo()
        return employee.user_id.strategic_branch_warehouse_id if employee.user_id else employee.strategic_branch_warehouse_id


class StrategicStaffingRequest(models.Model):
    _name = "strategic.staffing.request"
    _description = "Staffing Request"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    name = fields.Char(compute="_compute_name", store=True)
    warehouse_id = fields.Many2one("stock.warehouse", "Branch", required=True, index=True, readonly=True)
    company_id = fields.Many2one("res.company", required=True, index=True, readonly=True, default=lambda self: self.env.company)
    requested_by_id = fields.Many2one("res.users", "Requested By", required=True, readonly=True)
    department_id = fields.Many2one("hr.department", "Department", required=True, tracking=True)
    position = fields.Char(required=True, tracking=True)
    quantity = fields.Integer("Employees Required", required=True, default=1, tracking=True)
    reason = fields.Text(required=True)
    expected_date = fields.Date("Expected Joining Date", required=True)
    comments = fields.Text()
    state = fields.Selection([("draft", "Draft"), ("submitted", "Submitted"), ("approved", "Approved"), ("rejected", "Rejected")],
                             default="draft", required=True, index=True, readonly=True, tracking=True)
    submitted_at = fields.Datetime(readonly=True)
    decided_by_id = fields.Many2one("res.users", readonly=True)
    decided_at = fields.Datetime(readonly=True)
    rejection_reason = fields.Text(readonly=True)
    employee_ids = fields.One2many("hr.employee", "strategic_staffing_request_id", "Registered Employees")
    # Archived employees stay counted: replacing someone who left needs a new approval.

    _quantity_positive = models.Constraint("CHECK(quantity > 0)", "At least one employee must be requested.")

    @api.depends("position", "warehouse_id")
    def _compute_name(self):
        for request in self:
            request.name = "%s, %s" % (request.position, request.warehouse_id.display_name)

    # ------------------------------------------------------------------ data for the page
    def _row(self):
        self.ensure_one()
        request = self.sudo()
        employees = request.with_context(active_test=False).employee_ids
        return {
            "id": request.id, "department_id": request.department_id.id, "department": request.department_id.display_name, "position": request.position, "quantity": request.quantity, "reason": request.reason, "comments": request.comments or "",
            "expected_date": fields.Date.to_string(request.expected_date), "state": request.state,
            "status": dict(self._fields["state"].selection)[request.state], "branch": request.warehouse_id.display_name, "branch_id": request.warehouse_id.id,
            "requested_by": request.requested_by_id.name, "submitted_at": fields.Datetime.to_string(request.submitted_at) if request.submitted_at else False,
            "decided_by": request.decided_by_id.name or False, "decided_at": fields.Datetime.to_string(request.decided_at) if request.decided_at else False,
            "rejection_reason": request.rejection_reason or "", "registered": len(employees), "remaining": max(0, request.quantity - len(employees)),
            "fulfilment": "Filled" if len(employees) >= request.quantity else "Partly filled" if employees else "Not started",
            "employees": [{"id": e.id, "name": e.name, "email": e.work_email or "", "job": e.job_id.name or "", "has_login": bool(e.user_id),
                           "invitation": e.strategic_invitation_state or False, "invitation_error": e.strategic_invitation_error or ""} for e in employees],
        }

    @api.model
    def get_staffing_requests(self, filters=None):
        """Head Office: every branch of the company, with filters. Branch Manager: their own branch only, whatever is asked."""
        filters = filters or {}
        domain = [("company_id", "=", self.env.company.id)]
        head = _is_head_office(self.env)
        if head:
            if filters.get("warehouse_id"):
                domain.append(("warehouse_id", "=", int(filters["warehouse_id"])))
        else:
            domain.append(("warehouse_id", "=", _manager_branch(self.env).id))
        if filters.get("state"):
            domain.append(("state", "=", filters["state"]))
        if filters.get("position"):
            domain.append(("position", "ilike", filters["position"]))
        result = {"role": "head_office" if head else "branch_manager", "requests": [request._row() for request in self.search(domain)],
                  "positions": self.env["hr.job"].sudo().search([("company_id", "in", [False, self.env.company.id])]).mapped("name")}
        result["departments"] = [{"id": d.id, "name": d.display_name} for d in self.env["hr.department"].sudo().search([("company_id", "in", [False, self.env.company.id])])]
        if head:
            result["branches"] = [{"id": w.id, "name": w.display_name} for w in self.env["stock.warehouse"].search([("company_id", "=", self.env.company.id)], order="name")]
            result["managers"] = [{"id": e.id, "name": e.name} for e in self.env["hr.employee"].sudo().search([("company_id", "=", self.env.company.id)], order="name")]
        return result

    # ------------------------------------------------------------------ Branch Manager
    @api.model
    def save_staffing_request(self, values, submit=False):
        """Create or update the manager's own draft. The branch and the requester always come from the manager's account."""
        if _is_head_office(self.env):
            raise AccessError("Staffing requests are raised by Branch Managers.")
        branch = _manager_branch(self.env)
        position = (values.get("position") or "").strip()
        reason = (values.get("reason") or "").strip()
        try:
            quantity = int(values.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        if not position or not reason or quantity < 1 or not values.get("expected_date"):
            raise ValidationError("Enter the position, how many employees, the reason and the expected joining date.")
        vals = {"department_id": self._valid_department(values.get("department_id"), self.env.company, required=True).id, "position": position, "quantity": quantity, "reason": reason, "comments": (values.get("comments") or "").strip() or False,
                "expected_date": fields.Date.to_date(values["expected_date"])}
        if values.get("id"):
            request = self.browse(int(values["id"])).exists()
            if not request or request.warehouse_id != branch or request.state != "draft":
                raise AccessError("Only your own branch's draft requests can be changed.")
            request.sudo().write(vals)
        else:
            request = self.sudo().create(dict(vals, warehouse_id=branch.id, company_id=self.env.company.id, requested_by_id=self.env.user.id))
        if submit:
            request.with_user(self.env.user).action_submit()
        return request._row()

    @api.model
    def _valid_department(self, department_id, company, required=False):
        try:
            department = self.env["hr.department"].sudo().browse(int(department_id or 0)).exists()
        except (TypeError, ValueError):
            department = self.env["hr.department"]
        if (required and not department) or (department and department.company_id not in (self.env["res.company"], company)):
            raise ValidationError("Choose a department of this company.")
        return department

    def action_submit(self):
        branch = _manager_branch(self.env)
        for request in self:
            if request.warehouse_id != branch or request.state != "draft":
                raise AccessError("Only your own branch's draft requests can be submitted.")
            request.sudo().write({"state": "submitted", "submitted_at": fields.Datetime.now()})
            request.sudo().message_post(body="Submitted to Head Office.", subtype_xmlid="mail.mt_note")
            self.env["strategic.kpi.target"].sudo()._create_deduplicated_activity(
                request.sudo(), "Staffing request: %s" % request.sudo().name, request.sudo().reason, scope="organization", company=request.sudo().company_id)
        return True

    # ------------------------------------------------------------------ Head Office
    def _require_decider(self):
        if not _is_head_office(self.env):
            raise AccessError("Staffing requests are decided by Head Office.")
        for request in self:
            if request.requested_by_id == self.env.user:
                raise AccessError("You cannot decide your own staffing request.")

    def action_decide(self, decision, reason=False):
        self._require_decider()
        reason = (reason or "").strip()
        if decision not in ("approve", "reject"):
            raise UserError("Choose Approve or Reject.")
        if decision == "reject" and not reason:
            raise ValidationError("Explain why the request is rejected.")
        for request in self.sudo():
            if request.company_id not in self.env.companies:
                raise AccessError("This request belongs to another company.")
            if request.state != "submitted":
                raise UserError("Only submitted requests can be decided.")
            request.write({"state": "approved" if decision == "approve" else "rejected", "decided_by_id": self.env.user.id,
                           "decided_at": fields.Datetime.now(), "rejection_reason": reason or False})
            label = "Approved." if decision == "approve" else "Rejected: %s" % reason
            request.message_post(body=label, subtype_xmlid="mail.mt_note")
            request.activity_ids.filtered(lambda activity: (activity.summary or "").startswith("Staffing request")).action_feedback(feedback=label)
            self.env["strategic.kpi.target"].sudo()._create_deduplicated_activity(
                request, "Staffing request %s: %s" % (request.state, request.name), label, recipients=request.requested_by_id, company=request.company_id)
        return True

    def _check_new_email(self, email):
        normalized = email_normalize(email or "")
        if not normalized:
            raise ValidationError("Enter a valid work email address.")
        if self.env["hr.employee"].sudo().with_context(active_test=False).search_count([("work_email", "=ilike", escape_like_value(normalized))], limit=1):
            raise ValidationError("An employee with this work email already exists.")
        if self.env["res.users"].sudo().with_context(active_test=False).search_count([("login", "=ilike", escape_like_value(normalized))], limit=1):
            raise ValidationError("A user with this email already exists.")
        return normalized

    def register_employee(self, values):
        """Create the real employee for an approved request. No login is created here."""
        if not _is_head_office(self.env):
            raise AccessError("Employees are registered by Head Office.")
        self.ensure_one()
        request = self.sudo()
        if request.company_id not in self.env.companies:
            raise AccessError("This request belongs to another company.")
        if request.state != "approved":
            raise UserError("Employees can be registered only against an approved request.")
        if values.get("warehouse_id") and int(values["warehouse_id"]) != request.warehouse_id.id:
            raise ValidationError("This request is for %s; choose that branch." % request.warehouse_id.display_name)
        # A real update, not only a lock: Odoo's snapshot isolation makes a parallel registration fail here and retry with
        # fresh data, where it sees this employee. A bare FOR UPDATE would let it pass on its stale count.
        self.env.cr.execute("UPDATE strategic_staffing_request SET write_date = write_date WHERE id = %s", (request.id,))
        if len(request.with_context(active_test=False).employee_ids) >= request.quantity:
            raise UserError("All the employees of this request have been registered.")
        name = (values.get("name") or "").strip()
        if not name:
            raise ValidationError("Enter the employee's name.")
        email = self._check_new_email(values.get("work_email"))
        Job = self.env["hr.job"].sudo()
        job_name = (values.get("job") or request.position).strip()
        job = Job.search([("name", "=", job_name), ("company_id", "in", [False, request.company_id.id])], limit=1) \
            or Job.create({"name": job_name, "company_id": request.company_id.id})
        department = self._valid_department(values.get("department_id"), request.company_id) or request.department_id
        manager = self._branch_manager_employee(request.warehouse_id)
        if values.get("parent_id"):
            manager = self.env["hr.employee"].sudo().browse(int(values["parent_id"])).exists()
            if not manager or manager.company_id != request.company_id:
                raise ValidationError("Choose a reporting manager of this company.")
        employee = self.env["hr.employee"].sudo().create({
            "name": name, "work_email": email, "work_phone": (values.get("work_phone") or "").strip() or False, "job_id": job.id, "job_title": job.name,
            "department_id": department.id or False, "parent_id": manager.id or False, "company_id": request.company_id.id,
            "strategic_branch_warehouse_id": request.warehouse_id.id, "strategic_staffing_request_id": request.id})
        request.message_post(body="Employee registered: %s" % name, subtype_xmlid="mail.mt_note")
        return request._row() | {"new_employee_id": employee.id}

    @api.model
    def _branch_manager_employee(self, branch):
        managers = self.env["res.users"].sudo().search([("strategic_branch_warehouse_id", "=", branch.id), ("all_group_ids", "in", self.env.ref(_MANAGER).id)])
        managers = managers.filtered(lambda user: not user.has_group(_HEAD))
        return self.env["hr.employee"].sudo().search([("user_id", "in", managers.ids), ("company_id", "=", branch.company_id.id)], limit=1)

    @api.model
    def create_employee_login(self, employee_id):
        """A login with the Employee role only, linked to the employee and invited to set their own password."""
        if not _is_head_office(self.env):
            raise AccessError("Logins are created by Head Office.")
        employee = self.env["hr.employee"].sudo().browse(int(employee_id)).exists()
        if not employee or employee.company_id != self.env.company:
            raise AccessError("This employee is not in the active company.")
        if employee.user_id:
            raise UserError("%s already has a login." % employee.name)
        email = email_normalize(employee.work_email or "")
        if not email:
            raise ValidationError("%s needs a valid work email first." % employee.name)
        if self.env["res.users"].sudo().with_context(active_test=False).search_count([("login", "=ilike", escape_like_value(email))], limit=1):
            raise ValidationError("A user with this email already exists.")
        branch = employee.strategic_branch_warehouse_id
        if not branch or branch.company_id != employee.company_id:
            raise ValidationError("%s needs a branch first." % employee.name)
        user = self.env["res.users"].sudo().with_context(no_reset_password=True).create({
            "name": employee.name, "login": email, "email": email, "company_id": employee.company_id.id, "company_ids": [(6, 0, employee.company_id.ids)],
            "group_ids": [(6, 0, [self.env.ref("base.group_user").id, self.env.ref(_EMPLOYEE).id])], "strategic_branch_warehouse_id": branch.id})
        employee.user_id = user
        return self._send_invitation(employee)

    @api.model
    def resend_employee_invitation(self, employee_id):
        if not _is_head_office(self.env):
            raise AccessError("Invitations are sent by Head Office.")
        employee = self.env["hr.employee"].sudo().browse(int(employee_id)).exists()
        if not employee or employee.company_id != self.env.company or not employee.user_id:
            raise UserError("This employee has no login to invite.")
        return self._send_invitation(employee)

    @api.model
    def _send_invitation(self, employee):
        """A failed email leaves a correct employee and account, says so, and can be sent again. It is never reported as sent."""
        try:
            with self.env.cr.savepoint():
                employee.user_id.sudo().with_context(create_user=True).action_reset_password()
            employee.write({"strategic_invitation_state": "sent", "strategic_invitation_error": False})
        except Exception as error:  # The account stands whatever the mail system raises; the reason is shown to Head Office.
            message = str(error.args[0]) if error.args else str(error)
            _logger.warning("Invitation not sent to %s: %s", employee.work_email, message)
            employee.write({"strategic_invitation_state": "failed", "strategic_invitation_error": message[:240]})
        return {"employee_id": employee.id, "login": employee.user_id.login, "invitation": employee.strategic_invitation_state, "error": employee.strategic_invitation_error or False}


class StrategicEmployeeAssessment(models.Model):
    """A periodic manager assessment, kept apart from task figures and hidden from employees. hr_appraisal could replace it."""
    _name = "strategic.employee.assessment"
    _description = "Employee Assessment"
    _order = "period_end desc, id desc"

    user_id = fields.Many2one("res.users", "Employee", required=True, index=True, readonly=True)
    warehouse_id = fields.Many2one("stock.warehouse", "Branch", required=True, index=True, readonly=True)
    company_id = fields.Many2one("res.company", required=True, index=True, readonly=True)
    assessor_id = fields.Many2one("res.users", "Reviewer", required=True, readonly=True)
    period_start = fields.Date(required=True, readonly=True)
    period_end = fields.Date(required=True, readonly=True)
    work_quality = fields.Integer(required=True, readonly=True)
    teamwork = fields.Integer(required=True, readonly=True)
    communication = fields.Integer(required=True, readonly=True)
    responsibility = fields.Integer(required=True, readonly=True)
    problem_solving = fields.Integer(required=True, readonly=True)
    customer_service = fields.Integer(required=True, readonly=True)
    notes = fields.Text("Supporting Comments", required=True, readonly=True)

    _ratings_in_scale = models.Constraint(
        "CHECK(work_quality BETWEEN 1 AND 5 AND teamwork BETWEEN 1 AND 5 AND communication BETWEEN 1 AND 5 "
        "AND responsibility BETWEEN 1 AND 5 AND problem_solving BETWEEN 1 AND 5 AND customer_service BETWEEN 1 AND 5)", "Rate every criterion from 1 to 5.")
    _period_valid = models.Constraint("CHECK(period_start <= period_end)", "The period must start before it ends.")
    _one_per_period = models.Constraint("UNIQUE(user_id, period_start, period_end)", "This period has already been assessed.")


class StrategicPerformanceDashboardStaffing(models.AbstractModel):
    _inherit = "strategic.performance.dashboard"

    # ------------------------------------------------------------------ who may see whom
    @api.model
    def _performance_person(self, user_id):
        """The person and the branch filter (empty for Head Office) the signed-in user may read. Never trusts the request."""
        person = self.env["res.users"].sudo().browse(user_id if isinstance(user_id, int) and not isinstance(user_id, bool) else 0).exists()
        if not person or self.env.company not in person.company_ids:
            raise AccessError("This person is not available.")
        if _is_head_office(self.env):
            return person, self.env["stock.warehouse"]
        branch = _manager_branch(self.env)
        if person.strategic_branch_warehouse_id != branch or person == self.env.user:
            raise AccessError("This person is not one of your branch's employees.")
        return person, branch

    # ------------------------------------------------------------------ the figures
    @api.model
    def _task_performance(self, tasks, start, end):
        """Figures for these tasks due in the period. Each task counts once; on time is the result kept at submission."""
        tasks = tasks.sudo().filtered(lambda task: start <= task.date_deadline <= end)
        live = tasks.filtered(lambda task: task.state != "cancelled")
        done = live.filtered(lambda task: task.state == "done")
        on_time = done.filtered("submitted_on_time")
        high = live.filtered(lambda task: task.priority == "1")
        high_done = high & done
        returned = self.env["mail.message"].sudo().search_count([
            ("model", "=", "strategic.branch.task"), ("res_id", "in", live.ids), ("body", "=ilike", "<p>" + _RETURNED_MARK + "%")]) if live else 0
        objectives = {}
        for task in live.filtered("objective_id"):
            row = objectives.setdefault(task.objective_id.id, {"id": task.objective_id.id, "name": task.objective_id.display_name, "tasks": 0, "completed": 0})
            row["tasks"] += 1
            row["completed"] += task.state == "done"
        return {
            "assigned": len(live), "completed": len(done), "on_time": len(on_time), "late": len(done) - len(on_time),
            "overdue": len(live.filtered("is_overdue")), "awaiting_review": len(live.filtered(lambda task: task.state == "submitted")),
            "returned": returned, "extended": len(live.filtered("extension_ids")), "cancelled": len(tasks) - len(live), "high_priority": len(high), "high_priority_completed": len(high_done),
            # No completed task means no rate, not zero.
            "on_time_rate": round(len(on_time) / len(done) * 100, 1) if done else False,
            "objectives": sorted(objectives.values(), key=lambda row: row["name"]),
        }

    @api.model
    def _person_tasks(self, person, branch):
        domain = [("assigned_user_id", "=", person.id), ("company_id", "=", self.env.company.id)]
        if branch:
            domain.append(("warehouse_id", "=", branch.id))
        return self.env["strategic.branch.task"].sudo().search(domain)

    @api.model
    def _reported_blockers(self, person, tasks, start, end):
        """Blockers the person reported in the period: on their tasks, and on objectives for those who report at that level."""
        since, until = fields.Datetime.to_datetime(start), fields.Datetime.add(fields.Datetime.to_datetime(end), days=1)
        count = self.env["strategic.task.blocker"].sudo().search_count([("task_id", "in", tasks.ids), ("reported_by_id", "=", person.id), ("create_date", ">=", since), ("create_date", "<", until)])
        objectives = tasks.sudo().objective_id
        if not objectives:
            return count
        return count + self.env["mail.message"].sudo().search_count([
            ("model", "=", "strategic.objective"), ("res_id", "in", objectives.ids), ("author_id", "=", person.partner_id.id), ("subject", "=like", _BLOCKER_PREFIX + "%"),
            ("date", ">=", since), ("date", "<", until)])

    @api.model
    def _period(self, date_start, date_end):
        return self._resolve_period(date_start, date_end)

    @api.model
    def get_employee_performance(self, user_id, date_start=False, date_end=False):
        """One person's task figures and assessments, for their Branch Manager or Head Office."""
        person, branch = self._performance_person(user_id)
        start, end = self._period(date_start, date_end)
        tasks = self._person_tasks(person, branch)
        figures = self._task_performance(tasks, start, end)
        figures["blockers"] = self._reported_blockers(person, tasks, start, end)
        assessments = self.env["strategic.employee.assessment"].search([("user_id", "=", person.id), ("company_id", "=", self.env.company.id)])
        return {"employee": {"id": person.id, "name": person.name, "branch": person.strategic_branch_warehouse_id.display_name or False},
                "period": {"start": fields.Date.to_string(start), "end": fields.Date.to_string(end)}, "figures": figures,
                "criteria": _CRITERIA, "scale": _SCALE,
                "assessments": [dict({key: getattr(row, key) for key in _RATINGS}, id=row.id, assessor=row.sudo().assessor_id.name, notes=row.notes,
                                     start=fields.Date.to_string(row.period_start), end=fields.Date.to_string(row.period_end)) for row in assessments[:10]],
                "can_assess": not _is_head_office(self.env)}

    @api.model
    def get_my_performance(self, date_start=False, date_end=False):
        """The signed-in person's own task figures. Manager assessments are not included."""
        start, end = self._period(date_start, date_end)
        tasks = self.env["strategic.branch.task"].search([("assigned_user_id", "=", self.env.user.id), ("company_id", "=", self.env.company.id)])
        figures = self._task_performance(tasks, start, end)
        figures["blockers"] = self._reported_blockers(self.env.user, tasks, start, end)
        in_period = tasks.sudo().filtered(lambda task: start <= task.date_deadline <= end and task.state != "cancelled")
        months = {}
        for task in in_period.sorted("date_deadline"):
            row = months.setdefault(task.date_deadline.strftime("%Y-%m"), {"month": task.date_deadline.strftime("%B %Y"), "assigned": 0, "completed": 0, "on_time": 0})
            row["assigned"] += 1
            row["completed"] += task.state == "done"
            row["on_time"] += task.state == "done" and task.submitted_on_time
        feedback = in_period.filtered("review_note").sorted(lambda task: task.reviewed_at or task.write_date, reverse=True)[:10]
        role_employee = self._strategic_role() == "employee"
        return {
            "figures": figures, "period": {"start": fields.Date.to_string(start), "end": fields.Date.to_string(end)},
            "history": list(months.values()),
            "feedback": [{"id": task.id, "task": task.name, "note": task.review_note, "by": task.reviewed_by_id.name or "", "state": task.state,
                          "at": fields.Datetime.to_string(task.reviewed_at) if task.reviewed_at else False} for task in feedback],
            # Only KPIs with one individual owner; organization and branch results never appear here.
            "individual_kpis": self.get_employee_data(date_start, date_end)["kpis"] if role_employee else [],
        }

    @api.model
    def save_employee_assessment(self, user_id, values, date_start=False, date_end=False):
        """A Branch Manager's periodic assessment of their own employee."""
        if _is_head_office(self.env):
            raise AccessError("Assessments are written by the employee's Branch Manager.")
        person, branch = self._performance_person(user_id)
        start, end = self._period(date_start, date_end)
        ratings = {}
        for key in _RATINGS:
            try:
                ratings[key] = int(values.get(key) or 0)
            except (TypeError, ValueError):
                ratings[key] = 0
            if not 1 <= ratings[key] <= 5:
                raise ValidationError("Rate every criterion from 1 to 5.")
        notes = (values.get("notes") or "").strip()
        if not notes:
            raise ValidationError("Add comments that support these ratings.")
        if start > fields.Date.context_today(self):
            raise ValidationError("A period that has not started cannot be assessed.")
        Assessment = self.env["strategic.employee.assessment"].sudo()
        if Assessment.search_count([("user_id", "=", person.id), ("period_start", "=", start), ("period_end", "=", end)], limit=1):
            raise UserError("This period has already been assessed for %s." % person.name)
        Assessment.create(dict(ratings, user_id=person.id, warehouse_id=branch.id, company_id=self.env.company.id, assessor_id=self.env.user.id,
                               period_start=start, period_end=end, notes=notes))
        return self.get_employee_performance(user_id, date_start, date_end)

    @api.model
    def get_performance_overview(self, date_start=False, date_end=False):
        """Head Office: per-branch totals and per-person task figures. Comparison uses rates, not task counts alone."""
        if not _is_head_office(self.env):
            raise AccessError("The organization overview is for Head Office.")
        start, end = self._period(date_start, date_end)
        tasks = self.env["strategic.branch.task"].sudo().search([("company_id", "=", self.env.company.id), ("date_deadline", ">=", start), ("date_deadline", "<=", end)])
        branches = [dict(self._task_performance(group, start, end), id=branch.id, branch=branch.display_name, people=len(group.assigned_user_id))
                    for branch, group in tasks.grouped("warehouse_id").items()]
        people = [dict(self._task_performance(group, start, end), id=user.id, name=user.name, branch=user.strategic_branch_warehouse_id.display_name or "—")
                  for user, group in tasks.grouped("assigned_user_id").items()]
        return {"period": {"start": fields.Date.to_string(start), "end": fields.Date.to_string(end)},
                "branches": sorted(branches, key=lambda row: row["branch"]), "people": sorted(people, key=lambda row: row["name"])}
