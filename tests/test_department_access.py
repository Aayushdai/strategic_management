"""Phase 9A: Head Office assigns Employees to multiple operational departments. Membership grants nothing yet."""

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged

DASHBOARD = "strategic.performance.dashboard"


@tagged("post_install", "-at_install")
class TestDepartmentAssignment(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        ref = cls.env.ref
        Warehouse = cls.env["stock.warehouse"]
        cls.branch1 = Warehouse.create({"name": "DEP Branch 1", "code": "DPB1", "company_id": cls.company.id})
        cls.branch2 = Warehouse.create({"name": "DEP Branch 2", "code": "DPB2", "company_id": cls.company.id})
        cls.other_company = cls.env["res.company"].create({"name": "DEP Other Company"})
        cls.other_branch = Warehouse.sudo().search([("company_id", "=", cls.other_company.id)], limit=1)

        def user(login, group, warehouse=False, company=None):
            company = company or cls.company
            return cls.env["res.users"].with_context(no_reset_password=True).create({
                "name": login, "login": login, "email": "%s@example.com" % login,
                "company_id": company.id, "company_ids": [(6, 0, company.ids)],
                "group_ids": [(6, 0, [ref("base.group_user").id] + ([ref(group).id] if group else []))],
                "strategic_branch_warehouse_id": warehouse.id if warehouse else False})

        employee = "strategic_management.group_strategic_employee"
        manager = "strategic_management.group_strategic_branch_manager"
        cls.head = user("dep_head", "strategic_management.group_strategic_head_office_manager")
        cls.manager1 = user("dep_manager1", manager, cls.branch1)
        cls.manager2 = user("dep_manager2", manager, cls.branch2)
        cls.emp = user("dep_employee", employee, cls.branch1)
        cls.emp2 = user("dep_employee2", employee, cls.branch2)
        cls.no_branch = user("dep_no_branch", employee)
        cls.foreign = user("dep_foreign", employee, cls.other_branch, cls.other_company)
        cls.no_group = user("dep_no_group", False, cls.branch1)
        cls.sales = ref("strategic_management.department_sales")
        cls.inventory = ref("strategic_management.department_inventory")
        cls.purchasing = ref("strategic_management.department_purchasing")
        cls.Dashboard = cls.env[DASHBOARD]

    def _assign(self, target, *departments, by=None):
        return self.Dashboard.with_user(by or self.head).set_employee_departments(target.id, [d.id for d in departments])

    def _rows(self):
        return {row["key"]: row for row in self.Dashboard.with_user(self.head).get_department_assignments()["employees"]}

    # ------------------------------------------------------------------ assigning
    def test_head_office_opens_the_list_with_real_people_and_departments(self):
        data = self.Dashboard.with_user(self.head).get_department_assignments()
        self.assertEqual([row["name"] for row in data["departments"]], ["Sales", "Inventory", "Purchasing"])
        rows = {row["key"]: row for row in data["employees"]}
        row = rows["user-%s" % self.emp.id]
        self.assertEqual((row["name"], row["branch"], row["role"], row["department_ids"]), ("dep_employee", self.branch1.display_name, "Employee", []))
        for excluded in (self.foreign, self.manager1, self.head, self.no_group):
            self.assertNotIn("user-%s" % excluded.id, rows)

    def test_assign_one_many_add_and_remove(self):
        self.assertEqual(self._assign(self.emp, self.sales)["departments"], ["Sales"])
        self.assertEqual(self.emp.strategic_department_ids, self.sales)
        self._assign(self.emp, self.sales, self.inventory, self.purchasing)
        self.assertEqual(self.emp.strategic_department_ids, self.sales | self.inventory | self.purchasing)
        self._assign(self.emp, self.sales, self.purchasing)
        self.assertEqual(self.emp.strategic_department_ids, self.sales | self.purchasing, "the other assignments stay")
        self._assign(self.emp)
        self.assertFalse(self.emp.strategic_department_ids)

    def test_a_department_holds_many_employees_independently(self):
        self._assign(self.emp, self.sales)
        self._assign(self.emp2, self.sales, self.purchasing)
        self._assign(self.emp)
        self.assertEqual(self.emp2.strategic_department_ids, self.sales | self.purchasing)

    def test_duplicates_are_prevented(self):
        saved = self.Dashboard.with_user(self.head).set_employee_departments(self.emp.id, [self.sales.id, self.sales.id])
        self.assertEqual(saved["department_ids"], self.sales.ids)
        self.emp.write({"strategic_department_ids": [(4, self.sales.id)]})
        self.env.cr.execute("SELECT count(*) FROM strategic_department_res_users_rel WHERE user_id = %s", (self.emp.id,))
        self.assertEqual(self.env.cr.fetchone()[0], 1)

    def test_saving_changes_only_the_assignments(self):
        task_row = self.env["strategic.branch.task"].with_user(self.manager1).create_branch_task({
            "name": "DEP task", "description": "Do it.", "assigned_user_id": self.emp.id, "date_deadline": str(fields.Date.today())})
        task = self.env["strategic.branch.task"].browse(task_row["id"])
        task.with_user(self.emp).action_start()
        messages = task.message_ids
        groups, partner = self.emp.group_ids, self.emp.partner_id
        users, employees = self.env["res.users"].search_count([]), self.env["hr.employee"].sudo().search_count([])
        self._assign(self.emp, self.sales, self.inventory, self.purchasing)
        self.assertEqual(self.emp.group_ids, groups, "no Odoo group is granted")
        self.assertEqual(self.Dashboard.with_user(self.emp)._strategic_role(), "employee")
        self.assertEqual(self.emp.strategic_branch_warehouse_id, self.branch1)
        self.assertEqual(self.emp.partner_id, partner)
        self.assertEqual((task.assigned_user_id, task.state, task.message_ids), (self.emp, "in_progress", messages))
        self.assertEqual((users, employees), (self.env["res.users"].search_count([]), self.env["hr.employee"].sudo().search_count([])))

    def test_assignment_grants_no_data_access(self):
        self._assign(self.emp, self.sales, self.inventory, self.purchasing)
        dashboard = self.Dashboard.with_user(self.emp)
        self.assertIs(dashboard.get_business_overview_data()["sales"], False)
        self.assertFalse(dashboard.get_inventory_waste_data()["available"])
        self.assertFalse(dashboard.get_purchasing_overview_data()["available"])
        for model in ("pos.order", "purchase.order", "stock.picking"):
            with self.assertRaises(AccessError, msg=model):
                self.env[model].with_user(self.emp).search_read([], ["id"], limit=1)

    # ------------------------------------------------------------------ authorization
    def test_only_head_office_can_list_or_change(self):
        for actor in (self.emp, self.emp2, self.no_group):
            with self.assertRaises(AccessError):
                self._assign(self.emp, self.sales, by=actor)
            with self.assertRaises(AccessError):
                self.Dashboard.with_user(actor).get_department_assignments()
        self.assertFalse(self.emp.strategic_department_ids)
        with self.assertRaises(AccessError):
            self.Dashboard.with_user(self.manager1).get_department_assignments()
        self.assertEqual(self._assign(self.emp, self.sales, by=self.manager1)["departments"], ["Sales"], "the manager of the Employee's own branch may assign")
        with self.assertRaises(AccessError, msg="never another branch's Employee"):
            self._assign(self.foreign, self.sales, by=self.manager1)

    def test_direct_model_operations_are_refused(self):
        self._assign(self.emp2, self.sales)
        for actor in (self.emp, self.emp2, self.manager1, self.head):
            with self.assertRaises(AccessError):
                self.emp.with_user(actor).write({"strategic_department_ids": [(6, 0, self.sales.ids)]})
            with self.assertRaises(AccessError):
                self.emp2.with_user(actor).read(["strategic_department_ids"])
            with self.assertRaises(AccessError):
                self.sales.with_user(actor).write({"name": "X"})
            with self.assertRaises(AccessError):
                self.env["strategic.department"].with_user(actor).create({"name": "X", "code": "sales"})
            with self.assertRaises(AccessError):
                self.sales.with_user(actor).unlink()
        for actor in (self.emp, self.manager1):
            with self.assertRaises(AccessError):
                self.sales.with_user(actor).read(["name"])

    def test_invalid_identifiers_are_rejected(self):
        dashboard = self.Dashboard.with_user(self.head)
        for user_id, department_ids in ((str(self.emp.id), self.sales.ids), (True, self.sales.ids), (None, self.sales.ids),
                                        (self.emp.id, [str(self.sales.id)]), (self.emp.id, [True]), (self.emp.id, self.sales.id)):
            with self.assertRaises(ValidationError):
                dashboard.set_employee_departments(user_id, department_ids)
        with self.assertRaises(ValidationError):
            dashboard.set_employee_departments(self.emp.id, [self.sales.id, 999999999])
        with self.assertRaises(AccessError):
            dashboard.set_employee_departments(999999999, self.sales.ids)
        self.assertFalse(self.emp.strategic_department_ids, "a partly invalid request changes nothing")

    def test_invalid_organizational_assignments_are_refused(self):
        for target in (self.foreign, self.manager1, self.head, self.no_group):
            with self.assertRaises(AccessError):
                self._assign(target, self.sales)
        with self.assertRaises(UserError):
            self._assign(self.no_branch, self.sales)
        # The same rules hold for an administrator writing the field directly.
        for target in (self.manager1, self.no_branch, self.no_group):
            with self.assertRaises(ValidationError):
                target.write({"strategic_department_ids": [(6, 0, self.sales.ids)]})

    def test_employee_who_lost_their_branch_can_only_have_departments_removed(self):
        self._assign(self.emp, self.sales)
        self.emp.write({"strategic_branch_warehouse_id": False})
        row = self._rows()["user-%s" % self.emp.id]
        self.assertTrue(row["can_assign"])
        self.assertEqual(row["note"], "No branch in this company")
        with self.assertRaises(UserError):
            self._assign(self.emp, self.sales, self.inventory)
        self.assertFalse(self._assign(self.emp)["department_ids"])

    def test_employee_without_login_is_listed_but_not_assignable(self):
        employee = self.env["hr.employee"].sudo().create({"name": "DEP No Login", "company_id": self.company.id})
        users = self.env["res.users"].search_count([])
        row = self._rows()["employee-%s" % employee.id]
        self.assertEqual((row["id"], row["can_assign"], row["note"]), (False, False, "No login account"))
        self.assertEqual(users, self.env["res.users"].search_count([]), "no account is created")

    def test_branch_manager_sees_departments_of_own_employees_only(self):
        self._assign(self.emp, self.sales, self.purchasing)
        self._assign(self.emp2, self.inventory)
        Task = self.env["strategic.branch.task"]
        rows = {row["id"]: row for row in Task.with_user(self.manager1).get_employee_management()["employees"]}
        self.assertEqual(rows[self.emp.id]["departments"], ["Sales", "Purchasing"])
        self.assertNotIn(self.emp2.id, rows)
        with self.assertRaises(AccessError):
            Task.with_user(self.manager1).get_employee_tasks(self.emp2.id)

    # ------------------------------------------------------------------ task workflow fix found while verifying
    def test_client_context_cannot_unlock_task_lifecycle_fields(self):
        row = self.env["strategic.branch.task"].with_user(self.manager1).create_branch_task({
            "name": "DEP task", "description": "Do it.", "assigned_user_id": self.emp.id, "date_deadline": str(fields.Date.today())})
        task = self.env["strategic.branch.task"].browse(row["id"])
        task.with_user(self.emp).action_start()
        task.with_user(self.emp).action_submit("Done it.")
        with self.assertRaises(ValidationError):
            task.with_user(self.emp).with_context(strategic_task_workflow=True).write({"state": "done"})
        self.assertEqual(task.state, "submitted")
        task.with_user(self.manager1).action_accept()
        self.assertEqual(task.state, "done")
