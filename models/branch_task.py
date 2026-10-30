"""Branch tasks (Branch Manager Phase 3): operational work assigned to branch staff, with a short review workflow.

Odoo activities (the module's existing follow-ups) have no in-progress, submitted or reviewed states, so assignments
that need a review live here. A task never changes an objective: objective progress keeps coming from Key Results
and KPI evaluations only.

Assigned -> In Progress -> Submitted for Review -> Completed, or back to Changes Requested; open tasks can be Cancelled.
The assignee works the task; the branch's manager (or Head Office) assigns, reviews and cancels it. Overdue is
computed from the due date, never set by hand.
"""

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

_HEAD = "strategic_management.group_strategic_head_office_manager"
_MANAGER = "strategic_management.group_strategic_branch_manager"
_EMPLOYEE = "strategic_management.group_strategic_employee"
OPEN_STATES = ("assigned", "in_progress", "changes_requested", "submitted")
# Waiting on the assignee; a submitted task waits on the reviewer, so it is not the assignee's overdue work.
WORKING_STATES = ("assigned", "in_progress", "changes_requested")
_LIFECYCLE_FIELDS = {"state", "assigned_user_id", "warehouse_id", "company_id", "assigned_by_id", "completion_note",
                     "submitted_at", "reviewed_by_id", "reviewed_at"}
_MANAGER_FIELDS = {"name", "description", "date_deadline", "priority", "objective_id"}


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
        if self.env.context.get("strategic_task_workflow") or self.env.su:
            return super().write(vals)
        if _LIFECYCLE_FIELDS.intersection(vals):
            raise ValidationError("A task's assignee, branch and status change only through its workflow buttons.")
        for task in self:
            if not task._can_manage():
                raise AccessError("Only the branch's manager or Head Office can edit this task.")
            if task.state in ("done", "cancelled"):
                raise ValidationError("Completed or cancelled tasks cannot be edited.")
            if set(vals) - _MANAGER_FIELDS - {"review_note"}:
                raise ValidationError("Only the title, description, due date, priority, objective and review notes can be edited.")
            if "objective_id" in vals:
                task._check_objective(vals["objective_id"], task.warehouse_id)
        return super().write(vals)

    def unlink(self):
        raise UserError("Tasks are kept for their history; cancel the task instead.")

    def _transition(self, allowed_from, vals, message, notify=None):
        self.ensure_one()
        if self.state not in allowed_from:
            raise ValidationError("This task is %s, so it cannot be changed this way." % dict(self._fields["state"].selection)[self.state])
        self.with_context(strategic_task_workflow=True).write(vals)
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
            task._transition(("in_progress",), {"state": "submitted", "completion_note": note, "submitted_at": fields.Datetime.now()},
                             "Submitted for review: %s" % note, notify=task._reviewers())
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
        return {
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
        attention = [row for row in data["tasks"] if row["state"] == "submitted" or row["is_overdue"]]
        attention.sort(key=lambda row: (row["state"] != "submitted", row["date_deadline"]))
        return {"counts": data["counts"], "attention": attention[:5]}

    # ------------------------------------------------------------------ Employee Management (Phase 4): read-only workload view
    @api.model
    def _employee_scope(self):
        """The manager's branch, the tasks Task Management shows, and the people they belong to.

        People are the branch's active internal users other than managers, plus anyone else holding one of those tasks
        (for example someone since moved to another branch), so the per-person counts add up to Task Management's.
        """
        branch = self._manager_branch()
        tasks = self._managed_tasks(branch)
        # sudo: names and groups of this branch's internal users only, the same scope as the task form's assignee list.
        members = self.env["res.users"].sudo().search([
            ("active", "=", True), ("share", "=", False), ("company_ids", "in", self.env.company.id),
            ("strategic_branch_warehouse_id", "=", branch.id), ("id", "!=", self.env.user.id)])
        members = members.filtered(lambda user: not user.has_group(_MANAGER))
        return branch, tasks, (members | tasks.sudo().assigned_user_id).sorted(lambda user: (user.name or "").lower())

    @api.model
    def _employee_row(self, user, branch, tasks, assignable):
        """One person's counts (Task Management definitions) and, if they cannot be given tasks now, why."""
        note = False
        if user not in assignable:
            if not user.active:
                note = "Account archived"
            elif user.strategic_branch_warehouse_id != branch:
                note = "No longer in this branch"
            elif user.has_group(_MANAGER):
                note = "Branch Manager"
            else:
                note = "Cannot receive tasks: no Strategic Management employee access"
        return {"id": user.id, "name": user.name, "can_receive_tasks": user in assignable, "note": note, "counts": self._counts(tasks)}

    @api.model
    def get_employee_management(self):
        """Each person the Branch Manager supervises with their open, overdue and awaiting-review task counts."""
        branch, tasks, people = self._employee_scope()
        assignable = self._assignable_users().sudo()
        by_user = tasks.grouped("assigned_user_id")
        empty = self.browse()
        return {
            "branch": {"id": branch.id, "name": branch.display_name},
            "employees": [self._employee_row(user, branch, by_user.get(user, empty), assignable) for user in people],
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
