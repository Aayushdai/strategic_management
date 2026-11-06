"""Admin console: run the system without seeing the business's sensitive data.

The Admin sees people and access, branches and staffing, the status of work records, system health and an audit log;
the Admin can change roles (not Head Office), branches, departments, activate or deactivate users, create users, send
password-reset links and edit branch setup. Every read goes through sudo with an explicit list of fields, so the Admin
never receives money, personal details, performance ratings, or the notes inside reports, tasks and requests. Odoo's own
screens stay closed to the Admin (res_users._strategic_access_domain). Every change is written to the audit log, and
new users and role grants also give Head Office a To-Do, so access cannot be widened unnoticed.
"""

import logging
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import email_normalize
from odoo.tools.sql import escape_like_value

from .branch_task import WORKING_STATES

_logger = logging.getLogger(__name__)

_ADMIN = "strategic_management.group_strategic_admin"
_HEAD = "strategic_management.group_strategic_head_office_manager"
_MANAGER = "strategic_management.group_strategic_branch_manager"
_EMPLOYEE = "strategic_management.group_strategic_employee"
_ROLE_GROUPS = {"admin": _ADMIN, "head_office": _HEAD, "branch_manager": _MANAGER, "employee": _EMPLOYEE}
_ROLE_LABELS = {"admin": "Admin", "head_office": "Head Office Manager", "branch_manager": "Branch Manager", "employee": "Employee"}
# The Admin can give these roles; Head Office (which sees the money) is granted by Head Office only.
_ADMIN_ASSIGNABLE = ("employee", "branch_manager", "admin")
_APP_RIGHTS = (
    ("point_of_sale.group_pos_user", "Point of Sale"), ("stock.group_stock_user", "Inventory"), ("purchase.group_purchase_user", "Purchase"),
    ("sales_team.group_sale_salesman", "Sales"), ("account.group_account_invoice", "Invoicing"), ("mrp.group_mrp_user", "Manufacturing"),
    ("hr.group_hr_user", "Employees"), ("hr_attendance.group_hr_attendance_officer", "Attendances"), ("base.group_system", "Odoo Settings"),
)
_LIMIT = 500
# Public methods the Admin may call; the Admin-role tests allow exactly these and check they carry no sensitive data.
ADMIN_CONSOLE_METHODS = frozenset({
    "get_admin_overview", "get_admin_people", "get_admin_branches", "get_admin_work", "get_admin_health", "get_admin_audit",
    "admin_set_user_access", "admin_set_user_active", "admin_create_user", "admin_send_reset_link", "admin_update_branch",
})


def _valid_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class StrategicAdminAudit(models.Model):
    _name = "strategic.admin.audit"
    _description = "Strategic Management Admin Audit"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company, readonly=True)
    actor_id = fields.Many2one("res.users", "Changed by", required=True, default=lambda self: self.env.user, readonly=True)
    action = fields.Selection([
        ("role", "Role changed"), ("branch", "Branch changed"), ("departments", "Departments changed"),
        ("activate", "User activated"), ("deactivate", "User deactivated"), ("create_user", "User created"),
        ("reset_link", "Password reset link sent"), ("branch_setup", "Branch setup changed"),
    ], required=True, readonly=True)
    target_user_id = fields.Many2one("res.users", "User", readonly=True)
    warehouse_id = fields.Many2one("stock.warehouse", "Branch", readonly=True)
    detail = fields.Char(readonly=True)

    def _row(self):
        return {"id": self.id, "when": fields.Datetime.to_string(self.create_date), "actor": self.actor_id.name,
                "action": dict(self._fields["action"].selection)[self.action], "action_key": self.action,
                "user": self.target_user_id.name or False, "branch": self.warehouse_id.display_name or False, "detail": self.detail or ""}


class StrategicAdminConsole(models.AbstractModel):
    _name = "strategic.admin.console"
    _description = "Strategic Management Admin Console"

    # ------------------------------------------------------------------ guards
    @api.model
    def _require_admin(self):
        if not self.env.user.has_group(_ADMIN) or self.env.su:
            raise AccessError("The Admin console is for the Admin role.")

    @api.model
    def _role(self, user):
        return next((role for role in ("admin", "head_office", "branch_manager", "employee") if user.has_group(_ROLE_GROUPS[role])), None)

    @api.model
    def _users(self, active_too=True):
        Users = self.env["res.users"].sudo().with_context(active_test=False)
        domain = [("share", "=", False), ("company_ids", "in", self.env.company.id)]
        if not active_too:
            domain.append(("active", "=", True))
        return Users.search(domain, order="name, id")

    @api.model
    def _target(self, user_id):
        """A user the Admin may change: in the company, not themselves, not Head Office, not an Odoo superuser."""
        if not _valid_id(user_id):
            raise ValidationError("Choose a user.")
        target = self._users().filtered(lambda user: user.id == user_id)
        if not target:
            raise AccessError("This user is not in the active company.")
        if target == self.env.user:
            raise ValidationError("You cannot change your own account; ask another Admin or Head Office.")
        if target.has_group(_HEAD) or target.has_group("base.group_system") or target.id == 1:
            raise AccessError("Head Office and Odoo administrator accounts are managed by Head Office.")
        return target

    @api.model
    def _branches(self):
        return self.env["stock.warehouse"]._strategic_branches(self.env.company).sorted(lambda warehouse: (warehouse.name, warehouse.id))

    @api.model
    def _branch(self, warehouse_id):
        if not warehouse_id:
            return self.env["stock.warehouse"]
        branch = self._branches().filtered(lambda warehouse: warehouse.id == warehouse_id) if _valid_id(warehouse_id) else False
        if not branch:
            raise ValidationError("Choose a branch of this company.")
        return branch

    @api.model
    def _overdue_task_domain(self):
        # Same rule as strategic.branch.task.is_overdue, which is computed and cannot be searched.
        return [("company_id", "=", self.env.company.id), ("state", "in", list(WORKING_STATES)), ("date_deadline", "<", fields.Date.context_today(self))]

    @api.model
    def _audit(self, action, user=False, branch=False, detail="", notify=False):
        entry = self.env["strategic.admin.audit"].sudo().create({
            "action": action, "actor_id": self.env.user.id, "target_user_id": user.id if user else False,
            "warehouse_id": branch.id if branch else False, "detail": detail})
        if notify:
            # Head Office sees every widening of access as a To-Do on the audit entry.
            self.env["strategic.kpi.target"].sudo()._create_deduplicated_activity(
                entry, "Admin: %s" % entry._row()["action"], "%s%s by %s." % (user.name + ": " if user else "", detail, self.env.user.name),
                scope="organization", company=self.env.company)
        return entry

    # ------------------------------------------------------------------ reads
    @api.model
    def _person_row(self, user):
        role = self._role(user)
        employee = self.env["hr.employee"].sudo().with_context(active_test=False).search([("user_id", "=", user.id)], limit=1)
        return {
            "id": user.id, "name": user.name, "login": user.login, "role": role or False, "role_label": _ROLE_LABELS.get(role, "No role"),
            "branch_id": user.strategic_branch_warehouse_id.id or False, "branch": user.strategic_branch_warehouse_id.display_name or False,
            "department_ids": user.strategic_department_ids.ids, "departments": user.strategic_department_ids.mapped("name"),
            "active": user.active, "last_login": fields.Datetime.to_string(user.login_date) if user.login_date else False,
            "app_rights": [label for xmlid, label in _APP_RIGHTS if self.env.ref(xmlid, raise_if_not_found=False) in user.group_ids],
            "has_employee": bool(employee),
            "locked": user == self.env.user or user.has_group(_HEAD) or user.has_group("base.group_system") or user.id == 1,
            "is_self": user == self.env.user,
        }

    @api.model
    def get_admin_people(self):
        self._require_admin()
        return {
            "people": [self._person_row(user) for user in self._users()],
            "roles": [{"id": key, "name": _ROLE_LABELS[key]} for key in _ADMIN_ASSIGNABLE],
            "branches": [{"id": branch.id, "name": branch.display_name} for branch in self._branches()],
            "departments": [{"id": department.id, "name": department.name} for department in self.env["strategic.department"].sudo().search([], order="sequence, id")],
        }

    @api.model
    def _branch_rows(self):
        users = self._users(active_too=False)
        Employee = self.env["hr.employee"].sudo()
        Request = self.env["strategic.staffing.request"].sudo()
        rows = []
        for branch in self._branches():
            managers = users.filtered(lambda user: user.strategic_branch_warehouse_id == branch and user.has_group(_MANAGER) and not user.has_group(_HEAD))
            staff = Employee.search_count([("strategic_branch_warehouse_id", "=", branch.id), ("company_id", "=", self.env.company.id)])
            planned = branch.strategic_planned_headcount or 0
            rows.append({
                "id": branch.id, "name": branch.name, "code": branch.code, "managers": managers.mapped("name"),
                "logins": len(users.filtered(lambda user: user.strategic_branch_warehouse_id == branch)),
                "staff": staff, "planned": planned, "short": max(planned - staff, 0),
                "open_requests": Request.search_count([("warehouse_id", "=", branch.id), ("state", "in", ("submitted", "approved"))]),
            })
        return rows

    @api.model
    def get_admin_branches(self):
        self._require_admin()
        return {"branches": self._branch_rows()}

    @api.model
    def _label(self, record, field):
        return dict(record._fields[field]._description_selection(self.env)).get(record[field], record[field] or "")

    @api.model
    def get_admin_work(self, kind="objectives"):
        """Titles, owners, branches, statuses and dates only: no figures, descriptions, notes or reasons."""
        self._require_admin()
        company = [("company_id", "=", self.env.company.id)]
        today = fields.Date.context_today(self)
        date = lambda value: fields.Date.to_string(value) if value else False
        if kind == "objectives":
            records = self.env["strategic.objective"].sudo().search(company, order="deadline desc, id desc", limit=_LIMIT)
            rows = [{"id": r.id, "title": r.name, "branch": r.warehouse_id.display_name or "Entire business", "owner": r.owner_id.name or False,
                     "status": self._label(r, "state"), "status_key": r.state, "date": date(r.deadline), "overdue": bool(r.overdue)} for r in records]
        elif kind == "goals":
            records = self.env["strategic.goal.setup"].sudo().search(company, order="id desc", limit=_LIMIT)
            rows = [{"id": r.id, "title": r.objective_id.name, "branch": r.objective_id.warehouse_id.display_name or "Entire business",
                     "owner": r.objective_id.owner_id.name or False, "area": self._label(r, "business_area"),
                     "status": self._label(r, "setup_state"), "status_key": r.setup_state, "date": date(r.objective_id.deadline), "overdue": False}
                    for r in records]
        elif kind == "tasks":
            records = self.env["strategic.branch.task"].sudo().search(company, order="date_deadline desc, id desc", limit=_LIMIT)
            rows = [{"id": r.id, "title": r.name, "branch": r.warehouse_id.display_name or False, "owner": r.assigned_user_id.name or False,
                     "status": self._label(r, "state"), "status_key": r.state, "date": date(r.date_deadline), "overdue": bool(r.is_overdue)}
                    for r in records]
        elif kind == "reports":
            records = self.env["strategic.branch.report"].sudo().search(company, order="date_end desc, id desc", limit=_LIMIT)
            rows = [{"id": r.id, "title": r.name, "branch": r.warehouse_id.display_name or False, "owner": r.submitted_by_id.name or False,
                     "status": self._label(r, "state"), "status_key": r.state, "date": date(r.date_end), "overdue": False} for r in records]
        elif kind == "staffing":
            records = self.env["strategic.staffing.request"].sudo().search(company, order="id desc", limit=_LIMIT)
            rows = [{"id": r.id, "title": "%s × %s" % (r.quantity, r.position or "Staff"), "branch": r.warehouse_id.display_name or False,
                     "owner": r.requested_by_id.name or False, "status": self._label(r, "state"), "status_key": r.state,
                     "date": date(r.expected_date), "overdue": bool(r.expected_date and r.expected_date < today and r.state in ("submitted", "approved"))}
                    for r in records]
        else:
            raise ValidationError("Unknown record type.")
        return {"kind": kind, "rows": rows, "truncated": len(rows) >= _LIMIT}

    @api.model
    def _health_issues(self):
        issues = []

        def add(severity, area, title, detail, items=(), section=False):
            issues.append({"severity": severity, "area": area, "title": title, "detail": detail, "items": list(items)[:20],
                           "count": len(items) if items else 0, "section": section})

        users = self._users(active_too=False)
        no_branch = users.filtered(lambda user: (user.has_group(_MANAGER) or user.has_group(_EMPLOYEE)) and not user.has_group(_HEAD)
                                   and not user.has_group(_ADMIN) and not user.strategic_branch_warehouse_id)
        if no_branch:
            add("critical", "People", "Users without a branch", "Branch Managers and Employees see no branch data until a branch is assigned.",
                no_branch.mapped("name"), "admin_people")
        no_role = users.filtered(lambda user: not self._role(user) and user.id != 1)
        if no_role:
            add("info", "People", "Users without a Strategic Management role", "They cannot open this app; give them a role if they need it.",
                no_role.mapped("name"), "admin_people")
        branches = self._branch_rows()
        unmanaged = [row["name"] for row in branches if not row["managers"]]
        if unmanaged:
            add("warning", "Branches", "Branches without a Branch Manager", "Nobody receives this branch's tasks and reminders.", unmanaged, "admin_branches")
        short = ["%s: %s of %s" % (row["name"], row["staff"], row["planned"]) for row in branches if row["short"]]
        if short:
            add("warning", "Branches", "Branches below planned staff", "Staff count is under the branch's planned staff.", short, "admin_branches")
        heads = users.filtered(lambda user: user.active and user.has_group(_HEAD))
        if len(heads) < 2:
            add("info", "People", "Only one Head Office Manager", "If that account is locked out, nobody can grant Head Office access.", heads.mapped("name"))
        Setup = self.env["strategic.goal.setup"].sudo()
        needs_setup = Setup.search([("company_id", "=", self.env.company.id), ("setup_state", "=", "needs_setup")])
        if needs_setup:
            add("warning", "Work", "Business goals not set up", "These goals have no measure yet, so they cannot be tracked.",
                needs_setup.objective_id.mapped("name"), "admin_work")
        Evaluation = self.env["strategic.kpi.evaluation"].sudo()
        since = fields.Datetime.now() - timedelta(days=30)
        broken = Evaluation.search([("company_id", "=", self.env.company.id), ("status", "=", "data_quality_issue"), ("create_date", ">=", since)])
        if broken:
            add("warning", "Data", "KPIs that could not be measured (last 30 days)", "The data source returned no usable value; check the KPI setup.",
                sorted(set(broken.mapped("kpi_id.name"))), False)
        today = fields.Date.context_today(self)
        overdue_tasks = self.env["strategic.branch.task"].sudo().search_count(self._overdue_task_domain())
        if overdue_tasks:
            add("info", "Work", "Overdue tasks", "%s task(s) are past their deadline." % overdue_tasks, (), "admin_work")
        late_requests = self.env["strategic.staffing.request"].sudo().search([
            ("company_id", "=", self.env.company.id), ("state", "in", ("submitted", "approved")), ("expected_date", "<", today)])
        if late_requests:
            add("warning", "Branches", "Staffing requests past their joining date", "Still open after the date staff were needed.",
                ["%s: %s × %s" % (r.warehouse_id.name, r.quantity, r.position) for r in late_requests], "admin_work")
        if not self.env["ir.mail_server"].sudo().search_count([]):
            add("warning", "System", "No outgoing mail server", "Invitations and password-reset links cannot be emailed.", ())
        crons = self.env["ir.cron"].sudo().with_context(active_test=False).search([("ir_actions_server_id.model_id.model", "=like", "strategic.%")])
        stopped = crons.filtered(lambda cron: not cron.active)
        if stopped:
            add("warning", "System", "Scheduled jobs turned off", "Automatic KPI evaluations and reminders do not run.", stopped.mapped("name"))
        if "failure_count" in crons._fields:
            failing = crons.filtered(lambda cron: cron.active and cron.failure_count)
            if failing:
                add("critical", "System", "Scheduled jobs failing", "These jobs failed on their last runs.", failing.mapped("name"))
        order = {"critical": 0, "warning": 1, "info": 2}
        return sorted(issues, key=lambda issue: order[issue["severity"]])

    @api.model
    def get_admin_health(self):
        self._require_admin()
        crons = self.env["ir.cron"].sudo().with_context(active_test=False).search([("ir_actions_server_id.model_id.model", "=like", "strategic.%")], order="name")
        return {"issues": self._health_issues(), "jobs": [{
            "name": cron.name, "active": cron.active,
            "next": fields.Datetime.to_string(cron.nextcall) if cron.nextcall else False,
            "last": fields.Datetime.to_string(cron.lastcall) if "lastcall" in cron._fields and cron.lastcall else False,
            "failures": cron.failure_count if "failure_count" in cron._fields else 0} for cron in crons]}

    @api.model
    def get_admin_audit(self, limit=200):
        self._require_admin()
        limit = min(limit, 500) if _valid_id(limit) else 200
        entries = self.env["strategic.admin.audit"].sudo().search([("company_id", "=", self.env.company.id)], limit=limit)
        return {"entries": [entry._row() for entry in entries]}

    @api.model
    def get_admin_overview(self):
        self._require_admin()
        users = self._users()
        active = users.filtered("active")
        roles = {key: len(active.filtered(lambda user, key=key: self._role(user) == key)) for key in _ROLE_LABELS}
        company = [("company_id", "=", self.env.company.id)]
        Task = self.env["strategic.branch.task"].sudo()
        issues = self._health_issues()
        logins = active.filtered("login_date").sorted("login_date", reverse=True)[:8]
        return {
            "people": {"active": len(active), "inactive": len(users) - len(active), "no_role": len(active.filtered(lambda user: not self._role(user))),
                       "roles": [{"key": key, "label": _ROLE_LABELS[key], "count": count} for key, count in roles.items()]},
            "branches": len(self._branches()),
            "work": {
                "objectives_active": self.env["strategic.objective"].sudo().search_count(company + [("state", "=", "active")]),
                "tasks_open": Task.search_count(company + [("state", "not in", ("done", "cancelled"))]),
                "tasks_overdue": Task.search_count(self._overdue_task_domain()),
                "reports_waiting": self.env["strategic.branch.report"].sudo().search_count(company + [("state", "=", "submitted")]),
                "staffing_open": self.env["strategic.staffing.request"].sudo().search_count(company + [("state", "in", ("submitted", "approved"))]),
            },
            "health": {"critical": sum(1 for i in issues if i["severity"] == "critical"), "warning": sum(1 for i in issues if i["severity"] == "warning"),
                       "top": issues[:4]},
            "logins": [{"name": user.name, "when": fields.Datetime.to_string(user.login_date)} for user in logins],
            "audit": [entry._row() for entry in self.env["strategic.admin.audit"].sudo().search(company, limit=6)],
        }

    # ------------------------------------------------------------------ changes
    @api.model
    def admin_set_user_access(self, user_id, role, warehouse_id=False, department_ids=None):
        """Role, branch and departments in one save. Head Office is never granted here."""
        self._require_admin()
        target = self._target(user_id)
        if role not in _ADMIN_ASSIGNABLE:
            raise ValidationError("Choose Employee, Branch Manager or Admin. Head Office access is granted by Head Office.")
        if target.has_group("base.group_system") and role == "admin":
            raise ValidationError("An Odoo administrator cannot hold the Admin role.")
        branch = self._branch(warehouse_id)
        if role in ("branch_manager", "employee") and not branch:
            raise ValidationError("A %s needs a branch." % _ROLE_LABELS[role])
        department_ids = department_ids or []
        if not isinstance(department_ids, list) or not all(_valid_id(value) for value in department_ids):
            raise ValidationError("Choose valid departments.")
        departments = self.env["strategic.department"].sudo().browse(set(department_ids)).exists()
        if len(departments) != len(set(department_ids)):
            raise ValidationError("One of the selected departments does not exist.")
        if departments and role != "employee":
            raise ValidationError("Departments are for Employees only.")
        before = (self._role(target), target.strategic_branch_warehouse_id, target.strategic_department_ids)
        values = {}
        if before[0] != role:
            values["group_ids"] = [(3, self.env.ref(xmlid).id) for xmlid in _ROLE_GROUPS.values()] + [(4, self.env.ref(_ROLE_GROUPS[role]).id)]
        if role == "admin":
            values["strategic_branch_warehouse_id"] = False
        elif branch != before[1]:
            values["strategic_branch_warehouse_id"] = branch.id
        # Departments first go empty so a role change away from Employee never trips the department rule.
        target.sudo().write({"strategic_department_ids": [(6, 0, [])]})
        if values:
            target.sudo().write(values)
        if departments:
            target.sudo().write({"strategic_department_ids": [(6, 0, departments.ids)]})
        if before[0] != role:
            self._audit("role", target, detail="%s → %s" % (_ROLE_LABELS.get(before[0], "No role"), _ROLE_LABELS[role]), notify=True)
        if target.strategic_branch_warehouse_id != before[1]:
            self._audit("branch", target, target.strategic_branch_warehouse_id or before[1],
                        "%s → %s" % (before[1].display_name or "None", target.strategic_branch_warehouse_id.display_name or "None"))
        if target.strategic_department_ids != before[2]:
            self._audit("departments", target, detail=", ".join(target.strategic_department_ids.mapped("name")) or "None")
        return self._person_row(target)

    @api.model
    def admin_set_user_active(self, user_id, active):
        self._require_admin()
        target = self._target(user_id)
        if not isinstance(active, bool):
            raise ValidationError("Choose activate or deactivate.")
        if target.active != active:
            target.sudo().write({"active": active})
            self._audit("activate" if active else "deactivate", target, detail="Login %s" % ("allowed" if active else "blocked"), notify=active)
        return self._person_row(target)

    @api.model
    def _send_reset(self, user):
        """Never reported as sent unless the mail system accepted it; the account stands either way."""
        try:
            with self.env.cr.savepoint():
                user.sudo().with_context(create_user=True).action_reset_password()
            return {"sent": True, "message": "A password-reset link was emailed to %s." % user.login}
        except Exception as error:  # noqa: BLE001  any mail failure is shown to the Admin, never hidden
            message = str(error.args[0]) if error.args else str(error)
            _logger.warning("Password reset link not sent to %s: %s", user.login, message)
            return {"sent": False, "message": "The link could not be emailed: %s" % message}

    @api.model
    def admin_create_user(self, name, email, role, warehouse_id=False, department_ids=None):
        self._require_admin()
        name = (name or "").strip()
        login = email_normalize(email or "")
        if not name or not login:
            raise ValidationError("Enter a name and a valid email.")
        if role not in _ADMIN_ASSIGNABLE:
            raise ValidationError("Choose Employee, Branch Manager or Admin. Head Office access is granted by Head Office.")
        if self.env["res.users"].sudo().with_context(active_test=False).search_count([("login", "=ilike", escape_like_value(login))], limit=1):
            raise ValidationError("A user with this email already exists.")
        branch = self._branch(warehouse_id)
        if role in ("branch_manager", "employee") and not branch:
            raise ValidationError("A %s needs a branch." % _ROLE_LABELS[role])
        user = self.env["res.users"].sudo().with_context(no_reset_password=True).create({
            "name": name, "login": login, "email": login, "company_id": self.env.company.id, "company_ids": [(6, 0, self.env.company.ids)],
            "group_ids": [(6, 0, [self.env.ref("base.group_user").id, self.env.ref(_ROLE_GROUPS[role]).id])],
            "strategic_branch_warehouse_id": branch.id if branch and role != "admin" else False})
        if department_ids:
            if role != "employee" or not isinstance(department_ids, list) or not all(_valid_id(value) for value in department_ids):
                raise ValidationError("Departments are for Employees only.")
            departments = self.env["strategic.department"].sudo().browse(set(department_ids)).exists()
            user.write({"strategic_department_ids": [(6, 0, departments.ids)]})
        self._audit("create_user", user, branch, "%s, %s" % (_ROLE_LABELS[role], login), notify=True)
        result = self._send_reset(user)
        if result["sent"]:
            self._audit("reset_link", user, detail="Invitation to set a password")
        return {"person": self._person_row(user), "invitation": result}

    @api.model
    def admin_send_reset_link(self, user_id):
        self._require_admin()
        target = self._target(user_id)
        if not target.active:
            raise UserError("Activate %s first." % target.name)
        result = self._send_reset(target)
        if result["sent"]:
            self._audit("reset_link", target, detail="Sent to %s" % target.login)
        return result

    @api.model
    def admin_update_branch(self, warehouse_id, name, code, planned_staff):
        self._require_admin()
        branch = self._branch(warehouse_id)
        if not branch:
            raise ValidationError("Choose a branch.")
        name, code = (name or "").strip(), (code or "").strip().upper()
        if not name or not code:
            raise ValidationError("Enter the branch name and its short code.")
        if len(code) > 5:
            raise ValidationError("The short code has at most 5 characters.")
        if not isinstance(planned_staff, int) or isinstance(planned_staff, bool) or planned_staff < 0:
            raise ValidationError("Planned staff is a whole number, 0 or more.")
        clash = self._branches().filtered(lambda other: other != branch and (other.name == name or other.code == code))
        if clash:
            raise ValidationError("Another branch already uses this name or code.")
        before = (branch.name, branch.code, branch.strategic_planned_headcount)
        branch.sudo().write({"name": name, "code": code, "strategic_planned_headcount": planned_staff})
        after = (branch.name, branch.code, branch.strategic_planned_headcount)
        if after != before:
            labels = ("name", "code", "planned staff")
            self._audit("branch_setup", branch=branch, detail="; ".join("%s %s → %s" % (labels[i], before[i], after[i]) for i in range(3) if before[i] != after[i]))
        return {"branch": next(row for row in self._branch_rows() if row["id"] == branch.id)}
