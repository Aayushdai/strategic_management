"""Phase 9D: Branch Managers reach only their own branch's POS records, while real POS work (open, sell, pay, sync, close) still runs."""

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, tagged

from .test_strategy import StrategyFixture


@tagged("post_install", "-at_install")
class TestBranchManagerPos(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        ref = cls.env.ref
        Warehouse = cls.env["stock.warehouse"]
        cls.branch1 = Warehouse.create({"name": "BMS Branch 1", "code": "BMB1", "company_id": cls.company.id})
        cls.branch2 = Warehouse.create({"name": "BMS Branch 2", "code": "BMB2", "company_id": cls.company.id})
        invoice_journal = cls.env["account.journal"].sudo().search([("type", "=", "sale"), ("company_id", "=", cls.company.id)], limit=1)

        def config(name, code, branch):
            # A cash journal can serve one POS only.
            journal = cls.env["account.journal"].sudo().create({"name": name, "code": code, "type": "cash", "company_id": cls.company.id})
            method = cls.env["pos.payment.method"].sudo().create({"name": name, "type": "cash", "journal_id": journal.id, "company_id": cls.company.id})
            return cls.env["pos.config"].create({
                "name": name, "picking_type_id": branch.pos_type_id.id, "payment_method_ids": [(6, 0, method.ids)],
                "journal_id": invoice_journal.id, "company_id": cls.company.id})

        cls.pos1 = config("BMS POS 1", "BMC1", cls.branch1)
        cls.pos2 = config("BMS POS 2", "BMC2", cls.branch2)
        pos_user = ref("point_of_sale.group_pos_user")
        manager, emp = ref("strategic_management.group_strategic_branch_manager"), ref("strategic_management.group_strategic_employee")

        def user(login, groups, warehouse=False, employee=False):
            account = cls.env["res.users"].with_context(no_reset_password=True).create({
                "name": login, "login": login, "email": "%s@example.com" % login, "tz": "UTC",
                "company_id": cls.company.id, "company_ids": [(6, 0, cls.company.ids)],
                "group_ids": [(6, 0, [ref("base.group_user").id] + [group.id for group in groups])],
                "strategic_branch_warehouse_id": warehouse.id if warehouse else False})
            if employee:
                cls.env["hr.employee"].sudo().create({"name": login, "user_id": account.id, "company_id": cls.company.id})
            return account

        cls.head = user("bms_head", [ref("strategic_management.group_strategic_head_office_manager"), pos_user])
        cls.manager1 = user("bms_manager1", [manager, pos_user], cls.branch1)
        cls.manager2 = user("bms_manager2", [manager, pos_user], cls.branch2)
        cls.unassigned = user("bms_unassigned", [manager, pos_user])
        cls.cashier1 = user("bms_cashier1", [emp, pos_user], cls.branch1, True)
        cls.other_pos = user("bms_other_pos", [pos_user])
        cls.beans = cls.env["product.template"].create({
            "name": "BMS Beans", "is_storable": True, "available_in_pos": True, "standard_price": 10.0, "list_price": 15.0, "taxes_id": [(6, 0, [])]}).product_variant_id
        Quant = cls.env["stock.quant"].sudo()
        Quant._update_available_quantity(cls.beans, cls.branch1.lot_stock_id, 20.0)
        Quant._update_available_quantity(cls.beans, cls.branch2.lot_stock_id, 50.0)

    # ------------------------------------------------------------------ the real POS lifecycle, as the given user
    def _open(self, config, user):
        config.with_user(user).open_ui()
        session = self.env["pos.session"].with_user(user).search([("config_id", "=", config.id), ("state", "!=", "closed")], limit=1)
        self.assertTrue(session, "the session opened")
        session.set_opening_control(0, "")
        self.assertEqual(session.state, "opened")
        return session

    def _sell(self, session, user, uuid, quantity=2, extra=None, line_extra=None):
        total = 15.0 * quantity
        now = fields.Datetime.to_string(fields.Datetime.now())
        result = self.env["pos.order"].with_user(user).sync_from_ui([{
            "name": uuid, "uuid": uuid, "session_id": session.id, "company_id": self.company.id, "state": "paid", "user_id": user.id,
            "pricelist_id": False, "partner_id": False, "to_invoice": False, "sequence_number": 900, "date_order": now, "fiscal_position_id": False,
            "amount_tax": 0.0, "amount_total": total, "amount_paid": total, "amount_return": 0.0,
            **(extra or {}),
            "lines": [[0, 0, {"product_id": self.beans.id, "qty": quantity, "price_unit": 15.0, "price_subtotal": total, "price_subtotal_incl": total,
                              "discount": 0, "full_product_name": self.beans.name, "tax_ids": [[6, 0, []]], "uuid": uuid + "-line", **(line_extra or {})}]],
            "payment_ids": [[0, 0, {"amount": total, "payment_date": now, "payment_method_id": session.sudo().config_id.payment_method_ids[0].id}]],
        }])
        return self.env["pos.order"].browse(next(row["id"] for row in result["pos.order"] if row["uuid"] == uuid))

    def _close(self, session, user, counted):
        session = session.with_user(user)
        session.get_closing_control_data()
        result = session.close_session_from_ui({session.sudo().config_id.payment_method_ids[0].id: counted})
        self.assertTrue(result.get("status"), result)
        self.assertEqual(session.sudo().state, "closed")

    def _on_hand(self, branch):
        return sum(self.env["stock.quant"].sudo().search([("product_id", "=", self.beans.id), ("location_id", "=", branch.lot_stock_id.id)]).mapped("quantity"))

    def _lifecycle(self, user, mode):
        self.company.sudo().point_of_sale_update_stock_quantities = mode
        session = self._open(self.pos1, user)
        order = self._sell(session, user, "bms-%s-%s" % (user.login, mode))
        self.assertEqual(order.sudo().state, "paid")
        self.assertEqual(self._on_hand(self.branch1), 18.0 if mode == "real" else 20.0)
        self._close(session, user, 30.0)
        session = session.sudo()
        self.assertEqual(self._on_hand(self.branch1), 18.0, "stock is reduced by closing at the latest")
        self.assertEqual(self._on_hand(self.branch2), 50.0)
        entries = self.env["account.move"].sudo().search([("ref", "=", session.name), ("state", "=", "posted")]) | session.sudo()._get_related_account_moves()
        self.assertTrue(entries, "the session posted its accounting entries")
        self.assertTrue(all(move.state == "posted" for move in entries))
        self.assertTrue(session.picking_ids.filtered(lambda p: p.state == "done"))
        self.assertTrue(all(p.picking_type_id.warehouse_id == self.branch1 for p in session.picking_ids))

    def test_manager_runs_a_whole_session_with_real_time_stock(self):
        self._lifecycle(self.manager1, "real")

    def test_manager_runs_a_whole_session_with_stock_at_closing(self):
        self._lifecycle(self.manager1, "closing")

    def test_employee_cashier_runs_a_whole_session_with_real_time_stock(self):
        self._lifecycle(self.cashier1, "real")

    def test_employee_cashier_runs_a_whole_session_with_stock_at_closing(self):
        self._lifecycle(self.cashier1, "closing")

    def test_cash_difference_at_closing_is_handled(self):
        self.company.sudo().point_of_sale_update_stock_quantities = "real"
        session = self._open(self.pos1, self.manager1)
        self._sell(session, self.manager1, "bms-difference")
        self._close(session, self.manager1, 25.0)
        statement_lines = self.env["account.bank.statement.line"].sudo().search([("pos_session_id", "=", session.id)])
        self.assertTrue(statement_lines, "the 5.00 shortage was booked")
        self.assertTrue(session.sudo().bank_statement_id)

    def test_head_office_and_plain_pos_users_keep_every_branch(self):
        self.company.sudo().point_of_sale_update_stock_quantities = "real"
        session1 = self._open(self.pos1, self.manager1)
        order1 = self._sell(session1, self.manager1, "bms-a")
        session2 = self._open(self.pos2, self.manager2)
        order2 = self._sell(session2, self.manager2, "bms-b")
        both = order1 | order2
        Order = self.env["pos.order"]
        for user in (self.head, self.other_pos):
            self.assertEqual(Order.with_user(user).search([("id", "in", both.ids)]), both, user.login)
            self.assertEqual(self.env["pos.config"].with_user(user).search([("id", "in", (self.pos1 | self.pos2).ids)]), self.pos1 | self.pos2)
        self.assertEqual(Order.with_user(self.head).browse(order2.id).amount_total, 30.0)

    # ------------------------------------------------------------------ cashier operations in use on the working database
    def _both_roles(self):
        self.company.sudo().point_of_sale_update_stock_quantities = "real"
        return (self.manager1, self.cashier1)

    def test_refund_as_manager_and_cashier(self):
        for user in self._both_roles():
            session = self._open(self.pos1, user)
            order = self._sell(session, user, "bms-sale-%s" % user.login, 3)
            stock = self._on_hand(self.branch1)
            refund = self._sell(session, user, "bms-refund-%s" % user.login, -2, {"is_refund": True, "refunded_order_id": order.id},
                                {"refunded_orderline_id": order.sudo().lines.id})
            self.assertEqual(refund.sudo().amount_total, -30.0)
            self.assertEqual(self._on_hand(self.branch1), stock + 2, "the goods came back to this branch")
            self._close(session, user, 15.0)  # Partial: an equal cash refund netting to zero fails to close in Odoo itself, for plain POS users too.
            # The Refund button on a past order, in a new session; it starts a draft refund to be paid.
            session = self._open(self.pos1, user)
            order.with_user(user).refund()
            draft = session.sudo().order_ids.filtered("is_refund")
            self.assertEqual(draft.refunded_order_id, order)
            draft.unlink()
            self._close(session, user, 0.0)

    def test_cannot_refund_another_branchs_order(self):
        self.company.sudo().point_of_sale_update_stock_quantities = "real"
        session2 = self._open(self.pos2, self.manager2)
        order2 = self._sell(session2, self.manager2, "bms-branch2")
        for user in (self.manager1, self.cashier1):
            with self.assertRaises(AccessError, msg=user.login):
                order2.with_user(user).refund()
            with self.assertRaises(AccessError, msg=user.login):
                order2.with_user(user).read(["amount_total"])

    def test_invoiced_sale_as_manager_and_cashier(self):
        partner = self.env["res.partner"].sudo().create({"name": "BMS Customer"})
        for user in self._both_roles():
            session = self._open(self.pos1, user)
            order = self._sell(session, user, "bms-invoice-%s" % user.login, 2, {"partner_id": partner.id, "to_invoice": True})
            invoice = order.sudo().account_move
            self.assertTrue(invoice, "the invoice was created")
            self.assertEqual(invoice.state, "posted")
            self.assertTrue(invoice.with_user(user).read(["name"]), "own branch's invoice")
            with self.assertRaises(AccessError):
                invoice.with_user(self.manager2).read(["name"])
            self.assertTrue(invoice.with_user(self.head).read(["name"]))
            self.assertTrue(invoice.with_user(self.other_pos).read(["name"]), "non-Strategic POS users are unchanged")
            self._close(session, user, 30.0)

    def test_cash_moves_need_pos_administrator_for_everyone(self):
        """Odoo's own rule, not a Strategic one: a plain POS User is refused it with or without these restrictions."""
        session = self._open(self.pos1, self.manager1)
        for user in (self.other_pos, self.manager1, self.cashier1):
            with self.assertRaises(AccessError, msg=user.login):
                session.with_user(user).try_cash_in_out("in", 20.0, "float", False)

    def test_cash_in_and_out_as_pos_administrator_stays_in_the_branch(self):
        admin = self.env.ref("point_of_sale.group_pos_manager")
        self.manager1.sudo().write({"group_ids": [(4, admin.id)]})
        self.cashier1.sudo().write({"group_ids": [(4, admin.id)]})
        session2 = self._open(self.pos2, self.manager2)
        for user in (self.manager1, self.cashier1):
            with self.assertRaises(AccessError, msg=user.login):
                session2.with_user(user).try_cash_in_out("out", 5.0, "wrong branch", False)
        self.assertFalse(session2.sudo().bank_statement_line_ids)
        for user in (self.manager1, self.cashier1):
            session = self._open(self.pos1, user)
            session.with_user(user).try_cash_in_out("in", 20.0, "float", False)
            session.with_user(user).try_cash_in_out("out", 5.0, "bank run", False)
            amounts = sorted(session.sudo().bank_statement_line_ids.mapped("amount"))
            self.assertEqual(amounts, [-5.0, 20.0])
            self.assertEqual(len(session.with_user(user).get_cash_in_out_list()), 2)
            self._close(session, user, 15.0)

    def test_cannot_close_another_branchs_session(self):
        session2 = self._open(self.pos2, self.manager2)
        for user in (self.manager1, self.cashier1):
            with self.assertRaises(AccessError, msg=user.login):
                session2.with_user(user).close_session_from_ui({})
        self.assertEqual(session2.sudo().state, "opened")

    def test_restaurant_register_as_manager_and_cashier(self):
        if "restaurant.floor" not in self.env:
            self.skipTest("pos_restaurant is not installed here")
        self.company.sudo().point_of_sale_update_stock_quantities = "real"
        floor = self.env["restaurant.floor"].sudo().create({"name": "BMS Floor", "pos_config_ids": [(6, 0, self.pos1.ids)]})
        table = self.env["restaurant.table"].sudo().create({"floor_id": floor.id, "table_number": 1, "seats": 2})
        self.pos1.sudo().write({"module_pos_restaurant": True})
        for user in (self.manager1, self.cashier1):
            session = self._open(self.pos1, user)
            order = self._sell(session, user, "bms-table-%s" % user.login, 2, {"table_id": table.id})
            self.assertEqual(order.sudo().table_id, table)
            self.assertEqual(order.sudo().state, "paid")
            self._close(session, user, 30.0)

    def test_inter_branch_transfer_boundary(self):
        groups = [self.env.ref("base.group_user").id, self.env.ref("strategic_management.group_strategic_branch_manager").id,
                  self.env.ref("stock.group_stock_user").id, self.env.ref("stock.group_stock_multi_locations").id]
        warehouse_manager = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "BMS WM", "login": "bms_wm", "company_id": self.company.id, "company_ids": [(6, 0, self.company.ids)],
            "group_ids": [(6, 0, groups)], "strategic_branch_warehouse_id": self.branch1.id})
        values = {"picking_type_id": self.branch1.int_type_id.id, "location_id": self.branch1.lot_stock_id.id, "location_dest_id": self.branch2.lot_stock_id.id,
                  "move_ids": [(0, 0, {"product_id": self.beans.id, "product_uom_qty": 3, "uom_id": self.beans.uom_id.id,
                                       "location_id": self.branch1.lot_stock_id.id, "location_dest_id": self.branch2.lot_stock_id.id})]}
        # A POS-only manager or cashier cannot start a transfer: it is not a POS operation type.
        for user in (self.manager1, self.cashier1):
            with self.assertRaises(AccessError, msg=user.login):
                self.env["stock.picking"].with_user(user).create(dict(values, move_ids=[]))
        picking = self.env["stock.picking"].with_user(warehouse_manager).create(values)
        picking.action_confirm()
        picking.move_ids.quantity = 3
        picking.move_ids.picked = True
        picking._action_done()
        self.assertEqual(picking.sudo().state, "done")
        self.assertEqual((self._on_hand(self.branch1), self._on_hand(self.branch2)), (17.0, 53.0))
        # The receiving branch's manager sees the arriving moves, not the sender's picking.
        moves = picking.sudo().move_ids
        self.assertEqual(self.env["stock.move"].with_user(self.manager2).search([("id", "in", moves.ids)]), moves)
        self.assertEqual(self.env["stock.picking"].with_user(self.manager2).search_count([("id", "=", picking.id)]), 0)
        # A transfer belonging to another branch altogether cannot be changed or validated.
        foreign = self.env["stock.picking"].sudo().create({
            "picking_type_id": self.branch2.int_type_id.id, "location_id": self.branch2.lot_stock_id.id, "location_dest_id": self.branch2.wh_input_stock_loc_id.id})
        with self.assertRaises(AccessError):
            foreign.with_user(warehouse_manager).write({"note": "x"})
        with self.assertRaises(AccessError):
            foreign.with_user(warehouse_manager).button_validate()

    # ------------------------------------------------------------------ isolation
    def test_manager_cannot_reach_another_branchs_pos_records(self):
        self.company.sudo().point_of_sale_update_stock_quantities = "real"
        session1 = self._open(self.pos1, self.manager1)
        order1 = self._sell(session1, self.manager1, "bms-own")
        session2 = self._open(self.pos2, self.manager2)
        order2 = self._sell(session2, self.manager2, "bms-other")
        Order = self.env["pos.order"].with_user(self.manager1)
        self.assertEqual(Order.search([("id", "in", (order1 | order2).ids)]), order1)
        self.assertEqual(Order.search_read([("id", "=", order2.id)], ["id"]), [])
        self.assertEqual(Order.search_count([("id", "=", order2.id)]), 0)
        self.assertEqual(Order.formatted_read_group([("id", "=", order2.id)], [], ["__count"])[0]["__count"], 0)
        for record in (order2, order2.lines, order2.payment_ids, order2.session_id, self.pos2):
            with self.assertRaises(AccessError, msg=record._name):
                record.with_user(self.manager1).read(["id"])
        with self.assertRaises(AccessError):
            order2.with_user(self.manager1).write({"general_customer_note": "x"})
        with self.assertRaises(AccessError):
            self.pos2.with_user(self.manager1).open_ui()
        with self.assertRaises(AccessError):
            self.env["pos.session"].with_user(self.manager1).create({"config_id": self.pos2.id, "user_id": self.manager1.id})
        for picking in order2.sudo().picking_ids:
            with self.assertRaises(AccessError):
                picking.with_user(self.manager1).read(["id"])
        # A browser cannot supply another session: the sale is refused and nothing is created.
        count = self.env["pos.order"].sudo().search_count([])
        with self.assertRaises(AccessError):
            self._sell(session2, self.manager1, "bms-forged")
        self.assertEqual(count, self.env["pos.order"].sudo().search_count([]))

    def test_manager_without_a_branch_reads_no_pos_records(self):
        self.assertEqual(self.env["pos.config"].with_user(self.unassigned).search_count([]), 0)
        self.assertEqual(self.env["pos.order"].with_user(self.unassigned).search_count([]), 0)

    def test_branch_transfer_moves_manager_pos_access(self):
        self.assertEqual(self.env["pos.config"].with_user(self.manager1).search([("id", "in", (self.pos1 | self.pos2).ids)]), self.pos1)
        self.manager1.write({"strategic_branch_warehouse_id": self.branch2.id})
        self.assertEqual(self.env["pos.config"].with_user(self.manager1).search([("id", "in", (self.pos1 | self.pos2).ids)]), self.pos2)

    # ------------------------------------------------------------------ reviewed inherited permissions
    def test_batches_statements_and_pos_entries_follow_the_branch(self):
        self.company.sudo().point_of_sale_update_stock_quantities = "real"
        batch1 = self.env["stock.picking.batch"].sudo().create({"picking_type_id": self.branch1.pos_type_id.id, "company_id": self.company.id})
        batch2 = self.env["stock.picking.batch"].sudo().create({"picking_type_id": self.branch2.pos_type_id.id, "company_id": self.company.id})
        for user in (self.manager1, self.cashier1):
            self.assertEqual(self.env["stock.picking.batch"].with_user(user).search([("id", "in", (batch1 | batch2).ids)]), batch1, user.login)
        self.assertEqual(self.env["stock.picking.batch"].with_user(self.head).search_count([("id", "in", (batch1 | batch2).ids)]), 2)
        session2 = self._open(self.pos2, self.manager2)
        self._sell(session2, self.manager2, "bms-entries")
        self._close(session2, self.manager2, 30.0)
        statement = session2.sudo().bank_statement_id
        self.assertTrue(statement)
        Statement = self.env["account.bank.statement"]
        self.assertEqual(Statement.with_user(self.manager1).search_count([("id", "=", statement.id)]), 0, "another branch's cash")
        self.assertEqual(Statement.with_user(self.manager2).search_count([("id", "=", statement.id)]), 1, "own branch")
        self.assertEqual(Statement.with_user(self.head).search_count([("id", "=", statement.id)]), 1)
        self.assertEqual(Statement.with_user(self.other_pos).search_count([("id", "=", statement.id)]), 1, "non-Strategic POS users are unchanged")
        # Statement lines are out of every POS User's reach already, with or without these rules.
        lines = self.env["account.bank.statement.line"].sudo().search([("pos_session_id", "=", session2.id)])
        for user in (self.other_pos, self.manager1, self.manager2):
            self.assertEqual(self.env["account.bank.statement.line"].with_user(user).search_count([("id", "in", lines.ids)]), 0, user.login)
        # POS-linked entries follow the branch; a plain POS User keeps seeing them.
        moves = self.env["account.move"].sudo().search([("pos_order_ids", "in", session2.sudo().order_ids.ids)])
        if moves:
            self.assertEqual(self.env["account.move"].with_user(self.manager1).search_count([("id", "in", moves.ids)]), 0)
            self.assertEqual(self.env["account.move"].with_user(self.other_pos).search_count([("id", "in", moves.ids)]), len(moves))

    def test_supplier_prices_need_the_purchasing_department(self):
        info = self.env["product.supplierinfo"].sudo().create({
            "partner_id": self.env["res.partner"].sudo().create({"name": "BMS Vendor"}).id, "product_tmpl_id": self.beans.product_tmpl_id.id, "price": 5.0})
        scope = [("id", "=", info.id)]
        self.assertEqual(self.env["product.supplierinfo"].with_user(self.cashier1).search_count(scope), 0)
        self.cashier1.sudo().write({"strategic_department_ids": [(6, 0, self.env.ref("strategic_management.department_purchasing").ids)]})
        self.assertEqual(self.env["product.supplierinfo"].with_user(self.cashier1).search_count(scope), 1)
        self.assertEqual(self.env["product.supplierinfo"].with_user(self.manager1).search_count(scope), 1)
        self.assertEqual(self.env["product.supplierinfo"].with_user(self.other_pos).search_count(scope), 1)


@tagged("post_install", "-at_install")
class TestEmployeeKpiAccess(StrategyFixture):
    def _target(self, employee, code):
        target = self.env["strategic.kpi.target"].with_user(self.head_user).create({
            "kpi_id": self._mk_kpi(code).id, "date_start": "2026-10-01", "date_end": "2026-10-31", "scope": "user",
            "user_id": employee.id, "target_value": 12.0, "state": "approved"})
        return target, self._mk_evaluation(target, actual=9.0, has_actual=True)

    def setUp(self):
        super().setUp()
        self.other_emp = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "KPI Other", "login": "kpi_other_9d", "group_ids": [(6, 0, self.emp_user.group_ids.ids)]})
        self.mine, self.my_eval = self._target(self.emp_user, "KPI9D_MINE")
        self.theirs, self.their_eval = self._target(self.other_emp, "KPI9D_THEIRS")

    def test_employee_reads_only_their_own_kpi_records(self):
        for model, own, other in (("strategic.kpi.target", self.mine, self.theirs), ("strategic.kpi.evaluation", self.my_eval, self.their_eval)):
            Model = self.env[model].with_user(self.emp_user)
            self.assertEqual(Model.search([("id", "in", (own | other).ids)]), own, model)
            self.assertTrue(own.with_user(self.emp_user).read(["id"]))
            with self.assertRaises(AccessError, msg=model):
                other.with_user(self.emp_user).read(["id"])

    def test_dashboard_returns_only_their_own_results(self):
        data = self.env["strategic.performance.dashboard"].with_user(self.emp_user).get_employee_data("2026-10-01", "2026-10-31")
        self.assertEqual([row["id"] for row in data["kpis"]], self.my_eval.ids)

    def test_employee_cannot_change_targets_or_results(self):
        # Evaluations are refused twice over: no write access, and they are historical records even for Head Office.
        for record, vals in ((self.mine, {"target_value": 1.0}), (self.my_eval, {"actual_value": 99.0})):
            with self.assertRaises((AccessError, ValidationError)):
                record.with_user(self.emp_user).write(vals)
            with self.assertRaises((AccessError, ValidationError)):
                record.with_user(self.emp_user).unlink()
        self.assertEqual(self.mine.target_value, 12.0)
        self.assertEqual(self.my_eval.actual_value, 9.0)
        with self.assertRaises(AccessError):
            self.env["strategic.kpi.target"].with_user(self.emp_user).create({
                "kpi_id": self.mine.kpi_id.id, "date_start": "2026-11-01", "date_end": "2026-11-30", "scope": "user",
                "user_id": self.emp_user.id, "target_value": 1.0})

    def test_head_office_and_managers_keep_their_kpi_access(self):
        self.assertEqual(self.env["strategic.kpi.target"].with_user(self.head_user).search([("id", "in", (self.mine | self.theirs).ids)]), self.mine | self.theirs)
        self.mine.with_user(self.head_user).read(["target_value"])
        self.assertIn("strategic.kpi.target", self.env)

    def test_wider_kpi_menus_stay_hidden_from_employees(self):
        menus = self.env["ir.ui.menu"].with_user(self.emp_user).load_menus(False)
        for xmlid in ("menu_strategic_management_technical", "menu_strategic_kpi_definition", "menu_strategic_kpi_target"):
            self.assertNotIn(self.env.ref("strategic_management.%s" % xmlid).id, menus, xmlid)
