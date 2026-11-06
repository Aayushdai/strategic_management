"""The Admin role: assignment through People, and no operational data by any route."""

import inspect

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, tagged

ADMIN = "strategic_management.group_strategic_admin"
HEAD = "strategic_management.group_strategic_head_office_manager"
MANAGER = "strategic_management.group_strategic_branch_manager"
EMPLOYEE = "strategic_management.group_strategic_employee"
OPERATIONAL_MODELS = (
    "pos.order", "pos.order.line", "pos.session", "pos.config", "pos.payment", "report.pos.order",
    "stock.quant", "stock.picking", "stock.move", "stock.move.line", "stock.warehouse", "stock.lot", "report.stock.quantity",
    "purchase.order", "purchase.order.line", "product.supplierinfo", "account.move", "sale.order", "sale.order.line", "sale.report",
    "strategic.branch.task", "strategic.kpi.evaluation", "strategic.kpi.target", "strategic.staffing.request", "strategic.employee.assessment",
)


@tagged("post_install", "-at_install")
class TestAdminRole(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ref = cls.env.ref
        cls.company = cls.env.company
        cls.branch = cls.env["stock.warehouse"].create({"name": "ADM Branch", "code": "ADMB", "company_id": cls.company.id})
        cls.other_branch = cls.env["stock.warehouse"].create({"name": "ADM Branch 2", "code": "ADMC", "company_id": cls.company.id})

        def user(login, group, branch=False, extra=()):
            account = cls.env["res.users"].with_context(no_reset_password=True).create({
                "name": login, "login": login, "company_id": cls.company.id, "company_ids": [(6, 0, cls.company.ids)],
                "group_ids": [(6, 0, [ref("base.group_user").id, ref(group).id, *[ref(xmlid).id for xmlid in extra]])],
                "strategic_branch_warehouse_id": branch.id if branch else False})
            cls.env["hr.employee"].create({"name": login, "user_id": account.id, "company_id": cls.company.id})
            return account

        cls.head = user("adm_head", HEAD)
        cls.manager = user("adm_manager", MANAGER, cls.branch)
        cls.emp = user("adm_emp", EMPLOYEE, cls.branch)
        cls.admin = user("adm_admin", ADMIN)
        cls.Dashboard = cls.env["strategic.performance.dashboard"]
        cls.sales = cls.env["strategic.department"].search([("code", "=", "sales")])

    def _set(self, target, role, branch=False, by=None):
        return self.Dashboard.with_user(by or self.head).set_user_role(target.id, role, branch.id if branch else False)

    # ------------------------------------------------------------------ assignment
    def test_head_office_assigns_and_changes_every_role(self):
        self.assertEqual(self._set(self.emp, "admin")["role"], "admin")
        self.assertTrue(self.emp.has_group(ADMIN))
        self.assertFalse(self.emp.has_group(EMPLOYEE), "exactly one role")
        self.assertEqual(self._set(self.emp, "branch_manager", self.other_branch)["role"], "branch_manager")
        self.assertEqual(self.emp.strategic_branch_warehouse_id, self.other_branch)
        self.assertFalse(self.emp.has_group(ADMIN))
        self.assertEqual(self._set(self.emp, "head_office")["role"], "head_office")
        self.assertEqual(self._set(self.emp, "employee", self.branch)["role"], "employee", "demotion")
        self.assertFalse(self.emp.has_group(MANAGER))
        rows = {row["id"]: row for row in self.Dashboard.with_user(self.head).get_role_assignments()["people"]}
        self.assertEqual((rows[self.admin.id]["role_label"], rows[self.manager.id]["role_label"]), ("Admin", "Branch Manager"))
        self.assertFalse(rows[self.head.id]["can_change"])

    def test_only_head_office_changes_roles_and_input_is_checked(self):
        for actor in (self.manager, self.emp, self.admin):
            with self.assertRaises(AccessError, msg=actor.login):
                self._set(self.emp, "admin", by=actor)
            with self.assertRaises(AccessError, msg=actor.login):
                self.Dashboard.with_user(actor).get_role_assignments()
        with self.assertRaises(ValidationError):
            self._set(self.emp, "system_administrator")
        with self.assertRaises(ValidationError, msg="not your own role"):
            self._set(self.head, "employee")
        with self.assertRaises(ValidationError, msg="a manager needs a branch"):
            self._set(self.admin, "branch_manager")
        system = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "adm_sys", "login": "adm_sys", "company_id": self.company.id, "company_ids": [(6, 0, self.company.ids)], "group_ids": [(6, 0, [self.env.ref("base.group_system").id])]})
        with self.assertRaisesRegex(ValidationError, "system administrator"):
            self._set(system, "admin")

    def test_admin_is_not_an_odoo_administrator(self):
        for xmlid in ("base.group_system", "base.group_erp_manager", EMPLOYEE, MANAGER, HEAD, "stock.group_stock_user", "point_of_sale.group_pos_user", "purchase.group_purchase_user"):
            self.assertFalse(self.admin.has_group(xmlid), xmlid)
        self.assertEqual(self.Dashboard.with_user(self.admin).get_navigation_context()["role"], "admin")

    def test_role_change_keeps_departments(self):
        self.Dashboard.with_user(self.head).set_employee_departments(self.emp.id, self.sales.ids)
        self._set(self.emp, "admin")
        self.assertEqual(self.emp.strategic_department_ids, self.sales, "not silently removed")
        with self.assertRaises(AccessError, msg="but they give an Admin nothing"):
            self.Dashboard.with_user(self.emp).get_department_operations("sales")
        self._set(self.emp, "employee")
        self.assertEqual(self.emp.strategic_department_ids, self.sales)
        self.assertIn("sales", self.Dashboard.with_user(self.emp).get_department_operations("sales"))

    def test_becoming_admin_drops_operational_app_rights(self):
        pos = self.env.ref("point_of_sale.group_pos_user")
        self.emp.group_ids = [(4, pos.id)]
        self._set(self.emp, "admin")
        self.assertFalse(self.emp.has_group("point_of_sale.group_pos_user"))
        self.assertFalse(self.emp.has_group("base.group_system"))

    # ------------------------------------------------------------------ app rights across role changes
    def test_app_rights_return_exactly_when_leaving_admin(self):
        ref = self.env.ref
        pos, stock = ref("point_of_sale.group_pos_user"), ref("stock.group_stock_manager")
        self.emp.group_ids = [(4, pos.id), (4, stock.id)]
        before = self.emp.group_ids - ref(EMPLOYEE)
        self._set(self.emp, "admin")
        self.assertFalse(self.emp.has_group("point_of_sale.group_pos_user"))
        self.assertFalse(self.emp.has_group("stock.group_stock_user"), "implied rights go too")
        self.assertEqual(self.emp.group_ids & (pos | stock), self.env["res.groups"])
        self.assertEqual(self.env["stock.quant"].with_user(self.emp).search_count([]), 0)
        self._set(self.emp, "admin")
        self.assertEqual(self.emp.strategic_admin_saved_group_ids, pos | stock, "saving Admin again must not lose them")
        self._set(self.emp, "employee")
        self.assertEqual(self.emp.group_ids - ref(EMPLOYEE), before, "exactly what they had")
        self.assertTrue(self.emp.has_group("point_of_sale.group_pos_user"))
        self.assertTrue(self.emp.has_group("stock.group_stock_user"), "implied rights return with their group")
        self.assertFalse(self.emp.strategic_admin_saved_group_ids)
        self.assertFalse(self.emp.has_group(ADMIN))

    def _grant_while_admin(self):
        """POS before Admin; Sales and Stock granted by hand while Admin."""
        ref = self.env.ref
        self.emp.group_ids = [(4, ref("point_of_sale.group_pos_user").id)]
        self._set(self.emp, "admin")
        self.emp.group_ids = [(4, ref("sales_team.group_sale_manager").id), (4, ref("stock.group_stock_user").id)]
        self.assertTrue(self.emp.has_group("sales_team.group_sale_manager"), "the grant itself is possible")
        self.assertEqual(self.env["sale.order"].with_user(self.emp).search_count([]), 0, "and gives an Admin nothing")
        self.assertEqual(self.env["stock.quant"].with_user(self.emp).search_count([]), 0)

    def _assert_only_saved_rights_returned(self):
        self.assertFalse(self.emp.has_group(ADMIN))
        self.assertTrue(self.emp.has_group("point_of_sale.group_pos_user"), "what they had returns")
        self.assertFalse(self.emp.has_group("sales_team.group_sale_manager"), "what was granted meanwhile does not")
        self.assertFalse(self.emp.has_group("sales_team.group_sale_salesman"), "nor anything it implied")
        self.assertFalse(self.emp.has_group("stock.group_stock_user"))
        self.assertFalse(self.emp.strategic_admin_saved_group_ids)

    def test_rights_granted_while_admin_are_discarded_through_people(self):
        self._grant_while_admin()
        self.assertEqual(self.emp.strategic_admin_saved_group_ids, self.env.ref("point_of_sale.group_pos_user"))
        self._set(self.emp, "employee")
        self._assert_only_saved_rights_returned()

    def test_saving_admin_again_does_not_absorb_manual_grants(self):
        self._grant_while_admin()
        self._set(self.emp, "admin")
        self.assertEqual(self.emp.strategic_admin_saved_group_ids, self.env.ref("point_of_sale.group_pos_user"))
        self._set(self.emp, "employee")
        self._assert_only_saved_rights_returned()

    def test_odoo_user_settings_cannot_bypass_the_restoration(self):
        ref = self.env.ref
        self._grant_while_admin()
        self.emp.write({"group_ids": [(3, ref(ADMIN).id), (4, ref(EMPLOYEE).id)]})
        self._assert_only_saved_rights_returned()
        # And replacing every group at once, as a form can.
        self.emp.write({"group_ids": [(6, 0, [ref("base.group_user").id, ref(ADMIN).id])]})
        self.assertEqual(self.emp.strategic_admin_saved_group_ids, ref("point_of_sale.group_pos_user"), "entering by this path saves too")
        self.assertFalse(self.emp.has_group("point_of_sale.group_pos_user"))
        self.emp.write({"group_ids": [(6, 0, [ref("base.group_user").id, ref(EMPLOYEE).id, ref("stock.group_stock_manager").id])]})
        self.assertTrue(self.emp.has_group("point_of_sale.group_pos_user"))
        self.assertFalse(self.emp.has_group("stock.group_stock_manager"), "a right added while leaving is not saved either")

    def test_group_form_cannot_bypass_the_restoration(self):
        ref = self.env.ref
        self._grant_while_admin()
        self.env["res.groups"].browse(ref(ADMIN).id).write({"user_ids": [(3, self.emp.id)]})
        self.emp.group_ids = [(4, ref(EMPLOYEE).id)]
        self._assert_only_saved_rights_returned()
        ref(ADMIN).write({"user_ids": [(4, self.emp.id)]})
        self.assertEqual(self.emp.strategic_admin_saved_group_ids, ref("point_of_sale.group_pos_user"))
        self.assertFalse(self.emp.has_group("point_of_sale.group_pos_user"))

    def test_a_new_admin_account_starts_with_no_operational_rights(self):
        ref = self.env.ref
        user = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "adm_new", "login": "adm_new", "company_id": self.company.id, "company_ids": [(6, 0, self.company.ids)],
            "group_ids": [(6, 0, [ref("base.group_user").id, ref(ADMIN).id, ref("point_of_sale.group_pos_user").id])]})
        self.assertFalse(user.has_group("point_of_sale.group_pos_user"))
        self._set(user, "employee")
        self.assertFalse(user.has_group("point_of_sale.group_pos_user"), "it was never theirs before Admin")

    def test_other_writes_and_other_roles_are_untouched(self):
        ref = self.env.ref
        self.emp.group_ids = [(4, ref("point_of_sale.group_pos_user").id)]
        self.emp.write({"group_ids": [(4, ref("stock.group_stock_user").id)]})
        self.assertTrue(self.emp.has_group("stock.group_stock_user"), "granting rights to a non-Admin still works")
        self.emp.name = "adm_emp renamed"
        self.assertFalse(self.emp.strategic_admin_saved_group_ids)

    def test_leaving_admin_grants_nothing_new_and_second_round_is_clean(self):
        self._set(self.manager, "admin")
        self._set(self.manager, "branch_manager")
        self.assertFalse(self.manager.has_group("point_of_sale.group_pos_user"), "never had it, so it is not given")
        self.assertFalse(self.manager.has_group("stock.group_stock_user"))
        pos = self.env.ref("point_of_sale.group_pos_user")
        self.emp.group_ids = [(4, pos.id)]
        self._set(self.emp, "admin")
        self._set(self.emp, "employee")
        self.emp.group_ids = [(3, pos.id)]
        self._set(self.emp, "admin")
        self._set(self.emp, "employee")
        self.assertFalse(self.emp.has_group("point_of_sale.group_pos_user"), "an old saved right does not come back")

    def test_moving_between_other_roles_leaves_app_rights_alone(self):
        pos = self.env.ref("point_of_sale.group_pos_user")
        self.emp.group_ids = [(4, pos.id)]
        self._set(self.emp, "branch_manager", self.branch)
        self.assertTrue(self.emp.has_group("point_of_sale.group_pos_user"))
        self._set(self.emp, "employee")
        self.assertTrue(self.emp.has_group("point_of_sale.group_pos_user"))

    # ------------------------------------------------------------------ sales orders
    def test_admin_cannot_reach_sales_orders_even_with_sales_rights(self):
        partner = self.env["res.partner"].create({"name": "ADM Customer"})
        product = self.env["product.product"].create({"name": "ADM Product", "sale_ok": True})
        order = self.env["sale.order"].create({"partner_id": partner.id, "order_line": [(0, 0, {"product_id": product.id, "product_uom_qty": 1})]})
        self.admin.group_ids = [(4, self.env.ref("sales_team.group_sale_manager").id)]
        self.assertTrue(self.admin.has_group("sales_team.group_sale_manager"), "the worst case: granted by hand")
        for model, record in (("sale.order", order), ("sale.order.line", order.order_line)):
            As = self.env[model].with_user(self.admin)
            self.assertEqual(As.search_count([("id", "in", record.ids)]), 0, model)
            with self.assertRaises(AccessError, msg=model):
                record.with_user(self.admin).read(["id"])
            with self.assertRaises(AccessError, msg=model):
                record.with_user(self.admin).check_access("write")
        with self.assertRaises(AccessError):
            self.env["sale.order"].with_user(self.admin).create({"partner_id": partner.id})
        self.assertEqual(self.env["sale.report"].with_user(self.admin).search_count([]), 0)
        # The same right works for anyone who is not Admin.
        self.emp.group_ids = [(4, self.env.ref("sales_team.group_sale_manager").id)]
        self.env.flush_all()
        self.assertEqual(self.env["sale.order"].with_user(self.emp).search_count([("id", "=", order.id)]), 1)

    # ------------------------------------------------------------------ no data by any route
    def test_every_data_method_refuses_admin(self):
        # The Admin's own tools: the Admin console (test_admin_console) and the Admin's assigned KPI/OKR requests
        # (test_kpi_work_request); both are checked there to return nothing sensitive.
        from odoo.addons.strategic_management.models.admin_console import ADMIN_CONSOLE_METHODS
        allowed = {"get_navigation_context", "get_my_work_requests"} | ADMIN_CONSOLE_METHODS
        leaks, checked = [], []
        Models = [name for name in self.env.registry if name.startswith("strategic.")]
        for name in Models:
            Model = self.env[name].with_user(self.admin)
            for method in dir(type(Model)):
                function = getattr(type(Model), method, None)
                # Only this module's own methods: Odoo's generic helpers return no business data.
                dashboard_method = name == "strategic.performance.dashboard" and not method.startswith("_")
                if not (method.startswith("get_") or dashboard_method) or method in allowed or not callable(function) or "strategic_management" not in (getattr(function, "__module__", "") or ""):
                    continue
                checked.append(method)
                required = [p for p in inspect.signature(getattr(type(Model), method)).parameters.values() if p.name != "self" and p.default is p.empty and p.kind == p.POSITIONAL_OR_KEYWORD]
                try:
                    getattr(Model, method)(*[1 for _ in required])
                except AccessError:
                    continue
                except Exception:  # noqa: BLE001  refused or failed: either way nothing was returned
                    continue
                leaks.append("%s.%s" % (name, method))
        self.assertGreater(len(checked), 30, "the sweep must really cover the module's data methods")
        self.assertEqual(leaks, [])

    def test_operational_records_are_refused_directly(self):
        pos = self.env.ref("point_of_sale.group_pos_user")
        stock = self.env.ref("stock.group_stock_user")
        purchase = self.env.ref("purchase.group_purchase_user")
        # Even if someone later gives the Admin an app right by hand.
        self.admin.group_ids = [(4, pos.id), (4, stock.id), (4, purchase.id)]
        for model in OPERATIONAL_MODELS:
            if model in self.env:
                try:
                    count = self.env[model].with_user(self.admin).search_count([])
                except AccessError:
                    continue
                self.assertEqual(count, 0, model)
        employees = self.env["hr.employee.public"].with_user(self.admin).search([])
        self.assertEqual(employees.mapped("name"), ["adm_admin"])

    # ------------------------------------------------------------------ other roles
    def test_other_roles_keep_their_access(self):
        self.assertTrue(self.env["stock.warehouse"].with_user(self.head).search_count([("id", "=", self.branch.id)]))
        self.assertEqual(self.Dashboard.with_user(self.manager).get_navigation_context()["branch"]["id"], self.branch.id)
        self.assertEqual(self.Dashboard.with_user(self.head)._strategic_role(), "head_office")
        self.assertEqual(self.Dashboard.with_user(self.emp)._strategic_role(), "employee")
        self.assertTrue(self.Dashboard.with_user(self.head).get_head_office_data())
        self.assertTrue(self.Dashboard.with_user(self.manager).get_branch_data())

    def test_strategy_menu_is_off_the_top_bar_but_its_content_stays(self):
        menu = self.env.ref("strategic_management.menu_strategic_management_technical")
        self.assertFalse(menu.active)
        for user in (self.head, self.manager):
            names = [row["name"] for row in self.env["ir.ui.menu"].with_user(user).load_menus(False).values() if isinstance(row, dict)]
            self.assertNotIn("Strategy & KPIs", names)
            self.assertIn("Objectives", names)
        self.assertTrue(self.env.ref("strategic_management.action_strategic_kpi_definition"))
        self.assertTrue(self.env.ref("strategic_management.menu_strategic_kpi_definition").exists())
