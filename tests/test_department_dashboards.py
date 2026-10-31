"""Phase 9B: read-only department views for Employees, from the same calculations as the Branch Manager's.

Fixtures live in their own branches and an old, otherwise empty period (2001), so other data does not change the results.
"""

from datetime import datetime

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, tagged

PERIOD = ("2001-03-05", "2001-03-11")
DAY = datetime(2001, 3, 6, 10)
CODES = ("sales", "inventory", "purchasing")


@tagged("post_install", "-at_install")
class TestDepartmentDashboards(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        ref = cls.env.ref
        Warehouse = cls.env["stock.warehouse"]
        cls.branch1 = Warehouse.create({"name": "DD Branch 1", "code": "DDB1", "company_id": cls.company.id})
        cls.branch2 = Warehouse.create({"name": "DD Branch 2", "code": "DDB2", "company_id": cls.company.id})
        cls.pos1 = cls.env["pos.config"].create({"name": "DD POS 1", "picking_type_id": cls.branch1.pos_type_id.id})
        cls.pos2 = cls.env["pos.config"].create({"name": "DD POS 2", "picking_type_id": cls.branch2.pos_type_id.id})
        cls.other_company = cls.env["res.company"].create({"name": "DD Other Company"})
        cls.other_branch = Warehouse.sudo().search([("company_id", "=", cls.other_company.id)], limit=1)
        apps = [ref(xmlid).id for xmlid in ("stock.group_stock_user", "purchase.group_purchase_user", "point_of_sale.group_pos_user")]

        def user(login, group, warehouse=False, extra=(), employee=True):
            account = cls.env["res.users"].with_context(no_reset_password=True).create({
                "name": login, "login": login, "email": "%s@example.com" % login, "tz": "UTC",
                "company_id": cls.company.id, "company_ids": [(6, 0, cls.company.ids)],
                "group_ids": [(6, 0, [ref("base.group_user").id, ref(group).id] + list(extra))],
                "strategic_branch_warehouse_id": warehouse.id if warehouse else False})
            if employee:
                cls.env["hr.employee"].sudo().create({"name": login, "user_id": account.id, "company_id": cls.company.id})
            return account

        emp, manager = "strategic_management.group_strategic_employee", "strategic_management.group_strategic_branch_manager"
        cls.head = user("dd_head", "strategic_management.group_strategic_head_office_manager", extra=apps)
        cls.manager1 = user("dd_manager1", manager, cls.branch1, apps)
        cls.emp = user("dd_employee", emp, cls.branch1)
        cls.emp2 = user("dd_employee2", emp, cls.branch2)
        cls.no_record = user("dd_no_record", emp, cls.branch1, employee=False)
        cls.no_branch = user("dd_no_branch", emp)
        cls.sales = ref("strategic_management.department_sales")
        cls.inventory = ref("strategic_management.department_inventory")
        cls.purchasing = ref("strategic_management.department_purchasing")
        cls.kg = ref("uom.product_uom_kgm")
        cls.latte = cls.env["product.template"].create({"name": "DD Latte", "type": "consu", "available_in_pos": True, "list_price": 4.0}).product_variant_id
        cls.beans = cls.env["product.template"].create({
            "name": "DD Beans", "is_storable": True, "purchase_ok": True, "standard_price": 20.0, "uom_id": cls.kg.id}).product_variant_id
        cls.vendor = cls.env["res.partner"].create({"name": "DD Roaster", "supplier_rank": 1})
        cls.Dashboard = cls.env["strategic.performance.dashboard"]
        cls.Task = cls.env["strategic.branch.task"]
        cls._sell(cls.pos1, 30.0)
        cls._sell(cls.pos2, 700.0)
        cls.env["stock.quant"].sudo()._update_available_quantity(cls.beans, cls.branch1.lot_stock_id, 12.0)
        cls.env["stock.quant"].sudo()._update_available_quantity(cls.beans, cls.branch2.lot_stock_id, 99.0)
        cls.order1 = cls._purchase(cls.branch1, 10, 4)
        cls.order2 = cls._purchase(cls.branch2, 50, 0)

    @classmethod
    def _sell(cls, pos, total):
        session = cls.env["pos.session"].search([("config_id", "=", pos.id), ("state", "!=", "closed")], limit=1) \
            or cls.env["pos.session"].create({"config_id": pos.id, "user_id": cls.env.uid})
        return cls.env["pos.order"].create({
            "session_id": session.id, "company_id": cls.company.id, "state": "paid", "date_order": DAY,
            "amount_tax": 0.0, "amount_total": total, "amount_paid": total, "amount_return": 0.0,
            "lines": [(0, 0, {"product_id": cls.latte.id, "qty": 1, "price_unit": total, "price_subtotal": total,
                              "price_subtotal_incl": total, "full_product_name": cls.latte.name})],
        })

    @classmethod
    def _purchase(cls, branch, ordered, received):
        order = cls.env["purchase.order"].create({
            "partner_id": cls.vendor.id, "company_id": cls.company.id, "picking_type_id": branch.in_type_id.id,
            "order_line": [(0, 0, {"name": "Beans", "product_id": cls.beans.id, "product_qty": ordered, "price_unit": 20.0,
                                   "uom_id": cls.kg.id, "date_planned": DAY})],
        })
        order.button_confirm()
        order.write({"date_approve": DAY})
        if received:
            picking = order.picking_ids
            picking.move_ids.quantity = received
            picking.move_ids.picked = True
            picking.with_context(cancel_backorder=False)._action_done()
            picking.write({"date_done": DAY})
        return order

    def _assign(self, target, *departments):
        self.Dashboard.with_user(self.head).set_employee_departments(target.id, [d.id for d in departments])

    def _read(self, code, user=None, period=PERIOD):
        return self.Dashboard.with_user(user or self.emp).get_department_operations(code, *period)

    def _context(self, user=None):
        return self.Dashboard.with_user(user or self.emp).get_employee_context()

    def _codes(self, user=None):
        return [row["code"] for row in self._context(user)["departments"]]

    def _all(self, user=None):
        self._assign(user or self.emp, self.sales, self.inventory, self.purchasing)

    # ------------------------------------------------------------------ department access
    def test_each_single_department_opens_only_itself(self):
        for department in (self.sales, self.inventory, self.purchasing):
            self._assign(self.emp, department)
            self.assertEqual(self._codes(), [department.code])
            result = self._read(department.code)
            self.assertTrue(result[department.code]["available"], department.code)
            for code in set(CODES) - {department.code}:
                self.assertNotIn(code, result)
                with self.assertRaises(AccessError):
                    self._read(code)

    def test_two_and_three_departments_work_independently(self):
        self._assign(self.emp, self.sales, self.inventory)
        self.assertEqual(self._codes(), ["sales", "inventory"])
        self.assertTrue(self._read("sales")["sales"]["available"] and self._read("inventory")["inventory"]["available"])
        with self.assertRaises(AccessError):
            self._read("purchasing")
        self._all()
        self.assertEqual(self._codes(), list(CODES))
        for code in CODES:
            self.assertTrue(self._read(code)[code]["available"], code)

    def test_no_department_means_no_data_but_tasks_still_work(self):
        context = self._context()
        self.assertEqual((context["departments"], context["access_error"], context["name"]), ([], False, "dd_employee"))
        self.assertEqual(context["branch"]["id"], self.branch1.id)
        for code in CODES + ("finance", "", False, None, ["sales"], 1):
            with self.assertRaises(AccessError):
                self._read(code)
        self.assertEqual(self.Task.with_user(self.emp).get_my_tasks()["tasks"], [])

    def test_add_and_remove_take_effect_on_the_next_request(self):
        self._assign(self.emp, self.sales)
        with self.assertRaises(AccessError):
            self._read("inventory")
        self._assign(self.emp, self.sales, self.inventory)
        self.assertTrue(self._read("inventory")["inventory"]["available"])
        self._assign(self.emp, self.sales)
        self.assertEqual(self._codes(), ["sales"])
        with self.assertRaises(AccessError):
            self._read("inventory")

    # ------------------------------------------------------------------ data correctness
    def _manager_ops(self, period=PERIOD):
        return self.Dashboard.with_user(self.manager1).get_branch_operations(*period)

    def test_figures_match_the_branch_managers_exactly(self):
        self._all()
        manager = self._manager_ops()
        for code in CODES:
            mine = dict(self._read(code)[code])
            if code == "purchasing":
                self.assertEqual(mine.pop("receipts_done"), manager["inventory"]["receipts_done"])
            self.assertEqual(mine, manager[code], code)
        sales = self._read("sales")["sales"]
        self.assertEqual((sales["net_sales"], sales["orders"], sales["average_order_value"]), (30.0, 1, 30.0))
        self.assertEqual(sales["currency"]["name"], self.company.currency_id.name)

    def test_units_and_received_quantities(self):
        self._all()
        inventory = self._read("inventory")["inventory"]
        level = next(row for row in inventory["stock_levels"] if row["product_id"] == self.beans.id)
        self.assertEqual((level["on_hand"], level["unit"]), (16.0, self.env["uom.uom"].browse(self.kg.id).name), "12 in stock plus 4 received")
        purchasing = self._read("purchasing")["purchasing"]
        line = next(line for order in purchasing["deliveries"] if order["name"] == self.order1.name for line in order["lines"])
        self.assertEqual((line["ordered"], line["received"], line["unit"]), (10.0, 4.0, self.env["uom.uom"].browse(self.kg.id).name))
        self.assertEqual(purchasing["receipts_done"], 1)
        self.assertEqual(purchasing["confirmed_count"], 1)

    def test_no_orders_is_not_a_zero_average(self):
        self._assign(self.emp, self.sales)
        sales = self._read("sales", period=("2001-06-01", "2001-06-02"))["sales"]
        self.assertEqual((sales["net_sales"], sales["orders"]), (0.0, 0))
        self.assertIs(sales["average_order_value"], False)

    def test_period_figures_move_but_current_balances_do_not(self):
        self._all()
        later = ("2001-06-01", "2001-06-02")
        self.assertEqual(self._read("sales")["sales"]["orders"], 1)
        self.assertEqual(self._read("sales", period=later)["sales"]["orders"], 0)
        now, then = self._read("purchasing")["purchasing"], self._read("purchasing", period=later)["purchasing"]
        self.assertEqual((now["confirmed_count"], then["confirmed_count"]), (1, 0), "period figure")
        self.assertEqual((now["receipts_done"], then["receipts_done"]), (1, 0), "period figure")
        self.assertEqual(now["awaiting_count"], then["awaiting_count"], "open deliveries are current obligations")
        self.assertEqual(self._read("inventory")["inventory"]["stock_levels"], self._read("inventory", period=later)["inventory"]["stock_levels"])

    def test_movement_product_is_limited_to_the_branch(self):
        self._assign(self.emp, self.inventory)
        dashboard = self.Dashboard.with_user(self.emp)
        section = dashboard.get_department_operations("inventory", *PERIOD, self.beans.id)["inventory"]
        self.assertEqual(section["selected_product_id"], self.beans.id)
        self.assertEqual(section["movement"]["unit"], self.env["uom.uom"].browse(self.kg.id).name)
        # An unknown product falls back to the method's own default instead of reading it.
        self.assertNotEqual(dashboard.get_department_operations("inventory", *PERIOD, 999999999)["inventory"]["selected_product_id"], 999999999)

    # ------------------------------------------------------------------ branch and identity
    def test_cross_branch_data_is_never_returned(self):
        self._all()
        self._all(self.emp2)
        self.assertEqual(self._read("sales", self.emp2)["sales"]["net_sales"], 700.0)
        self.assertEqual(self._read("sales")["sales"]["net_sales"], 30.0)
        mine = [row["name"] for row in self._read("purchasing")["purchasing"]["deliveries"]]
        self.assertIn(self.order1.name, mine)
        self.assertNotIn(self.order2.name, mine)
        levels = {row["product_id"]: row["on_hand"] for row in self._read("inventory")["inventory"]["stock_levels"]}
        self.assertEqual(levels[self.beans.id], 16.0, "not branch 2's 99")
        dashboard = self.Dashboard.with_user(self.emp)
        with self.assertRaises(AccessError):
            self._read(self.branch2.id)
        forged = dashboard.with_context(warehouse_id=self.branch2.id, strategic_branch=self.branch2.id)
        self.assertEqual(forged.get_department_operations("sales", *PERIOD)["sales"]["net_sales"], 30.0)

    def test_branch_transfer_moves_access_with_the_account(self):
        self._assign(self.emp, self.sales)
        self.assertEqual(self._read("sales")["sales"]["net_sales"], 30.0)
        self.emp.write({"strategic_branch_warehouse_id": self.branch2.id})
        result = self._read("sales")
        self.assertEqual((result["branch"]["id"], result["sales"]["net_sales"]), (self.branch2.id, 700.0))
        self.assertEqual(self._codes(), ["sales"], "membership is unchanged")

    def test_losing_branch_or_employee_record_closes_data_but_not_tasks(self):
        self._assign(self.emp, self.sales)
        self.emp.write({"strategic_branch_warehouse_id": False})
        self.assertEqual(self._codes(), [])
        self.assertIn("not assigned to a branch", self._context()["access_error"])
        with self.assertRaises(AccessError):
            self._read("sales")
        self.emp.write({"strategic_branch_warehouse_id": self.other_branch.id})
        with self.assertRaises(AccessError):
            self._read("sales")
        self.emp.write({"strategic_branch_warehouse_id": self.branch1.id})
        self.assertTrue(self._read("sales"))
        self.env["hr.employee"].sudo().search([("user_id", "=", self.emp.id)]).write({"active": False})
        with self.assertRaises(AccessError):
            self._read("sales")
        self.assertIn("no active employee record", self._context()["access_error"])
        self.assertEqual(self.Task.with_user(self.emp).get_my_tasks()["tasks"], [])

    def test_user_without_employee_record_is_refused(self):
        self._assign(self.no_record, self.sales)
        self.assertEqual(self._codes(self.no_record), [])
        with self.assertRaises(AccessError):
            self._read("sales", self.no_record)

    def test_managers_and_head_office_are_refused(self):
        for actor in (self.head, self.manager1):
            with self.assertRaises(AccessError):
                self._read("sales", actor)
            with self.assertRaises(AccessError):
                self._context(actor)

    # ------------------------------------------------------------------ read-only
    def test_client_context_cannot_open_the_elevated_read(self):
        forged = self.Dashboard.with_user(self.emp).with_context(strategic_department_read=True)
        self.assertFalse(forged._department_read_active())
        self.assertIs(forged.get_business_overview_data(*PERIOD)["sales"], False)
        self.assertFalse(forged.get_inventory_waste_data(*PERIOD)["available"])
        self.assertFalse(forged.get_purchasing_overview_data(*PERIOD)["available"])

    def test_existing_data_methods_stay_closed_to_members(self):
        self._all()
        dashboard = self.Dashboard.with_user(self.emp)
        self.assertIs(dashboard.get_business_overview_data(*PERIOD)["sales"], False)
        self.assertFalse(dashboard.get_inventory_waste_data(*PERIOD)["available"])
        self.assertFalse(dashboard.get_purchasing_overview_data(*PERIOD)["available"])
        with self.assertRaises(AccessError):
            dashboard.get_branch_operations(*PERIOD)

    def test_groups_and_direct_model_access_are_unchanged(self):
        groups = self.emp.group_ids
        self._all()
        self.assertEqual(self.emp.group_ids, groups)
        # stock.quant is not listed: Odoo itself lets every internal user read it, with or without a department.
        for model in ("pos.order", "pos.config", "pos.payment", "stock.picking", "stock.move", "stock.warehouse.orderpoint",
                      "stock.lot", "purchase.order", "purchase.order.line"):
            with self.assertRaises(AccessError, msg=model):
                self.env[model].with_user(self.emp).search_read([], ["id"], limit=1)

    def test_members_cannot_change_business_records(self):
        self._all()
        as_emp = lambda record: record.with_user(self.emp)
        order = self.env["pos.order"].search([("config_id", "=", self.pos1.id)], limit=1)
        picking = self.order1.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))[:1] or self.order2.picking_ids[:1]
        draft = self.env["purchase.order"].create({"partner_id": self.vendor.id})
        for action in (
            lambda: as_emp(order).write({"general_customer_note": "x"}),
            lambda: as_emp(order).unlink(),
            lambda: as_emp(self.pos1).write({"name": "x"}),
            lambda: self.env["stock.quant"].with_user(self.emp).create({
                "product_id": self.beans.id, "location_id": self.branch1.lot_stock_id.id, "inventory_quantity": 5}),
            lambda: as_emp(picking).button_validate(),
            lambda: as_emp(picking).write({"note": "x"}),
            lambda: self.env["stock.warehouse.orderpoint"].with_user(self.emp).create({
                "product_id": self.beans.id, "warehouse_id": self.branch1.id, "location_id": self.branch1.lot_stock_id.id,
                "product_min_qty": 1, "product_max_qty": 2}),
            lambda: self.env["purchase.order"].with_user(self.emp).create({"partner_id": self.vendor.id}),
            lambda: as_emp(self.order1).write({"note": "x"}),
            lambda: as_emp(self.order1).button_cancel(),
            lambda: as_emp(draft).button_confirm(),
            lambda: as_emp(self.vendor).write({"supplier_rank": 9}),
            lambda: self.Dashboard.with_user(self.emp).set_employee_departments(self.emp.id, []),
            lambda: as_emp(self.emp).write({"strategic_department_ids": [(5,)]}),
            lambda: as_emp(self.emp).read(["strategic_department_ids"]),
        ):
            with self.assertRaises(AccessError):
                action()

    def test_reading_writes_nothing(self):
        self._all()
        models = ("pos.order", "stock.move", "purchase.order", "stock.picking", "stock.quant")
        before = {model: self.env[model].sudo().search_count([]) for model in models}
        for code in CODES:
            self._read(code)
        self.assertEqual(before, {model: self.env[model].sudo().search_count([]) for model in models})

    # ------------------------------------------------------------------ tasks
    def _task(self, assignee=None):
        row = self.Task.with_user(self.manager1).create_branch_task({
            "name": "DD task", "description": "Do it.", "assigned_user_id": (assignee or self.emp).id,
            "date_deadline": str(fields.Date.today())})
        return self.Task.browse(row["id"])

    def test_task_workflow_needs_no_department(self):
        task = self._task()
        rows = self.Task.with_user(self.emp).get_my_tasks()["tasks"]
        self.assertEqual([(row["id"], row["description"], row["priority_label"]) for row in rows],
                         [(task.id, "Do it.", task._row()["priority_label"])])
        task.with_user(self.emp).action_start()
        task.with_user(self.emp).action_submit("Done it.")
        self.assertEqual(task.state, "submitted")
        task.with_user(self.manager1).action_request_changes("Once more.")
        task.with_user(self.emp).action_start()
        task.with_user(self.emp).action_submit("Again.")
        with self.assertRaises(AccessError):
            task.with_user(self.emp).action_accept()
        with self.assertRaises(AccessError):
            task.with_user(self.emp).action_cancel()
        with self.assertRaises(ValidationError):
            task.with_user(self.emp).write({"assigned_user_id": self.emp2.id})
        with self.assertRaises(ValidationError):
            task.with_user(self.emp).with_context(strategic_task_workflow=True).write({"state": "done"})
        with self.assertRaises(AccessError):
            self.Task.with_user(self.emp).create_branch_task({
                "name": "Self", "description": "x", "assigned_user_id": self.emp.id, "date_deadline": str(fields.Date.today())})
        task.with_user(self.manager1).action_accept()
        self.assertEqual(task.state, "done")

    def test_department_changes_leave_tasks_alone(self):
        self._assign(self.emp, self.sales)
        task = self._task()
        self._assign(self.emp)
        self.assertEqual(task.assigned_user_id, self.emp)
        self.assertEqual([row["id"] for row in self.Task.with_user(self.emp).get_my_tasks()["tasks"]], [task.id])

    def test_head_office_can_still_assign_to_a_branch_manager(self):
        task = self.Task.with_user(self.head).create({
            "name": "DD for manager", "description": "Do it.", "assigned_user_id": self.manager1.id, "date_deadline": str(fields.Date.today())})
        self.assertEqual(task.assigned_user_id, self.manager1)
