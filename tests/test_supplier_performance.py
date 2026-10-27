"""BDD scenarios for Purchasing Phase 2, supplier performance (tests/features/supplier_performance.feature).

Real confirmed, cancelled and draft purchase orders, receipts validated in several shipments (one in grams for a kg
product), a cancelled receipt, a return made with Odoo's return flow, product variants and a foreign currency. The café
is in Asia/Kathmandu (UTC+5:45), so a calculation using UTC days would fail. Every scenario has its own weeks of 2001.
"""

from datetime import datetime

import pytz

from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import format_amount

from odoo.addons.strategic_management.services.kpi_sources.supplier_delivery import delivery_performance_facts

TZ = pytz.timezone("Asia/Kathmandu")


def local(day, hour=10, minute=0):
    return TZ.localize(datetime.strptime(day, "%Y-%m-%d").replace(hour=hour, minute=minute)).astimezone(pytz.UTC).replace(tzinfo=None)


@tagged("post_install", "-at_install")
class TestSupplierPerformance(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "Asia/Kathmandu"})
        ref = cls.env.ref
        Warehouse = cls.env["stock.warehouse"]
        cls.branch1 = Warehouse.create({"name": "BG Supply Branch 1", "code": "BGS1", "company_id": cls.company.id})
        cls.branch2 = Warehouse.create({"name": "BG Supply Branch 2", "code": "BGS2", "company_id": cls.company.id})

        def user(login, group, warehouse=False):
            values = {"name": login, "login": login, "email": "%s@example.com" % login, "tz": "Asia/Kathmandu",
                      "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
                      "group_ids": [(6, 0, [ref("base.group_user").id, ref(group).id])]}
            if warehouse:
                values["strategic_branch_warehouse_id"] = warehouse.id
            return cls.env["res.users"].with_context(no_reset_password=True).create(values)

        cls.head = user("bg_supply_head", "strategic_management.group_strategic_head_office_manager")
        cls.manager1 = user("bg_supply_manager1", "strategic_management.group_strategic_branch_manager", cls.branch1)
        cls.kg, cls.gram, cls.litre = ref("uom.product_uom_kgm"), ref("uom.product_uom_gram"), ref("uom.product_uom_litre")
        roast = cls.env["product.attribute"].create({"name": "BG Roast", "value_ids": [(0, 0, {"name": "Light"}), (0, 0, {"name": "Dark"})]})
        template = cls.env["product.template"].create({
            "name": "BG Supply Beans", "uom_id": cls.kg.id, "is_storable": True, "purchase_ok": True,
            "attribute_line_ids": [(0, 0, {"attribute_id": roast.id, "value_ids": [(6, 0, roast.value_ids.ids)]})],
        })
        cls.beans = template.product_variant_ids.filtered(lambda p: "Light" in p.display_name)
        cls.dark = template.product_variant_ids - cls.beans
        cls.milk = cls.env["product.template"].create({"name": "BG Supply Milk", "uom_id": cls.litre.id, "is_storable": True,
                                                       "purchase_ok": True}).product_variant_id
        Partner = cls.env["res.partner"]
        cls.fast = Partner.create({"name": "BG Fast Roaster", "is_company": True, "supplier_rank": 1})
        cls.slow = Partner.create({"name": "BG Slow Roaster", "is_company": True, "supplier_rank": 1})
        cls.abroad = Partner.create({"name": "BG Import Roaster", "is_company": True, "supplier_rank": 1})
        cls.tax = cls.env["account.tax"].create({"name": "BG Supply VAT 13%", "amount": 13.0, "type_tax_use": "purchase",
                                                 "company_id": cls.company.id})
        cls.plan = cls.env["strategic.plan"].with_user(cls.head).create({
            "name": "BG Supply Plan", "company_id": cls.company.id, "date_start": "2000-01-01", "date_end": "2099-12-31"})

    # ------------------------------------------------------------------ helpers
    def _po(self, supplier, product, qty, due, uom=None, price=500.0, discount=0.0, branch=None, confirm="2001-01-02",
            currency=None):
        """One purchase order line; ``due`` is the promised arrival (UTC datetime), captured at confirmation."""
        values = {"partner_id": supplier.id, "company_id": self.company.id, "picking_type_id": (branch or self.branch1).in_type_id.id,
                  "order_line": [(0, 0, {"name": product.name, "product_id": product.id, "product_qty": qty, "price_unit": price,
                                         "discount": discount, "uom_id": (uom or product.uom_id).id, "date_planned": due,
                                         "tax_ids": [(6, 0, self.tax.ids)]})]}
        if currency:
            values["currency_id"] = currency.id
        order = self.env["purchase.order"].create(values)
        if confirm:
            order.button_confirm()
            order.write({"date_approve": local(confirm)})
        return order

    def _receive(self, order, qty, when):
        picking = order.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))[:1]
        picking.move_ids.quantity = qty
        picking.move_ids.picked = True
        picking._action_done()
        picking.move_ids.filtered(lambda m: m.state == "done").write({"date": when})

    def _goal(self, metric, period, scope="organization", branch=None, product=None, partner=None):
        Setup = self.env["strategic.goal.setup"].with_user(self.head)
        intake = {"name": "Supplier performance", "business_area": "purchasing", "deadline": "2001-12-31", "owner_id": self.head.id}
        if scope == "branch":
            parent = Setup.create_goal(dict(intake, plan_id=self.plan.id, name="Company buying"))["objective_id"]
            intake.update(scope="branch", warehouse_id=branch.id, parent_objective_id=parent)
        else:
            intake["plan_id"] = self.plan.id
        setup = Setup.browse(Setup.create_goal(intake)["setup_id"])
        payload = setup.configure_measurement({
            "template_key": "purchase_monitor", "measure_key": metric, "period_preset": "custom", "period_start": period[0],
            "period_end": period[1], "product_id": product.id if product else False, "partner_id": partner.id if partner else False})
        return setup, payload

    def _money(self, amount):
        return format_amount(self.env, amount, self.company.currency_id)

    @staticmethod
    def _cards(monitor):
        return {card["key"]: card["value_display"] for card in monitor["cards"]}

    @staticmethod
    def _rows(monitor, title):
        return {row["label"]: row["display"] for row in next(b for b in monitor["blocks"] if b["title"] == title)["rows"]}

    def _week_with_partial_and_late_deliveries(self):
        """Branch 1, due 8-14 January 2001 (local days):
        fast 10 kg due 9th, received 8th (1 day early); fast 10 kg due 10th, received 10th (on the day);
        slow 100 kg due 10th: 60 kg on 9th, 40 kg on 13th; slow 20 kg due 11th, receipt cancelled, nothing received;
        slow 10 kg due 12th received 15th (after the week, still late); fast 500 g due 12th received 12th.
        Not due in the week: a cancelled order, an RFQ, an order due on the 20th. Branch 2: 50 L due 10th, received 11th."""
        orders = {
            "early": self._po(self.fast, self.beans, 10.0, local("2001-01-09", 12)),
            "on_day": self._po(self.fast, self.beans, 10.0, local("2001-01-10", 12)),
            "split": self._po(self.slow, self.beans, 100.0, local("2001-01-10", 12)),
            "missing": self._po(self.slow, self.beans, 20.0, local("2001-01-11", 12)),
            "late": self._po(self.slow, self.beans, 10.0, local("2001-01-12", 12)),
            "grams": self._po(self.fast, self.beans, 500.0, local("2001-01-12", 12), uom=self.gram, price=0.5),
            "milk": self._po(self.fast, self.milk, 50.0, local("2001-01-10", 12), branch=self.branch2, price=2.0),
        }
        cancelled = self._po(self.slow, self.beans, 30.0, local("2001-01-10", 12))
        cancelled.button_cancel()
        self._po(self.slow, self.beans, 40.0, local("2001-01-10", 12), confirm=False)
        self._po(self.slow, self.beans, 50.0, local("2001-01-20", 12))
        self._receive(orders["early"], 10.0, local("2001-01-08", 15))
        self._receive(orders["on_day"], 10.0, local("2001-01-10", 15))
        self._receive(orders["split"], 60.0, local("2001-01-09", 15))
        self._receive(orders["split"], 40.0, local("2001-01-13", 15))
        orders["missing"].picking_ids.action_cancel()
        self._receive(orders["late"], 10.0, local("2001-01-15", 15))
        self._receive(orders["grams"], 0.5, local("2001-01-12", 15))       # Odoo receives in the product unit: 500 g = 0.5 kg
        self._receive(orders["milk"], 50.0, local("2001-01-11", 15))
        return orders

    # ------------------------------------------------------------------ A. on-time delivery rate
    def test_on_time_rate_is_weighted_by_quantity_shipment_by_shipment(self):
        """Due 150.5 kg; on time 10 + 10 + 60 (of the split 100) + 0.5 = 80.5 kg = 53.5 %. The cancelled receipt leaves
        20 kg undelivered and not on time; the late 10 kg counts though it arrived after the week."""
        orders = self._week_with_partial_and_late_deliveries()
        self.assertEqual(orders["split"].order_line.strategic_otd_baseline_date, local("2001-01-10", 12),
                         "the promised date is captured at confirmation")
        _setup, payload = self._goal("purchase_mon_on_time", ("2001-01-08", "2001-01-14"), "branch", self.branch1)
        monitor = payload["monitor"]
        self.assertEqual(monitor["value_display"], "53.5%")
        cards = self._cards(monitor)
        self.assertEqual((cards["due"], cards["undelivered"], cards["lines"]), ("150.5 kg", "20 kg", "6"))
        suppliers = self._rows(monitor, "By supplier")
        self.assertEqual(suppliers["BG Fast Roaster"], "100.0%")
        self.assertEqual(suppliers["BG Slow Roaster"], "46.2%", "60 of 130 kg on time")
        rows = {row["cells"][0]: row["cells"][2] for row in monitor["table"]["rows"]}
        self.assertIn("60 of 100 kg on time", rows[orders["split"].name])
        self.assertIn("40 kg on 13 Jan 2001 (+3 d)", rows[orders["split"].name])
        self.assertIn("nothing received · 20 kg not delivered", rows[orders["missing"].name])
        self.assertEqual(len(rows), 6, "cancelled orders, RFQs and lines due later are not due")
        self.assertNotIn("By branch", [block["title"] for block in monitor["blocks"]])

        whole = self._goal("purchase_mon_on_time", ("2001-01-08", "2001-01-14"))[1]["monitor"]
        self.assertEqual(whole["value_display"], "L: 0.0% · kg: 53.5%", "litres and kilograms are never weighted together")
        self.assertIn("By branch", [block["title"] for block in whole["blocks"]])
        product = self._goal("purchase_mon_on_time", ("2001-01-08", "2001-01-14"), product=self.milk)[1]["monitor"]
        self.assertEqual(product["value_display"], "0.0%", "50 L arrived one day late")

    # ------------------------------------------------------------------ B. average delivery delay
    def test_average_delay_is_weighted_by_received_quantity(self):
        """(10 x -1 + 10 x 0 + 60 x -1 + 40 x 3 + 10 x 3 + 0.5 x 0) / 130.5 kg = +0.6 days; the 20 kg never received have
        no delay and are shown as due but not delivered."""
        self._week_with_partial_and_late_deliveries()
        _setup, payload = self._goal("purchase_mon_delay", ("2001-01-08", "2001-01-14"), "branch", self.branch1)
        monitor = payload["monitor"]
        self.assertEqual(monitor["value_display"], "+0.6 days (late)")
        self.assertEqual(self._cards(monitor)["undelivered"], "20 kg")
        suppliers = self._rows(monitor, "By supplier")
        self.assertEqual(suppliers["BG Fast Roaster"], "-0.5 days (early)", "(10 x -1 + 10 x 0 + 0.5 x 0) / 20.5")
        self.assertEqual(suppliers["BG Slow Roaster"], "+0.8 days (late)", "(60 x -1 + 40 x 3 + 10 x 3) / 110")
        on_day = self._goal("purchase_mon_delay", ("2001-01-10", "2001-01-10"), "branch", self.branch1, product=self.beans,
                            partner=self.fast)[1]["monitor"]
        self.assertEqual(on_day["value_display"], "0.0 days (on time)", "a genuine zero, not missing data")

    def test_missing_promised_dates_and_returns_are_explicit(self):
        """A line confirmed before promised dates were recorded is listed as not judged; a later return does not change
        when goods arrived, so the rate and delay stay the same."""
        period = ("2001-01-15", "2001-01-21")
        kept = self._po(self.fast, self.beans, 10.0, local("2001-01-16", 12))
        self._receive(kept, 10.0, local("2001-01-16", 15))
        legacy = self._po(self.fast, self.beans, 5.0, local("2001-01-17", 12))
        self.env.flush_all()
        self.env.cr.execute("UPDATE purchase_order_line SET strategic_otd_baseline_captured = false, strategic_otd_baseline_date = NULL "
                            "WHERE id = %s", [legacy.order_line.id])
        legacy.order_line.invalidate_recordset()
        monitor = self._goal("purchase_mon_on_time", period, "branch", self.branch1)[1]["monitor"]
        self.assertEqual(monitor["value_display"], "100.0%")
        self.assertEqual(self._cards(monitor)["excluded"], "1")
        self.assertIn("no promised date was recorded when the order was confirmed", monitor["note"])
        receipt = kept.picking_ids.filtered(lambda p: p.state == "done")
        returned = receipt._create_return()
        returned.move_ids.write({"product_uom_qty": 4.0})
        returned.action_confirm()
        returned.move_ids.quantity = 4.0
        returned.move_ids.picked = True
        returned._action_done()
        returned.move_ids.write({"date": local("2001-01-18", 15)})
        after = self._goal("purchase_mon_on_time", period, "branch", self.branch1)[1]["monitor"]
        self.assertEqual(after["value_display"], "100.0%", "a return does not rewrite the delivery")
        delay = self._goal("purchase_mon_delay", period, "branch", self.branch1)[1]["monitor"]
        self.assertEqual(delay["value_display"], "0.0 days (on time)")

    def test_days_follow_the_company_time_zone(self):
        """Promised 21 Jan 19:00 UTC is 22 Jan in Kathmandu (in the week of 22-28 Jan); promised 28 Jan 19:00 UTC is
        29 Jan (outside). A receipt at 22 Jan 18:45 UTC is 23 Jan local: one day late, not on time."""
        inside = self._po(self.slow, self.beans, 10.0, datetime(2001, 1, 21, 19, 0))
        self._po(self.slow, self.beans, 10.0, datetime(2001, 1, 28, 19, 0))
        self._receive(inside, 10.0, datetime(2001, 1, 22, 18, 45))
        facts, excluded, problem = delivery_performance_facts(self.env, self.company, "2001-01-22", "2001-01-28", self.branch1)
        self.assertFalse(problem or excluded)
        self.assertEqual([(str(f["expected"]), f["receipts"][0][2]) for f in facts], [("2001-01-22", 1)])
        monitor = self._goal("purchase_mon_on_time", ("2001-01-22", "2001-01-28"), "branch", self.branch1)[1]["monitor"]
        self.assertEqual(monitor["value_display"], "0.0%")

    def test_no_deliveries_due_is_not_zero_percent(self):
        for metric in ("purchase_mon_on_time", "purchase_mon_delay"):
            setup, payload = self._goal(metric, ("2001-03-05", "2001-03-11"), "branch", self.branch1)
            self.assertEqual(payload["monitor"]["status"], "empty")
            self.assertIn("this is not 0%", payload["monitor"]["note"])
            self.assertEqual(setup.setup_state, "ready", "no data is checked, not a failed setup")
        self._po(self.slow, self.beans, 10.0, local("2001-03-14", 12))
        monitor = self._goal("purchase_mon_delay", ("2001-03-12", "2001-03-18"), "branch", self.branch1)[1]["monitor"]
        self.assertEqual(monitor["value_display"], "Nothing received yet", "due but not received has no delay yet")

    # ------------------------------------------------------------------ C. price comparison
    def test_price_comparison_per_supplier(self):
        """Light beans, 5-11 Feb: fast 10 kg at 500 less 10 % and 1,000 g at 0.50/g = 5,000 / 11 kg = 454.55/kg;
        slow 5 kg at 520; import 4 kg at 1,000 BGX (2 BGX = 1) = 500/kg. Tax, a cancelled order, an RFQ, the dark
        variant and an order outside the week are not compared."""
        currency = self.env["res.currency"].create({"name": "BGX", "symbol": "¤", "active": True})
        self.env["res.currency.rate"].create({"currency_id": currency.id, "name": "2001-01-01", "rate": 2.0, "company_id": self.company.id})
        due = local("2001-02-20", 12)
        self._po(self.fast, self.beans, 10.0, due, discount=10.0, confirm="2001-02-06")
        self._po(self.fast, self.beans, 1000.0, due, uom=self.gram, price=0.5, confirm="2001-02-07")
        self._po(self.slow, self.beans, 5.0, due, price=520.0, confirm="2001-02-08")
        self._po(self.abroad, self.beans, 4.0, due, price=1000.0, confirm="2001-02-09", currency=currency)
        self._po(self.slow, self.beans, 50.0, due, price=100.0, confirm="2001-02-08").button_cancel()
        self._po(self.slow, self.beans, 50.0, due, price=50.0, confirm=False)
        self._po(self.slow, self.dark, 5.0, due, price=900.0, confirm="2001-02-08")
        self._po(self.slow, self.beans, 5.0, due, price=10.0, confirm="2001-02-12")
        setup, payload = self._goal("purchase_mon_price_comparison", ("2001-02-05", "2001-02-11"), product=self.beans)
        monitor = payload["monitor"]
        self.assertEqual(setup.setup_state, "ready", payload["readiness"])
        per_kg = lambda amount: "%s/kg" % self._money(amount)
        self.assertEqual(monitor["value_display"], "%s – %s" % (per_kg(5000.0 / 11.0), per_kg(520.0)))
        rows = {row["cells"][0]: row["cells"] for row in monitor["table"]["rows"]}
        self.assertEqual(list(rows), ["BG Fast Roaster", "BG Import Roaster", "BG Slow Roaster"], "cheapest first, not a ranking")
        self.assertEqual(rows["BG Fast Roaster"][1:4], [per_kg(5000.0 / 11.0), "11 kg · 2", "%s – %s" % (per_kg(450.0), per_kg(500.0))])
        self.assertEqual((rows["BG Import Roaster"][1], rows["BG Import Roaster"][4]), (per_kg(500.0), "BGX"))
        self.assertEqual(self._cards(monitor)["suppliers"], "3")
        self.assertIn("not a supplier ranking", monitor["note"])

    def test_price_comparison_refuses_what_cannot_be_compared(self):
        _setup, payload = self._goal("purchase_mon_price_comparison", ("2001-02-05", "2001-02-11"))
        self.assertIn("choose the product whose purchase price is shown", payload["readiness"][0]["message"])
        self._po(self.fast, self.beans, 5.0, local("2001-03-20", 12), uom=self.litre, confirm="2001-03-06")
        monitor = self._goal("purchase_mon_price_comparison", ("2001-03-05", "2001-03-11"), product=self.beans)[1]["monitor"]
        self.assertEqual(monitor["status"], "data_problem")
        self.assertIn("cannot be converted", monitor["note"])
        no_rate = self.env["res.currency"].create({"name": "BGZ", "symbol": "¤", "active": True})
        self._po(self.fast, self.beans, 5.0, local("2001-04-20", 12), confirm="2001-04-03", currency=no_rate)
        monitor = self._goal("purchase_mon_price_comparison", ("2001-04-02", "2001-04-08"), product=self.beans)[1]["monitor"]
        self.assertEqual(monitor["status"], "data_problem")
        self.assertIn("no exchange rate", monitor["note"])
        self._po(self.fast, self.beans, 0.0, local("2001-05-20", 12), confirm="2001-05-08")
        setup, payload = self._goal("purchase_mon_price_comparison", ("2001-05-07", "2001-05-13"), product=self.beans)
        self.assertEqual(payload["monitor"]["status"], "empty", "a zero quantity gives no price, not a price of zero")

    # ------------------------------------------------------------------ permissions and phase 1
    def test_branch_managers_see_their_own_branch_only(self):
        self._week_with_partial_and_late_deliveries()
        period = ("2001-01-08", "2001-01-14")
        own = self._goal("purchase_mon_on_time", period, "branch", self.branch1)[0]
        other = self._goal("purchase_mon_on_time", period, "branch", self.branch2)[0]
        company = self._goal("purchase_mon_on_time", period)[0]
        view = own.with_user(self.manager1)._goal_payload(own.objective_id)["monitor"]
        self.assertEqual(view["value_display"], "53.5%")
        company_view = company.with_user(self.manager1)._goal_payload(company.objective_id)["monitor"]
        self.assertEqual((company_view["status"], company_view["cards"]), ("no_access", []))
        with self.assertRaises(AccessError):
            other.with_user(self.manager1).read(["measure_key"])

    def test_phase_one_measures_are_unchanged(self):
        """The nine measures share one option; the six Phase 1 measures still answer from the same week of orders."""
        self._week_with_partial_and_late_deliveries()
        period = ("2001-01-01", "2001-01-14")      # includes the confirmations on 2 January
        results = {metric: self._goal(metric, period, "branch", self.branch1, product=self.beans)[1]["monitor"]
                   for metric in ("purchase_mon_value", "purchase_mon_ordered", "purchase_mon_received",
                                  "purchase_mon_outstanding", "purchase_mon_overdue", "purchase_mon_unit_price")}
        for metric, monitor in results.items():
            self.assertEqual(monitor["status"], "ok", "%s: %s" % (metric, monitor["note"]))
        values = {metric: monitor["value_display"] for metric, monitor in results.items()}
        self.assertEqual(values, {
            "purchase_mon_value": self._money(100250.0),                 # 200 kg x 500 + 500 g x 0.50, cancelled and RFQ excluded
            "purchase_mon_ordered": "200.5 kg",
            "purchase_mon_received": "120.5 kg",                         # receipts completed by 14 January
            "purchase_mon_outstanding": "3 orders",                      # 20 kg (receipt cancelled), 10 kg (came 15th), 50 kg due 20th
            "purchase_mon_overdue": "2 orders",                          # due 11th and 12th; the 20th is not late
            "purchase_mon_unit_price": "%s/kg" % self._money(500.0),     # grams converted: 0.50/g = 500/kg
        })

    # ------------------------------------------------------------------ time breakdown (timeline and drill-down)
    def test_timeline_recalculates_the_rate_per_promised_day(self):
        orders = self._week_with_partial_and_late_deliveries()
        setup, payload = self._goal("purchase_mon_on_time", ("2001-01-08", "2001-01-14"), "branch", self.branch1)
        timeline = payload["monitor"]["timeline"]
        self.assertEqual((timeline["kind"], timeline["granularity"], timeline["drill"]), ("rate", "day", False),
                         "promised days have no time of day, so a day is the finest interval")
        days = {b["start"][:10]: b for b in timeline["buckets"]}
        self.assertEqual(days["2001-01-09"]["display"], "100.0%")
        self.assertEqual(days["2001-01-11"]["display"], "0.0%", "the cancelled receipt: due and not delivered")
        self.assertTrue(days["2001-01-08"]["empty"], "nothing was due on the 8th: no rate, not 0 %")
        detail = setup.get_monitor_interval("2001-01-10T00:00", "2001-01-11T00:00", "day")
        self.assertEqual({row["name"] for row in detail["records"]}, {orders["on_day"].name, orders["split"].name})
        self.assertEqual(detail["children"], [])
