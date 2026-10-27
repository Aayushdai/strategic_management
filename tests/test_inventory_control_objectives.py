"""BDD scenarios for the inventory-control objectives (tests/features/inventory_control_objectives.feature).

Recipe coverage, planned stock count completion, waste recording compliance and unexplained ingredient use, measured
from real POS orders (Odoo's own kit deduction), applied stock counts, waste records and planned counts through
approved KPI targets. Measured weeks are in 2001, so no real company data falls inside them.
"""

from datetime import datetime
from itertools import count

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.strategic_management.models.kpi import _validate_calculation_contract

WEEK = ("2001-01-08", "2001-01-14")
FEB = ("2001-02-05", "2001-02-11")
MARCH = ("2001-03-05", "2001-03-11")
APRIL = ("2001-04-02", "2001-04-08")
MAY = ("2001-05-07", "2001-05-13")
_CODES = count()


def at(day, hour=10):
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=hour)


@tagged("post_install", "-at_install")
class TestInventoryControlObjectives(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        cls.company.sudo().point_of_sale_update_stock_quantities = "real"
        ref = cls.env.ref
        Warehouse = cls.env["stock.warehouse"]
        cls.branch1 = Warehouse.create({"name": "BG Control Branch 1", "code": "BGK1", "company_id": cls.company.id})
        cls.branch2 = Warehouse.create({"name": "BG Control Branch 2", "code": "BGK2", "company_id": cls.company.id})
        cls.pos = {
            branch: cls.env["pos.config"].create({"name": "POS %s" % branch.code, "picking_type_id": branch.pos_type_id.id})
            for branch in (cls.branch1, cls.branch2)
        }

        def user(login, group, warehouse=False):
            values = {"name": login, "login": login, "email": "%s@example.com" % login, "tz": "UTC",
                      "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
                      "group_ids": [(6, 0, [ref("base.group_user").id, ref("stock.group_stock_user").id, ref(group).id])]}
            if warehouse:
                values["strategic_branch_warehouse_id"] = warehouse.id
            return cls.env["res.users"].with_context(no_reset_password=True).create(values)

        cls.head = user("bg_control_head", "strategic_management.group_strategic_head_office_manager")
        cls.manager1 = user("bg_control_manager1", "strategic_management.group_strategic_branch_manager", cls.branch1)
        cls.kg, cls.gram, cls.units = ref("uom.product_uom_kgm"), ref("uom.product_uom_gram"), ref("uom.product_uom_unit")
        Template = cls.env["product.template"]
        cls.coffee = Template.create({"name": "BG Control Coffee Beans", "uom_id": cls.kg.id, "is_storable": True}).product_variant_id
        cls.milk = Template.create({"name": "BG Control Milk", "uom_id": ref("uom.product_uom_litre").id,
                                    "is_storable": True}).product_variant_id

        def menu_item(name, needs_recipe):
            return Template.create({"name": name, "uom_id": cls.units.id, "type": "consu", "available_in_pos": True,
                                    "strategic_recipe_required": needs_recipe}).product_variant_id

        cls.latte = menu_item("BG Control Latte", True)
        cls.cappuccino = menu_item("BG Control Cappuccino", True)       # needs a recipe, has none
        cls.mocha = menu_item("BG Control Mocha", True)                 # recipe changed after the sale
        cls.water = menu_item("BG Control Bottled Water", False)        # sold as it is: no recipe needed

        def recipe(item, changed_on="2000-01-01"):
            bom = cls.env["mrp.bom"].create({
                "product_tmpl_id": item.product_tmpl_id.id, "product_qty": 1.0, "type": "phantom",
                "bom_line_ids": [(0, 0, {"product_id": cls.coffee.id, "product_qty": 18.0, "uom_id": cls.gram.id})],
            })
            cls.env.cr.execute("UPDATE mrp_bom SET create_date = '2000-01-01', write_date = %s WHERE id = %s", [changed_on, bom.id])
            bom.invalidate_recordset()
            return bom

        recipe(cls.latte)
        recipe(cls.mocha, changed_on="2001-04-05")
        Tag = cls.env["stock.scrap.reason.tag"]
        cls.spoiled = Tag.search([("name", "=", "Spoilage/Expiry")], limit=1) or Tag.create({"name": "Spoilage/Expiry"})
        cls.Plan = cls.env["strategic.stock.count.plan"]

    # ------------------------------------------------------------------ helpers
    def _measure(self, calculation, unit, product=None, period=WEEK, branch=None):
        """One approved KPI target for a branch, evaluated by its real collector."""
        branch = branch or self.branch1
        kpi = self.env["strategic.kpi.definition"].with_user(self.head).create({
            "name": calculation, "code": "BGK-%s-%s" % (calculation, next(_CODES)), "company_id": self.company.id,
            "unit": unit, "direction": "higher", "source_type": "inventory", "calculation_type": calculation,
            "source_product_id": product.id if product else False,
        })
        target = self.env["strategic.kpi.target"].with_user(self.head).create({
            "kpi_id": kpi.id, "date_start": period[0], "date_end": period[1], "scope": "branch",
            "warehouse_id": branch.id, "target_basis": "absolute", "target_value": 1.0,
        })
        target.action_approve()
        return self.env["strategic.kpi.evaluation"].browse(target.action_evaluate_source()["res_id"])

    def _receive(self, quantity, day, branch=None):
        move = self.env["stock.move"].create({
            "product_id": self.coffee.id, "product_uom_qty": quantity, "quantity": quantity, "uom_id": self.kg.id,
            "location_id": self.env.ref("stock.stock_location_suppliers").id,
            "location_dest_id": (branch or self.branch1).lot_stock_id.id,
        })
        move._action_confirm()
        move.picked = True
        move._action_done()
        move.date = at(day)

    def _sell(self, product, quantity, day, branch=None):
        config = self.pos[branch or self.branch1]
        session = self.env["pos.session"].search([("config_id", "=", config.id), ("state", "!=", "closed")], limit=1) \
            or self.env["pos.session"].create({"config_id": config.id, "user_id": self.env.uid})
        total = 4.0 * quantity
        order = self.env["pos.order"].create({
            "session_id": session.id, "company_id": self.company.id, "state": "paid", "date_order": at(day, 12),
            "amount_tax": 0.0, "amount_total": total, "amount_paid": total, "amount_return": 0.0,
            "lines": [(0, 0, {"product_id": product.id, "qty": quantity, "price_unit": 4.0, "price_subtotal": total,
                              "price_subtotal_incl": total, "full_product_name": product.name})],
        })
        order._create_order_picking()          # Odoo's own POS stock deduction, real-time mode
        order.picking_ids.move_ids.write({"date": at(day, 12)})

    def _scrap(self, quantity, day, tagged_reason=True):
        move = self.env["stock.move"].create({
            "product_id": self.coffee.id, "product_uom_qty": quantity, "quantity": quantity, "uom_id": self.kg.id,
            "location_id": self.branch1.lot_stock_id.id,
            "scrap_reason_tag_ids": [(6, 0, self.spoiled.ids if tagged_reason else [])],
            "location_dest_id": self.env["stock.location"].search(
                [("usage", "=", "inventory"), ("company_id", "in", [False, self.company.id])], limit=1).id,
        })
        move.action_scrap()
        move.date = at(day, 14)

    def _count(self, product, day, difference=0.0, hour=8, branch=None, apply=True):
        """A physical count at the branch: counted = system quantity + difference, applied unless told otherwise."""
        location = (branch or self.branch1).lot_stock_id
        Quant = self.env["stock.quant"].with_context(inventory_mode=True)
        quant = Quant.search([("product_id", "=", product.id), ("location_id", "=", location.id)], limit=1)
        system = quant.quantity if quant else 0.0
        if quant:
            quant.inventory_quantity = system + difference
        else:
            quant = Quant.create({"product_id": product.id, "location_id": location.id, "inventory_quantity": system + difference})
        if apply:
            quant.action_apply_inventory()
            self.env["stock.move"].search([("product_id", "=", product.id), ("is_inventory", "=", True)],
                                          order="id desc", limit=1).date = at(day, hour)

    def _plan(self, day, products, branch=None, due=None):
        return self.Plan.with_user(self.head).create({
            "warehouse_id": (branch or self.branch1).id, "product_ids": [(6, 0, products.ids)], "date_planned": day,
            "date_due": due or day,
        })

    # ------------------------------------------------------------------ A: recipe coverage
    def test_recipe_coverage_counts_each_sold_menu_item_that_needs_a_recipe_once(self):
        """Given Branch 1 sold lattes (valid recipe), cappuccinos (no recipe), mochas (recipe changed after the sale) and
        bottled water (no recipe needed), and Branch 2 sold only lattes, then Branch 1 covers 1 of 3 items (33.3%),
        names the two gaps and ignores the water; Branch 2 covers 100%."""
        day = APRIL[0]
        self._sell(self.latte, 10, "2001-04-03")
        self._sell(self.cappuccino, 5, "2001-04-03")
        self._sell(self.mocha, 2, "2001-04-03")
        self._sell(self.water, 30, "2001-04-03")
        self._sell(self.latte, 4, day, branch=self.branch2)

        branch1 = self._measure("inventory_recipe_coverage_pct", "%", period=APRIL)
        self.assertEqual(branch1.status, "valid", branch1.reason)
        self.assertAlmostEqual(branch1.actual_value, 100.0 / 3, places=4, msg="counted once per item, not by quantity sold")
        self.assertIn("1 of 3 menu items", branch1.source_note)
        self.assertIn("BG Control Cappuccino (no recipe", branch1.source_note)
        self.assertIn("BG Control Mocha (", branch1.source_note)
        self.assertNotIn("Bottled Water", branch1.source_note)

        branch2 = self._measure("inventory_recipe_coverage_pct", "%", period=APRIL, branch=self.branch2)
        self.assertEqual((branch2.status, branch2.actual_value), ("valid", 100.0), branch2.reason)

    def test_recipe_coverage_without_sales_is_not_evaluated_and_takes_no_product(self):
        self._sell(self.water, 3, MAY[0])
        evaluation = self._measure("inventory_recipe_coverage_pct", "%", period=MAY)
        self.assertEqual(evaluation.status, "not_evaluated", "only unflagged items sold: no 0% or 100%")
        self.assertFalse(evaluation.has_actual_value)
        with self.assertRaises(ValidationError):
            _validate_calculation_contract(self.env, "inventory", "inventory_recipe_coverage_pct", self.coffee.id, "%", self.company.id)

    # ------------------------------------------------------------------ B: planned stock count completion
    def test_planned_count_completion_uses_applied_counts_on_time_per_branch(self):
        """Given Branch 1 plans: counted on time (done), quantities entered but never applied (missed), counted only after
        the count-by date (missed), cancelled in time (left out) and cancelled after its date (missed), then Branch 1
        completes 1 of 4 (25%). Branch 2 completes 1 of 2: a Branch 1 count does not satisfy a Branch 2 plan."""
        done = self._plan("2001-01-08", self.coffee | self.milk)
        not_applied = self._plan("2001-01-11", self.coffee, due="2001-01-12")
        late = self._plan("2001-01-09", self.milk)
        cancelled_in_time = self._plan("2001-01-10", self.milk)
        cancelled_late = self._plan("2001-01-13", self.milk)
        self._count(self.coffee, "2001-01-08")
        self._count(self.milk, "2001-01-08", difference=2.0)
        self._count(self.coffee, "2001-01-11", difference=1.0, apply=False)
        self._count(self.milk, "2001-01-12")
        cancelled_in_time.write({"state": "cancelled", "cancelled_on": "2001-01-09", "cancel_reason": "Branch closed for repairs"})
        with self.assertRaisesRegex(ValidationError, "Enter why"):
            cancelled_late.action_cancel()
        cancelled_late.cancel_reason = "Forgot"
        cancelled_late.action_cancel()

        self.assertEqual([p.count_status for p in (done, not_applied, late, cancelled_in_time, cancelled_late)],
                         ["done", "missed", "missed", "cancelled", "missed"])
        self.assertEqual(not_applied.missing_product_ids, self.coffee)
        branch1 = self._measure("inventory_count_plan_completion_pct", "%")
        self.assertEqual(branch1.status, "valid", branch1.reason)
        self.assertAlmostEqual(branch1.actual_value, 25.0)
        self.assertIn("1 of 4 planned stock counts", branch1.source_note)
        self.assertIn("BGK1 count 2001-01-11 (not counted: BG Control Coffee Beans", branch1.source_note)

        self._plan("2001-01-09", self.coffee, branch=self.branch2)
        self._plan("2001-01-08", self.milk, branch=self.branch2)
        self._count(self.coffee, "2001-01-09", branch=self.branch2)
        branch2 = self._measure("inventory_count_plan_completion_pct", "%", branch=self.branch2)
        self.assertAlmostEqual(branch2.actual_value, 50.0, msg=branch2.reason)

    def test_planned_counts_without_plans_or_not_yet_due_are_not_scored(self):
        evaluation = self._measure("inventory_count_plan_completion_pct", "%", period=FEB)
        self.assertEqual(evaluation.status, "not_evaluated")
        self.assertIn("No stock count is planned", evaluation.source_note)
        future = self._plan("2099-01-05", self.coffee)
        self.assertEqual(future.count_status, "open")
        future.date_planned = "2099-01-06"          # not due yet: still editable

    def test_past_plans_are_locked_and_branch_managers_only_read_their_own_branch(self):
        own = self._plan("2001-01-08", self.coffee)
        other = self._plan("2001-01-08", self.coffee, branch=self.branch2)
        with self.assertRaisesRegex(ValidationError, "past its count-by date"):
            own.with_user(self.head).write({"product_ids": [(6, 0, self.milk.ids)]})
        with self.assertRaisesRegex(ValidationError, "past its count-by date"):
            own.with_user(self.head).unlink()
        Plan = self.Plan.with_user(self.manager1)
        self.assertEqual(Plan.search([("id", "in", (own | other).ids)]), own)
        with self.assertRaises(AccessError):
            other.with_user(self.manager1).read(["name"])
        with self.assertRaises(AccessError):
            Plan.create({"warehouse_id": self.branch1.id, "product_ids": [(6, 0, self.coffee.ids)], "date_planned": "2099-01-01"})

    # ------------------------------------------------------------------ C and D: waste recording, ingredient use
    def test_counts_reveal_unrecorded_loss_and_unexplained_ingredient_use(self):
        """Given 10 kg of coffee counted on Monday, 5 kg received, 100 lattes sold (18 g each, 1.8 kg deducted by
        Odoo), 0.5 kg recorded as spoiled and a Friday count 0.5 kg short, then half of the 1 kg identified loss was
        recorded (50%) and the counts show 2.3 kg used against 1.8 kg explained by sales and recipes: +0.5 kg."""
        self._receive(10.0, "2001-01-02")
        self._count(self.coffee, "2001-01-08", hour=8)
        self._receive(5.0, "2001-01-09")
        self._sell(self.latte, 100, "2001-01-09")
        self._scrap(0.5, "2001-01-10")
        self._count(self.coffee, "2001-01-12", difference=-0.5, hour=20)

        recording = self._measure("inventory_waste_recording_pct", "%", self.coffee)
        self.assertEqual(recording.status, "valid", recording.reason)
        self.assertAlmostEqual(recording.actual_value, 50.0)
        self.assertIn("Found missing at the stock counts and never recorded: 0.5 kg", recording.source_note)

        use = self._measure("inventory_ingredient_use_difference_quantity", "kg", self.coffee)
        self.assertEqual(use.status, "valid", use.reason)
        self.assertAlmostEqual(use.actual_value, 0.5, msg="the closing count, not the recipe-made POS moves, is the evidence")
        self.assertIn("the counts show 2.3 kg used; POS sales and recipes explain 1.8 kg", use.source_note)

        other_branch = self._measure("inventory_ingredient_use_difference_quantity", "kg", self.coffee, branch=self.branch2)
        self.assertEqual(other_branch.status, "not_evaluated", "Branch 1 counts are not Branch 2 evidence")

    def test_waste_without_an_approved_reason_is_not_properly_recorded(self):
        """0.3 kg spoiled with a reason, 0.2 kg scrapped without one and 0.5 kg found missing: 30% properly recorded."""
        self._receive(10.0, "2001-02-01")
        self._count(self.coffee, FEB[0])
        self._scrap(0.3, "2001-02-06")
        self._scrap(0.2, "2001-02-07", tagged_reason=False)
        self._count(self.coffee, "2001-02-10", difference=-0.5)
        evaluation = self._measure("inventory_waste_recording_pct", "%", self.coffee, period=FEB)
        self.assertEqual(evaluation.status, "valid", evaluation.reason)
        self.assertAlmostEqual(evaluation.actual_value, 30.0)
        self.assertIn("Recorded without an approved reason: 0.2 kg", evaluation.source_note)
        with self.assertRaises(ValidationError):
            _validate_calculation_contract(self.env, "inventory", "inventory_waste_recording_pct", False, "%", self.company.id)

    def test_without_counts_or_with_recipe_gaps_nothing_is_guessed(self):
        self._receive(10.0, "2001-03-01")
        self._scrap(0.4, "2001-03-06")
        recording = self._measure("inventory_waste_recording_pct", "%", self.coffee, period=MARCH)
        self.assertEqual(recording.status, "not_evaluated", "recorded waste alone cannot show what was not recorded")
        self._count(self.coffee, MARCH[0])
        one_count = self._measure("inventory_ingredient_use_difference_quantity", "kg", self.coffee, period=MARCH)
        self.assertEqual(one_count.status, "not_evaluated")
        self.assertIn("at least two different days", one_count.source_note)

        self._sell(self.cappuccino, 5, "2001-03-07")
        self._sell(self.latte, 10, "2001-03-07")
        self._count(self.coffee, "2001-03-08", hour=20)
        gap = self._measure("inventory_ingredient_use_difference_quantity", "kg", self.coffee, period=MARCH)
        self.assertEqual(gap.status, "data_quality_issue")
        self.assertIn("BG Control Cappuccino", gap.source_note)

    # ------------------------------------------------------------------ goal flow
    def test_planned_count_goal_is_measured_through_business_goals(self):
        """Given two planned counts in the week across branches, one applied on time, when the owner sets 'Complete
        planned stock counts' at 80%, then the goal measures 50% and is not achieved."""
        self._plan("2001-01-08", self.coffee)
        self._plan("2001-01-09", self.coffee, branch=self.branch2)
        self._count(self.coffee, "2001-01-08")
        Setup = self.env["strategic.goal.setup"].with_user(self.head)
        plan = self.env["strategic.plan"].with_user(self.head).create({
            "name": "BG Control Plan", "company_id": self.company.id, "date_start": "2000-01-01", "date_end": "2099-12-31"})
        setup = Setup.browse(Setup.create_goal({
            "name": "Count stock as planned", "business_area": "inventory_waste", "deadline": WEEK[1],
            "plan_id": plan.id, "owner_id": self.head.id})["setup_id"])
        payload = setup.configure_measurement({"template_key": "count_completion", "target_value": 80.0,
                                               "period_start": WEEK[0], "period_end": WEEK[1]})
        self.assertEqual(setup.setup_state, "ready", payload["readiness"])
        data = next(check for check in payload["readiness"] if check["key"] == "data")
        self.assertIn("50.0%", data["message"])
        setup.prepare_target()
        self.assertEqual((setup.target_id.range_min, setup.target_id.range_max), (80.0, 100.0))
        setup.approve_target()
        setup.evaluate_now()
        evaluation = setup.key_result_id.evaluation_id
        self.assertEqual((evaluation.status, evaluation.actual_value), ("valid", 50.0), evaluation.reason)
        self.assertNotEqual(evaluation.attainment_status, "achieved")
