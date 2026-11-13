"""Branch Manager Phase 6: the manager's own branch sales, inventory and purchasing, from real POS, Stock and Purchase records.

Each figure is compared with the records behind it. Fixtures live in their own branches and an old, otherwise empty
period (2001), so other data in the database does not change the results.
"""

from datetime import datetime, timedelta

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged

PERIOD = ("2001-03-05", "2001-03-11")


def at(day, hour=10):
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=hour)


@tagged("post_install", "-at_install")
class TestBranchOperations(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        cls.company.sudo().point_of_sale_update_stock_quantities = "real"
        ref = cls.env.ref
        Warehouse = cls.env["stock.warehouse"]
        cls.branch1 = Warehouse.create({"name": "OPS Branch 1", "code": "OPB1", "company_id": cls.company.id})
        cls.branch2 = Warehouse.create({"name": "OPS Branch 2", "code": "OPB2", "company_id": cls.company.id})
        cls.pos1 = cls.env["pos.config"].create({"name": "OPS POS 1", "picking_type_id": cls.branch1.pos_type_id.id})
        cls.pos2 = cls.env["pos.config"].create({"name": "OPS POS 2", "picking_type_id": cls.branch2.pos_type_id.id})
        # Branch Managers get the standard Odoo read access a branch manager needs for these apps.
        apps = [ref("stock.group_stock_user").id, ref("purchase.group_purchase_user").id, ref("point_of_sale.group_pos_user").id]

        def user(login, groups, warehouse=False):
            return cls.env["res.users"].with_context(no_reset_password=True).create({
                "name": login, "login": login, "email": "%s@example.com" % login, "tz": "UTC",
                "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
                "group_ids": [(6, 0, [ref("base.group_user").id] + groups)],
                "strategic_branch_warehouse_id": warehouse.id if warehouse else False})

        manager = ref("strategic_management.group_strategic_branch_manager").id
        cls.head = user("ops_head", apps + [ref("strategic_management.group_strategic_head_office_manager").id])
        cls.manager1 = user("ops_manager1", apps + [manager], cls.branch1)
        cls.manager2 = user("ops_manager2", apps + [manager], cls.branch2)
        cls.unassigned = user("ops_unassigned", apps + [manager])
        cls.employee = user("ops_employee", [ref("strategic_management.group_strategic_employee").id], cls.branch1)
        cls.kg, cls.units = ref("uom.product_uom_kgm"), ref("uom.product_uom_unit")
        cls.coffee = cls.env["product.template"].create({
            "name": "OPS Coffee Beans", "uom_id": cls.kg.id, "is_storable": True, "purchase_ok": True, "standard_price": 20.0,
        }).product_variant_id
        # Valuation history dated before the fixtures' period, so scrap is valued at the product cost (as in the chain tests).
        cls.env["product.value"].sudo().search([("product_id", "=", cls.coffee.id)]).write({"date": "2000-01-01 00:00:00"})
        cls.latte = cls.env["product.template"].create({
            "name": "OPS Latte", "uom_id": cls.units.id, "type": "consu", "available_in_pos": True, "list_price": 4.0,
        }).product_variant_id
        Tag = cls.env["stock.scrap.reason.tag"]
        cls.spoiled = Tag.search([("name", "=", "Spoilage/Expiry")], limit=1) or Tag.create({"name": "Spoilage/Expiry"})
        cls.damaged = Tag.search([("name", "=", "Damaged Goods")], limit=1) or Tag.create({"name": "Damaged Goods"})
        cls.vendor = cls.env["res.partner"].create({"name": "OPS Roaster", "supplier_rank": 1})
        cls.Dashboard = cls.env["strategic.performance.dashboard"]

    # ------------------------------------------------------------------ fixtures
    def _ops(self, user=None, period=PERIOD):
        return self.Dashboard.with_user(user or self.manager1).get_branch_operations(*period)

    def _sell(self, pos, total, day, state="paid", refund=False, tax=0.0):
        session = self.env["pos.session"].search([("config_id", "=", pos.id), ("state", "!=", "closed")], limit=1) \
            or self.env["pos.session"].create({"config_id": pos.id, "user_id": self.env.uid})
        qty = -1 if refund else 1
        return self.env["pos.order"].create({
            "session_id": session.id, "company_id": self.company.id, "state": state, "date_order": at(day), "is_refund": refund,
            "amount_tax": tax, "amount_total": total, "amount_paid": total, "amount_return": 0.0,
            "lines": [(0, 0, {"product_id": self.latte.id, "qty": qty, "price_unit": abs(total - tax), "price_subtotal": total - tax,
                              "price_subtotal_incl": total, "full_product_name": self.latte.name})],
        })

    def _receive(self, branch, quantity, day):
        move = self.env["stock.move"].create({
            "product_id": self.coffee.id, "product_uom_qty": quantity, "quantity": quantity, "uom_id": self.kg.id,
            "location_id": self.env.ref("stock.stock_location_suppliers").id, "location_dest_id": branch.lot_stock_id.id,
        })
        move._action_confirm()
        move.picked = True
        move._action_done()
        move.write({"date": at(day)})
        return move

    def _purchase(self, branch, ordered, received, day, planned_day=None, confirm=True, done_day=None, no_backorder=False):
        order = self.env["purchase.order"].create({
            "partner_id": self.vendor.id, "company_id": self.company.id, "picking_type_id": branch.in_type_id.id,
            "order_line": [(0, 0, {"name": "Coffee", "product_id": self.coffee.id, "product_qty": ordered, "price_unit": 20.0,
                                   "uom_id": self.kg.id, "date_planned": at(planned_day or day)})],
        })
        if not confirm:
            order.write({"date_order": at(day)})
            return order
        order.button_confirm()
        order.write({"date_approve": at(day)})
        if received:
            picking = order.picking_ids
            picking.move_ids.quantity = received
            picking.move_ids.picked = True
            picking.with_context(cancel_backorder=no_backorder)._action_done()
            picking.write({"date_done": at(done_day or day, 12)})
            picking.move_ids.filtered(lambda move: move.state == "done").write({"date": at(done_day or day, 12)})
        return order

    def _scrap(self, branch, quantity, day, tag):
        move = self.env["stock.move"].create({
            "product_id": self.coffee.id, "product_uom_qty": quantity, "quantity": quantity, "uom_id": self.kg.id,
            "location_id": branch.lot_stock_id.id, "scrap_reason_tag_ids": [(6, 0, tag.ids)],
            "location_dest_id": self.env["stock.location"].search([("usage", "=", "inventory"), ("company_id", "in", [False, self.company.id])], limit=1).id,
        })
        move.action_scrap()
        move.write({"date": at(day)})
        return move

    # ------------------------------------------------------------------ 1-6: sales
    def test_sales_use_paid_orders_net_of_tax_and_refunds(self):
        self._sell(self.pos1, 11.0, "2001-03-05", tax=1.0)            # 10 excl. tax
        self._sell(self.pos1, 20.0, "2001-03-06")
        self._sell(self.pos1, -4.0, "2001-03-07", refund=True)        # reduces sales, not an order
        self._sell(self.pos1, 50.0, "2001-03-07", state="cancel")     # not a sale
        self._sell(self.pos1, 30.0, "2001-03-08", state="draft")      # not completed
        self._sell(self.pos1, 99.0, "2001-03-12")                     # outside the period
        self._sell(self.pos2, 70.0, "2001-03-06")                     # another branch
        sales = self._ops()["sales"]
        self.assertTrue(sales["available"])
        self.assertEqual((sales["net_sales"], sales["orders"]), (26.0, 2))
        self.assertEqual(sales["average_order_value"], 13.0, "net sales / completed non-refund orders")
        orders = self.env["pos.order"].search([("config_id", "=", self.pos1.id), ("state", "in", ["paid", "done"]),
                                               ("date_order", ">=", at(PERIOD[0], 0)), ("date_order", "<", at("2001-03-12", 0))])
        self.assertEqual(sales["net_sales"], sum(o.amount_total - o.amount_tax for o in orders))
        self.assertEqual(sales["orders"], len(orders.filtered(lambda o: not o.is_refund)))
        trend = {row["key"]: row["sales"] for row in sales["trend"]}
        self.assertEqual(self._ops()["granularity"], "day")
        self.assertEqual(len(trend), 7, "one bar per day of the period")
        self.assertEqual((trend["2001-03-05"], trend["2001-03-06"], trend["2001-03-07"], trend["2001-03-08"]), (10.0, 20.0, -4.0, 0.0))
        self.assertEqual(self._ops(self.manager2)["sales"]["net_sales"], 70.0, "each manager sees only their branch")

    def test_empty_period_has_no_average_and_longer_periods_group_by_week(self):
        ops = self._ops(period=("2001-06-01", "2001-08-31"))
        self.assertEqual((ops["sales"]["net_sales"], ops["sales"]["orders"]), (0.0, 0))
        self.assertIs(ops["sales"]["average_order_value"], False, "no orders: no average, never a division by zero")
        self.assertEqual(ops["granularity"], "week")

    # ------------------------------------------------------------------ 7-14: inventory
    def test_inventory_waste_receipts_and_units(self):
        self._receive(self.branch1, 10.0, "2001-03-05")
        self._scrap(self.branch1, 1.5, "2001-03-06", self.spoiled)
        self._scrap(self.branch1, 0.5, "2001-03-07", self.damaged)
        self._scrap(self.branch2, 4.0, "2001-03-07", self.spoiled)     # another branch
        self._purchase(self.branch1, 5.0, 5.0, "2001-03-08")
        inv = self._ops()["inventory"]
        self.assertTrue(inv["available"])
        self.assertTrue(inv["waste_breakdown_available"])
        coffee = next(row for row in inv["waste_by_product"] if row["product"] == self.coffee.display_name)
        self.assertEqual((coffee["quantity"], coffee["unit"]), (2.0, self.kg.name), "waste keeps the product's unit")
        self.assertEqual({row["reason"] for row in inv["waste_by_reason"]}, {"Spoilage/Expiry", "Damaged Goods"})
        self.assertAlmostEqual(sum(row["cost"] for row in inv["waste_by_reason"]), inv["waste_cost"], places=2)
        self.assertEqual(inv["receipts_done"], 1, "only validated supplier receipts with their own done date")
        # The same records through the Inventory page's per-product movement: ordered, received, wasted, remaining apart.
        data = self.Dashboard.with_user(self.manager1).get_inventory_waste_data(*PERIOD, False, self.coffee.id)
        movement = data["movement"]
        self.assertEqual((movement["ordered_quantity"], movement["received_quantity"], movement["waste_quantity"]), (5.0, 15.0, 2.0))
        self.assertEqual(movement["unit"], self.kg.name)
        on_hand = sum(self.env["stock.quant"].search([("product_id", "=", self.coffee.id), ("location_id", "child_of", self.branch1.view_location_id.id),
                                                      ("location_id.usage", "=", "internal")]).mapped("quantity"))
        self.assertEqual(on_hand, 13.0, "remaining is the actual balance, not ordered - used")

    def test_unreceived_order_is_ordered_not_received(self):
        self._receive(self.branch1, 1.0, "2001-03-05")
        self._purchase(self.branch1, 8.0, 0.0, "2001-03-06")
        movement = self.Dashboard.with_user(self.manager1).get_inventory_waste_data(*PERIOD, False, self.coffee.id)["movement"]
        self.assertEqual((movement["ordered_quantity"], movement["received_quantity"]), (8.0, 1.0), "ordered is not received")
        self.assertEqual(movement["recipe_consumption_quantity"], False, "no sales and no recipe: no estimate is invented")
        self.assertIn("Recipe consumption", movement["recipe_consumption_note"])

    def test_low_stock_uses_reorder_rules_and_expiry_needs_lot_dates(self):
        inv = self._ops()["inventory"]
        self.assertFalse([row for row in inv["low_stock"] if row["product_id"] == self.coffee.id])
        self.env["stock.warehouse.orderpoint"].create({"product_id": self.coffee.id, "warehouse_id": self.branch1.id,
                                                       "location_id": self.branch1.lot_stock_id.id, "product_min_qty": 50.0, "product_max_qty": 80.0})
        self._receive(self.branch1, 10.0, "2001-03-05")
        row = next(row for row in self._ops()["inventory"]["low_stock"] if row["product_id"] == self.coffee.id)
        self.assertEqual((row["available"], row["minimum"], row["unit"]), (10.0, 50.0, self.kg.name))
        self.assertFalse([row for row in self._ops(self.manager2)["inventory"]["low_stock"] if row["product_id"] == self.coffee.id],
                         "branch 1's reorder rule is not branch 2's alert")
        inv = self._ops()["inventory"]
        if not inv["expiry_tracking_available"]:
            self.assertEqual(inv["expiring"], [], "no expiry data: no expiry warnings")
        else:
            self.assertFalse([row for row in inv["expiring"] if row["product"] == self.coffee.display_name], "untracked stock never expires")

    # ------------------------------------------------------------------ 15-20: purchasing
    def test_purchasing_states_outstanding_and_overdue(self):
        self._purchase(self.branch1, 5.0, 5.0, "2001-03-05")                                  # fully received: 100
        later = fields.Date.to_string(fields.Date.today() + timedelta(days=30))
        self._purchase(self.branch1, 10.0, 4.0, "2001-03-06", planned_day=later)              # partly received: 200, backorder open
        self._purchase(self.branch1, 3.0, 0.0, "2001-03-07", planned_day=later)                # awaiting, not overdue: 60
        self._purchase(self.branch1, 7.0, 0.0, "2001-03-08", confirm=False)                   # draft RFQ
        cancelled = self._purchase(self.branch1, 9.0, 0.0, "2001-03-08")
        cancelled.button_cancel()
        self._purchase(self.branch2, 50.0, 0.0, "2001-03-06")                                 # another branch
        pur = self._ops()["purchasing"]
        self.assertTrue(pur["available"])
        self.assertEqual((pur["confirmed_count"], pur["confirmed_value"]), (3, 360.0), "draft and cancelled are not purchases")
        self.assertEqual(pur["awaiting_count"], 2, "the backorder of the partial order and the receipt with nothing received")
        self.assertEqual(pur["overdue_count"], 0, "an outstanding receipt is not overdue before its date")
        self.assertEqual(self._ops(self.manager2)["purchasing"]["confirmed_value"], 1000.0)
        late = self._purchase(self.branch1, 2.0, 0.0, "2001-03-09")                           # expected in 2001: overdue
        pur = self._ops()["purchasing"]
        self.assertEqual((pur["awaiting_count"], pur["overdue_count"]), (3, 1))
        self.assertIn(late.name, [row["name"] for row in pur["deliveries"] if row["status"] == "Overdue"])
        self.assertTrue(all("id" not in row for row in pur["deliveries"]), "no links to Odoo's unscoped purchase screens")

    def test_open_deliveries_include_older_orders_but_spend_stays_in_the_period(self):
        later = fields.Date.to_string(fields.Date.today() + timedelta(days=30))
        # Ordered before the period (December 2000) and still open now:
        old_awaiting = self._purchase(self.branch1, 3.0, 0.0, "2000-12-01", planned_day=later)        # 60 to receive, not yet due
        old_overdue = self._purchase(self.branch1, 2.0, 0.0, "2000-12-02", planned_day="2000-12-10")  # 40 to receive, past due
        old_partial = self._purchase(self.branch1, 10.0, 4.0, "2000-12-03", planned_day=later)        # 6 kg (120) still on a backorder
        # Ordered before the period and no longer outstanding:
        old_received = self._purchase(self.branch1, 5.0, 5.0, "2000-12-04", done_day="2001-03-07")    # received during the period
        old_closed = self._purchase(self.branch1, 10.0, 4.0, "2000-12-05", no_backorder=True)          # rest will not come
        old_cancelled = self._purchase(self.branch1, 9.0, 0.0, "2000-12-06")
        old_cancelled.button_cancel()
        # Ordered in the period, nothing received yet; and another branch's older open order:
        in_period = self._purchase(self.branch1, 5.0, 0.0, "2001-03-06", planned_day=later)             # 100
        self._purchase(self.branch2, 50.0, 0.0, "2000-12-01")
        self.assertEqual((old_partial.receipt_status, old_closed.receipt_status, old_received.receipt_status), ("partial", "full", "full"))

        ops = self._ops()
        pur = ops["purchasing"]
        self.assertEqual((pur["confirmed_count"], pur["confirmed_value"]), (1, 100.0), "spend is only the period's own order")
        self.assertEqual(pur["awaiting_count"], 4, "one open receipt each: old awaiting, old overdue, old partial's backorder, in-period")
        self.assertEqual(pur["awaiting_value"], 320.0, "60 + 40 + 120 (only the 6 kg still due) + 100")
        self.assertEqual((pur["overdue_count"], pur["overdue_value"]), (1, 40.0), "only the order past its expected date")
        rows = {row["name"]: row for row in pur["deliveries"]}
        self.assertEqual(set(rows), {old_awaiting.name, old_overdue.name, old_partial.name, in_period.name},
                         "received, closed, cancelled and other-branch orders are not outstanding")
        self.assertEqual([rows[o.name]["status"] for o in (old_overdue, old_partial, old_awaiting, in_period)],
                         ["Overdue", "Partial", "Awaiting", "Awaiting"])
        self.assertEqual(pur["deliveries"][0]["name"], old_overdue.name, "overdue first")
        self.assertEqual(rows[old_partial.name]["outstanding_value"], 120.0)
        self.assertEqual(ops["inventory"]["receipts_done"], 1, "the older order received during the period counts as a period receipt")
        # The open receipts behind the count are exactly these orders' own pickings, each counted once.
        open_receipts = self.env["stock.picking"].search([("picking_type_id", "=", self.branch1.in_type_id.id), ("state", "not in", ("done", "cancel"))])
        self.assertEqual(len(open_receipts), pur["awaiting_count"])
        self.assertEqual(self._ops(self.manager2)["purchasing"]["awaiting_count"], 1, "each branch sees only its own open receipts")

    # ------------------------------------------------------------------ 21-23: scope and roles
    def test_only_branch_managers_with_a_branch_get_figures(self):
        self._sell(self.pos2, 70.0, "2001-03-06")
        ops = self._ops()
        self.assertEqual(ops["branch"]["id"], self.branch1.id)
        self.assertEqual(ops["sales"]["net_sales"], 0.0)
        with self.assertRaises(TypeError):
            self.Dashboard.with_user(self.manager1).get_branch_operations(*PERIOD, self.branch2.id)
        with self.assertRaises(AccessError):
            self._ops(self.unassigned)
        with self.assertRaises(AccessError):
            self._ops(self.employee)
        with self.assertRaises(AccessError):
            self._ops(self.head)
        # Head Office keeps its organization-level pages, including this branch.
        head = self.Dashboard.with_user(self.head).get_business_overview_data(*PERIOD, self.branch2.id)
        self.assertEqual(head["sales"], 70.0)

    def test_a_source_the_manager_cannot_read_is_unavailable_not_zero(self):
        # Odoo lets Stock users read POS and purchase orders too, so only a manager with none of the three apps is denied.
        ref = self.env.ref
        self.manager1.sudo().write({"group_ids": [(3, ref(xmlid).id) for xmlid in
                                                  ("stock.group_stock_user", "purchase.group_purchase_user", "point_of_sale.group_pos_user")]})
        ops = self._ops()
        # Stock quants stay readable: Odoo itself grants every internal user read access to them. Purchasing too:
        # every Branch Manager reads their own branch's purchase orders (ir.access.csv), with or without Purchase rights.
        self.assertTrue(ops["purchasing"]["available"])
        for key, figure in (("sales", "net_sales"),):
            self.assertFalse(ops[key]["available"], key)
            self.assertNotIn(figure, ops[key], "%s shows no figure rather than zero" % key)
            self.assertTrue(ops[key]["messages"], "%s says why" % key)

    def test_manager_without_inventory_rights_sees_own_branch_reorder_rules_only(self):
        # Like a real branch manager account: Point of Sale user only, so Odoo's reorder rules are not readable directly.
        ref = self.env.ref
        pos_only = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "ops_pos_manager", "login": "ops_pos_manager", "email": "ops_pos_manager@example.com", "tz": "UTC",
            "company_id": self.company.id, "company_ids": [(6, 0, [self.company.id])], "strategic_branch_warehouse_id": self.branch1.id,
            "group_ids": [(6, 0, [ref("base.group_user").id, ref("point_of_sale.group_pos_user").id,
                                  ref("strategic_management.group_strategic_branch_manager").id])]})
        Orderpoint = self.env["stock.warehouse.orderpoint"]
        for branch in (self.branch1, self.branch2):
            Orderpoint.create({"product_id": self.coffee.id, "warehouse_id": branch.id, "location_id": branch.lot_stock_id.id,
                               "product_min_qty": 25.0, "product_max_qty": 40.0})
        self._receive(self.branch1, 21.0, "2001-03-05")
        # Read-only, own branch only: the other branch's rule stays invisible and nothing is editable.
        visible = Orderpoint.with_user(pos_only).search([("product_id", "=", self.coffee.id)])
        self.assertEqual(visible.warehouse_id, self.branch1)
        with self.assertRaises(AccessError):
            visible.with_user(pos_only).write({"product_min_qty": 1.0})
        data = self.Dashboard.with_user(pos_only).get_inventory_waste_data(*PERIOD)
        rows = [row for row in data["stock_health"] if row["product_id"] == self.coffee.id]
        self.assertEqual([(row["warehouse_id"], row["available_quantity"], row["minimum_quantity"], row["unit"], row["status_label"]) for row in rows],
                         [(self.branch1.id, 21.0, 25.0, self.kg.name, "Low stock")], "own branch only, live figures")
        spoofed = self.Dashboard.with_user(pos_only).get_inventory_waste_data(*PERIOD, self.branch2.id)
        self.assertEqual({row["warehouse_id"] for row in spoofed["stock_health"] if row["product_id"] == self.coffee.id}, {self.branch1.id},
                         "a branch sent by the browser is ignored")
        self.assertEqual(self._ops(pos_only)["inventory"]["low_stock_count"], data["summary"]["low_stock_count"])
        head = {row["warehouse_id"] for row in self.Dashboard.with_user(self.head).get_inventory_waste_data(*PERIOD)["stock_health"] if row["product_id"] == self.coffee.id}
        self.assertEqual(head, {self.branch1.id, self.branch2.id}, "Head Office keeps every branch")

