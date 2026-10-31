"""Phase 9A: operational departments and Head Office's assignment of Employees to them.

A department here is an operational area (Sales, Inventory, Purchasing), not an org-chart unit: hr.department gives each
employee one department. Membership lives on res.users, beside the branch, and is separate from roles and security
groups: an Employee holds no rights on POS, Stock or Purchase records. get_department_operations checks role, employee
record, branch, company and membership on every call, then reads through the existing calculations for the user's own
branch and returns only the fields the operations view selects.
"""

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

_EMPLOYEE = "strategic_management.group_strategic_employee"
_MANAGER = "strategic_management.group_strategic_branch_manager"
_DEPARTMENT_READ = "strategic_department_read"


class StrategicDepartment(models.Model):
    _name = "strategic.department"
    _description = "Strategic Operational Department"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    code = fields.Selection([("sales", "Sales"), ("inventory", "Inventory"), ("purchasing", "Purchasing")], required=True)
    sequence = fields.Integer(default=10)

    _code_unique = models.Constraint("UNIQUE(code)", "Each operational department exists once.")


class HrEmployee(models.Model):
    _inherit = "hr.employee"

    def _strategic_clear_access_cache(self):
        # Direct-access restrictions read the user's employee record, and Odoo caches evaluated rules.
        self.env.transaction.invalidate_ormcache()

    @api.model_create_multi
    def create(self, vals_list):
        employees = super().create(vals_list)
        if any(vals.get("user_id") for vals in vals_list):
            self._strategic_clear_access_cache()
        return employees

    def write(self, vals):
        result = super().write(vals)
        if {"user_id", "active", "company_id"} & set(vals):
            self._strategic_clear_access_cache()
        return result

    def unlink(self):
        result = super().unlink()
        self._strategic_clear_access_cache()
        return result


class StrategicPerformanceDashboardDepartments(models.AbstractModel):
    _inherit = "strategic.performance.dashboard"

    # ------------------------------------------------------------------ employee access (Phase 9B)
    @api.model
    def _department_read_active(self):
        """True only inside get_department_operations: a browser can send the context key but can never obtain sudo."""
        return self.env.su and bool(self.env.context.get(_DEPARTMENT_READ))

    @api.model
    def _employee_access(self):
        """The Employee's own branch and departments, re-read on every call; never taken from the request."""
        user = self._require_employee_dashboard_access().sudo()
        departments = self.env["strategic.department"].sudo()
        employee = self.env["hr.employee"].sudo().search([("user_id", "=", user.id), ("company_id", "=", self.env.company.id)], limit=1)
        if not employee:
            return user.browse(), departments, "Your account has no active employee record in this company. Please contact Head Office."
        branch = user.strategic_branch_warehouse_id
        if not branch or branch.company_id != self.env.company:
            return user.browse(), departments, "Your account is not assigned to a branch in this company. Please contact Head Office."
        return branch, user.strategic_department_ids, False

    @api.model
    def get_employee_context(self):
        """The signed-in Employee's name, branch and departments, for the dashboard and sidebar (no business data)."""
        branch, departments, error = self._employee_access()
        return {
            "name": self.env.user.name,
            "branch": {"id": branch.id, "name": branch.display_name} if branch else False,
            "departments": [{"code": department.code, "name": department.name} for department in departments],
            "access_error": error,
        }

    @api.model
    def get_department_operations(self, department_code, date_start=False, date_end=False, product_id=False):
        """One assigned department's read-only figures for the Employee's own branch; anything else is refused."""
        branch, departments, error = self._employee_access()
        if error:
            raise AccessError(error)
        if department_code not in departments.mapped("code"):
            raise AccessError("You are not assigned to this department.")
        start, end = self._resolve_period(date_start, date_end)
        result = self._operations_header(branch, start, end)
        # sudo: every check above passed, and the methods below pin the branch to this user's own account.
        reader = self.sudo().with_context(**{_DEPARTMENT_READ: True})
        result[department_code] = reader._operations_section({
            "sales": lambda: reader._operations_sales(start, end, result["granularity"]),
            "inventory": lambda: reader._operations_inventory(start, end, branch, product_id),
            "purchasing": lambda: reader._operations_purchasing(start, end),
        }[department_code])
        if department_code == "purchasing" and result["purchasing"].get("available"):
            result["purchasing"]["receipts_done"] = reader._operations_receipts_done(start, end, branch)
        return result

    # ------------------------------------------------------------------ Head Office assignment (Phase 9A)
    @api.model
    def _require_department_admin(self):
        if self._strategic_role() != "head_office":
            raise AccessError("Department assignments are managed by Head Office.")

    @api.model
    def _assignable_department_users(self):
        """Active internal Employees of the active company (managers keep their role's own access)."""
        users = self.env["res.users"].sudo().search([
            ("active", "=", True), ("share", "=", False), ("company_ids", "in", self.env.company.id)], order="name, id")
        return users.filtered(lambda user: user.has_group(_EMPLOYEE) and not user.has_group(_MANAGER))

    @api.model
    def _department_assignment_row(self, user):
        branch = user.strategic_branch_warehouse_id
        valid = branch and branch.company_id == self.env.company
        departments = user.strategic_department_ids
        return {
            "key": "user-%s" % user.id, "id": user.id, "name": user.name, "role": "Employee",
            "branch": branch.display_name if valid else False,
            "department_ids": departments.ids, "departments": departments.mapped("name"),
            # Without a valid branch nothing can be added, but an old assignment can still be removed.
            "can_assign": bool(valid or departments),
            "note": False if valid else "No branch in this company",
        }

    @api.model
    def get_department_assignments(self):
        """The active company's Employees and their departments, plus employees who have no login; Head Office only."""
        self._require_department_admin()
        departments = self.env["strategic.department"].sudo().search([])
        rows = [self._department_assignment_row(user) for user in self._assignable_department_users()]
        # Roles and branches belong to user accounts, so an employee record without one is listed but cannot be assigned.
        without_login = self.env["hr.employee"].sudo().search([("user_id", "=", False), ("company_id", "=", self.env.company.id)], order="name, id")
        rows += [{"key": "employee-%s" % employee.id, "id": False, "name": employee.name, "role": False, "branch": False,
                  "department_ids": [], "departments": [], "can_assign": False, "note": "No login account"} for employee in without_login]
        return {"departments": [{"id": department.id, "name": department.name} for department in departments], "employees": rows}

    @api.model
    def set_employee_departments(self, user_id, department_ids):
        """Replace an Employee's departments; Head Office only. Role, branch, groups and tasks are not touched."""
        self._require_department_admin()
        valid_int = lambda value: isinstance(value, int) and not isinstance(value, bool)
        if not valid_int(user_id) or not isinstance(department_ids, list) or not all(valid_int(value) for value in department_ids):
            raise ValidationError("Choose an employee and valid departments.")
        target = self._assignable_department_users().filtered(lambda user: user.id == user_id)
        if not target:
            raise AccessError("This person is not an Employee of the active company.")
        requested = set(department_ids)
        departments = self.env["strategic.department"].sudo().browse(requested).exists()
        if len(departments) != len(requested):
            raise ValidationError("One of the selected departments does not exist.")
        branch = target.strategic_branch_warehouse_id
        if departments and not (branch and branch.company_id == self.env.company):
            raise UserError("Assign this employee to a branch of the active company first.")
        target.write({"strategic_department_ids": [(6, 0, departments.ids)]})
        return self._department_assignment_row(target)
