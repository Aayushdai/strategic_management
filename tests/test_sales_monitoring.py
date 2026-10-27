"""BDD scenarios for goal types and configurable sales monitoring (tests/features/sales_monitoring.feature).

Real POS orders (paid, posted, draft, cancelled, refunds) in two branches, a café time zone ahead of UTC
(Asia/Kathmandu, UTC+5:45) so a window applied in UTC would fail, and the real goal workflow. Weeks are in 2001.
"""

from datetime import datetime

import pytz

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.strategic_management.services.kpi_sources.pos import window_sales

TZ = pytz.timezone("Asia/Kathmandu")
WEEK = ("2001-01-08", "2001-01-14")


def utc(day, hour, minute=0):
    local = TZ.localize(datetime.strptime(day, "%Y-%m-%d").replace(hour=hour, minute=minute))
    return local.astimezone(pytz.UTC).replace(tzinfo=None)


@tagged("post_install", "-at_install")
class TestSalesMonitoring(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "Asia/Kathmandu"})
        ref = cls.env.ref
        Warehouse = cls.env["stock.warehouse"]
        cls.branch1 = Warehouse.create({"name": "BG Monitor Branch 1", "code": "BGM1", "company_id": cls.company.id})
        cls.branch2 = Warehouse.create({"name": "BG Monitor Branch 2", "code": "BGM2", "company_id": cls.company.id})
        cls.branch3 = Warehouse.create({"name": "BG Monitor Branch 3 (no POS)", "code": "BGM3", "company_id": cls.company.id})
        cls.pos = {branch: cls.env["pos.config"].create({"name": "POS %s" % branch.code, "picking_type_id": branch.pos_type_id.id})
                   for branch in (cls.branch1, cls.branch2)}

        def user(login, group, warehouse=False):
            values = {"name": login, "login": login, "email": "%s@example.com" % login, "tz": "Asia/Kathmandu",
                      "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
                      "group_ids": [(6, 0, [ref("base.group_user").id, ref(group).id])]}
            if warehouse:
                values["strategic_branch_warehouse_id"] = warehouse.id
            return cls.env["res.users"].with_context(no_reset_password=True).create(values)

        cls.head = user("bg_monitor_head", "strategic_management.group_strategic_head_office_manager")
        cls.manager1 = user("bg_monitor_manager1", "strategic_management.group_strategic_branch_manager", cls.branch1)
        cls.employee = user("bg_monitor_employee", "strategic_management.group_strategic_employee", cls.branch1)
        Category = cls.env["pos.category"]
        cls.drinks = Category.create({"name": "BG Monitor Drinks"})
        cls.hot_drinks = Category.create({"name": "BG Monitor Hot drinks", "parent_id": cls.drinks.id})
        cls.food = Category.create({"name": "BG Monitor Food"})
        units, kg = ref("uom.product_uom_unit"), ref("uom.product_uom_kgm")

        def item(name, uom, category):
            return cls.env["product.template"].create({
                "name": name, "uom_id": uom.id, "type": "consu", "available_in_pos": True,
                "pos_categ_ids": [(6, 0, category.ids)]}).product_variant_id

        cls.latte = item("BG Monitor Latte", units, cls.hot_drinks)
        cls.muffin = item("BG Monitor Muffin", units, cls.food)
        cls.beans = item("BG Monitor Beans by weight", kg, cls.food)
        cls.plan = cls.env["strategic.plan"].with_user(cls.head).create({
            "name": "BG Monitor Plan", "company_id": cls.company.id, "date_start": "2000-01-01", "date_end": "2099-12-31"})

        # Branch 1, 17:00-21:00 local time, Monday 8 to Sunday 14 January 2001:
        cls.o1 = cls._order(cls.branch1, utc("2001-01-08", 17, 30), [(cls.latte, 2, 4.0, 0), (cls.muffin, 1, 3.0, 0)])  # 11.00
        cls._order(cls.branch1, utc("2001-01-08", 20, 59), [(cls.latte, 1, 4.0, 10)])                     # 3.60 after 10 %
        cls._order(cls.branch1, utc("2001-01-08", 21, 0), [(cls.latte, 5, 4.0, 0)])                       # end excluded
        cls._order(cls.branch1, utc("2001-01-08", 16, 59), [(cls.latte, 5, 4.0, 0)])                      # before start
        cls._order(cls.branch1, utc("2001-01-09", 18, 0), [(cls.beans, 0.5, 20.0, 0)])                    # 10.00, kg
        cls._order(cls.branch1, utc("2001-01-09", 12, 0), [(cls.latte, 3, 4.0, 0)])                       # midday
        cls._order(cls.branch1, utc("2001-01-15", 18, 0), [(cls.latte, 7, 4.0, 0)])                       # after the period
        cls._order(cls.branch1, utc("2001-01-09", 19, 0), [(cls.latte, 4, 4.0, 0)], state="cancel")
        cls._order(cls.branch1, utc("2001-01-09", 19, 30), [(cls.latte, 4, 4.0, 0)], state="draft")
        cls._order(cls.branch1, utc("2001-01-10", 18, 30), [(cls.latte, -1, 4.0, 0)], refund=True)       # -4.00
        cls._order(cls.branch2, utc("2001-01-08", 18, 0), [(cls.latte, 10, 4.0, 0)])                      # 40.00
        cls._order(cls.branch1, utc("2001-01-03", 18, 0), [(cls.latte, 5, 4.0, 0)])                       # previous week 20.00

    @classmethod
    def _order(cls, branch, date_order, lines, state="paid", refund=False):
        """A POS order as the POS records it: lines excluding tax after discount, 13 % tax only in the order totals."""
        config = cls.pos[branch]
        session = cls.env["pos.session"].search([("config_id", "=", config.id), ("state", "!=", "closed")], limit=1) \
            or cls.env["pos.session"].create({"config_id": config.id, "user_id": cls.env.uid})
        values, untaxed = [], 0.0
        for product, qty, price, discount in lines:
            subtotal = qty * price * (1 - discount / 100.0)
            untaxed += subtotal
            values.append((0, 0, {"product_id": product.id, "qty": qty, "price_unit": price, "discount": discount,
                                  "price_subtotal": subtotal, "price_subtotal_incl": subtotal * 1.13,
                                  "full_product_name": product.name}))
        tax = round(untaxed * 0.13, 2)
        return cls.env["pos.order"].create({
            "session_id": session.id, "company_id": cls.company.id, "state": state, "date_order": date_order,
            "amount_tax": tax, "amount_total": untaxed + tax, "amount_paid": untaxed + tax, "amount_return": 0.0,
            "is_refund": refund, "lines": values,
        })

    def _sales(self, window=(17.0, 21.0), branch=None, product=False, category=False, period=WEEK):
        return window_sales(self.env, self.company, fields.Date.to_date(period[0]), fields.Date.to_date(period[1]),
                            window[0], window[1], branch if branch is not None else self.branch1, product, category)

    def _goal(self, values, scope="organization", branch=None, user=None):
        user = user or self.head
        Setup = self.env["strategic.goal.setup"].with_user(user)
        intake = {"name": "Evening sales", "business_area": "sales_finance", "deadline": "2001-01-31", "owner_id": user.id}
        if scope == "branch":
            parent = Setup.create_goal(dict(intake, plan_id=self.plan.id, name="Company sales"))["objective_id"]
            intake.update(scope="branch", warehouse_id=branch.id, parent_objective_id=parent)
        else:
            intake["plan_id"] = self.plan.id
        setup = Setup.browse(Setup.create_goal(intake)["setup_id"])
        config = {"template_key": "sales_time_window", "measure_key": "pos_window_sales", "period_preset": "custom",
                  "period_start": WEEK[0], "period_end": WEEK[1], "window_start": "17:00", "window_end": "21:00"}
        config.update(values)
        payload = setup.configure_measurement(config)
        return setup, payload

    # ------------------------------------------------------------------ calculation
    def test_window_uses_local_time_end_exclusive_on_every_day_of_the_period(self):
        """Given orders at 17:30, 20:59, 21:00, 16:59 and midday in the café's time zone, then only 17:30 and 20:59
        count on Monday, 18:00 on Tuesday, and the refund on Wednesday; nothing after Sunday or outside the window."""
        data = self._sales()
        self.assertFalse(data["problem"])
        total = data["total"]
        self.assertAlmostEqual(total["revenue"], 11.0 + 3.6 + 10.0 - 4.0, msg="excluding 13 % tax, after the 10 % discount")
        self.assertEqual(total["orders"], 3, "one order with two lines is one order; the refund is not an order")
        self.assertEqual(total["quantity"], {"Units": 3.0, "kg": 0.5}, "different units are kept apart")
        days = {str(day): bucket["revenue"] for day, bucket in data["days"].items()}
        self.assertEqual(len(days), 7, "every day of the period is listed")
        self.assertAlmostEqual(days["2001-01-08"], 14.6)
        self.assertAlmostEqual(days["2001-01-09"], 10.0)
        self.assertAlmostEqual(days["2001-01-10"], -4.0, msg="the refund reduces sales on the day it was made")
        self.assertEqual(days["2001-01-11"], 0.0)

    def test_product_category_and_branch_filters(self):
        latte = self._sales(product=self.latte)["total"]
        self.assertAlmostEqual(latte["revenue"], 8.0 + 3.6 - 4.0)
        self.assertEqual((latte["orders"], latte["quantity"]), (2, {"Units": 2.0}))
        drinks = self._sales(category=self.drinks)["total"]
        self.assertAlmostEqual(drinks["revenue"], 7.6, msg="a parent category includes its sub-categories")
        food = self._sales(category=self.food)["total"]
        self.assertAlmostEqual(food["revenue"], 13.0)
        self.assertEqual(food["quantity"], {"Units": 1.0, "kg": 0.5})
        self.assertAlmostEqual(self._sales(branch=self.branch2)["total"]["revenue"], 40.0)
        whole = self._sales(branch=False)
        self.assertAlmostEqual(whole["total"]["revenue"], 60.6, msg="each order once, whichever branch")
        by_branch = {bucket["name"]: bucket["revenue"] for bucket in whole["branches"].values()}
        self.assertAlmostEqual(by_branch[self.branch1.display_name], 20.6)
        self.assertAlmostEqual(by_branch[self.branch2.display_name], 40.0)

    def test_missing_data_is_never_zero_sales(self):
        self.assertIn("No Point of Sale is linked", self._sales(branch=self.branch3)["problem"])
        empty = self._sales(period=("2001-03-05", "2001-03-11"))
        self.assertEqual(empty["orders_in_period"], 0)
        quiet = self._sales(window=(6.0, 7.0))
        self.assertEqual((quiet["orders_in_period"] > 0, quiet["total"]["orders"]), (True, 0))

    # ------------------------------------------------------------------ monitoring goal
    def test_monitoring_goal_needs_no_target_and_is_never_achieved_or_failed(self):
        """Given 'Monitor sales in a time window' for the whole business, 17:00-21:00 for 8-14 January, then it is
        ready without a target, shows 60.60 with both branches and the previous week, and once approved it is
        'Monitoring', not on track or at risk."""
        setup, payload = self._goal({})
        self.assertEqual(setup.setup_state, "ready", payload["readiness"])
        self.assertEqual(setup.objective_id.goal_type, "monitoring")
        self.assertEqual((payload["target"], payload["can_prepare_target"], payload["can_submit"]), ("No target (monitoring)", False, True))
        monitor = payload["monitor"]
        currency = self.company.currency_id
        self.assertEqual(monitor["window_label"], "17:00–21:00")
        self.assertEqual(monitor["value_display"], payload["current"])
        self.assertIn("60.60", monitor["value_display"])
        self.assertEqual([row["label"] for row in monitor["branches"]], sorted([self.branch1.display_name, self.branch2.display_name]))
        self.assertIn("20.00", monitor["previous"]["display"], "previous week, same window: the 20.00 order on 3 January")
        self.assertIn("+203.0%", monitor["previous"]["change"])
        self.assertEqual(len(monitor["trend"]), 7)
        cards = {card["key"]: card["value_display"] for card in monitor["cards"]}
        self.assertEqual(cards["orders"], "4 orders")
        self.assertEqual(cards["quantity"], "13 Units · 0.5 kg", "different units are listed, never added")
        self.assertIn(currency.symbol or currency.name, cards["aov"])
        with self.assertRaisesRegex(UserError, "no target"):
            setup.prepare_target()

        setup.submit_goal()
        setup.approve_goal()
        goal = next(g for g in setup.get_goals("sales_finance") if g["objective_id"] == setup.objective_id.id)
        self.assertEqual((goal["status"], goal["tone"], goal["category"]), ("Monitoring", "neutral", "monitoring"))
        self.assertFalse(goal["bar"], "no progress bar or achievement for monitoring")

    def test_monitoring_settings_are_validated(self):
        _setup, payload = self._goal({"window_start": "22:00", "window_end": "02:00"})
        setup_check = next(check for check in payload["readiness"] if check["key"] == "setup")
        self.assertFalse(setup_check["ok"])
        self.assertIn("across midnight", setup_check["message"])
        _setup, payload = self._goal({"measure_key": "pos_window_average_order_value", "product_id": self.latte.id})
        self.assertIn("remove the product and category filters", payload["readiness"][0]["message"])
        with self.assertRaisesRegex(UserError, "HH:MM"):
            self._goal({"window_start": "5pm"})
        _setup, payload = self._goal({"measure_key": "pos_window_average_order_value", "window_start": "06:00", "window_end": "07:00"})
        self.assertEqual(payload["monitor"]["value_display"], "No orders", "no division by zero")
        self.assertIn("none between 06:00 and 07:00", payload["monitor"]["note"])

    def test_today_preset_and_empty_periods(self):
        now = fields.Datetime.now()
        self._order(self.branch1, now, [(self.muffin, 2, 3.0, 0)])
        setup, payload = self._goal({"measure_key": "pos_window_quantity", "period_preset": "today", "product_id": self.muffin.id,
                                     "window_start": "00:00", "window_end": "24:00"}, scope="branch", branch=self.branch1)
        self.assertEqual(payload["monitor"]["value_display"], "2 Units")
        self.assertEqual(payload["monitor"]["period_label"].count(" – "), 1)
        _setup, payload = self._goal({"period_start": "2001-03-05", "period_end": "2001-03-11"})
        self.assertEqual(payload["monitor"]["status"], "empty")
        self.assertEqual(payload["current"], "No POS data", "an empty period is not shown as zero sales")
        _setup, payload = self._goal({}, scope="branch", branch=self.branch3)
        self.assertEqual(payload["monitor"]["status"], "data_problem")
        self.assertFalse(next(check for check in payload["readiness"] if check["key"] == "data")["ok"])

    # ------------------------------------------------------------------ permissions
    def test_branch_managers_see_their_own_branch_only(self):
        own, _payload = self._goal({}, scope="branch", branch=self.branch1)
        other, _payload = self._goal({}, scope="branch", branch=self.branch2)
        company, _payload = self._goal({})
        own_view = own.with_user(self.manager1)._goal_payload(own.objective_id)["monitor"]
        self.assertEqual(own_view["status"], "ok")
        self.assertIn("20.60", own_view["value_display"])
        self.assertEqual(own_view["branches"], [], "no branch comparison for a branch manager")
        company_view = company.with_user(self.manager1)._goal_payload(company.objective_id)
        self.assertEqual(company_view["monitor"]["status"], "no_access", "company-wide sales are Head Office only")
        self.assertFalse(company_view["monitor"]["cards"])
        with self.assertRaises(AccessError):
            other.with_user(self.manager1).read(["measure_key"])
        with self.assertRaises(AccessError):
            own.with_user(self.employee).read(["measure_key"])

    # ------------------------------------------------------------------ the other goal types
    def test_optimization_and_compliance_keep_the_existing_target_rules(self):
        Setup = self.env["strategic.goal.setup"].with_user(self.head)
        sales = Setup.browse(Setup.create_goal({"name": "Grow sales", "business_area": "sales_finance", "deadline": "2001-01-31",
                                                "plan_id": self.plan.id, "owner_id": self.head.id})["setup_id"])
        payload = sales.configure_measurement({"template_key": "net_sales", "target_value": 0.0,
                                               "period_start": WEEK[0], "period_end": WEEK[1]})
        self.assertEqual(sales.objective_id.goal_type, "optimization")
        self.assertIn("enter a target above zero", payload["readiness"][0]["message"], "optimization still needs a target")

        counts = Setup.browse(Setup.create_goal({"name": "Count stock", "business_area": "inventory_waste", "deadline": "2001-01-31",
                                                 "plan_id": self.plan.id, "owner_id": self.head.id})["setup_id"])
        counts.configure_measurement({"template_key": "count_completion", "target_value": 80.0,
                                      "period_start": WEEK[0], "period_end": WEEK[1]})
        self.assertEqual(counts.objective_id.goal_type, "compliance")
        counts.prepare_target()
        counts.approve_target()
        counts.submit_goal()
        counts.approve_goal()
        counts.evaluate_now()
        goal = next(g for g in Setup.get_goals("inventory_waste") if g["objective_id"] == counts.objective_id.id)
        self.assertEqual(counts.key_result_id.evaluation_id.status, "not_evaluated", "no plans: no evidence either way")
        self.assertEqual((goal["status"], goal["category"]), ("Not evaluated", "setup"),
                         "missing data is not a failed requirement")
        self.assertNotEqual(goal["tone"], "bad")

    # ------------------------------------------------------------------ time breakdown (timeline and drill-down)
    def test_timeline_adds_up_to_the_goal_and_drills_into_hours(self):
        """Given the company-wide evening-sales goal for 8-14 January, then the card shows one bar per day that adds
        up to 60.60, and Monday opens into the window's hours with the real orders behind each hour."""
        setup, payload = self._goal({})
        timeline = payload["monitor"]["timeline"]
        self.assertEqual((timeline["status"], timeline["granularity"], timeline["drill"], timeline["kind"]), ("ok", "day", "hour", "flow"))
        self.assertIn("Asia/Kathmandu", timeline["caption"])
        buckets = timeline["buckets"]
        self.assertEqual(len(buckets), 7)
        self.assertAlmostEqual(sum(bucket["value"] for bucket in buckets), 60.6, msg="intervals reconcile with the goal")
        monday = buckets[0]
        self.assertAlmostEqual(monday["value"], 54.6)
        self.assertEqual(monday["records"], 3)
        self.assertTrue(buckets[2]["negative"], "the refund day is negative, not hidden")
        self.assertEqual(buckets[3]["value"], 0.0, "a day inside the period with no sales in the window is a real zero")

        detail = setup.get_monitor_interval(monday["start"], monday["end"], "day")
        self.assertAlmostEqual(detail["value"], 54.6)
        self.assertEqual(detail["scope"]["window"], "17:00–21:00")
        self.assertEqual(detail["scope"]["branch"], "All branches")
        hours = {child["label"]: child["value"] for child in detail["children"]}
        self.assertEqual(list(hours), ["17:00", "18:00", "19:00", "20:00"], "only the window's hours")
        self.assertAlmostEqual(hours["17:00"], 11.0)
        self.assertAlmostEqual(hours["18:00"], 40.0)
        self.assertAlmostEqual(hours["20:00"], 3.6)
        self.assertEqual(hours["19:00"], 0.0)
        self.assertIn(self.o1.name, [row["name"] for row in detail["records"]])
        self.assertEqual({row["model"] for row in detail["records"]}, {"pos.order"})

        hour = setup.get_monitor_interval("2001-01-08T17:00", "2001-01-08T18:00", "hour")
        self.assertEqual([row["name"] for row in hour["records"]], [self.o1.name])
        self.assertAlmostEqual(hour["value"], 11.0)
        self.assertEqual(hour["children"], [], "an hour is the finest interval")

    def test_timeline_rates_and_counts_are_recalculated_per_interval(self):
        setup, payload = self._goal({"measure_key": "pos_window_average_order_value"})
        timeline = payload["monitor"]["timeline"]
        self.assertEqual(timeline["kind"], "rate")
        empty_day = timeline["buckets"][3]
        self.assertIsNone(empty_day["value"])
        self.assertEqual(empty_day["display"], "No orders", "an average with no orders is not zero")
        self.assertTrue(empty_day["empty"])
        _setup, payload = self._goal({"measure_key": "pos_window_orders"})
        self.assertEqual(sum(b["value"] for b in payload["monitor"]["timeline"]["buckets"]), 4, "4 orders, the refund is not an order")

    def test_timeline_clamps_requests_and_keeps_branch_scope(self):
        own, payload = self._goal({}, scope="branch", branch=self.branch1)
        company, _payload = self._goal({})
        mine = own.with_user(self.manager1)
        monday = mine.get_monitor_interval("2001-01-08T00:00", "2001-01-09T00:00", "day")
        self.assertAlmostEqual(monday["value"], 14.6, msg="only branch 1's orders, never branch 2's 40.00")
        self.assertEqual(monday["scope"]["branch"], self.branch1.display_name)
        wide = mine.get_monitor_interval("2000-01-01T00:00", "2002-01-01T00:00", "week")
        self.assertAlmostEqual(wide["value"], 20.6, msg="a widened request is clamped to the goal's own period")
        with self.assertRaises(UserError):
            mine.get_monitor_interval("2001-02-01T00:00", "2001-02-02T00:00", "day")
        with self.assertRaises(UserError):
            mine.get_monitor_interval("yesterday", "today", "day")
        with self.assertRaises(AccessError):
            company.with_user(self.manager1).get_monitor_interval("2001-01-08T00:00", "2001-01-09T00:00", "day")
        self.assertNotIn("timeline", company.with_user(self.manager1)._goal_payload(company.objective_id)["monitor"])

    def test_timeline_marks_future_hours_and_skips_empty_periods(self):
        self._order(self.branch1, fields.Datetime.now(), [(self.muffin, 2, 3.0, 0)])
        _setup, payload = self._goal({"measure_key": "pos_window_quantity", "period_preset": "today", "product_id": self.muffin.id,
                                      "window_start": "00:00", "window_end": "24:00"}, scope="branch", branch=self.branch1)
        timeline = payload["monitor"]["timeline"]
        self.assertEqual(timeline["granularity"], "hour")
        future = [bucket for bucket in timeline["buckets"] if bucket["future"]]
        self.assertTrue(all(bucket["display"] == "Not yet" and bucket["value"] is None for bucket in future), "the future is not zero")
        self.assertEqual(sum(b["value"] for b in timeline["buckets"] if b["value"] is not None), 2.0)
        _setup, payload = self._goal({"period_start": "2001-03-05", "period_end": "2001-03-11"})
        self.assertNotIn("timeline", payload["monitor"], "no orders at all: no chart is drawn")
