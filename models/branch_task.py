"""Branch tasks (Branch Manager Phase 3): operational work assigned to branch staff, with a short review workflow.

Odoo activities (the module's existing follow-ups) have no in-progress, submitted or reviewed states, so assignments
that need a review live here. A task never changes an objective: objective progress keeps coming from Key Results
and KPI evaluations only.

Assigned -> In Progress -> Submitted for Review -> Completed, or back to Changes Requested; open tasks can be Cancelled.
The assignee works the task; the branch's manager (or Head Office) assigns, reviews and cancels it. Overdue is
computed from the due date, never set by hand.

The due date changes only through an approved extension, never during review or later. Each submission keeps the deadline
that applied and whether it was met, so later changes cannot alter past on-time results. Extensions and blockers are
kept as their own records and only change through the actions here.
"""

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import html2plaintext

_HEAD = "strategic_management.group_strategic_head_office_manager"
_MANAGER = "strategic_management.group_strategic_branch_manager"
_EMPLOYEE = "strategic_management.group_strategic_employee"
OPEN_STATES = ("assigned", "in_progress", "changes_requested", "submitted")
# Waiting on the assignee; a submitted task waits on the reviewer, so it is not the assignee's overdue work.
WORKING_STATES = ("assigned", "in_progress", "changes_requested")
_LIFECYCLE_FIELDS = {"state", "assigned_user_id", "warehouse_id", "company_id", "assigned_by_id", "completion_note",
                     "submitted_at", "reviewed_by_id", "reviewed_at", "submission_deadline", "submitted_on_time"}
_MANAGER_FIELDS = {"name", "description", "priority", "objective_id"}


class StrategicBranchTask(models.Model):
    _name = "strategic.branch.task"
    _description = "Branch Task"
    _inherit = ["mail.thread"]
    _order = "date_deadline, priority desc, id"

    name = fields.Char("Task", required=True, tracking=True)
    description = fields.Text(required=True)
    assigned_user_id = fields.Many2one("res.users", "Assigned To", required=True, index=True, tracking=True)
    warehouse_id = fields.Many2one("stock.warehouse", "Branch", required=True, index=True, readonly=True)
    company_id = fields.Many2one("res.company", required=True, index=True, readonly=True, default=lambda self: self.env.company)
    assigned_by_id = fields.Many2one("res.users", "Assigned By", required=True, readonly=True, default=lambda self: self.env.user)
    date_deadline = fields.Date("Due Date", required=True, tracking=True)
    priority = fields.Selection([("0", "Normal"), ("1", "High")], default="0", required=True)
    objective_id = fields.Many2one("strategic.objective", "Related Objective", index=True, ondelete="set null")
    state = fields.Selection([
        ("assigned", "Assigned"),
        ("in_progress", "In Progress"),
        ("submitted", "Submitted for Review"),
        ("changes_requested", "Changes Requested"),
        ("done", "Completed"),
        ("cancelled", "Cancelled"),
    ], default="assigned", required=True, index=True, tracking=True, readonly=True)
    completion_note = fields.Text("Completion Notes", readonly=True)
    review_note = fields.Text("Review Notes")
    submitted_at = fields.Datetime(readonly=True)
    submission_deadline = fields.Date("Deadline at Submission", readonly=True)
    submitted_on_time = fields.Boolean("Submitted On Time", readonly=True)
    extension_ids = fields.One2many("strategic.task.deadline.extension", "task_id", "Deadline Extensions")
    blocker_ids = fields.One2many("strategic.task.blocker", "task_id", "Blockers")
    reviewed_by_id = fields.Many2one("res.users", readonly=True)
    reviewed_at = fields.Datetime(readonly=True)
    is_overdue = fields.Boolean("Overdue", compute="_compute_is_overdue")

    @api.depends("date_deadline", "state")
    def _compute_is_overdue(self):
        today = fields.Date.context_today(self)
        for task in self:
            task.is_overdue = bool(task.state in WORKING_STATES and task.date_deadline and task.date_deadline < today)

    # ------------------------------------------------------------------ who may do what
    def _is_head_office(self, user=None):
        return (user or self.env.user).has_group(_HEAD)

    def _is_branch_manager_of(self, warehouse, user=None):
        user = user or self.env.user
        return bool(warehouse) and user.has_group(_MANAGER) and not user.has_group(_HEAD) and user.strategic_branch_warehouse_id == warehouse

    def _can_manage(self, user=None):
        """Assign, edit, review and cancel: Head Office, or the task branch's manager, never the assignee."""
        self.ensure_one()
        user = user or self.env.user
        if user == self.assigned_user_id:
            return False
        return self._is_head_office(user) or self._is_branch_manager_of(self.warehouse_id, user)

    @api.model
    def _assignable_users(self, assigner=None):
        """Who the user may assign tasks to: their branch's staff (Branch Manager) or any branch user (Head Office)."""
        assigner = assigner or self.env.user
        Users = self.env["res.users"].sudo()
        domain = [("active", "=", True), ("share", "=", False), ("company_ids", "in", self.env.company.id),
                  ("all_group_ids", "in", self.env.ref(_EMPLOYEE).id), ("id", "!=", assigner.id)]
        if self._is_head_office(assigner):
            users = Users.search(domain + [("strategic_branch_warehouse_id", "!=", False)])
            return users.filtered(lambda user: not user.has_group(_HEAD))
        if not assigner.has_group(_MANAGER):
            return Users.browse()
        branch = self._manager_branch(assigner)
        users = Users.search(domain + [("strategic_branch_warehouse_id", "=", branch.id)])
        return users.filtered(lambda user: not user.has_group(_MANAGER))

    @api.model
    def _manager_branch(self, user=None):
        """The Branch Manager's own valid branch (same check as the rest of the app), or an AccessError."""
        context = self.env["strategic.performance.dashboard"].with_user(user or self.env.user).get_navigation_context()
        if context["role"] != "branch_manager":
            raise AccessError("Only Branch Managers and Head Office assign branch tasks.")
        if context["branch_error"]:
            raise AccessError(context["branch_error"])
        return self.env["stock.warehouse"].browse(context["branch"]["id"])

    # ------------------------------------------------------------------ create and write
    @api.model_create_multi
    def create(self, vals_list):
        user = self.env.user
        if not self.env.su and not (self._is_head_office(user) or user.has_group(_MANAGER)):
            raise AccessError("Only Branch Managers and Head Office assign branch tasks.")
        allowed = self._assignable_users(user) if not self.env.su else False
        for vals in vals_list:
            if _LIFECYCLE_FIELDS.intersection(vals) - {"assigned_user_id"}:
                raise ValidationError("A new task starts as Assigned; its workflow fields are set by the workflow.")
            assignee = self.env["res.users"].sudo().browse(vals.get("assigned_user_id")).exists()
            if not assignee:
                raise ValidationError("Choose the employee responsible for this task.")
            if allowed is not False and assignee not in allowed:
                raise AccessError("You can only assign tasks to employees of your own branch.")
            if not assignee.strategic_branch_warehouse_id:
                raise ValidationError("%s has no branch; assign their branch before giving them tasks." % assignee.name)
            for key in ("name", "description"):
                if not (vals.get(key) or "").strip():
                    raise ValidationError("Enter the task title and what needs to be done.")
            vals.update(warehouse_id=assignee.strategic_branch_warehouse_id.id, company_id=self.env.company.id,
                        assigned_by_id=user.id, state="assigned")
            self._check_objective(vals.get("objective_id"), assignee.strategic_branch_warehouse_id)
        tasks = super().create(vals_list)
        for task in tasks:
            task.message_subscribe(partner_ids=task.assigned_user_id.partner_id.ids)
        return tasks

    def _check_objective(self, objective_id, warehouse):
        """An optional objective must be one the assigner can read and that applies to the task's branch."""
        if not objective_id:
            return
        objective = self.env["strategic.objective"].browse(objective_id).exists()
        if not objective:
            raise ValidationError("The related objective no longer exists.")
        objective.check_access("read")
        if objective.scope == "branch" and objective.warehouse_id != warehouse:
            raise AccessError("A task can only support company-wide objectives or objectives of its own branch.")

    def write(self, vals):
        if self.env.su:
            return super().write(vals)
        if _LIFECYCLE_FIELDS.intersection(vals):
            raise ValidationError("A task's assignee, branch and status change only through its workflow buttons.")
        if "date_deadline" in vals:
            raise ValidationError("A due date changes only through an approved extension with a reason.")
        for task in self:
            if not task._can_manage():
                raise AccessError("Only the branch's manager or Head Office can edit this task.")
            if task.state in ("done", "cancelled"):
                raise ValidationError("Completed or cancelled tasks cannot be edited.")
            if set(vals) - _MANAGER_FIELDS - {"review_note"}:
                raise ValidationError("Only the title, description, priority, objective and review notes can be edited.")
            if "objective_id" in vals:
                task._check_objective(vals["objective_id"], task.warehouse_id)
        return super().write(vals)

    def unlink(self):
        raise UserError("Tasks are kept for their history; cancel the task instead.")

    def _transition(self, allowed_from, vals, message, notify=None):
        self.ensure_one()
        if self.state not in allowed_from:
            raise ValidationError("This task is %s, so it cannot be changed this way." % dict(self._fields["state"].selection)[self.state])
        # sudo, not a context flag: a client can send any context key but cannot obtain sudo.
        self.sudo().write(vals)
        self.message_post(body=message, subtype_xmlid="mail.mt_note", partner_ids=notify.partner_id.ids if notify else [])
        return True

    def _reviewers(self):
        """Who reviews a submission: whoever assigned the task while they still manage it, else the branch's managers."""
        self.ensure_one()
        if self.assigned_by_id and self._can_manage(self.assigned_by_id):
            return self.assigned_by_id
        return self.env["strategic.kpi.target"].sudo()._get_schedule_notification_users("branch", self.company_id, warehouse=self.warehouse_id)             .filtered(lambda user: self._can_manage(user))

    def _require_assignee(self):
        if self.env.user != self.assigned_user_id:
            raise AccessError("Only the person the task is assigned to can do this.")

    def _require_manager(self):
        if not self._can_manage():
            raise AccessError("Only the branch's manager or Head Office can review this task, never its assignee.")

    # ------------------------------------------------------------------ workflow
    def action_start(self):
        for task in self:
            task._require_assignee()
            task._transition(("assigned", "changes_requested"), {"state": "in_progress"}, "Work started.")
        return True

    def action_submit(self, note=False):
        for task in self:
            task._require_assignee()
            note = (note or "").strip()
            if not note:
                raise ValidationError("Describe what was done before submitting for review.")
            now = fields.Datetime.now()
            # The company's time zone: the browser's context and the employee's own preference are theirs to change.
            local_day = fields.Datetime.context_timestamp(task.with_context(tz=task.company_id.partner_id.tz or "UTC"), now).date()
            task._transition(("in_progress",), {"state": "submitted", "completion_note": note, "submitted_at": now, "submission_deadline": task.date_deadline,
                                                "submitted_on_time": local_day <= task.date_deadline},
                             "Submitted for review: %s" % note, notify=task._reviewers())
        return True

    def action_extend_deadline(self, new_deadline, reason):
        """An approved extension: the only way a due date moves, later only, with its reason and approver kept."""
        self.ensure_one()
        self._require_manager()
        reason = (reason or "").strip()
        try:
            new_deadline = fields.Date.to_date(new_deadline)
        except (TypeError, ValueError):
            new_deadline = False
        if not new_deadline or not reason:
            raise ValidationError("Choose the new due date and give the reason for the extension.")
        if new_deadline <= self.date_deadline:
            raise ValidationError("An extension must be later than the current due date.")
        previous = self.date_deadline
        self.env["strategic.task.deadline.extension"].sudo().create({
            "task_id": self.id, "previous_deadline": previous, "new_deadline": new_deadline, "reason": reason, "approved_by_id": self.env.user.id})
        return self._transition(WORKING_STATES, {"date_deadline": new_deadline}, "Due date extended from %s to %s: %s" % (previous, new_deadline, reason), notify=self.assigned_user_id)

    def _open_blocker(self):
        Blocker = self.env["strategic.task.blocker"].sudo()
        return Blocker.search([("task_id", "=", self.id), ("state", "=", "open")], limit=1) if self.state in OPEN_STATES else Blocker

    def action_report_blocker(self, problem, help_needed):
        """The assignee tells the branch's manager what stops the work. The due date does not move by itself."""
        self.ensure_one()
        self._require_assignee()
        problem, help_needed = (problem or "").strip(), (help_needed or "").strip()
        if not problem or not help_needed:
            raise ValidationError("Describe the problem and say what help you need.")
        if self.state not in WORKING_STATES:
            raise ValidationError("Blockers can be reported only while you are working on the task.")
        if self._open_blocker():
            raise UserError("This task already has an open blocker.")
        self.env["strategic.task.blocker"].sudo().create({"task_id": self.id, "reported_by_id": self.env.user.id, "problem": problem, "help_needed": help_needed})
        self.message_post(body="Blocker reported: %s Help needed: %s" % (problem, help_needed), subtype_xmlid="mail.mt_note", partner_ids=self._reviewers().partner_id.ids)
        return True

    def action_resolve_blocker(self, resolution):
        self.ensure_one()
        self._require_manager()
        resolution = (resolution or "").strip()
        blocker = self._open_blocker()
        if not blocker:
            raise UserError("This task has no open blocker.")
        if not resolution:
            raise ValidationError("Say how the blocker was resolved.")
        blocker.write({"state": "resolved", "resolved_by_id": self.env.user.id, "resolved_at": fields.Datetime.now(), "resolution": resolution})
        self.message_post(body="Blocker resolved: %s" % resolution, subtype_xmlid="mail.mt_note", partner_ids=self.assigned_user_id.partner_id.ids)
        return True

    def action_accept(self):
        for task in self:
            task._require_manager()
            task._transition(("submitted",), {"state": "done", "reviewed_by_id": self.env.user.id, "reviewed_at": fields.Datetime.now()},
                             "Submission accepted.")
        return True

    def action_request_changes(self, note=False):
        for task in self:
            task._require_manager()
            note = (note or task.review_note or "").strip()
            if not note:
                raise ValidationError("Explain what needs to change.")
            task._transition(("submitted",), {"state": "changes_requested", "review_note": note, "reviewed_by_id": self.env.user.id,
                                              "reviewed_at": fields.Datetime.now()}, "Changes requested: %s" % note)
        return True

    def action_cancel(self):
        for task in self:
            task._require_manager()
            task._transition(OPEN_STATES, {"state": "cancelled"}, "Task cancelled.")
        return True

    # ------------------------------------------------------------------ data for the pages
    def _row(self):
        self.ensure_one()
        user = self.env.user
        record = self.sudo()
        blocker = self._open_blocker()
        return {
            "extensions": [{"id": row.id, "previous": fields.Date.to_string(row.previous_deadline), "new": fields.Date.to_string(row.new_deadline), "reason": row.reason,
                            "approved_by": row.approved_by_id.name, "at": fields.Datetime.to_string(row.create_date)} for row in record.extension_ids],
            "open_blocker": {"problem": blocker.problem, "help_needed": blocker.help_needed, "reported_by": blocker.reported_by_id.name,
                             "at": fields.Datetime.to_string(blocker.create_date)} if blocker else False,
            "resolved_blockers": [{"id": row.id, "problem": row.problem, "resolution": row.resolution, "resolved_by": row.resolved_by_id.name}
                                  for row in record.blocker_ids if row.state == "resolved"],
            "can_report_blocker": user == self.assigned_user_id and self.state in WORKING_STATES and not blocker,
            "can_resolve_blocker": bool(blocker) and self._can_manage(),
            "can_extend": self.state in WORKING_STATES and self._can_manage(),
            "submission_deadline": fields.Date.to_string(self.submission_deadline) if self.submission_deadline else False,
            "submitted_on_time": self.submitted_on_time,
            "id": self.id, "name": self.name, "description": self.description, "assigned_to": self.assigned_user_id.name,
            "assigned_user_id": self.assigned_user_id.id, "assigned_by": self.assigned_by_id.name, "branch": self.warehouse_id.display_name,
            "date_deadline": fields.Date.to_string(self.date_deadline), "priority": self.priority,
            "priority_label": dict(self._fields["priority"].selection)[self.priority],
            "state": self.state, "status": dict(self._fields["state"].selection)[self.state], "is_overdue": self.is_overdue,
            "objective": self.objective_id.sudo().display_name if self.objective_id else False,
            "completion_note": self.completion_note or "", "review_note": self.review_note or "",
            "submitted_at": fields.Datetime.to_string(self.submitted_at) if self.submitted_at else False,
            "reviewed_by": self.reviewed_by_id.name or False,
            "can_start": user == self.assigned_user_id and self.state in ("assigned", "changes_requested"),
            "can_submit": user == self.assigned_user_id and self.state == "in_progress",
            "can_review": self.state == "submitted" and self._can_manage(),
            "can_cancel": self.state in OPEN_STATES and self._can_manage(),
        }

    @api.model
    def _counts(self, tasks):
        return {"open": len(tasks.filtered(lambda task: task.state in OPEN_STATES)),
                "overdue": len(tasks.filtered("is_overdue")),
                "review": len(tasks.filtered(lambda task: task.state == "submitted"))}

    @api.model
    def get_my_tasks(self):
        """Tasks assigned to the current user (any role), newest work first."""
        tasks = self.search([("assigned_user_id", "=", self.env.user.id), ("company_id", "=", self.env.company.id)])
        return {"tasks": [task._row() for task in tasks], "counts": self._counts(tasks)}

    @api.model
    def get_employee_workspace(self):
        """The signed-in Employee's working day, from their own tasks only."""
        self.env["strategic.performance.dashboard"]._require_employee_dashboard_access()
        company = self.env.company
        tasks = self.search([("assigned_user_id", "=", self.env.user.id), ("company_id", "=", company.id)])
        today = fields.Date.context_today(self.with_context(tz=company.partner_id.tz or "UTC"))
        working = tasks.filtered(lambda task: task.state in WORKING_STATES)
        # Most urgent first: late, sent back for correction, due today, high priority, then by due date.
        ordered = working.sorted(lambda task: (not task.is_overdue, task.state != "changes_requested", task.date_deadline != today, task.priority != "1", task.date_deadline, task.id))
        upcoming = working.filtered(lambda task: today < task.date_deadline <= fields.Date.add(today, days=7)).sorted("date_deadline")
        blockers = self.env["strategic.task.blocker"].sudo().search([("task_id", "in", tasks.ids)], limit=5)
        messages = self.env["mail.message"].sudo().search([
            ("model", "=", self._name), ("res_id", "in", tasks.ids), ("message_type", "!=", "user_notification")], order="date desc, id desc", limit=30)
        names = {task.id: task.name for task in tasks}
        activity = [{"id": message.id, "task": names[message.res_id], "text": text, "author": message.author_id.name or "", "date": fields.Datetime.to_string(message.date)}
                    for message, text in ((message, html2plaintext(message.body or "").strip()) for message in messages) if text][:8]
        return {
            "counts": {"open": len(tasks.filtered(lambda task: task.state in OPEN_STATES)), "due_today": len(working.filtered(lambda task: task.date_deadline == today)),
                       "overdue": len(working.filtered("is_overdue")), "review": len(tasks.filtered(lambda task: task.state == "submitted")),
                       "correction": len(working.filtered(lambda task: task.state == "changes_requested"))},
            "today": fields.Date.to_string(today),
            "tasks": [task._row() for task in ordered[:10]],
            "tasks_total": len(ordered),
            "upcoming": [task._row() for task in upcoming[:5]],
            "activity": activity,
            "blockers": [{"id": row.id, "task": row.task_id.name, "problem": row.problem, "state": row.state, "status": "Open" if row.state == "open" else "Resolved",
                          "resolution": row.resolution or ""} for row in blockers],
            "instructions": [{"id": task.id, "task": task.name, "note": task.review_note, "by": task.sudo().reviewed_by_id.name or task.sudo().assigned_by_id.name}
                             for task in working.filtered(lambda task: task.state == "changes_requested" and task.review_note)],
        }

    @api.model
    def _managed_tasks(self, branch):
        """The tasks Task Management shows: the branch's tasks, except the manager's own (those are in My Tasks)."""
        return self.search([("warehouse_id", "=", branch.id), ("assigned_user_id", "!=", self.env.user.id),
                            ("company_id", "=", self.env.company.id)])

    @api.model
    def get_task_management(self):
        """The Branch Manager's workspace: tasks assigned to their branch's staff, who can be assigned, and objectives."""
        branch = self._manager_branch()
        tasks = self._managed_tasks(branch)
        objectives = self.env["strategic.objective"].search([
            ("company_id", "=", self.env.company.id), ("state", "=", "active"),
            "|", ("scope", "=", "organization"), ("warehouse_id", "=", branch.id)], order="name")
        return {
            "branch": {"id": branch.id, "name": branch.display_name},
            "tasks": [task._row() for task in tasks],
            "counts": self._counts(tasks),
            "assignees": [{"id": user.id, "name": user.name} for user in self._assignable_users().sorted("name")],
            "objectives": [{"id": objective.id, "name": objective.name, "scope": "Your branch" if objective.scope == "branch" else "Company-wide"}
                           for objective in objectives],
        }

    @api.model
    def create_branch_task(self, values):
        """Create from the Task Management form: only the essential fields are accepted."""
        self._manager_branch()
        allowed = {"name", "description", "assigned_user_id", "date_deadline", "priority", "objective_id"}
        vals = {key: value for key, value in (values or {}).items() if key in allowed and value not in (None, "")}
        if not vals.get("date_deadline"):
            raise ValidationError("Choose the due date.")
        return self.create(vals)._row()

    @api.model
    def get_branch_task_summary(self):
        """Compact overview for the Branch Manager dashboard: counts and the few tasks needing action."""
        data = self.get_task_management()
        attention = [row for row in data["tasks"] if row["state"] == "submitted" or row["is_overdue"] or row["open_blocker"]]
        attention.sort(key=lambda row: (not row["open_blocker"], row["state"] != "submitted", row["date_deadline"]))
        return {"counts": data["counts"], "attention": attention[:5], "blocked": sum(1 for row in data["tasks"] if row["open_blocker"])}

    # ------------------------------------------------------------------ Employee Management (Phase 4): read-only workload view
    @api.model
    def _employee_scope(self):
        """The manager's branch, the tasks Task Management shows, and the people they belong to.

        People are the branch's active internal users other than managers, plus anyone else holding one of those tasks
        (for example someone since moved to another branch), so the per-person counts add up to Task Management's.
        """
        head = self._is_head_office()
        branch = self.env["stock.warehouse"] if head else self._manager_branch()
        if head:
            tasks = self.search([("assigned_user_id", "!=", self.env.user.id), ("company_id", "=", self.env.company.id)])
        else:
            tasks = self._managed_tasks(branch)
        # sudo: names and groups of this branch's internal users only, the same scope as the task form's assignee list.
        members = self.env["res.users"].sudo().search([
            ("active", "=", True), ("share", "=", False), ("company_ids", "in", self.env.company.id),
            ("strategic_branch_warehouse_id", "=", branch.id) if branch else ("strategic_branch_warehouse_id", "!=", False), ("id", "!=", self.env.user.id)])
        members = members.filtered(lambda user: not user.has_group(_HEAD if head else _MANAGER))
        return branch, tasks, (members | tasks.sudo().assigned_user_id).sorted(lambda user: (user.name or "").lower())

    @api.model
    def _employee_row(self, user, branch, tasks, assignable):
        """One person's counts (Task Management definitions) and, if they cannot be given tasks now, why."""
        note = False
        if user not in assignable:
            if not user.active:
                note = "Account archived"
            elif branch and user.strategic_branch_warehouse_id != branch:
                note = "No longer in this branch"
            elif user.has_group(_MANAGER):
                note = "Branch Manager"
            else:
                note = "Cannot receive tasks: no Strategic Management employee access"
        return {"id": user.id, "name": user.name, "can_receive_tasks": user in assignable, "note": note, "counts": self._counts(tasks),
                "branch": user.sudo().strategic_branch_warehouse_id.display_name or False, "role": "Employee", "department_ids": user.sudo().strategic_department_ids.ids,
                # Same rule as set_employee_departments: managers keep their role's own access.
                "can_assign_departments": user.has_group(_EMPLOYEE) and not user.has_group(_MANAGER) and bool(user.sudo().strategic_branch_warehouse_id),
                "departments": user.sudo().strategic_department_ids.mapped("name")}

    @api.model
    def get_employee_management(self):
        """Each person the Branch Manager supervises with their open, overdue and awaiting-review task counts."""
        branch, tasks, people = self._employee_scope()
        assignable = self._assignable_users().sudo()
        by_user = tasks.grouped("assigned_user_id")
        empty = self.browse()
        employees = [self._employee_row(user, branch, by_user.get(user, empty), assignable) for user in people]
        # Registered employees who have no login yet: tasks go to user accounts, so they cannot receive any.
        waiting = self.env["hr.employee"].sudo().search(
            [("user_id", "=", False), ("strategic_branch_warehouse_id", "=", branch.id) if branch else ("strategic_branch_warehouse_id", "!=", False),
             ("company_id", "=", self.env.company.id)], order="name")
        without_login = [{"id": e.id, "name": e.name, "job": e.job_id.name or "", "branch": e.strategic_branch_warehouse_id.display_name} for e in waiting]
        return {
            "branch": {"id": branch.id, "name": branch.display_name} if branch else False,
            "employees": employees,
            "without_login": without_login,
            "departments": [{"id": department.id, "name": department.name} for department in self.env["strategic.department"].sudo().search([])],
        }

    @api.model
    def get_employee_tasks(self, user_id):
        """One supervised person's current tasks and recent history; anyone outside the manager's scope is refused."""
        branch, tasks, people = self._employee_scope()
        person = people.filtered(lambda user: user.id == user_id) if isinstance(user_id, int) else people.browse()
        if not person:
            raise AccessError("This person is not one of your branch's employees.")
        own = tasks.filtered(lambda task: task.assigned_user_id.id == person.id)
        history = own.filtered(lambda task: task.state not in OPEN_STATES).sorted("write_date", reverse=True)
        return {
            "employee": self._employee_row(person, branch, own, self._assignable_users().sudo()),
            "current": [task._row() for task in own.filtered(lambda task: task.state in OPEN_STATES)],
            "history": [task._row() for task in history[:10]],
            "history_total": len(history),
        }


class StrategicTaskDeadlineExtension(models.Model):
    """One approved change of a task's due date. Created only by action_extend_deadline; nobody can edit it."""
    _name = "strategic.task.deadline.extension"
    _description = "Task Deadline Extension"
    _order = "id"

    task_id = fields.Many2one("strategic.branch.task", required=True, index=True, ondelete="cascade", readonly=True)
    previous_deadline = fields.Date(required=True, readonly=True)
    new_deadline = fields.Date(required=True, readonly=True)
    reason = fields.Text(required=True, readonly=True)
    approved_by_id = fields.Many2one("res.users", required=True, readonly=True)


class StrategicTaskBlocker(models.Model):
    """What stops an employee's task, for the branch's manager. Neutral: it never changes a rate or a due date by itself."""
    _name = "strategic.task.blocker"
    _description = "Task Blocker"
    _order = "id desc"

    task_id = fields.Many2one("strategic.branch.task", required=True, index=True, ondelete="cascade", readonly=True)
    reported_by_id = fields.Many2one("res.users", required=True, index=True, readonly=True)
    problem = fields.Text(required=True, readonly=True)
    help_needed = fields.Text(required=True, readonly=True)
    state = fields.Selection([("open", "Open"), ("resolved", "Resolved")], default="open", required=True, readonly=True)
    resolved_by_id = fields.Many2one("res.users", readonly=True)
    resolved_at = fields.Datetime(readonly=True)
    resolution = fields.Text(readonly=True)

    _one_open_per_task = models.UniqueIndex("(task_id) WHERE state = 'open'", "This task already has an open blocker.")
