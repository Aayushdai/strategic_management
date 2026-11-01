"""Phase 9C: direct model access of Strategic Employees, with the group combinations real Employees hold (POS User).

Global ir.access restrictions apply to Strategic Employees only; other roles and other internal users keep their access.
"""

from datetime import datetime

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged

PERIOD = ("2001-03-05", "2001-03-11")
DAY = datetime(2001, 3, 6, 10)
DASHBOARD = "strategic.performance.dashboard"


@tagged("post_install", "-at_install")
class TestEmployeeSecurity(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        cls.company.sudo().point_of_sale_update_stock_quantities = "real"
        ref = cls.env.ref
        Warehouse = cls.env["stock.warehouse"]
        cls.branch1 = Warehouse.create({"name": "SEC Branch 1", "code": "SCB1", "company_id": cls.company.id})
        cls.branch2 = Warehouse.create({"name": "SEC Branch 2", "code": "SCB2", "company_id": cls.company.id})
        cls.pos1 = cls.env["pos.config"].create({"name": "SEC POS 1", "picking_type_id": cls.branch1.pos_type_id.id})
        cls.pos2 = cls.env["pos.config"].create({"name": "SEC POS 2", "picking_type_id": cls.branch2.pos_type_id.id})
        cls.pos_user = ref("point_of_sale.group_pos_user")
        cls.stock_user = ref("stock.group_stock_user")
        cls.purchase_user = ref("purchase.group_purchase_user")
        emp, manager = "strategic_management.group_strategic_employee", "strategic_management.group_strategic_branch_manager"

        def user(login, groups=(), warehouse=False, employee=True):
            account = cls.env["res.users"].with_context(no_reset_password=True).create({
                "name": login, "login": login, "email": "%s@example.com" % login, "tz": "UTC",
                "company_id": cls.company.id, "company_ids": [(6, 0, cls.company.ids)],
                "group_ids": [(6, 0, [ref("base.group_user").id] + [group.id for group in groups])],
                "strategic_branch_warehouse_id": warehouse.id if warehouse else False})
            if employee:
                cls.env["hr.employee"].sudo().create({"name": login, "user_id": account.id, "company_id": cls.company.id})
            return account

        emp_group, manager_group = ref(emp), ref(manager)
        cls.head = user("sec_head", [ref("strategic_management.group_strategic_head_office_manager"), cls.pos_user, cls.stock_user])
        cls.manager1 = user("sec_manager1", [manager_group, cls.pos_user], cls.branch1)
        cls.manager_stock = user("sec_manager_stock", [manager_group, cls.stock_user], cls.branch1)
        # The combination real Employees hold, and one without any operational group.
        cls.cashier = user("sec_cashier", [emp_group, cls.pos_user], cls.branch1)
        cls.plain = user("sec_plain", [emp_group], cls.branch1)
        cls.plain2 = user("sec_plain2", [emp_group], cls.branch2)
        cls.warehouse_staff = user("sec_staff", [emp_group, cls.stock_user], cls.branch1)
        cls.no_branch = user("sec_no_branch", [emp_group, cls.pos_user])
        # Not part of Strategic Management: must keep what Odoo gives them.
        cls.other = user("sec_other", [], False, employee=False)
        cls.other_pos = user("sec_other_pos", [cls.pos_user], False, employee=False)
        cls.other_stock = user("sec_other_stock", [cls.stock_user], False, employee=False)
        cls.sales = ref("strategic_management.department_sales")
        cls.inventory = ref("strategic_management.department_inventory")
        cls.purchasing = ref("strategic_management.department_purchasing")
        cls.latte = cls.env["product.template"].create({"name": "SEC Latte", "type": "consu", "available_in_pos": True, "list_price": 4.0}).product_variant_id
        cls.beans = cls.env["product.template"].create({
            "name": "SEC Beans", "is_storable": True, "purchase_ok": True, "available_in_pos": True, "standard_price": 20.0, "list_price": 30.0}).product_variant_id
        cls.vendor = cls.env["res.partner"].create({"name": "SEC Roaster", "supplier_rank": 1})
        Quant = cls.env["stock.quant"].sudo()
        Quant._update_available_quantity(cls.beans, cls.branch1.lot_stock_id, 12.0)
        Quant._update_available_quantity(cls.beans, cls.branch2.lot_stock_id, 99.0)
        cls.quant1 = Quant.search([("product_id", "=", cls.beans.id), ("location_id", "=", cls.branch1.lot_stock_id.id)])
        cls.quant2 = Quant.search([("product_id", "=", cls.beans.id), ("location_id", "=", cls.branch2.lot_stock_id.id)])
        cls.order1 = cls._sell(cls.pos1, 30.0)
        cls.order2 = cls._sell(cls.pos2, 700.0)
        cls.po1 = cls._purchase(cls.branch1)
        cls.po2 = cls._purchase(cls.branch2)
        cls.Dashboard = cls.env[DASHBOARD]
        cls.Task = cls.env["strategic.branch.task"]

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
    def _purchase(cls, branch):
        order = cls.env["purchase.order"].create({
            "partner_id": cls.vendor.id, "company_id": cls.company.id, "picking_type_id": branch.in_type_id.id,
            "order_line": [(0, 0, {"name": "Beans", "product_id": cls.beans.id, "product_qty": 5, "price_unit": 20.0, "date_planned": DAY})],
        })
        order.button_confirm()
        return order

    def _assign(self, target, *departments):
        self.Dashboard.with_user(self.head).set_employee_departments(target.id, [d.id for d in departments])

    def _quants(self, user):
        return self.env["stock.quant"].with_user(user).search([("product_id", "=", self.beans.id)])

    # ------------------------------------------------------------------ stock.quant by department (all read APIs)
    def test_quant_access_by_department(self):
        for user in (self.plain, self.cashier):
            for departments, visible in (
                ((), False), ((self.sales,), False), ((self.purchasing,), False), ((self.sales, self.purchasing), False),
                ((self.inventory,), True), ((self.sales, self.inventory), True), ((self.inventory, self.purchasing), True),
                ((self.sales, self.inventory, self.purchasing), True),
            ):
                self._assign(user, *departments)
                expected = self.quant1 if visible else self.quant1.browse()
                self.assertEqual(self._quants(user), expected, "%s %s" % (user.login, [d.code for d in departments]))

    def test_every_read_api_obeys_the_restriction(self):
        self._assign(self.plain, self.inventory)
        Quant = self.env["stock.quant"].with_user(self.plain)
        domain = [("product_id", "=", self.beans.id)]
        self.assertEqual([row["id"] for row in Quant.search_read(domain, ["id"])], self.quant1.ids)
        self.assertEqual(Quant.search_count(domain), 1)
        self.assertEqual(Quant._read_group(domain, [], ["quantity:sum"])[0][0], 12.0)
        self.assertEqual(Quant.formatted_read_group(domain, [], ["quantity:sum"])[0]["quantity:sum"], 12.0)
        self.assertEqual(self.quant1.with_user(self.plain).read(["quantity"])[0]["quantity"], 12.0)
        with self.assertRaises(AccessError):
            self.quant2.with_user(self.plain).read(["quantity"])
        self.assertFalse(self.env["stock.quant"].with_user(self.plain).browse(self.quant2.id).has_access("read"))
        self.assertNotIn(self.quant2.id, [row[0] for row in Quant.name_search("", domain, limit=50)])
        self._assign(self.plain, self.sales)
        self.assertEqual(Quant.search_count(domain), 0)
        self.assertEqual(Quant._read_group(domain, [], ["quantity:sum"])[0][0], 0)
        with self.assertRaises(AccessError):
            self.quant1.with_user(self.plain).read(["quantity"])

    def test_other_stock_models_follow_the_department_and_branch(self):
        move_lines = self.env["stock.move.line"].sudo().create([{
            "product_id": self.beans.id, "location_id": self.env.ref("stock.stock_location_suppliers").id, "location_dest_id": branch.lot_stock_id.id,
            "quantity": 1, "company_id": self.company.id, "state": "draft"} for branch in (self.branch1, self.branch2)])
        Line = self.env["stock.move.line"].with_user(self.plain)
        scope = [("id", "in", move_lines.ids)]
        self.assertEqual(Line.search_count(scope), 0, "no Inventory department")
        self._assign(self.plain, self.inventory)
        self.assertEqual(Line.search(scope), move_lines[0], "own branch only")
        for action in (lambda: move_lines[0].with_user(self.plain).write({"lot_name": "x"}),
                       lambda: move_lines[0].with_user(self.plain).unlink(),
                       lambda: Line.create({"product_id": self.beans.id, "location_id": self.branch1.lot_stock_id.id,
                                            "location_dest_id": self.branch1.wh_input_stock_loc_id.id, "quantity": 1, "company_id": self.company.id})):
            with self.assertRaises(AccessError):
                action()
        for model in ("stock.move", "stock.picking", "stock.lot", "stock.warehouse.orderpoint"):
            if model in ("stock.move", "stock.picking"):
                self.assertFalse(self.env[model].with_user(self.cashier).search_count([("id", "in", (self.po1.picking_ids.ids if model == "stock.picking" else self.po1.picking_ids.move_ids.ids))]),
                                 "%s: a cashier does not read receipts" % model)
            else:
                with self.assertRaises(AccessError, msg=model):
                    self.env[model].with_user(self.plain).search_read([], ["id"], limit=1)

    def test_product_stock_figures_do_not_leak_other_branches(self):
        self.assertEqual(self.beans.with_user(self.cashier).qty_available, 0, "no Inventory department")
        self.env.invalidate_all()
        self._assign(self.cashier, self.inventory)
        self.assertEqual(self.beans.with_user(self.cashier).qty_available, 12.0, "own branch only")

    def test_stock_report_follows_the_department(self):
        Report = self.env["report.stock.quantity"].with_user(self.plain)
        self.assertEqual(Report.search_count([("warehouse_id", "=", self.branch2.id)]), 0)
        self._assign(self.plain, self.inventory)
        self.assertEqual(Report.search_count([("warehouse_id", "=", self.branch2.id)]), 0)

    # ------------------------------------------------------------------ other roles keep their access
    def test_head_office_keeps_organization_wide_access(self):
        self.assertEqual(self._quants(self.head), self.quant1 | self.quant2)
        self.assertEqual(self.env["pos.order"].with_user(self.head).search([("id", "in", (self.order1 | self.order2).ids)]), self.order1 | self.order2)

    def test_branch_manager_access_is_limited_to_their_branch(self):
        self.assertEqual(self._quants(self.manager_stock), self.quant1, "Phase 9D: own branch only")
        self.assertEqual(self.env["pos.order"].with_user(self.manager1).search([("id", "in", (self.order1 | self.order2).ids)]), self.order1)
        self.quant1.with_user(self.manager_stock).write({"inventory_quantity": 12.0})
        with self.assertRaises(AccessError):
            self.quant2.with_user(self.manager_stock).write({"inventory_quantity": 1.0})

    def test_other_internal_users_keep_their_access(self):
        self.assertEqual(self._quants(self.other), self.quant1 | self.quant2)
        self.assertEqual(self._quants(self.other_stock), self.quant1 | self.quant2)
        self.assertEqual(self.env["pos.order"].with_user(self.other_pos).search_count([("id", "in", (self.order1 | self.order2).ids)]), 2)
        picking = self.po2.picking_ids
        self.assertEqual(self.env["stock.picking"].with_user(self.other_stock).search_count([("id", "=", picking.id)]), 1)
        self.assertTrue(self.env["stock.move.line"].with_user(self.other).has_access("write"))

    def test_extra_stock_group_cannot_widen_an_employees_reach(self):
        self.assertFalse(self._quants(self.warehouse_staff), "Inventory / User without the department")
        self._assign(self.warehouse_staff, self.inventory)
        self.assertEqual(self._quants(self.warehouse_staff), self.quant1)
        self.assertFalse(self.env["stock.picking"].with_user(self.warehouse_staff).search_count([("id", "in", self.po2.picking_ids.ids)]), "other branch")
        self.assertEqual(self.env["stock.picking"].with_user(self.warehouse_staff).search(([("id", "in", self.po1.picking_ids.ids)])), self.po1.picking_ids)

    # ------------------------------------------------------------------ POS with the real group combination
    def test_pos_user_employee_sees_only_their_branch(self):
        for model, own, other in (("pos.order", self.order1, self.order2), ("pos.session", self.order1.session_id, self.order2.session_id),
                                  ("pos.config", self.pos1, self.pos2), ("pos.order.line", self.order1.lines, self.order2.lines)):
            Model = self.env[model].with_user(self.cashier)
            self.assertEqual(Model.search([("id", "in", (own | other).ids)]), own, model)
            with self.assertRaises(AccessError, msg=model):
                other.with_user(self.cashier).read(["id"])
        with self.assertRaises(AccessError):
            self.order2.with_user(self.cashier).write({"general_customer_note": "x"})
        self.assertEqual(self.env["report.pos.order"].with_user(self.cashier).search_count([("order_id", "=", self.order2.id)]), 0)

    def test_employee_without_branch_or_pos_group_reads_no_pos_records(self):
        self.assertEqual(self.env["pos.order"].with_user(self.no_branch).search_count([]), 0)
        with self.assertRaises(AccessError):
            self.env["pos.order"].with_user(self.plain).search_read([], ["id"], limit=1)
        self._assign(self.plain, self.sales)
        with self.assertRaises(AccessError):
            self.env["pos.order"].with_user(self.plain).search_read([], ["id"], limit=1)
        self.assertEqual(self.Dashboard.with_user(self.plain).get_department_operations("sales", *PERIOD)["sales"]["orders"], 1,
                         "the department shows figures, not records")

    def test_cashier_can_still_sell_and_stock_is_reduced(self):
        session = self.env["pos.session"].search([("config_id", "=", self.pos1.id), ("state", "!=", "closed")], limit=1)
        method = self.pos1.payment_method_ids[:1] or self.env["pos.payment.method"].search([("company_id", "=", self.company.id), ("journal_id.type", "=", "cash")], limit=1)
        result = self.env["pos.order"].with_user(self.cashier).sync_from_ui([{
            "name": "SEC cashier sale", "uuid": "sec-cashier-sale", "session_id": session.id, "company_id": self.company.id, "state": "paid",
            "pricelist_id": False, "partner_id": False, "user_id": self.cashier.id, "to_invoice": False, "sequence_number": 901,
            "date_order": fields.Datetime.to_string(fields.Datetime.now()), "fiscal_position_id": False,
            "amount_tax": 0.0, "amount_total": 30.0, "amount_paid": 30.0, "amount_return": 0.0,
            "lines": [[0, 0, {"product_id": self.beans.id, "qty": 2, "price_unit": 15.0, "price_subtotal": 30.0, "price_subtotal_incl": 30.0,
                              "discount": 0, "full_product_name": self.beans.name, "tax_ids": [[6, 0, []]], "uuid": "sec-line"}]],
            "payment_ids": [[0, 0, {"amount": 30.0, "payment_date": fields.Datetime.to_string(fields.Datetime.now()), "payment_method_id": method.id}]],
        }])
        order = self.env["pos.order"].browse(result["pos.order"][0]["id"])
        self.assertEqual(order.state, "paid")
        self.assertEqual(self.quant1.quantity, 10.0, "the sale's picking ran as the cashier")
        self.assertEqual(self.quant2.quantity, 99.0)

    # ------------------------------------------------------------------ read-only operations
    def test_cashier_cannot_touch_receipts_or_transfers(self):
        receipt = self.po1.picking_ids
        for user in (self.cashier, self.plain):
            for action in (lambda: receipt.with_user(user).button_validate(),
                           lambda: receipt.with_user(user).write({"note": "x"}),
                           lambda: receipt.move_ids.with_user(user).write({"quantity": 5}),
                           lambda: receipt.with_user(user).unlink(),
                           lambda: self.env["stock.picking"].with_user(user).create({
                               "picking_type_id": self.branch1.in_type_id.id, "location_id": self.env.ref("stock.stock_location_suppliers").id,
                               "location_dest_id": self.branch1.lot_stock_id.id})):
                with self.assertRaises(AccessError, msg=user.login):
                    action()
        self.assertEqual(receipt.state, "assigned" if receipt.state == "assigned" else receipt.state)
        self.assertNotEqual(receipt.state, "done")

    def test_inventory_member_cannot_adjust_stock(self):
        self._assign(self.cashier, self.inventory)
        self._assign(self.plain, self.inventory)
        for user in (self.cashier, self.plain):
            with self.assertRaises(AccessError):
                self.quant1.with_user(user).write({"inventory_quantity": 1})
            with self.assertRaises(AccessError):
                self.env["stock.quant"].with_user(user).create({"product_id": self.beans.id, "location_id": self.branch1.lot_stock_id.id, "inventory_quantity": 5})
            with self.assertRaises(AccessError):
                self.env["stock.warehouse.orderpoint"].with_user(user).create({
                    "product_id": self.beans.id, "warehouse_id": self.branch1.id, "location_id": self.branch1.lot_stock_id.id, "product_min_qty": 1, "product_max_qty": 2})

    def test_purchasing_member_cannot_change_purchasing(self):
        self._assign(self.cashier, self.purchasing)
        for action in (lambda: self.env["purchase.order"].with_user(self.cashier).create({"partner_id": self.vendor.id}),
                       lambda: self.po1.with_user(self.cashier).write({"note": "x"}),
                       lambda: self.po1.with_user(self.cashier).button_cancel(),
                       lambda: self.vendor.with_user(self.cashier).write({"supplier_rank": 9})):
            with self.assertRaises(AccessError):
                action()
        for model in ("purchase.order", "purchase.order.line"):
            with self.assertRaises(AccessError):
                self.env[model].with_user(self.cashier).search_read([], ["id"], limit=1)

    # ------------------------------------------------------------------ revocation and transfer, without any restart
    def test_revocation_applies_to_the_next_request_for_each_department(self):
        self._assign(self.plain, self.sales, self.inventory, self.purchasing)
        dashboard = self.Dashboard.with_user(self.plain)
        self.assertEqual(self._quants(self.plain), self.quant1)
        for remove, code, rest in ((self.inventory, "inventory", (self.sales, self.purchasing)),
                                   (self.sales, "sales", (self.inventory, self.purchasing)),
                                   (self.purchasing, "purchasing", (self.sales, self.inventory))):
            self._assign(self.plain, self.sales, self.inventory, self.purchasing)
            self.assertTrue(dashboard.get_department_operations(code, *PERIOD))
            self._assign(self.plain, *rest)
            with self.assertRaises(AccessError):
                dashboard.get_department_operations(code, *PERIOD)
            if code == "inventory":
                self.assertFalse(self._quants(self.plain), "the cached rule was cleared")
                with self.assertRaises(AccessError):
                    self.quant1.with_user(self.plain).read(["quantity"])
        self._assign(self.plain)
        self.assertEqual(self.Task.with_user(self.plain).get_my_tasks()["tasks"], [], "My Tasks keeps working")

    def test_branch_transfer_changes_direct_access_at_once(self):
        self._assign(self.plain, self.inventory, self.sales)
        task = self.Task.browse(self.Task.with_user(self.manager1).create_branch_task({
            "name": "SEC task", "description": "Do it.", "assigned_user_id": self.plain.id, "date_deadline": str(fields.Date.today())})["id"])
        self.assertEqual(self._quants(self.plain), self.quant1)
        self.plain.write({"strategic_branch_warehouse_id": self.branch2.id})
        self.assertEqual(self._quants(self.plain), self.quant2, "new branch at once")
        with self.assertRaises(AccessError):
            self.quant1.with_user(self.plain).read(["quantity"])
        self.assertEqual(self.Dashboard.with_user(self.plain).get_department_operations("sales", *PERIOD)["sales"]["net_sales"], 700.0)
        self.assertEqual(self.Task.with_user(self.plain).get_my_tasks()["tasks"][0]["id"], task.id, "task history stays")
        self.assertEqual(self._quants(self.head), self.quant1 | self.quant2, "Head Office oversight stays")
        self.plain.write({"strategic_branch_warehouse_id": False})
        self.assertFalse(self._quants(self.plain))

    def test_archiving_the_employee_record_closes_direct_access(self):
        self._assign(self.plain, self.inventory)
        self.assertEqual(self._quants(self.plain), self.quant1)
        self.env["hr.employee"].sudo().search([("user_id", "=", self.plain.id)]).write({"active": False})
        self.assertFalse(self._quants(self.plain))

    # ------------------------------------------------------------------ menus and management operations
    def _menu_names(self, user):
        """Top-bar entries under the Strategic Management app, as the web client loads them."""
        menus = self.env["ir.ui.menu"].with_user(user).load_menus(False)
        root = self.env.ref("strategic_management.menu_strategic_management_root").id
        return {menus[child]["name"] for child in menus[root]["children"]}

    def test_employee_has_no_management_menus(self):
        for user in (self.plain, self.cashier):
            self.assertEqual(self._menu_names(user), set(), user.login)
            self.assertIn(self.env.ref("strategic_management.menu_strategic_management_root").id, self.env["ir.ui.menu"].with_user(user).load_menus(False))

    def test_head_office_and_manager_keep_their_menus(self):
        expected = {"Business Goals", "Objectives"}
        self.assertLessEqual(expected, self._menu_names(self.head))
        self.assertLessEqual(expected, self._menu_names(self.manager1))

    def test_direct_management_actions_are_refused(self):
        # A client action is only a screen name and cannot be restricted by group; what it loads is what must be refused.
        with self.assertRaises(AccessError):
            self.env["strategic.goal.setup"].with_user(self.plain).get_goals()
        for model in ("strategic.objective", "strategic.plan", "strategic.goal.setup", "strategic.branch.report", "strategic.department"):
            with self.assertRaises(AccessError, msg=model):
                self.env[model].with_user(self.plain).search_read([], ["id"], limit=1)
        for model in ("strategic.kpi.target", "strategic.kpi.definition"):
            with self.assertRaises(AccessError, msg=model):
                self.env[model].with_user(self.plain).create({"name": "x"})

    def test_report_and_objective_operations_are_refused(self):
        dashboard = self.Dashboard.with_user(self.cashier)
        Report = self.env["strategic.branch.report"].with_user(self.cashier)
        # This one answers an Employee with an empty, unavailable result rather than an error: nothing is disclosed.
        requests = dashboard.get_objective_requests()
        self.assertEqual((requests["available"], requests["can_decide"], requests["pending"], requests["history"]), (False, False, [], []))
        plan = self.env["strategic.plan"].with_user(self.head).create({
            "name": "SEC Plan", "company_id": self.company.id, "date_start": "2000-01-01", "date_end": "2099-12-31"})
        parent = self.env["strategic.objective"].with_user(self.head).create({
            "name": "SEC Company goal", "plan_id": plan.id, "perspective": "internal_process", "scope": "organization",
            "deadline": "2099-12-31", "owner_id": self.head.id})
        parent.with_user(self.head).action_activate()
        goal = self.env["strategic.objective"].with_user(self.head).create({
            "name": "SEC Branch goal", "plan_id": plan.id, "perspective": "internal_process", "scope": "branch",
            "warehouse_id": self.branch1.id, "parent_objective_id": parent.id, "deadline": "2099-06-30", "owner_id": self.head.id})
        for action in (lambda: dashboard.decide_objective_request(goal.id, "approve"),
                       lambda: dashboard.decide_objective_request(goal.id, "reject", "No."),
                       lambda: dashboard.get_branch_operations(*PERIOD),
                       lambda: dashboard.get_branch_objective_summary(),
                       lambda: dashboard.get_department_assignments(),
                       lambda: dashboard.set_employee_departments(self.cashier.id, self.inventory.ids),
                       lambda: Report.create_branch_report(*PERIOD),
                       lambda: Report.get_branch_reports(),
                       lambda: self.Task.with_user(self.cashier).get_task_management(),
                       lambda: self.Task.with_user(self.cashier).get_employee_management()):
            with self.assertRaises(AccessError):
                action()

    # ------------------------------------------------------------------ attachments
    def test_attachments_of_unreadable_records_stay_protected(self):
        Attachment = self.env["ir.attachment"].sudo()
        on_purchase = Attachment.create({"name": "sec-po.txt", "raw": b"x", "res_model": "purchase.order", "res_id": self.po1.id})
        on_receipt = Attachment.create({"name": "sec-receipt.txt", "raw": b"x", "res_model": "stock.picking", "res_id": self.po1.picking_ids[:1].id})
        on_other_pos = Attachment.create({"name": "sec-pos.txt", "raw": b"x", "res_model": "pos.order", "res_id": self.order2.id})
        for attachment in (on_purchase, on_receipt, on_other_pos):
            with self.assertRaises(AccessError, msg=attachment.name):
                attachment.with_user(self.cashier).read(["name"])
            with self.assertRaises(AccessError, msg=attachment.name):
                attachment.with_user(self.cashier).unlink()
        names = Attachment.with_user(self.cashier).search([("name", "like", "sec-")]).mapped("name")
        self.assertFalse(set(names) & {"sec-po.txt", "sec-receipt.txt", "sec-pos.txt"})
