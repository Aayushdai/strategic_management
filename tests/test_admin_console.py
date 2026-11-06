"""Admin console: the Admin runs people, access and branches, and never receives sensitive data."""

import json
from datetime import date, timedelta

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.strategic_management.models.admin_console import ADMIN_CONSOLE_METHODS

ADMIN = "strategic_management.group_strategic_admin"
HEAD = "strategic_management.group_strategic_head_office_manager"
MANAGER = "strategic_management.group_strategic_branch_manager"
EMPLOYEE = "strategic_management.group_strategic_employee"
SECRETS = ("SECRET-TASK-DESCRIPTION", "SECRET-REPORT-COMMENT", "SECRET-STAFFING-REASON", "SECRET-PRIVATE-PHONE", "SECRET-COMPLETION-NOTE")
# Keys that would carry money, personal details, ratings or notes; none may appear anywhere in the console's data.
FORBIDDEN_KEYS = {"amount", "price", "cost", "sales", "revenue", "salary", "wage", "phone", "private_phone", "address", "rating", "ratings",
                  "score", "reason", "note", "notes", "comment", "description", "target_value", "attainment", "actual_value", "email_private"}


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


@tagged("post_install", "-at_install")
class TestAdminConsole(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ref = cls.env.ref
        cls.company = cls.env.company
        cls.branch = cls.env["stock.warehouse"].create({"name": "ACN Branch", "code": "ACNB", "company_id": cls.company.id, "strategic_planned_headcount": 4})
        cls.branch2 = cls.env["stock.warehouse"].create({"name": "ACN Branch 2", "code": "ACNC", "company_id": cls.company.id})

        def user(login, group, branch=False):
            account = cls.env["res.users"].with_context(no_reset_password=True).create({
                "name": login, "login": login, "company_id": cls.company.id, "company_ids": [(6, 0, cls.company.ids)],
                "group_ids": [(6, 0, [ref("base.group_user").id, ref(group).id])], "strategic_branch_warehouse_id": branch.id if branch else False})
            cls.env["hr.employee"].create({"name": login, "user_id": account.id, "company_id": cls.company.id,
                                           "strategic_branch_warehouse_id": branch.id if branch else False, "private_phone": "SECRET-PRIVATE-PHONE"})
            return account

        cls.head = user("acn_head", HEAD)
        cls.manager = user("acn_manager", MANAGER, cls.branch)
        cls.emp = user("acn_emp", EMPLOYEE, cls.branch)
        cls.admin = user("acn_admin", ADMIN)
        cls.admin2 = user("acn_admin2", ADMIN)
        cls.Console = cls.env["strategic.admin.console"]
        cls.sales = cls.env["strategic.department"].search([("code", "=", "sales")], limit=1)
        Task = cls.env["strategic.branch.task"]
        row = Task.with_user(cls.manager).create_branch_task({
            "name": "ACN visible task", "description": "SECRET-TASK-DESCRIPTION", "assigned_user_id": cls.emp.id,
            "date_deadline": str(date.today() + timedelta(days=3)), "priority": "0"})
        Task.browse(row["id"]).sudo().write({"completion_note": "SECRET-COMPLETION-NOTE"})
        cls.env["strategic.branch.report"].sudo().create({
            "name": "ACN report", "warehouse_id": cls.branch.id, "company_id": cls.company.id, "date_start": date.today() - timedelta(days=7),
            "date_end": date.today(), "comment": "SECRET-REPORT-COMMENT"})
        cls.env["strategic.staffing.request"].sudo().create({
            "warehouse_id": cls.branch.id, "company_id": cls.company.id, "requested_by_id": cls.manager.id, "department_id": cls.sales.id, "position": "Barista", "quantity": 2,
            "reason": "SECRET-STAFFING-REASON", "expected_date": date.today() + timedelta(days=10)})

    def _as(self, user=None):
        return self.Console.with_user(user or self.admin)

    def _all_reads(self):
        console = self._as()
        data = [console.get_admin_overview(), console.get_admin_people(), console.get_admin_branches(), console.get_admin_health(), console.get_admin_audit()]
        data += [console.get_admin_work(kind) for kind in ("objectives", "goals", "tasks", "reports", "staffing")]
        return data

    # ------------------------------------------------------------------ who may use it
    def test_only_the_admin_role_uses_the_console(self):
        for actor in (self.head, self.manager, self.emp):
            for method in sorted(ADMIN_CONSOLE_METHODS):
                args = {"get_admin_work": ["tasks"], "admin_set_user_access": [self.emp.id, "employee", self.branch.id],
                        "admin_set_user_active": [self.emp.id, False], "admin_create_user": ["X", "x@example.com", "employee", self.branch.id],
                        "admin_send_reset_link": [self.emp.id], "admin_update_branch": [self.branch.id, "N", "NN", 1]}.get(method, [])
                with self.assertRaises(AccessError, msg="%s %s" % (actor.login, method)):
                    getattr(self._as(actor), method)(*args)

    # ------------------------------------------------------------------ nothing sensitive
    def test_console_never_returns_sensitive_data(self):
        data = self._all_reads()
        text = json.dumps(data, default=str)
        for secret in SECRETS:
            self.assertNotIn(secret, text, secret)
        leaked = FORBIDDEN_KEYS & set(_keys(data))
        self.assertFalse(leaked, "sensitive keys in the Admin's data: %s" % leaked)
        tasks = self._as().get_admin_work("tasks")["rows"]
        self.assertIn("ACN visible task", [row["title"] for row in tasks], "the work record itself is visible")
        staffing = self._as().get_admin_work("staffing")["rows"]
        self.assertIn("2 × Barista", [row["title"] for row in staffing])

    def test_admin_still_cannot_open_the_records_directly(self):
        for model in ("strategic.branch.task", "strategic.branch.report", "strategic.staffing.request", "strategic.admin.audit"):
            try:
                count = self.env[model].with_user(self.admin).search_count([])
            except AccessError:
                continue
            self.assertEqual(count, 0, model)

    # ------------------------------------------------------------------ people and access
    def test_admin_changes_role_branch_and_departments_with_audit_and_head_office_todo(self):
        row = self._as().admin_set_user_access(self.emp.id, "employee", self.branch2.id, self.sales.ids)
        self.assertEqual((row["role"], row["branch_id"], row["department_ids"]), ("employee", self.branch2.id, self.sales.ids))
        row = self._as().admin_set_user_access(self.emp.id, "branch_manager", self.branch2.id, [])
        self.assertTrue(self.emp.has_group(MANAGER))
        self.assertFalse(self.emp.sudo().strategic_department_ids, "departments are cleared away from Employee")
        audit = self.env["strategic.admin.audit"].search([("target_user_id", "=", self.emp.id)])
        self.assertEqual(set(audit.mapped("action")), {"branch", "departments", "role"})
        role_entry = audit.filtered(lambda entry: entry.action == "role")
        self.assertTrue(role_entry.activity_ids, "Head Office gets a To-Do for a role grant")
        self.assertIn(self.head, role_entry.activity_ids.user_id)
        self.assertEqual(self.env["strategic.admin.audit"].with_user(self.head).search_count([("id", "in", audit.ids)]), len(audit), "Head Office reads the log")

    def test_admin_guardrails(self):
        with self.assertRaises(AccessError, msg="Head Office accounts"):
            self._as().admin_set_user_access(self.head.id, "employee", self.branch.id)
        with self.assertRaises(ValidationError, msg="no Head Office grants"):
            self._as().admin_set_user_access(self.emp.id, "head_office")
        with self.assertRaises(ValidationError, msg="own account"):
            self._as().admin_set_user_active(self.admin.id, False)
        with self.assertRaises(ValidationError, msg="branch needed"):
            self._as().admin_set_user_access(self.emp.id, "branch_manager", False)
        with self.assertRaises(ValidationError, msg="departments are for Employees"):
            self._as().admin_set_user_access(self.emp.id, "branch_manager", self.branch.id, self.sales.ids)
        with self.assertRaises(ValidationError, msg="branch of another company or nonsense"):
            self._as().admin_set_user_access(self.emp.id, "employee", 999999)
        with self.assertRaises(ValidationError):
            self._as().admin_create_user("Head", "acn.head2@example.com", "head_office")
        people = {row["id"]: row for row in self._as().get_admin_people()["people"]}
        self.assertTrue(people[self.head.id]["locked"])
        self.assertTrue(people[self.admin.id]["locked"])
        self.assertFalse(people[self.admin2.id]["locked"], "another Admin is managed by the Admin")
        self.assertNotIn("head_office", [role["id"] for role in self._as().get_admin_people()["roles"]])

    def test_deactivate_and_reactivate(self):
        self.assertFalse(self._as().admin_set_user_active(self.emp.id, False)["active"])
        self.assertFalse(self.emp.active)
        self.assertTrue(self._as().admin_set_user_active(self.emp.id, True)["active"])
        actions = self.env["strategic.admin.audit"].search([("target_user_id", "=", self.emp.id)]).mapped("action")
        self.assertEqual(sorted(actions), ["activate", "deactivate"])

    def test_create_user_with_role_branch_and_audit(self):
        result = self._as().admin_create_user("New Barista", "acn.new@example.com", "employee", self.branch.id, self.sales.ids)
        person = result["person"]
        user = self.env["res.users"].browse(person["id"])
        self.assertEqual((user.login, person["role"], user.strategic_branch_warehouse_id), ("acn.new@example.com", "employee", self.branch))
        self.assertEqual(user.sudo().strategic_department_ids, self.sales)
        self.assertIn("sent", result["invitation"])
        self.assertTrue(self.env["strategic.admin.audit"].search([("target_user_id", "=", user.id), ("action", "=", "create_user")]))
        with self.assertRaises(ValidationError, msg="duplicate email"):
            self._as().admin_create_user("Again", "ACN.NEW@example.com", "employee", self.branch.id)

    # ------------------------------------------------------------------ branches and health
    def test_branch_setup_and_staffing_view(self):
        rows = {row["id"]: row for row in self._as().get_admin_branches()["branches"]}
        self.assertEqual((rows[self.branch.id]["planned"], rows[self.branch.id]["managers"]), (4, ["acn_manager"]))
        self.assertEqual(rows[self.branch.id]["open_requests"], 0, "a draft request is not open")
        updated = self._as().admin_update_branch(self.branch.id, "ACN Renamed", "acnr", 6)["branch"]
        self.assertEqual((updated["name"], updated["code"], updated["planned"]), ("ACN Renamed", "ACNR", 6))
        with self.assertRaises(ValidationError):
            self._as().admin_update_branch(self.branch.id, "ACN Branch 2", "ZZZ", 1)
        with self.assertRaises(ValidationError):
            self._as().admin_update_branch(self.branch.id, "X", "TOOLONG", 1)
        self.assertTrue(self.env["strategic.admin.audit"].search([("warehouse_id", "=", self.branch.id), ("action", "=", "branch_setup")]))

    def test_health_lists_setup_problems(self):
        self.manager.sudo().strategic_branch_warehouse_id = False
        titles = [issue["title"] for issue in self._as().get_admin_health()["issues"]]
        self.assertIn("Users without a branch", titles)
        self.assertIn("Branches without a Branch Manager", titles)
