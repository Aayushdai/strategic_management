"""Phase 9E: POS sales of a kit with a lot-tracked ingredient, on real stock moves. Needs product_expiry for expiry dates."""

import copy
from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestPosRecipeLots(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        cls.company.sudo().point_of_sale_update_stock_quantities = "real"
        ref = cls.env.ref
        Warehouse = cls.env["stock.warehouse"]
        cls.branch1 = Warehouse.create({"name": "LOT Branch 1", "code": "LTB1", "company_id": cls.company.id})
        cls.branch2 = Warehouse.create({"name": "LOT Branch 2", "code": "LTB2", "company_id": cls.company.id})
        invoice_journal = cls.env["account.journal"].sudo().search([("type", "=", "sale"), ("company_id", "=", cls.company.id)], limit=1)
        # The real registers take Card only.
        bank = cls.env["account.journal"].sudo().search([("type", "=", "bank"), ("company_id", "=", cls.company.id)], limit=1)
        cls.card = cls.env["pos.payment.method"].sudo().create({"name": "LOT Card", "type": "bank", "journal_id": bank.id, "company_id": cls.company.id})

        def config(name, branch):
            return cls.env["pos.config"].create({"name": name, "picking_type_id": branch.pos_type_id.id, "payment_method_ids": [(6, 0, cls.card.ids)],
                                                 "journal_id": invoice_journal.id, "company_id": cls.company.id})

        cls.pos1, cls.pos2 = config("LOT POS 1", cls.branch1), config("LOT POS 2", cls.branch2)
        pos_user = ref("point_of_sale.group_pos_user")

        def user(login, groups, branch=False, employee=False):
            account = cls.env["res.users"].with_context(no_reset_password=True).create({
                "name": login, "login": login, "email": "%s@example.com" % login, "tz": "UTC",
                "company_id": cls.company.id, "company_ids": [(6, 0, cls.company.ids)],
                "group_ids": [(6, 0, [ref("base.group_user").id] + [group.id for group in groups])],
                "strategic_branch_warehouse_id": branch.id if branch else False})
            if employee:
                cls.env["hr.employee"].sudo().create({"name": login, "user_id": account.id, "company_id": cls.company.id})
            return account

        manager = ref("strategic_management.group_strategic_branch_manager")
        cls.head = user("lot_head", [ref("strategic_management.group_strategic_head_office_manager"), pos_user])
        cls.manager1 = user("lot_manager1", [manager, pos_user], cls.branch1)
        cls.manager2 = user("lot_manager2", [manager, pos_user], cls.branch2)
        cls.cashier1 = user("lot_cashier1", [ref("strategic_management.group_strategic_employee"), pos_user], cls.branch1, True)
        cls.liters, cls.kg, cls.grams = ref("uom.product_uom_litre"), ref("uom.product_uom_kgm"), ref("uom.product_uom_gram")
        cls.milk = cls.env["product.template"].create({
            "name": "LOT Milk", "is_storable": True, "tracking": "lot", "uom_id": cls.liters.id, "use_expiration_date": True}).product_variant_id
        cls.beans = cls.env["product.template"].create({"name": "LOT Beans", "is_storable": True, "uom_id": cls.kg.id}).product_variant_id
        cls.latte = cls.env["product.template"].create({
            "name": "LOT Latte", "type": "consu", "available_in_pos": True, "list_price": 1.15, "taxes_id": [(6, 0, [])]}).product_variant_id
        cls.env["mrp.bom"].sudo().create({
            "product_tmpl_id": cls.latte.product_tmpl_id.id, "type": "phantom", "product_qty": 1,
            "bom_line_ids": [(0, 0, {"product_id": cls.beans.id, "product_qty": 18, "uom_id": cls.grams.id}),
                             (0, 0, {"product_id": cls.milk.id, "product_qty": 0.2, "uom_id": cls.liters.id})]})
        Quant = cls.env["stock.quant"].sudo()
        Quant._update_available_quantity(cls.beans, cls.branch1.lot_stock_id, 10.0)
        Quant._update_available_quantity(cls.beans, cls.branch2.lot_stock_id, 10.0)
        cls.Quant = Quant

    # ------------------------------------------------------------------ helpers
    def _lot(self, name, quantity, branch=None, days=10):
        values = {"name": name, "product_id": self.milk.id, "company_id": self.company.id}
        if days is not None:
            values["expiration_date"] = fields.Datetime.now() + timedelta(days=days)
        lot = self.env["stock.lot"].sudo().create(values)
        if days is None:
            lot.expiration_date = False  # Odoo fills a date in on creation when the product uses expiry dates.
        self.Quant._update_available_quantity(self.milk, (branch or self.branch1).lot_stock_id, quantity, lot_id=lot)
        return lot

    def _session(self, config, user):
        config.with_user(user).open_ui()
        session = self.env["pos.session"].with_user(user).search([("config_id", "=", config.id), ("state", "!=", "closed")], limit=1)
        if session.sudo().state == "opening_control":
            session.set_opening_control(0, "")
        return session

    def _payload(self, session, user, uuid, quantity):
        total = round(1.15 * quantity, 2)
        now = fields.Datetime.to_string(fields.Datetime.now())
        return {"name": uuid, "uuid": uuid, "session_id": session.id, "company_id": self.company.id, "state": "paid", "user_id": user.id,
                "pricelist_id": False, "partner_id": False, "to_invoice": False, "sequence_number": 900, "date_order": now, "fiscal_position_id": False,
                "amount_tax": 0.0, "amount_total": total, "amount_paid": total, "amount_return": 0.0,
                "lines": [[0, 0, {"product_id": self.latte.id, "qty": quantity, "price_unit": 1.15, "price_subtotal": total, "price_subtotal_incl": total,
                                  "discount": 0, "full_product_name": self.latte.name, "tax_ids": [[6, 0, []]], "uuid": uuid + "-line"}]],
                "payment_ids": [[0, 0, {"amount": total, "payment_date": now, "payment_method_id": self.card.id, "uuid": uuid + "-pay"}]]}

    def _sell(self, uuid, quantity, user=None, config=None):
        user = user or self.manager1
        session = self._session(config or self.pos1, user)
        result = self.env["pos.order"].with_user(user).sync_from_ui([self._payload(session, user, uuid, quantity)])
        return self.env["pos.order"].browse(next(row["id"] for row in result["pos.order"] if row["uuid"] == uuid)).sudo()

    def _on_hand(self, product, branch=None, lot=None):
        domain = [("product_id", "=", product.id), ("location_id", "=", (branch or self.branch1).lot_stock_id.id)]
        if lot is not None:
            domain.append(("lot_id", "=", lot.id if lot else False))
        return round(sum(self.Quant.search(domain).mapped("quantity")), 4)

    def _lines(self, order):
        return sorted(((line.lot_id.name or "", line.quantity) for line in order.picking_ids.move_line_ids.filtered(lambda ml: ml.product_id == self.milk)))

    # ------------------------------------------------------------------ allocation
    def test_one_valid_lot(self):
        lot = self._lot("L-ONE", 10.0)
        order = self._sell("lot-one", 12)
        picking = order.picking_ids
        self.assertEqual(picking.state, "done")
        self.assertFalse(picking.strategic_pos_stock_error)
        self.assertEqual(self._lines(order), [("L-ONE", 2.4)])
        self.assertEqual(self._on_hand(self.milk, lot=lot), 7.6)
        self.assertEqual(order.state, "paid")
        self.assertEqual(order.payment_ids.payment_method_id, self.card)
        self.assertEqual(order.payment_ids.amount, 13.8)

    def test_quantity_spanning_two_lots(self):
        first, second = self._lot("L-FIRST", 1.0, days=5), self._lot("L-SECOND", 5.0, days=9)
        order = self._sell("lot-two", 12)
        self.assertEqual(order.picking_ids.state, "done")
        self.assertEqual(self._lines(order), [("L-FIRST", 1.0), ("L-SECOND", 1.4)])
        self.assertEqual(self._on_hand(self.milk, lot=first), 0)
        self.assertEqual(self._on_hand(self.milk, lot=second), 3.6)

    def test_soonest_expiry_goes_first(self):
        self._lot("L-LATE", 5.0, days=30)
        self._lot("L-SOON", 5.0, days=2)
        self._lot("L-MID", 5.0, days=10)
        self.assertEqual(self._lines(self._sell("lot-fefo", 5)), [("L-SOON", 1.0)])

    def test_expired_lot_is_never_used(self):
        expired = self._lot("L-EXPIRED", 5.0, days=-1)
        order = self._sell("lot-expired", 5)
        self.assertEqual(order.picking_ids.state, "assigned")
        self.assertEqual(self._on_hand(self.milk, lot=expired), 5.0)
        self.assertIn("no valid lot", order.picking_ids.strategic_pos_stock_error)

    def test_lot_without_expiry_goes_after_dated_lots(self):
        self._lot("L-NODATE", 5.0, days=None)
        self._lot("L-DATED", 0.5, days=20)
        self.assertEqual(self._lines(self._sell("lot-nodate", 5)), [("L-DATED", 0.5), ("L-NODATE", 0.5)])

    def test_product_without_expiry_dates_uses_oldest_lot_first(self):
        """The real Milk's setting today: tracked by lot, expiry dates not in use."""
        self.milk.product_tmpl_id.sudo().use_expiration_date = False
        first, second = self._lot("L-OLDER", 0.5, days=None), self._lot("L-NEWER", 5.0, days=None)
        self.Quant.search([("lot_id", "=", first.id)]).in_date = fields.Datetime.now() - timedelta(days=3)
        self.assertEqual(self._lines(self._sell("lot-fifo", 5)), [("L-NEWER", 0.5), ("L-OLDER", 0.5)])
        self.assertEqual(self._on_hand(self.milk, lot=second), 4.5)

    def test_coffee_beans_are_deducted_in_their_own_unit(self):
        self._lot("L-BEANS", 5.0)
        order = self._sell("lot-beans", 10)
        self.assertEqual(order.picking_ids.state, "done")
        self.assertEqual(self._on_hand(self.beans), 9.82, "10 Lattes at 18 g take 0.18 kg")

    # ------------------------------------------------------------------ failures stay visible and nothing is consumed
    def test_insufficient_lots_consume_nothing_and_are_reported(self):
        lot = self._lot("L-SHORT", 1.0)
        beans = self._on_hand(self.beans)
        order = self._sell("lot-short", 12)
        picking = order.picking_ids
        self.assertEqual(order.state, "paid", "the sale and the payment stand")
        self.assertEqual(order.payment_ids.amount, 13.8)
        self.assertEqual(picking.state, "assigned")
        self.assertEqual(self._on_hand(self.milk, lot=lot), 1.0)
        self.assertEqual(self._on_hand(self.beans), beans, "the whole picking waits")
        self.assertIn("1.4", picking.strategic_pos_stock_error)
        self.assertIn(self.milk.display_name, picking.strategic_pos_stock_error)
        self.assertEqual(self._lines(order), [("", 1.4), ("L-SHORT", 1.0)], "never more than the lot holds")
        self.assertFalse(picking.backorder_ids, "no quiet partial delivery")

    def test_no_lots_at_all_is_reported_to_the_branch(self):
        order = self._sell("lot-none", 5)
        picking = order.picking_ids
        self.assertEqual(picking.state, "assigned")
        self.assertIn("Lot/Serial", picking.strategic_pos_stock_error)
        self.assertIn(order.name, picking.strategic_pos_stock_error)
        self.assertTrue(picking.message_ids.filtered(lambda m: "POS stock not processed" in (m.body or "")))
        activity = picking.activity_ids.filtered(lambda a: "POS stock not processed" in (a.summary or ""))
        self.assertIn(self.manager1, activity.user_id)
        self.assertNotIn(self.manager2, activity.user_id)

    def test_untracked_stock_of_a_tracked_product_is_not_consumed(self):
        self.Quant._update_available_quantity(self.milk, self.branch1.lot_stock_id, 50.0)
        order = self._sell("lot-untracked", 5)
        self.assertEqual(order.picking_ids.state, "assigned")
        self.assertEqual(self._on_hand(self.milk), 50.0)

    def test_retry_after_stock_arrives(self):
        order = self._sell("lot-retry", 5)
        picking = order.picking_ids
        beans = self._on_hand(self.beans)
        self.assertEqual(picking.state, "assigned")
        lot = self._lot("L-ARRIVED", 3.0)
        orders, payments = self.env["pos.order"].sudo().search_count([]), self.env["pos.payment"].sudo().search_count([])
        picking.with_user(self.manager1).action_strategic_retry_pos_stock()
        self.assertEqual(picking.state, "done")
        self.assertFalse(picking.strategic_pos_stock_error)
        self.assertFalse(picking.activity_ids.filtered(lambda a: "POS stock not processed" in (a.summary or "")))
        self.assertEqual(self._on_hand(self.milk, lot=lot), 2.0)
        self.assertEqual(self._on_hand(self.beans), round(beans - 0.09, 4))
        self.assertEqual((orders, payments), (self.env["pos.order"].sudo().search_count([]), self.env["pos.payment"].sudo().search_count([])))
        # Repeating changes nothing.
        picking.with_user(self.manager1).action_strategic_retry_pos_stock()
        self.assertEqual(self._on_hand(self.milk, lot=lot), 2.0)

    def test_retry_still_short_stays_failed_and_keeps_stock(self):
        order = self._sell("lot-retry-short", 5)
        lot = self._lot("L-LITTLE", 0.5)
        result = order.picking_ids.with_user(self.manager1).action_strategic_retry_pos_stock()
        self.assertEqual(result["params"]["type"], "danger")
        self.assertEqual(order.picking_ids.state, "assigned")
        self.assertEqual(self._on_hand(self.milk, lot=lot), 0.5)
        self.assertIn("0.5", order.picking_ids.strategic_pos_stock_error)

    def test_repeated_synchronization_creates_nothing_new(self):
        lot = self._lot("L-SYNC", 10.0)
        session = self._session(self.pos1, self.manager1)
        payload = self._payload(session, self.manager1, "lot-sync", 5)
        Order = self.env["pos.order"].with_user(self.manager1)
        Order.sync_from_ui([copy.deepcopy(payload)])  # Odoo removes keys from what it is given.
        counts = (self.env["pos.order"].sudo().search_count([("uuid", "=", "lot-sync")]), self.env["stock.picking"].sudo().search_count([("origin", "like", "%")]))
        Order.sync_from_ui([copy.deepcopy(payload)])
        self.assertEqual(counts, (self.env["pos.order"].sudo().search_count([("uuid", "=", "lot-sync")]), self.env["stock.picking"].sudo().search_count([("origin", "like", "%")])))
        order = self.env["pos.order"].sudo().search([("uuid", "=", "lot-sync")])
        self.assertEqual(len(order.payment_ids), 1)
        self.assertEqual(self._on_hand(self.milk, lot=lot), 9.0)

    # ------------------------------------------------------------------ branch boundaries and roles
    def test_lots_of_another_branch_are_never_used(self):
        other = self._lot("L-BRANCH2", 50.0, self.branch2, days=1)
        order = self._sell("lot-cross", 5)
        self.assertEqual(order.picking_ids.state, "assigned")
        self.assertEqual(self._on_hand(self.milk, self.branch2, other), 50.0)
        own = self._lot("L-BRANCH1", 5.0, days=30)
        self.assertEqual(self._lines(self._sell("lot-own", 5)), [("L-BRANCH1", 1.0)], "the sooner lot elsewhere is ignored")
        self.assertEqual(self._on_hand(self.milk, self.branch1, own), 4.0)
        self.assertEqual(self._on_hand(self.milk, self.branch2, other), 50.0)

    def test_a_browser_cannot_choose_another_branchs_lot(self):
        foreign = self._lot("L-FOREIGN", 50.0, self.branch2)
        own = self._lot("L-MINE", 5.0)
        session = self._session(self.pos1, self.cashier1)
        payload = self._payload(session, self.cashier1, "lot-forged", 5)
        payload["lines"][0][2]["pack_lot_ids"] = [[0, 0, {"lot_name": foreign.name}]]
        result = self.env["pos.order"].with_user(self.cashier1).sync_from_ui([payload])
        order = self.env["pos.order"].browse(result["pos.order"][0]["id"]).sudo()
        self.assertEqual(self._lines(order), [("L-MINE", 1.0)])
        self.assertEqual(self._on_hand(self.milk, self.branch2, foreign), 50.0)
        self.assertEqual(self._on_hand(self.milk, lot=own), 4.0)

    def test_employee_cashier_sells_and_closes_under_the_restrictions(self):
        lot = self._lot("L-CASHIER", 5.0)
        order = self._sell("lot-cashier", 5, self.cashier1)
        self.assertEqual(order.picking_ids.state, "done")
        self.assertEqual(self._on_hand(self.milk, lot=lot), 4.0)
        session = self.env["pos.session"].with_user(self.cashier1).browse(order.session_id.id)
        result = session.close_session_from_ui({self.card.id: 5.75})
        self.assertTrue(result.get("status"), result)
        self.assertEqual(session.sudo().state, "closed")
        self.assertEqual(self.Quant.with_user(self.cashier1).search_count([("lot_id", "=", lot.id)]), 0, "selling grants no stock reading")
        with self.assertRaises(AccessError):
            lot.with_user(self.cashier1).read(["name"])

    def test_manager_session_with_card_closes_with_stock_done(self):
        self._lot("L-CLOSE", 5.0)
        order = self._sell("lot-close", 5)
        session = self.env["pos.session"].with_user(self.manager1).browse(order.session_id.id)
        result = session.close_session_from_ui({self.card.id: 5.75})
        self.assertTrue(result.get("status"), result)
        self.assertEqual(session.sudo().state, "closed")
        self.assertEqual(order.picking_ids.state, "done")

    def test_stock_at_closing_mode_allocates_too(self):
        self.company.sudo().point_of_sale_update_stock_quantities = "closing"
        lot = self._lot("L-ATCLOSE", 5.0)
        order = self._sell("lot-atclose", 5)
        self.assertEqual(self._on_hand(self.milk, lot=lot), 5.0, "nothing moves before closing")
        session = order.session_id.with_user(self.manager1)
        session.close_session_from_ui({self.card.id: 5.75})
        self.assertEqual(self._on_hand(self.milk, lot=lot), 4.0)

    def test_retry_is_refused_for_cashiers_and_other_branches(self):
        order = self._sell("lot-auth", 5)
        picking = order.picking_ids
        with self.assertRaises(AccessError):
            picking.with_user(self.cashier1).action_strategic_retry_pos_stock()
        with self.assertRaises(AccessError):
            picking.with_user(self.manager2).action_strategic_retry_pos_stock()
        self.assertEqual(picking.state, "assigned")
        self._lot("L-HEAD", 5.0)
        picking.with_user(self.head).action_strategic_retry_pos_stock()
        self.assertEqual(picking.state, "done")

    def test_stuck_pickings_from_before_this_fix_are_not_touched_by_retry(self):
        order = self._sell("lot-old", 5)
        picking = order.picking_ids
        picking.sudo().strategic_pos_stock_error = False
        self._lot("L-OLD", 5.0)
        with self.assertRaises(UserError):
            picking.with_user(self.manager1).action_strategic_retry_pos_stock()
        self.assertEqual(picking.state, "assigned")

    def test_pos_user_without_any_strategic_role_can_sell_a_kit(self):
        """Odoo refused this before Phase 9E: exploding a kit reads scrap reasons, which a POS User cannot."""
        plain = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "lot_plain", "login": "lot_plain", "company_id": self.company.id, "company_ids": [(6, 0, self.company.ids)],
            "group_ids": [(6, 0, [self.env.ref("base.group_user").id, self.env.ref("point_of_sale.group_pos_user").id])]})
        lot = self._lot("L-PLAIN", 5.0)
        order = self._sell("lot-plain-pos", 5, plain)
        self.assertEqual(order.picking_ids.state, "done")
        self.assertEqual(order.picking_ids.create_uid, plain, "still recorded as the cashier's")
        self.assertEqual(self._on_hand(self.milk, lot=lot), 4.0)

    def test_a_plain_product_sale_still_works(self):
        before = self._on_hand(self.beans)
        self.beans.sudo().available_in_pos = True
        session = self._session(self.pos1, self.manager1)
        payload = self._payload(session, self.manager1, "lot-plain", 2)
        payload["lines"][0][2]["product_id"] = self.beans.id
        result = self.env["pos.order"].with_user(self.manager1).sync_from_ui([payload])
        order = self.env["pos.order"].browse(result["pos.order"][0]["id"]).sudo()
        self.assertEqual(order.picking_ids.state, "done")
        self.assertEqual(self._on_hand(self.beans), before - 2)
