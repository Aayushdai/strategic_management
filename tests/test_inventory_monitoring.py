"""BDD scenarios for Inventory Monitoring, first release (tests/features/inventory_monitoring.feature).

Odoo's current stock per branch: on hand, available, forecast, below minimum, negative stock and stock held by
unfinished POS deliveries. Stock is placed with Odoo's own quant API, reservations come from a real confirmed delivery,
incoming stock from a real receipt, and the unfinished POS delivery from a real POS sale of a lot-tracked product.
Products sit in their own test categories so demo data never enters the totals.
"""

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger


@tagged("post_install", "-at_install")
class TestInventoryMonitoring(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        cls.company.sudo().point_of_sale_update_stock_quantities = "real"
        ref = cls.env.ref
        Warehouse = cls.env["stock.warehouse"]
        cls.branch1 = Warehouse.create({"name": "BG Stock Branch 1", "code": "BGT1", "company_id": cls.company.id})
        cls.branch2 = Warehouse.create({"name": "BG Stock Branch 2", "code": "BGT2", "company_id": cls.company.id})
        cls.pos = cls.env["pos.config"].create({"name": "BG Stock POS", "picking_type_id": cls.branch1.pos_type_id.id})

        def user(login, group, warehouse=False):
            values = {"name": login, "login": login, "email": "%s@example.com" % login, "tz": "UTC",
                      "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
                      "group_ids": [(6, 0, [ref("base.group_user").id, ref(group).id])]}
            if warehouse:
                values["strategic_branch_warehouse_id"] = warehouse.id
            return cls.env["res.users"].with_context(no_reset_password=True).create(values)

        cls.head = user("bg_stock_head", "strategic_management.group_strategic_head_office_manager")
        cls.manager1 = user("bg_stock_manager1", "strategic_management.group_strategic_branch_manager", cls.branch1)
        cls.employee = user("bg_stock_employee", "strategic_management.group_strategic_employee", cls.branch1)
        Category = cls.env["product.category"]
        cls.cafe = Category.create({"name": "BG Stock Cafe"})
        cls.coffee_category = Category.create({"name": "BG Stock Coffee", "parent_id": cls.cafe.id})
        cls.dairy = Category.create({"name": "BG Stock Dairy", "parent_id": cls.cafe.id})
        cls.empty_category = Category.create({"name": "BG Stock Empty"})
        cls.kg, cls.litre, cls.units = ref("uom.product_uom_kgm"), ref("uom.product_uom_litre"), ref("uom.product_uom_unit")

        def product(name, uom, category, **extra):
            return cls.env["product.template"].create(dict({"name": name, "uom_id": uom.id, "is_storable": True,
                                                            "categ_id": category.id}, **extra)).product_variant_id

        cls.beans = product("BG Stock Beans", cls.kg, cls.coffee_category)
        cls.milk = product("BG Stock Milk", cls.litre, cls.dairy, tracking="lot", available_in_pos=True, list_price=2.0)
        cls.cups = product("BG Stock Cups", cls.units, cls.cafe)          # never stocked anywhere: no quant at all
        Quant = cls.env["stock.quant"]
        Quant._update_available_quantity(cls.beans, cls.branch1.lot_stock_id, 20.0)
        Quant._update_available_quantity(cls.beans, cls.branch2.lot_stock_id, 5.0)
        Quant._update_available_quantity(cls.milk, cls.branch1.lot_stock_id, 10.0)   # lot-tracked but stocked without a lot
        Quant._update_available_quantity(cls.milk, cls.branch2.lot_stock_id, -3.0)   # more taken out than recorded
        cls.plan = cls.env["strategic.plan"].with_user(cls.head).create({
            "name": "BG Stock Plan", "company_id": cls.company.id, "date_start": "2000-01-01", "date_end": "2099-12-31"})

    # ------------------------------------------------------------------ helpers
    def _picking(self, branch, product, qty, incoming):
        picking_type = branch.in_type_id if incoming else branch.out_type_id
        locations = (self.env.ref("stock.stock_location_suppliers"), branch.lot_stock_id) if incoming else \
            (branch.lot_stock_id, self.env.ref("stock.stock_location_customers"))
        picking = self.env["stock.picking"].create({
            "picking_type_id": picking_type.id, "location_id": locations[0].id, "location_dest_id": locations[1].id,
            "move_ids": [(0, 0, {"product_id": product.id, "product_uom_qty": qty, "uom_id": product.uom_id.id,
                                 "location_id": locations[0].id, "location_dest_id": locations[1].id})]})
        picking.action_confirm()
        picking.action_assign()
        return picking

    def _goal(self, metric, scope="organization", branch=None, product=None, category=None, user=None):
        user = user or self.head
        Setup = self.env["strategic.goal.setup"].with_user(self.head)
        intake = {"name": "Watch stock", "business_area": "inventory_waste", "deadline": "2099-12-31", "owner_id": self.head.id}
        if scope == "branch":
            parent = Setup.create_goal(dict(intake, plan_id=self.plan.id, name="Company stock"))["objective_id"]
            intake.update(scope="branch", warehouse_id=branch.id, parent_objective_id=parent)
        else:
            intake["plan_id"] = self.plan.id
        setup = Setup.browse(Setup.create_goal(intake)["setup_id"])
        payload = setup.configure_measurement({
            "template_key": "inventory_monitor", "measure_key": metric, "product_id": product.id if product else False,
            "product_category_id": (category or self.cafe).id})
        return setup, payload

    @staticmethod
    def _table(monitor):
        return {(row["cells"][0], row["cells"][1]): row["cells"][2:] for row in monitor["table"]["rows"]}

    # ------------------------------------------------------------------ A-C: on hand, available, forecast
    def test_on_hand_is_a_current_snapshot_per_branch_and_unit(self):
        """Branch 1: 20 kg beans, 10 L milk, no cups record at all; Branch 2: 5 kg beans, -3 L milk. Units stay apart."""
        setup, payload = self._goal("inv_mon_on_hand")
        self.assertEqual(setup.setup_state, "ready", payload["readiness"])
        self.assertEqual(setup.period_preset, False, "a snapshot has no reporting period")
        monitor = payload["monitor"]
        self.assertTrue(monitor["period_label"].startswith("Current snapshot"))
        self.assertIn("not a balance for a past period", monitor["note"])
        table = self._table(monitor)
        b1, b2 = self.branch1.display_name, self.branch2.display_name
        self.assertEqual(table[("BG Stock Beans", b1)], ["20 kg"])
        self.assertEqual(table[("BG Stock Milk", b1)], ["10 L"])
        self.assertEqual(table[("BG Stock Cups", b1)], ["0 Units"], "no stock record is listed as zero, not dropped")
        self.assertEqual(table[("BG Stock Milk", b2)], ["-3 L (negative)"])
        totals = {card["key"]: card["value_display"] for card in monitor["cards"]}
        self.assertEqual(totals["on_hand"], "7 L · 0 Units · 25 kg", "kilograms, litres and pieces are never added together")
        self.assertEqual(totals["negative"], "1")
        branch_rows = {row["label"]: row["display"] for row in monitor["blocks"][0]["rows"]}
        self.assertEqual(branch_rows[b1], "10 L · 0 Units · 20 kg")

    def test_available_and_forecast_use_reservations_and_pending_moves(self):
        """A confirmed delivery reserves 4 kg; a pending receipt brings 10 kg: available 16 kg, forecast 26 kg."""
        self._picking(self.branch1, self.beans, 4.0, incoming=False)
        self._picking(self.branch1, self.beans, 10.0, incoming=True)
        available = self._table(self._goal("inv_mon_available", "branch", self.branch1, product=self.beans)[1]["monitor"])
        self.assertEqual(available[("BG Stock Beans", self.branch1.display_name)], ["20 kg", "4 kg", "16 kg"])
        monitor = self._goal("inv_mon_forecast", "branch", self.branch1, product=self.beans)[1]["monitor"]
        self.assertEqual(self._table(monitor)[("BG Stock Beans", self.branch1.display_name)], ["20 kg", "10 kg", "4 kg", "26 kg"])
        self.assertIn("not a demand forecast", monitor["note"])
        self.assertEqual(monitor["value_display"], "26 kg")

    # ------------------------------------------------------------------ D: below minimum
    def test_below_minimum_uses_reordering_rules_only(self):
        """Beans rule min 30 against a forecast of 26 kg: 4 kg short. Milk rule min 5 against 10 L: fine. Cups: no rule."""
        self._picking(self.branch1, self.beans, 4.0, incoming=False)
        self._picking(self.branch1, self.beans, 10.0, incoming=True)
        Rule = self.env["stock.warehouse.orderpoint"]
        Rule.create({"product_id": self.beans.id, "warehouse_id": self.branch1.id, "location_id": self.branch1.lot_stock_id.id,
                     "product_min_qty": 30.0, "product_max_qty": 40.0, "trigger": "manual"})
        Rule.create({"product_id": self.milk.id, "warehouse_id": self.branch1.id, "location_id": self.branch1.lot_stock_id.id,
                     "product_min_qty": 5.0, "product_max_qty": 10.0, "trigger": "manual"})
        monitor = self._goal("inv_mon_below_minimum", "branch", self.branch1)[1]["monitor"]
        self.assertEqual(monitor["value_display"], "1 below minimum")
        rows = {row["cells"][0]: row["cells"][2:] for row in monitor["table"]["rows"]}
        self.assertEqual(list(rows), ["BG Stock Beans"])
        self.assertEqual(rows["BG Stock Beans"], ["20 kg", "26 kg", "30 kg", "4 kg"])
        cards = {card["key"]: card["value_display"] for card in monitor["cards"]}
        self.assertEqual((cards["rules"], cards["without"]), ("2", "1"), "cups have no rule and are not judged")
        with self.assertRaises(Exception), self.env.cr.savepoint(), mute_logger("odoo.sql_db"):
            Rule.create({"product_id": self.beans.id, "warehouse_id": self.branch1.id, "location_id": self.branch1.lot_stock_id.id,
                         "product_min_qty": 1.0, "trigger": "manual"})
            self.env.flush_all()          # Odoo allows one rule per product and location, so rows cannot duplicate
        none = self._goal("inv_mon_below_minimum", "branch", self.branch1, product=self.cups)[1]["monitor"]
        self.assertEqual(none["status"], "empty")
        self.assertIn("does not mean stock is sufficient", none["note"])

    # ------------------------------------------------------------------ E: negative stock
    def test_negative_stock_is_flagged_not_corrected(self):
        monitor = self._goal("inv_mon_negative")[1]["monitor"]
        self.assertEqual(monitor["value_display"], "1 negative balance")
        row = monitor["table"]["rows"][0]["cells"]
        self.assertEqual(row[:3], ["BG Stock Milk", self.branch2.display_name, "-3 L"])
        self.assertIn("Not adjusted automatically", row[3])
        self.assertEqual(self.milk.with_context(warehouse_id=self.branch2.id).qty_available, -3.0, "nothing was changed")
        clean = self._goal("inv_mon_negative", "branch", self.branch1)[1]["monitor"]
        self.assertEqual(clean["value_display"], "No negative stock")

    # ------------------------------------------------------------------ F: unfinished POS deliveries
    def test_unfinished_pos_delivery_is_shown_and_left_untouched(self):
        """A POS sale of 4 L of lot-tracked milk without a lot number leaves its delivery unfinished. Odoo reserves nothing
        for it here (it reserves what it can; the real deliveries do hold stock), so 0 L reserved and 4 L still to take out
        are shown as Odoo records them, with a warning that does not claim a reservation."""
        session = self.env["pos.session"].search([("config_id", "=", self.pos.id), ("state", "!=", "closed")], limit=1) \
            or self.env["pos.session"].create({"config_id": self.pos.id, "user_id": self.env.uid})
        order = self.env["pos.order"].create({
            "session_id": session.id, "company_id": self.company.id, "state": "paid", "amount_tax": 0.0, "amount_total": 8.0,
            "amount_paid": 8.0, "amount_return": 0.0,
            "lines": [(0, 0, {"product_id": self.milk.id, "qty": 4.0, "price_unit": 2.0, "price_subtotal": 8.0,
                              "price_subtotal_incl": 8.0, "full_product_name": self.milk.name})]})
        order._create_order_picking()
        picking = order.picking_ids
        self.assertTrue(picking and picking.state not in ("done", "cancel"), "Odoo left the delivery unfinished")
        state_before = picking.state
        monitor = self._goal("inv_mon_pos_reserved", "branch", self.branch1)[1]["monitor"]
        self.assertEqual(monitor["value_display"], "1 unfinished POS delivery")
        cells = monitor["table"]["rows"][0]["cells"]
        self.assertEqual(cells[:4], [self.branch1.display_name, "BG Stock Milk", "0 L", "4 L"])
        self.assertIn(picking.name, cells[4])
        self.assertIn("lot number missing", cells[4])
        self.assertEqual(cells[5], order.name)
        available = self._goal("inv_mon_available", "branch", self.branch1, product=self.milk)[1]["monitor"]
        self.assertIn("Unfinished POS deliveries: 1 delivery (%s) reserve nothing now, but 4 L" % picking.name, available["warnings"][0])
        self.assertEqual(self._table(available)[("BG Stock Milk", self.branch1.display_name)], ["10 L", "0 L", "10 L"])
        forecast = self._goal("inv_mon_forecast", "branch", self.branch1, product=self.milk)[1]["monitor"]
        self.assertEqual(self._table(forecast)[("BG Stock Milk", self.branch1.display_name)], ["10 L", "0 L", "4 L", "6 L"])
        self.assertEqual(picking.state, state_before, "nothing was validated, cancelled or unreserved")
        quiet = self._goal("inv_mon_pos_reserved", "branch", self.branch2)[1]["monitor"]
        self.assertEqual((quiet["value_display"], quiet["warnings"]), ("No unfinished POS deliveries", []))

    # ------------------------------------------------------------------ filters, units, empty data
    def test_filters_units_and_empty_data(self):
        dairy = self._goal("inv_mon_on_hand", category=self.dairy)[1]["monitor"]
        self.assertEqual({key[0] for key in self._table(dairy)}, {"BG Stock Milk"}, "category filter")
        empty_setup, empty = self._goal("inv_mon_on_hand", category=self.empty_category)
        self.assertEqual(empty["monitor"]["status"], "empty")
        self.assertEqual(empty["current"], "Nothing to show")
        self.assertEqual(empty_setup.setup_state, "ready", "an empty selection is checked, not failed")
        self.env.ref("uom.product_uom_unit").sudo().name = "Kg"           # rolled back with the test
        renamed = self._goal("inv_mon_on_hand", product=self.cups)[1]["monitor"]
        self.assertIn("Odoo's 'Units' unit is renamed 'Kg'", renamed["warnings"][0])

    # ------------------------------------------------------------------ permissions
    def test_branch_managers_see_their_own_branch_only(self):
        own = self._goal("inv_mon_on_hand", "branch", self.branch1)[0]
        other = self._goal("inv_mon_on_hand", "branch", self.branch2)[0]
        company = self._goal("inv_mon_on_hand")[0]
        view = own.with_user(self.manager1)._goal_payload(own.objective_id)["monitor"]
        self.assertEqual({key[1] for key in self._table(view)}, {self.branch1.display_name})
        self.assertEqual(view["blocks"], [], "no branch comparison for a branch manager")
        company_view = company.with_user(self.manager1)._goal_payload(company.objective_id)["monitor"]
        self.assertEqual((company_view["status"], company_view["table"]), ("no_access", False))
        head_view = company._goal_payload(company.objective_id)["monitor"]
        self.assertIn(self.branch2.display_name, {key[1] for key in self._table(head_view)}, "Head Office compares branches")
        with self.assertRaises(AccessError):
            other.with_user(self.manager1).read(["measure_key"])
        with self.assertRaises(AccessError):
            own.with_user(self.employee).read(["measure_key"])
        Setup = self.env["strategic.goal.setup"].with_user(self.manager1)
        # A branch ID sent by the client is not trusted: the server puts a branch manager's goal on their own branch.
        peek = Setup.create_goal({"name": "Peek", "business_area": "inventory_waste", "deadline": "2099-12-31", "scope": "branch",
                                  "warehouse_id": self.branch2.id, "parent_objective_id": company.objective_id.id,
                                  "owner_id": self.manager1.id})
        self.assertEqual(self.env["strategic.objective"].browse(peek["objective_id"]).warehouse_id, self.branch1)
        with self.assertRaisesRegex(UserError, "Choose the branch"):
            self.env["strategic.goal.setup"].with_user(self.head).create_goal({
                "name": "Nowhere", "business_area": "inventory_waste", "deadline": "2099-12-31", "scope": "branch",
                "warehouse_id": 999999, "parent_objective_id": company.objective_id.id, "owner_id": self.head.id})

    # ------------------------------------------------------------------ time breakdown
    def test_stock_is_a_snapshot_without_invented_history(self):
        setup, payload = self._goal("inv_mon_on_hand")
        timeline = payload["monitor"]["timeline"]
        self.assertEqual(timeline["status"], "snapshot")
        self.assertNotIn("buckets", timeline, "no past balances are drawn")
        self.assertIn("past balances are not kept", timeline["note"])
        detail = setup.get_monitor_interval("2026-01-01T00:00", "2026-01-02T00:00", "day")
        self.assertEqual(detail["status"], "snapshot")
