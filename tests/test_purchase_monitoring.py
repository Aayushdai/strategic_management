"""BDD scenarios for purchasing monitoring (tests/features/purchase_monitoring.feature).

Real purchase orders (confirmed, draft, cancelled), partial receipts, a supplier return made with Odoo's own return
flow, order lines in grams for a product kept in kg, and two branches. The café is in Asia/Kathmandu (UTC+5:45).
Week measured: 8-14 January 2001.
"""

from datetime import datetime

import pytz

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import format_amount

TZ = pytz.timezone("Asia/Kathmandu")
WEEK = ("2001-01-08", "2001-01-14")


def local(day, hour=10):
    return TZ.localize(datetime.strptime(day, "%Y-%m-%d").replace(hour=hour)).astimezone(pytz.UTC).replace(tzinfo=None)


@tagged("post_install", "-at_install")
class TestPurchaseMonitoring(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "Asia/Kathmandu"})
        ref = cls.env.ref
        Warehouse = cls.env["stock.warehouse"]
        cls.branch1 = Warehouse.create({"name": "BG Buy Branch 1", "code": "BGB1", "company_id": cls.company.id})
        cls.branch2 = Warehouse.create({"name": "BG Buy Branch 2", "code": "BGB2", "company_id": cls.company.id})

        def user(login, group, warehouse=False):
            values = {"name": login, "login": login, "email": "%s@example.com" % login, "tz": "Asia/Kathmandu",
                      "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
                      "group_ids": [(6, 0, [ref("base.group_user").id, ref(group).id])]}
            if warehouse:
                values["strategic_branch_warehouse_id"] = warehouse.id
            return cls.env["res.users"].with_context(no_reset_password=True).create(values)

        cls.head = user("bg_buy_head", "strategic_management.group_strategic_head_office_manager")
        cls.manager1 = user("bg_buy_manager1", "strategic_management.group_strategic_branch_manager", cls.branch1)
        Category = cls.env["product.category"]
        cls.ingredients = Category.create({"name": "BG Buy Ingredients"})
        cls.coffee_category = Category.create({"name": "BG Buy Coffee", "parent_id": cls.ingredients.id})
        cls.dairy = Category.create({"name": "BG Buy Dairy", "parent_id": cls.ingredients.id})
        cls.packaging = Category.create({"name": "BG Buy Packaging"})
        cls.kg, cls.gram, cls.litre, cls.units = ref("uom.product_uom_kgm"), ref("uom.product_uom_gram"), ref("uom.product_uom_litre"), ref("uom.product_uom_unit")

        def product(name, uom, category):
            return cls.env["product.template"].create({"name": name, "uom_id": uom.id, "is_storable": True, "purchase_ok": True,
                                                       "categ_id": category.id}).product_variant_id

        cls.beans = product("BG Buy Coffee Beans", cls.kg, cls.coffee_category)
        cls.milk = product("BG Buy Milk", cls.litre, cls.dairy)
        cls.cups = product("BG Buy Cups", cls.units, cls.packaging)
        Partner = cls.env["res.partner"]
        cls.roaster = Partner.create({"name": "BG Buy Roaster", "is_company": True, "supplier_rank": 1})
        cls.highland = Partner.create({"name": "BG Buy Highland Roasters", "is_company": True, "supplier_rank": 1})
        cls.dairy_co = Partner.create({"name": "BG Buy Dairy Co", "is_company": True, "supplier_rank": 1})
        cls.tax = cls.env["account.tax"].create({"name": "BG Buy VAT 13%", "amount": 13.0, "type_tax_use": "purchase",
                                                 "company_id": cls.company.id})
        cls.plan = cls.env["strategic.plan"].with_user(cls.head).create({
            "name": "BG Buy Plan", "company_id": cls.company.id, "date_start": "2000-01-01", "date_end": "2099-12-31"})

        # PO1: 20 kg at 500/kg less 10 %, 13 % tax; 15 kg received on the 10th, 5 kg left, expected on the 11th.
        cls.po1 = cls._po(cls.roaster, cls.branch1, [(cls.beans, 20.0, 500.0, 10.0, cls.kg)], "2001-01-09", "2001-01-11")
        cls._receive(cls.po1, {cls.beans: 15.0}, "2001-01-10")
        # PO2: 100 L of milk, fully received.
        cls.po2 = cls._po(cls.dairy_co, cls.branch1, [(cls.milk, 100.0, 2.0, 0.0, cls.litre)], "2001-01-09", "2001-01-09")
        cls._receive(cls.po2, {cls.milk: 100.0}, "2001-01-09")
        # PO3: Branch 2, beans and cups, nothing received yet, expected after the week.
        cls.po3 = cls._po(cls.highland, cls.branch2, [(cls.beans, 10.0, 520.0, 0.0, cls.kg), (cls.cups, 500.0, 0.2, 0.0, cls.units)],
                          "2001-01-12", "2001-01-20")
        # PO8: 500 g of beans ordered in grams (0.60/g = 600/kg), expected on the 10th, not received.
        cls.po8 = cls._po(cls.roaster, cls.branch1, [(cls.beans, 500.0, 0.6, 0.0, cls.gram)], "2001-01-09", "2001-01-10")
        # Not commitments: an RFQ and a cancelled order; an order confirmed after the week; last week's order.
        cls._po(cls.roaster, cls.branch1, [(cls.beans, 99.0, 500.0, 0.0, cls.kg)])
        cancelled = cls._po(cls.roaster, cls.branch1, [(cls.beans, 50.0, 500.0, 0.0, cls.kg)], "2001-01-09", "2001-01-11")
        cancelled.button_cancel()
        cls._po(cls.roaster, cls.branch1, [(cls.beans, 7.0, 500.0, 0.0, cls.kg)], "2001-01-15", "2001-01-16")
        cls.po7 = cls._po(cls.roaster, cls.branch1, [(cls.beans, 4.0, 480.0, 0.0, cls.kg)], "2001-01-02", "2001-01-03")
        cls._receive(cls.po7, {cls.beans: 4.0}, "2001-01-03")

    @classmethod
    def _po(cls, supplier, branch, lines, confirm_day=None, expected_day="2001-01-11", currency=None):
        values = {"partner_id": supplier.id, "company_id": cls.company.id, "picking_type_id": branch.in_type_id.id,
                  "order_line": [(0, 0, {"name": product.name, "product_id": product.id, "product_qty": qty, "price_unit": price,
                                         "discount": discount, "uom_id": uom.id, "date_planned": local(expected_day, 12),
                                         "tax_ids": [(6, 0, cls.tax.ids)]})
                                 for product, qty, price, discount, uom in lines]}
        if currency:
            values["currency_id"] = currency.id
        order = cls.env["purchase.order"].create(values)
        if confirm_day:
            order.button_confirm()
            order.write({"date_approve": local(confirm_day)})
            order.order_line.write({"date_planned": local(expected_day, 12)})
        return order

    @classmethod
    def _receive(cls, order, quantities, day):
        picking = order.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))[:1]
        for move in picking.move_ids:
            move.quantity = quantities.get(move.product_id, 0.0)
            move.picked = True
        picking._action_done()
        picking.move_ids.filtered(lambda m: m.state == "done").write({"date": local(day, 15)})

    def _goal(self, values, scope="organization", branch=None, user=None):
        user = user or self.head
        Setup = self.env["strategic.goal.setup"].with_user(user)
        intake = {"name": "Watch purchasing", "business_area": "purchasing", "deadline": "2001-01-31", "owner_id": user.id}
        if scope == "branch":
            parent = Setup.create_goal(dict(intake, plan_id=self.plan.id, name="Company purchasing"))["objective_id"]
            intake.update(scope="branch", warehouse_id=branch.id, parent_objective_id=parent)
        else:
            intake["plan_id"] = self.plan.id
        setup = Setup.browse(Setup.create_goal(intake)["setup_id"])
        config = {"template_key": "purchase_monitor", "measure_key": "purchase_mon_value", "period_preset": "custom",
                  "period_start": WEEK[0], "period_end": WEEK[1]}
        config.update(values)
        return setup, setup.configure_measurement(config)

    def _money(self, amount):
        return format_amount(self.env, amount, self.company.currency_id)

    @staticmethod
    def _block(monitor, title):
        return {row["label"]: row["display"] for row in next(b for b in monitor["blocks"] if b["title"] == title)["rows"]}

    # ------------------------------------------------------------------ confirmed value and its breakdowns
    def test_confirmed_value_excludes_tax_rfqs_cancelled_and_other_periods(self):
        """9,000 (20 kg x 500 less 10 %) + 200 + 5,300 + 300 = 14,800 excluding tax; RFQs, cancelled orders and
        orders confirmed outside the week are left out; supplier, category and branch rows add up to the total."""
        setup, payload = self._goal({})
        self.assertEqual(setup.setup_state, "ready", payload["readiness"])
        self.assertEqual(setup.objective_id.goal_type, "monitoring")
        monitor = payload["monitor"]
        self.assertEqual(monitor["value_display"], self._money(14800.0))
        cards = {card["key"]: card["value_display"] for card in monitor["cards"]}
        self.assertEqual((cards["orders"], cards["suppliers"]), ("4", "3"))
        self.assertEqual(cards["ordered"], "100 L · 500 Units · 30.5 kg", "different units listed, grams converted to kg")
        suppliers = self._block(monitor, "By supplier")
        self.assertEqual(suppliers["BG Buy Roaster"], "%s · 62.8%%" % self._money(9300.0))
        self.assertEqual(suppliers["BG Buy Highland Roasters"], "%s · 35.8%%" % self._money(5300.0))
        self.assertEqual(suppliers["BG Buy Dairy Co"], "%s · 1.4%%" % self._money(200.0))
        categories = self._block(monitor, "By product category")
        self.assertTrue(categories["BG Buy Ingredients / BG Buy Coffee"].startswith(self._money(14500.0)))
        branches = self._block(monitor, "By branch")
        self.assertTrue(branches[self.branch1.display_name].startswith(self._money(9500.0)))
        self.assertTrue(branches[self.branch2.display_name].startswith(self._money(5300.0)))
        self.assertIn("1,920.00", monitor["previous"]["display"].replace(" ", " "))
        self.assertIn("+670.8%", monitor["previous"]["change"])
        self.assertEqual(len(monitor["trend"]), 7)
        self.assertEqual((payload["target"], payload["can_prepare_target"], payload["can_submit"]), ("No target (monitoring)", False, True))
        with self.assertRaisesRegex(UserError, "no target"):
            setup.prepare_target()

    def test_supplier_product_and_category_filters(self):
        _setup, payload = self._goal({"partner_id": self.roaster.id})
        self.assertEqual(payload["monitor"]["value_display"], self._money(9300.0), "the roaster's two orders this week")
        _setup, payload = self._goal({"product_category_id": self.ingredients.id})
        self.assertEqual(payload["monitor"]["value_display"], self._money(14700.0), "a parent category includes coffee and dairy")
        _setup, payload = self._goal({"measure_key": "purchase_mon_ordered", "product_id": self.beans.id})
        self.assertEqual(payload["monitor"]["value_display"], "30.5 kg")
        cards = {card["key"]: card["value_display"] for card in payload["monitor"]["cards"]}
        self.assertEqual(cards["received"], "15 kg", "received in the same week, from completed receipts")

    # ------------------------------------------------------------------ received, returns
    def test_received_uses_completed_receipts_net_of_returns(self):
        """15 kg of the 20 kg arrived (a partial receipt is not the ordered quantity); 3 kg sent back with Odoo's
        return flow leave 12 kg received, and Odoo's own rule decides whether the 3 kg are owed again."""
        _setup, payload = self._goal({"measure_key": "purchase_mon_received", "product_id": self.beans.id}, scope="branch", branch=self.branch1)
        self.assertEqual(payload["monitor"]["value_display"], "15 kg")
        receipt = self.po1.picking_ids.filtered(lambda p: p.state == "done")
        returned = receipt._create_return()
        returned.move_ids.write({"product_uom_qty": 3.0})
        returned.action_confirm()
        returned.move_ids.quantity = 3.0
        returned.move_ids.picked = True
        returned._action_done()
        returned.move_ids.write({"date": local("2001-01-11", 15)})
        self.assertTrue(returned.move_ids.purchase_line_id, "Odoo keeps the purchase line on the return")
        _setup, payload = self._goal({"measure_key": "purchase_mon_received", "product_id": self.beans.id}, scope="branch", branch=self.branch1)
        cards = {card["key"]: card["value_display"] for card in payload["monitor"]["cards"]}
        self.assertEqual((payload["monitor"]["value_display"], cards["returned"], cards["receipts"]), ("12 kg", "3 kg", "1"))
        # Awaiting delivery follows Odoo's rule: a return that updates the order quantities is owed again.
        owed_again = 3.0 if returned.move_ids.to_refund or not returned.move_ids.origin_returned_move_id else 0.0
        _setup, payload = self._goal({"measure_key": "purchase_mon_outstanding"})
        self.assertEqual(payload["monitor"]["status"], "ok", payload["monitor"]["note"])
        cards = {card["key"]: card["value_display"] for card in payload["monitor"]["cards"]}
        self.assertEqual(cards["open_value"], self._money(9000.0 * (5.0 + owed_again) / 20.0 + 5300.0 + 300.0))
        self.assertEqual(self.po1.order_line.qty_received, 15.0 - owed_again, "the same rule as Odoo's own received quantity")

    # ------------------------------------------------------------------ outstanding and overdue
    def test_outstanding_and_overdue_at_the_end_of_the_period(self):
        """At the end of the week: PO1 (5 kg left, expected the 11th), PO3 (expected the 20th) and PO8 (0.5 kg,
        expected the 10th) await delivery; PO2 was received in full. Only PO1 and PO8 are overdue."""
        _setup, payload = self._goal({"measure_key": "purchase_mon_outstanding"})
        monitor = payload["monitor"]
        cards = {card["key"]: card["value_display"] for card in monitor["cards"]}
        self.assertEqual(monitor["value_display"], "3 orders")
        self.assertEqual(cards["open_value"], self._money(2250.0 + 5300.0 + 300.0), "unreceived share of each line, excl. tax")
        self.assertEqual((cards["overdue"], cards["overdue_value"]), ("2", self._money(2550.0)))
        self.assertEqual(cards["remaining"], "500 Units · 15.5 kg")
        names = [row["cells"][0] for row in monitor["table"]["rows"]]
        self.assertNotIn(self.po2.name, names, "a fully received order is not outstanding")
        _setup, payload = self._goal({"measure_key": "purchase_mon_overdue"})
        rows = {row["cells"][0]: row["cells"][1] for row in payload["monitor"]["table"]["rows"]}
        self.assertEqual(set(rows), {self.po1.name, self.po8.name}, "PO3 is open but not late")
        self.assertIn("4 days late", rows[self.po1.name])
        self.assertIn("5 days late", rows[self.po8.name])

    # ------------------------------------------------------------------ price, units, currencies
    def test_purchase_price_is_weighted_in_the_product_unit_and_needs_a_product(self):
        """(9,000 + 5,200 + 300) / (20 + 10 + 0.5 kg) = 475.41 per kg; the gram line is converted, not added as 500."""
        _setup, payload = self._goal({"measure_key": "purchase_mon_unit_price", "product_id": self.beans.id})
        monitor = payload["monitor"]
        self.assertEqual(monitor["value_display"], "%s/kg" % self._money(14500.0 / 30.5))
        suppliers = self._block(monitor, "By supplier")
        self.assertEqual(suppliers["BG Buy Highland Roasters"], "%s/kg" % self._money(520.0))
        self.assertIn("not the supplier's current price list", monitor["note"])
        _setup, payload = self._goal({"measure_key": "purchase_mon_unit_price"})
        self.assertIn("choose the product whose purchase price is shown", payload["readiness"][0]["message"])

    def test_unrelated_units_and_missing_exchange_rates_are_refused(self):
        self._po(self.roaster, self.branch1, [(self.beans, 5.0, 500.0, 0.0, self.litre)], "2001-02-06", "2001-02-07")
        _setup, payload = self._goal({"period_start": "2001-02-05", "period_end": "2001-02-11"})
        self.assertEqual(payload["monitor"]["status"], "data_problem")
        self.assertIn("cannot be converted", payload["monitor"]["note"])
        currency = self.env["res.currency"].create({"name": "BGX", "symbol": "¤", "active": True})
        self._po(self.roaster, self.branch1, [(self.beans, 1.0, 500.0, 0.0, self.kg)], "2001-03-06", "2001-03-07", currency=currency)
        _setup, payload = self._goal({"period_start": "2001-03-05", "period_end": "2001-03-11"})
        self.assertEqual(payload["monitor"]["status"], "data_problem")
        self.assertIn("no exchange rate", payload["monitor"]["note"])

    # ------------------------------------------------------------------ empty data, permissions
    def test_zero_is_shown_only_when_it_is_real(self):
        _setup, payload = self._goal({"period_start": "2001-04-02", "period_end": "2001-04-08"})
        self.assertEqual((payload["monitor"]["status"], payload["monitor"]["value_display"]), ("ok", self._money(0.0)))
        self.assertIn("so the value is zero", payload["monitor"]["note"])
        _setup, payload = self._goal({"measure_key": "purchase_mon_unit_price", "product_id": self.beans.id,
                                      "period_start": "2001-04-02", "period_end": "2001-04-08"})
        self.assertEqual(payload["monitor"]["status"], "empty", "no purchase gives no price, not a price of zero")
        self.assertEqual(payload["current"], "No purchases")

    def test_branch_managers_see_their_own_branch_only(self):
        own, _payload = self._goal({}, scope="branch", branch=self.branch1)
        other, _payload = self._goal({}, scope="branch", branch=self.branch2)
        company, _payload = self._goal({})
        view = own.with_user(self.manager1)._goal_payload(own.objective_id)["monitor"]
        self.assertEqual(view["value_display"], self._money(9500.0))
        self.assertNotIn("By branch", [block["title"] for block in view["blocks"]])
        company_view = company.with_user(self.manager1)._goal_payload(company.objective_id)["monitor"]
        self.assertEqual((company_view["status"], company_view["cards"]), ("no_access", []))
        with self.assertRaises(AccessError):
            other.with_user(self.manager1).read(["measure_key"])

    # ------------------------------------------------------------------ time breakdown (timeline and drill-down)
    def test_timeline_by_confirmation_time_reconciles_and_lists_orders(self):
        setup, payload = self._goal({})
        timeline = payload["monitor"]["timeline"]
        self.assertEqual((timeline["granularity"], timeline["kind"], timeline["drill"]), ("day", "flow", "hour"))
        self.assertAlmostEqual(sum(b["value"] for b in timeline["buckets"]), 14800.0, msg="days add up to the confirmed value")
        busy = next(b for b in timeline["buckets"] if b["value"])
        detail = setup.get_monitor_interval(busy["start"], busy["end"], "day")
        self.assertAlmostEqual(detail["value"], busy["value"])
        self.assertEqual({row["model"] for row in detail["records"]}, {"purchase.order"})
        self.assertAlmostEqual(sum(c["value"] for c in detail["children"]), busy["value"], msg="hours add up to the day")

    def test_timeline_quantities_keep_units_apart_and_positions_are_labelled(self):
        _setup, payload = self._goal({"measure_key": "purchase_mon_ordered"})
        timeline = payload["monitor"]["timeline"]
        self.assertIn("never added", timeline["note"])
        mixed = [b for b in timeline["buckets"] if b["value"] is None and not b["future"] and "·" in b["display"]]
        self.assertTrue(all(" · " in b["display"] for b in mixed), "a day with two units lists both, not a sum")
        setup, payload = self._goal({"measure_key": "purchase_mon_overdue"})
        timeline = payload["monitor"]["timeline"]
        self.assertEqual((timeline["kind"], timeline["granularity"]), ("position", "day"))
        self.assertIn("not activity over time", timeline["note"])
        self.assertEqual(sum(b["value"] for b in timeline["buckets"]), 2, "PO1 and PO8, each once, on its expected day")
        detail = setup.get_monitor_interval(timeline["buckets"][0]["start"], timeline["buckets"][-1]["end"], "week")
        self.assertEqual({row["name"] for row in detail["records"]}, {self.po1.name, self.po8.name})

    def test_timeline_with_mixed_units_compares_records_not_quantities(self):
        _setup, payload = self._goal({"measure_key": "purchase_mon_ordered"})
        timeline = payload["monitor"]["timeline"]
        self.assertIn("bar height shows the number of records", timeline["note"])
        peak = max(timeline["buckets"], key=lambda bucket: bucket["records"])
        self.assertEqual(peak["height"], 100.0, "litres, kilograms and units are not drawn on one quantity scale")
