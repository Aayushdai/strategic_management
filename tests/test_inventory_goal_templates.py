"""BDD scenarios for the Inventory & Waste goal options and the OKR view (tests/features/inventory_goal_templates.feature).

Real objective, KPI, target, Key Result and evaluation records; the real inventory collector. The measured week is in
January 2001 so no real company stock movement falls inside it.
"""

from datetime import datetime, timedelta

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import format_amount

from odoo.addons.strategic_management.models.goal_templates import measure_bar, target_bounds
from odoo.addons.strategic_management.models.kpi import _validate_calculation_contract
from odoo.addons.strategic_management.services.kpi_sources.inventory import waste_move_cost

WEEK = ("2001-01-08", "2001-01-14")


@tagged("post_install", "-at_install")
class TestToleranceRule(TransactionCase):
    def test_tolerance_uses_the_size_of_the_difference(self):
        self.assertEqual(target_bounds("tolerance", 2.0), (-2.0, 2.0))
        self.assertEqual(measure_bar("tolerance", -1.0, -2.0, 2.0),
                         {"kind": "usage", "pct": 50.0, "bar": 50.0, "tone": "good", "margin": 50.0})
        self.assertEqual(measure_bar("tolerance", 1.0, -2.0, 2.0)["pct"], 50.0)
        outside = measure_bar("tolerance", -3.0, -2.0, 2.0)
        self.assertEqual((outside["pct"], outside["bar"], outside["tone"], outside["margin"]), (150.0, 100.0, "exceeded", -50.0))


@tagged("post_install", "-at_install")
class TestInventoryGoalTemplates(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        head_group = cls.env.ref("strategic_management.group_strategic_head_office_manager")
        cls.head = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "bg_inv_head", "login": "bg_inv_head", "email": "bg_inv_head@example.com", "tz": "UTC",
            "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
            "group_ids": [(6, 0, [cls.env.ref("base.group_user").id, head_group.id, cls.env.ref("stock.group_stock_user").id])],
        })
        cls.milk = cls.env["product.template"].create({
            "name": "BG Inventory Milk", "uom_id": cls.env.ref("uom.product_uom_litre").id, "is_storable": True,
        }).product_variant_id
        cls.plan = cls.env["strategic.plan"].with_user(cls.head).create({
            "name": "BG Inventory Plan", "company_id": cls.company.id, "date_start": "2000-01-01", "date_end": "2099-12-31",
        })
        cls.Setup = cls.env["strategic.goal.setup"].with_user(cls.head)

    def _new_goal(self, area="inventory_waste", name="Keep stock records honest"):
        return self.Setup.browse(self.Setup.create_goal({
            "name": name, "business_area": area, "deadline": WEEK[1], "plan_id": self.plan.id, "owner_id": self.head.id,
        })["setup_id"])

    def _configure(self, setup, template, target=2.0, product=None):
        return setup.configure_measurement({
            "template_key": template, "product_id": (product or self.milk).id, "target_value": target,
            "period_start": WEEK[0], "period_end": WEEK[1],
        })

    def test_catalogue_lists_inventory_options_with_honest_statuses(self):
        templates = self.Setup.get_form_options()["templates"]
        purchasing = [t for t in templates if t["area"] == "purchasing"]
        inventory = {t["key"]: t for t in templates if t["area"] == "inventory_waste"}
        self.assertEqual(len(purchasing), 21)
        self.assertEqual(sum(1 for t in purchasing if t["status"] == "available"), 13)
        self.assertEqual(len(inventory), 15)
        self.assertEqual(sorted(k for k, t in inventory.items() if t["status"] == "available"), [
            "branch_waste_rate", "consumption_variance", "count_accuracy", "count_completion", "excess_stock", "inventory_monitor",
            "minimum_stock",
            "recipe_coverage", "stock_flow_variance", "stock_value", "waste_cost_reduction", "waste_quantity",
            "waste_record_completeness", "waste_to_sales"])
        self.assertEqual(sorted(k for k, t in inventory.items() if t["status"] == "unsupported"), ["stock_coverage"])
        self.assertTrue(inventory["branch_waste_rate"]["branch_only"])
        for template in inventory.values():
            self.assertTrue(template["how"] if template["status"] == "available" else template["needs"], template["key"])

    def test_options_belong_to_their_own_area(self):
        setup = self._new_goal()
        with self.assertRaisesRegex(UserError, "Choose one of the Inventory & Waste goal options"):
            self._configure(setup, "quantity_limit")
        with self.assertRaisesRegex(UserError, "cannot be measured yet"):
            self._configure(setup, "stock_coverage")
        purchasing = self._new_goal(area="purchasing", name="Buy well")
        with self.assertRaisesRegex(UserError, "Choose one of the Purchasing goal options"):
            self._configure(purchasing, "stock_flow_variance")
        self.assertFalse(setup.target_id)
        self.assertFalse(purchasing.target_id)

    def test_stock_flow_goal_builds_okr_key_result_and_kpi(self):
        setup = self._new_goal()
        payload = self._configure(setup, "stock_flow_variance", target=2.0)
        self.assertEqual(setup.setup_state, "ready", payload["readiness"])
        setup.prepare_target()
        self.assertEqual((setup.target_id.range_min, setup.target_id.range_max), (-2.0, 2.0))
        self.assertEqual(setup.kpi_id.calculation_type, "inventory_unexplained_variance_quantity")
        setup.approve_target()

        goal = next(g for g in self.Setup.get_goals("inventory_waste") if g["objective_id"] == setup.objective_id.id)
        self.assertEqual(len(goal["okr"]), 1)
        kr = goal["okr"][0]["key_results"][0]
        self.assertEqual(kr["id"], setup.key_result_id.id)
        self.assertEqual(kr["kpi"], setup.kpi_id.name)
        self.assertEqual(kr["target"], "Within ±2 L")
        self.assertEqual(kr["direction"], "Close to zero (tolerance)")
        self.assertIn("BG Inventory Milk", kr["source"])
        self.assertEqual(kr["readiness_tone"], "setup")

        # The week has ended and the product had no movements: the real collector measures a zero difference.
        setup.evaluate_now()
        goal = next(g for g in self.Setup.get_goals("inventory_waste") if g["objective_id"] == setup.objective_id.id)
        kr = goal["okr"][0]["key_results"][0]
        evaluation = setup.key_result_id.evaluation_id
        self.assertEqual(evaluation.status, "valid", evaluation.reason)
        self.assertAlmostEqual(evaluation.actual_value, 0.0)
        self.assertEqual(evaluation.attainment_status, "achieved")
        self.assertEqual((kr["current"], kr["readiness_tone"]), ("0 L", "available"))
        self.assertTrue(goal["result"]["final"])

    def test_goal_without_okr_has_an_empty_okr_view(self):
        setup = self._new_goal()
        goal = next(g for g in self.Setup.get_goals("inventory_waste") if g["objective_id"] == setup.objective_id.id)
        self.assertEqual(goal["okr"], [])


def at(day, hour=10):
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=hour)


@tagged("post_install", "-at_install")
class TestInventoryMeasurements(TransactionCase):
    """Real stock moves, scrap, counts, valuation and POS orders through the goal workflow (2001, no live data)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        cls.head = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "bg_invm_head", "login": "bg_invm_head", "email": "bg_invm_head@example.com", "tz": "UTC",
            "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
            "group_ids": [(6, 0, [cls.env.ref("base.group_user").id, cls.env.ref("stock.group_stock_user").id,
                                  cls.env.ref("strategic_management.group_strategic_head_office_manager").id])],
        })
        Warehouse = cls.env["stock.warehouse"]
        cls.b1 = Warehouse.create({"name": "BG Waste Branch 1", "code": "BGW1", "company_id": cls.company.id})
        cls.b2 = Warehouse.create({"name": "BG Waste Branch 2", "code": "BGW2", "company_id": cls.company.id})
        cls.b3 = Warehouse.create({"name": "BG Waste Branch 3", "code": "BGW3", "company_id": cls.company.id})
        litre = cls.env.ref("uom.product_uom_litre")
        cls.milk = cls.env["product.template"].create({
            "name": "BG Waste Milk", "uom_id": litre.id, "is_storable": True, "standard_price": 2.0,
        }).product_variant_id
        cls.cream = cls.env["product.template"].create({
            "name": "BG Waste Cream", "uom_id": litre.id, "is_storable": True, "standard_price": 5.0,
        }).product_variant_id
        cls.free = cls.env["product.template"].create({
            "name": "BG Waste No Cost", "uom_id": litre.id, "is_storable": True, "standard_price": 0.0,
        }).product_variant_id
        # Moves are back-dated to 2001 and Odoo re-values them at the cost in force on that date:
        # give the products a cost history that starts before then, as a real café's would.
        kg = cls.env.ref("uom.product_uom_kgm")
        cls.beans = cls.env["product.template"].create({
            "name": "BG Waste Beans", "uom_id": kg.id, "is_storable": True, "standard_price": 200.0, "list_price": 999.0,
        }).product_variant_id
        cls.cups = cls.env["product.template"].create({"name": "BG Waste Cups", "uom_id": kg.id, "standard_price": 1.0}).product_variant_id
        cls.env["product.value"].sudo().search([("product_id", "in", (cls.milk | cls.cream | cls.beans).ids)]).write({"date": "2000-01-01 00:00:00"})
        Tag = cls.env["stock.scrap.reason.tag"]
        cls.damaged = Tag.search([("name", "=", "Damaged Goods")], limit=1) or Tag.create({"name": "Damaged Goods"})
        cls.other_reason = Tag.create({"name": "BG Staff Drink"})
        cls.suppliers = cls.env.ref("stock.stock_location_suppliers")
        cls.loss = cls.env["stock.location"].search([("usage", "=", "inventory"), ("company_id", "in", [False, cls.company.id])], limit=1)
        cls.pos_b1 = cls.env["pos.config"].create({"name": "BG Waste POS 1", "picking_type_id": cls.b1.pos_type_id.id})
        cls.plan = cls.env["strategic.plan"].with_user(cls.head).create({
            "name": "BG Waste Plan", "company_id": cls.company.id, "date_start": "2000-01-01", "date_end": "2099-12-31",
        })
        cls.Setup = cls.env["strategic.goal.setup"].with_user(cls.head)
        cls.company_goal = cls.Setup.create_goal({
            "name": "Waste less", "business_area": "inventory_waste", "deadline": "2099-12-31",
            "plan_id": cls.plan.id, "owner_id": cls.head.id,
        })["objective_id"]

    # ------------------------------------------------------------------ helpers
    def _done(self, product, source, destination, quantity, day, **values):
        move = self.env["stock.move"].create({
            "product_id": product.id, "product_uom_qty": quantity, "quantity": quantity, "uom_id": product.uom_id.id,
            "location_id": source.id, "location_dest_id": destination.id, **values,
        })
        move._action_confirm()
        move.picked = True
        move._action_done()
        move.write({"date": at(day)})
        return move

    def _receive(self, quantity, day, warehouse=None, product=None):
        return self._done(product or self.milk, self.suppliers, (warehouse or self.b1).lot_stock_id, quantity, day)

    def _scrap(self, quantity, day, warehouse=None, product=None, tag=None):
        product = product or self.milk
        move = self.env["stock.move"].create({
            "product_id": product.id, "product_uom_qty": quantity, "quantity": quantity, "uom_id": product.uom_id.id,
            "location_id": (warehouse or self.b1).lot_stock_id.id, "location_dest_id": self.loss.id,
            "scrap_reason_tag_ids": [(6, 0, (tag if tag is not None else self.damaged).ids)],
        })
        move.action_scrap()
        self.assertEqual((move.state, move.is_scrap), ("done", True))
        move.write({"date": at(day)})
        return move

    def _count(self, counted, day, warehouse=None, product=None):
        product = product or self.milk
        Move = self.env["stock.move"]
        before = Move.search([("product_id", "=", product.id), ("is_inventory", "=", True)])
        quant = self.env["stock.quant"].search([("product_id", "=", product.id), ("location_id", "=", (warehouse or self.b1).lot_stock_id.id)])
        quant.inventory_quantity = counted
        quant.action_apply_inventory()
        moves = Move.search([("product_id", "=", product.id), ("is_inventory", "=", True)]) - before
        moves.write({"date": at(day)})
        return moves

    def _sale(self, config, total, tax, day):
        session = self.env["pos.session"].search([("config_id", "=", config.id), ("state", "!=", "closed")], limit=1)             or self.env["pos.session"].create({"config_id": config.id, "user_id": self.env.uid})
        return self.env["pos.order"].create({
            "session_id": session.id, "company_id": self.company.id, "amount_tax": tax, "amount_total": total,
            "amount_paid": total, "amount_return": 0.0, "state": "paid", "date_order": at(day),
        })

    def _goal(self, template, target, product=None, warehouse=None, period=WEEK, ready=True):
        values = {"name": template, "business_area": "inventory_waste", "deadline": period[1], "owner_id": self.head.id}
        if warehouse:
            values.update(scope="branch", warehouse_id=warehouse.id, parent_objective_id=self.company_goal)
        else:
            values.update(scope="organization", plan_id=self.plan.id)
        setup = self.Setup.browse(self.Setup.create_goal(values)["setup_id"])
        payload = setup.configure_measurement({
            "template_key": template, "product_id": product.id if product else False, "target_value": target,
            "period_start": period[0], "period_end": period[1],
        })
        if not ready:
            return setup, payload
        self.assertEqual(setup.setup_state, "ready", payload["readiness"])
        setup.prepare_target()
        setup.approve_target()
        return setup

    def _final(self, setup):
        setup.evaluate_now()
        goal = next(g for g in self.Setup.get_goals("inventory_waste") if g["objective_id"] == setup.objective_id.id)
        return goal, setup.key_result_id.evaluation_id

    # ------------------------------------------------------------------ waste quantity
    def test_waste_quantity_counts_tagged_scrap_of_the_product_branch_and_period(self):
        self._receive(10.0, "2001-01-02")
        self._receive(10.0, "2001-01-02", warehouse=self.b2)
        self._receive(10.0, "2001-01-02", product=self.cream)
        self._scrap(0.5, "2001-01-09")
        self._scrap(0.25, "2001-01-14")                       # last day of the period counts
        self._scrap(1.0, "2001-01-15")                        # after the period
        self._scrap(2.0, "2001-01-10", warehouse=self.b2)     # other branch
        self._scrap(3.0, "2001-01-10", product=self.cream)    # other product
        setup = self._goal("waste_quantity", 1.0, product=self.milk, warehouse=self.b1)
        goal, evaluation = self._final(setup)
        self.assertEqual(evaluation.status, "valid", evaluation.reason)
        self.assertAlmostEqual(evaluation.actual_value, 0.75)
        self.assertEqual(evaluation.attainment_status, "achieved")
        self.assertEqual(setup.kpi_id.unit, "L")
        self.assertTrue(goal["result"]["final"])
        self.assertIn("Achieved", goal["result"]["status"])

    def test_waste_without_an_approved_reason_blocks_the_measurement(self):
        self._receive(10.0, "2001-01-02")
        self._scrap(0.5, "2001-01-09", tag=self.other_reason)
        setup, payload = self._goal("waste_quantity", 1.0, product=self.milk, warehouse=self.b1, ready=False)
        data = next(c for c in payload["readiness"] if c["key"] == "data")
        self.assertFalse(data["ok"])
        self.assertIn("unsupported waste category", data["message"])
        self.assertEqual(setup.setup_state, "needs_setup")

    def test_no_waste_is_a_valid_zero(self):
        setup = self._goal("waste_quantity", 1.0, product=self.milk, warehouse=self.b1)
        _goal, evaluation = self._final(setup)
        self.assertEqual((evaluation.status, evaluation.actual_value, evaluation.has_actual_value), ("valid", 0.0, True))
        self.assertIn("not proof that no physical waste occurred", evaluation.source_note or evaluation.reason or "")

    # ------------------------------------------------------------------ waste cost
    def test_waste_quantity_and_cost_are_separate_measurements(self):
        """52 kg of beans scrapped at an inventory cost of 200 per kg: 52 kg and 10,400, never mixed up."""
        self._receive(100.0, "2001-01-02", product=self.beans)
        self._scrap(30.0, "2001-01-09", product=self.beans)
        self._scrap(22.0, "2001-01-12", product=self.beans)
        quantity = self._goal("waste_quantity", 100.0, product=self.beans, warehouse=self.b1)
        cost = self._goal("waste_cost_reduction", 10000.0, product=self.beans, warehouse=self.b1)

        goal, evaluation = self._final(quantity)
        self.assertAlmostEqual(evaluation.actual_value, 52.0)
        self.assertEqual((quantity.kpi_id.unit, evaluation.unit), ("kg", "kg"))
        self.assertEqual(goal["result"]["value_display"], "52 kg")
        self.assertEqual(goal["result"]["target_display"], "At most 100 kg")
        self.assertEqual(goal["current"], "52 kg", "dashboard and detail show the same value")
        self.assertEqual(goal["result"]["bar"]["pct"], 52.0)
        self.assertEqual(evaluation.attainment_status, "achieved")

        goal, evaluation = self._final(cost)
        self.assertEqual(evaluation.status, "valid", evaluation.reason)
        self.assertAlmostEqual(evaluation.actual_value, 10400.0, msg="cost from valuation (200/kg), not the 999 sales price")
        symbol = self.company.currency_id.symbol
        self.assertEqual((cost.kpi_id.unit, evaluation.unit), (symbol, symbol))
        self.assertEqual(cost.kpi_id.source_product_id, self.beans)
        self.assertNotEqual(cost.kpi_id, quantity.kpi_id)
        money = format_amount(self.env, 10400.0, self.company.currency_id)
        self.assertEqual(goal["result"]["value_display"], money)
        self.assertEqual(goal["current"], money)
        self.assertEqual(goal["result"]["target_display"], "Up to %s" % format_amount(self.env, 10000.0, self.company.currency_id))
        self.assertEqual(goal["result"]["bar"]["pct"], 104.0)
        self.assertEqual(evaluation.attainment_status, "not_achieved")
        self.assertEqual(goal["result"]["count_label"], "2 waste records", "each waste move is counted once")

    def test_waste_cost_filters_product_branch_reason_and_period(self):
        self._receive(100.0, "2001-01-02", product=self.beans)
        self._receive(100.0, "2001-01-02", product=self.beans, warehouse=self.b2)
        self._receive(10.0, "2001-01-02")
        self._scrap(10.0, "2001-01-09", product=self.beans)                     # 2,000 counted
        self._scrap(5.0, "2001-01-09", product=self.beans, warehouse=self.b2)   # other branch
        self._scrap(4.0, "2001-01-15", product=self.beans)                      # after the period
        self._scrap(1.0, "2001-01-09")                                          # other product
        branch = self._goal("waste_cost_reduction", 5000.0, product=self.beans, warehouse=self.b1)
        _goal, evaluation = self._final(branch)
        self.assertAlmostEqual(evaluation.actual_value, 2000.0)
        company = self._goal("waste_cost_reduction", 5000.0, product=self.beans)
        _goal, evaluation = self._final(company)
        self.assertAlmostEqual(evaluation.actual_value, 3000.0)
        self.assertEqual(company.kpi_id, branch.kpi_id, "the same approved measure is reused, not duplicated")
        # The recorded valuation is kept: a later cost change does not rewrite past waste cost.
        self.beans.standard_price = 300.0
        company.target_id.action_evaluate_source()
        self.assertAlmostEqual(company.key_result_id.evaluation_id.actual_value, 3000.0)

    def test_waste_cost_without_valuation_is_never_zero_or_estimated(self):
        self._receive(10.0, "2001-01-02", product=self.free)
        self._scrap(1.0, "2001-01-09", product=self.free)
        _setup, payload = self._goal("waste_cost_reduction", 3.0, product=self.free, ready=False)
        data = next(c for c in payload["readiness"] if c["key"] == "data")
        self.assertFalse(data["ok"])
        self.assertIn("BG Waste No Cost has no cost", data["message"])
        # Quantity tracking of the same product stays usable.
        quantity = self._goal("waste_quantity", 3.0, product=self.free, warehouse=self.b1)
        _goal, evaluation = self._final(quantity)
        self.assertAlmostEqual(evaluation.actual_value, 1.0)

    def test_only_valued_outgoing_waste_has_a_cost(self):
        # Odoo 20 also values non-stored consumables; their recorded value is used, like any other product.
        self._done(self.cups, self.suppliers, self.b1.lot_stock_id, 10.0, "2001-01-02")
        self._scrap(2.0, "2001-01-09", product=self.cups)
        setup = self._goal("waste_cost_reduction", 3.0, product=self.cups)
        _goal, evaluation = self._final(setup)
        self.assertAlmostEqual(evaluation.actual_value, 2.0)
        # A move Odoo did not value as stock leaving, or with an inconsistent sign, has no cost rather than zero.
        Move = self.env["stock.move"]
        self.assertIsNone(waste_move_cost(Move.new({"is_out": False, "value": 0.0})))
        self.assertIsNone(waste_move_cost(Move.new({"is_out": True, "value": 5.0})))
        self.assertEqual(waste_move_cost(Move.new({"is_out": True, "value": -5.0})), 5.0)

    def test_target_units_must_match_the_measurement(self):
        symbol = self.company.currency_id.symbol
        _validate_calculation_contract(self.env, "inventory", "inventory_recorded_waste_cost", self.beans.id, symbol, self.company.id)
        with self.assertRaisesRegex(ValidationError, "company currency"):
            _validate_calculation_contract(self.env, "inventory", "inventory_recorded_waste_cost", self.beans.id, "kg", self.company.id)
        with self.assertRaisesRegex(ValidationError, "Select the product"):
            _validate_calculation_contract(self.env, "inventory", "inventory_recorded_waste_cost", False, symbol, self.company.id)
        with self.assertRaisesRegex(ValidationError, "base unit"):
            _validate_calculation_contract(self.env, "inventory", "inventory_validated_waste_quantity", self.beans.id, symbol, self.company.id)

    def test_waste_exactly_at_the_limit_is_within_it(self):
        self._receive(100.0, "2001-01-02", product=self.beans)
        self._scrap(50.0, "2001-01-09", product=self.beans)
        setup = self._goal("waste_cost_reduction", 10000.0, product=self.beans, warehouse=self.b1)
        goal, evaluation = self._final(setup)
        self.assertAlmostEqual(evaluation.actual_value, 10000.0)
        self.assertEqual(evaluation.attainment_status, "achieved")
        self.assertEqual(goal["result"]["status"], "Achieved: within the limit")
        self.assertEqual(goal["result"]["bar"]["pct"], 100.0)

    def test_only_head_office_prepares_and_approves_waste_targets(self):
        manager = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "bg_invm_branch", "login": "bg_invm_branch", "email": "bg_invm_branch@example.com",
            "company_id": self.company.id, "company_ids": [(6, 0, [self.company.id])], "strategic_branch_warehouse_id": self.b1.id,
            "group_ids": [(6, 0, [self.env.ref("base.group_user").id, self.env.ref("stock.group_stock_user").id,
                                  self.env.ref("strategic_management.group_strategic_branch_manager").id])],
        })
        setup, _payload = self._goal("waste_cost_reduction", 10000.0, product=self.beans, warehouse=self.b1, ready=False)
        self.assertEqual(setup.setup_state, "ready")
        with self.assertRaises(AccessError):
            setup.with_user(manager).prepare_target()
        self.assertFalse(setup.target_id)

    # ------------------------------------------------------------------ waste rate
    def test_waste_to_sales_divides_by_the_same_branch_net_sales(self):
        self._receive(10.0, "2001-01-02")
        self._receive(10.0, "2001-01-02", warehouse=self.b2)
        self._scrap(0.5, "2001-01-09")                        # 1.00 at branch 1
        self._scrap(1.0, "2001-01-09", warehouse=self.b2)     # branch 2: not in branch 1's rate
        self._sale(self.pos_b1, 12.0, 2.0, "2001-01-10")      # net 10.00
        self._sale(self.pos_b1, 12.0, 2.0, "2001-01-20")      # outside the period
        setup = self._goal("branch_waste_rate", 5.0, warehouse=self.b1)
        _goal, evaluation = self._final(setup)
        self.assertEqual(evaluation.status, "valid", evaluation.reason)
        self.assertAlmostEqual(evaluation.actual_value, 10.0)
        self.assertEqual(evaluation.attainment_status, "not_achieved")
        self.assertEqual(setup.kpi_id.unit, "%")

    def test_waste_rate_without_sales_or_without_a_pos_has_no_result(self):
        self._receive(10.0, "2001-01-02")
        self._scrap(0.5, "2001-01-09")
        no_sales = self._goal("branch_waste_rate", 5.0, warehouse=self.b1)
        _goal, evaluation = self._final(no_sales)
        self.assertEqual(evaluation.status, "not_evaluated")
        self.assertIn("no net POS sales", evaluation.reason)
        self.assertEqual(no_sales.objective_id.scoring_status, "not_evaluated")

        _setup, payload = self._goal("branch_waste_rate", 5.0, warehouse=self.b3, ready=False)
        data = next(c for c in payload["readiness"] if c["key"] == "data")
        self.assertFalse(data["ok"])
        self.assertIn("No Point of Sale is linked to", data["message"])

    def test_branch_waste_rate_is_refused_for_the_whole_company(self):
        setup = self.Setup.browse(self.Setup.create_goal({
            "name": "Company rate", "business_area": "inventory_waste", "deadline": WEEK[1], "plan_id": self.plan.id,
        })["setup_id"])
        with self.assertRaisesRegex(UserError, "measured for one branch"):
            setup.configure_measurement({"template_key": "branch_waste_rate", "target_value": 5.0,
                                         "period_start": WEEK[0], "period_end": WEEK[1]})

    # ------------------------------------------------------------------ stock levels and counts
    def test_excess_stock_judges_closing_stock_against_the_approved_maximum(self):
        self._receive(10.0, "2001-01-09")
        self._receive(5.0, "2001-01-20")                       # after the period: not part of closing stock
        setup = self._goal("excess_stock", 8.0, product=self.milk, warehouse=self.b1)
        goal, evaluation = self._final(setup)
        self.assertEqual(evaluation.status, "valid", evaluation.reason)
        self.assertAlmostEqual(evaluation.actual_value, 10.0)
        self.assertEqual(evaluation.attainment_status, "not_achieved")
        self.assertEqual(goal["result"]["over_display"], "2 L")

    def test_count_accuracy_measures_physical_counts_not_stock_flow(self):
        self._receive(10.0, "2001-01-02")
        self._count(8.5, "2001-01-09")                         # 1.5 L short
        self._count(8.5, "2001-01-12")                         # counted again, no difference
        count = self._goal("count_accuracy", 1.0, product=self.milk, warehouse=self.b1)
        _goal, evaluation = self._final(count)
        self.assertEqual(evaluation.status, "valid", evaluation.reason)
        self.assertAlmostEqual(evaluation.actual_value, 1.5)
        self.assertIn("2 applied stock counts", evaluation.source_note or evaluation.reason)
        self.assertEqual(evaluation.attainment_status, "not_achieved")
        # The stock-flow check explains the same shortage as an adjustment: a different measure.
        flow = self._goal("stock_flow_variance", 1.0, product=self.milk, warehouse=self.b1)
        _goal, flow_evaluation = self._final(flow)
        self.assertAlmostEqual(flow_evaluation.actual_value, 0.0)

    def test_count_accuracy_without_counts_has_no_result(self):
        self._receive(10.0, "2001-01-02")
        setup = self._goal("count_accuracy", 1.0, product=self.milk, warehouse=self.b1)
        _goal, evaluation = self._final(setup)
        self.assertEqual(evaluation.status, "not_evaluated")
        self.assertIn("No stock count was applied", evaluation.reason)

    # ------------------------------------------------------------------ workflow
    def test_limits_are_not_evaluated_before_the_period_ends_and_reevaluation_is_idempotent(self):
        today = fields.Date.context_today(self.env.user)
        open_week = (fields.Date.to_string(today - timedelta(days=1)), fields.Date.to_string(today + timedelta(days=5)))
        setup = self._goal("waste_quantity", 1.0, product=self.milk, warehouse=self.b1, period=open_week)
        with self.assertRaisesRegex(ValidationError, "judged on the whole period"):
            setup.evaluate_now()
        self.assertFalse(setup.key_result_id.evaluation_id)
        goal = next(g for g in self.Setup.get_goals("inventory_waste") if g["objective_id"] == setup.objective_id.id)
        self.assertEqual(goal["result"]["value_label"], "So far")

        closed = self._goal("waste_quantity", 1.0, product=self.milk, warehouse=self.b1)
        closed.evaluate_now()
        first = closed.key_result_id.evaluation_id
        closed.evaluate_now()
        self.assertEqual(closed.key_result_id.evaluation_id, first)
        self.assertEqual(len(closed.objective_id.okr_objective_ids), 1)
        self.assertEqual(len(closed.objective_id.okr_objective_ids.key_result_ids), 1)
        with self.assertRaises(UserError):
            closed.approve_target()

    # ------------------------------------------------------------------ select, save, link, evaluate, reopen
    def _counts(self):
        return {model: self.env[model].search_count([]) for model in (
            "strategic.kpi.definition", "strategic.kpi.target", "strategic.okr.objective", "strategic.key.result", "strategic.kpi.evaluation")}

    def test_selected_option_is_saved_and_linked_to_the_right_key_result(self):
        self._receive(10.0, "2001-01-02")
        self._scrap(2.0, "2001-01-09")                                   # 2 L of milk at a cost of 2 per L
        before = self._counts()
        setup = self.Setup.browse(self.Setup.create_goal({
            "name": "Waste less milk", "business_area": "inventory_waste", "deadline": WEEK[1], "owner_id": self.head.id,
            "scope": "branch", "warehouse_id": self.b1.id, "parent_objective_id": self.company_goal,
        })["setup_id"])
        objective = setup.objective_id

        def payload():
            return next(g for g in self.Setup.get_goals("inventory_waste") if g["objective_id"] == objective.id)

        # Before anything is chosen there is no Key Result, and the page says what to do next.
        self.assertIn("Save and check readiness", payload()["current_note"])
        # Select "Track recorded waste quantity" and save: the choice is stored, nothing is linked yet.
        self._configure_waste(setup, "waste_quantity", 5.0)
        self.assertEqual((setup.template_key, setup.measure_key, setup.product_id, setup.setup_state),
                         ("waste_quantity", "inventory_validated_waste_quantity", self.milk, "ready"))
        self.assertEqual((str(setup.period_start), str(setup.period_end)), WEEK)
        self.assertFalse(setup.key_result_id or setup.target_id)
        self.assertIn("Head Office prepares the target next", payload()["current_note"])
        setup.prepare_target()
        self.assertEqual(setup.target_id.state, "draft")
        self.assertIn("created when Head Office approves the target", payload()["current_note"])
        setup.approve_target()

        kr, kpi, target, okr = setup.key_result_id, setup.kpi_id, setup.target_id, setup.okr_id
        self.assertEqual((kr.primary_kpi_id, kr.target_id, kr.okr_objective_id, kr.required), (kpi, target, okr, True))
        self.assertEqual(okr.strategic_objective_id, objective)
        self.assertEqual((kpi.calculation_type, kpi.source_type, kpi.source_product_id, kpi.unit),
                         ("inventory_validated_waste_quantity", "inventory", self.milk, "L"))
        self.assertEqual((str(target.date_start), str(target.date_end)), WEEK)
        self.assertEqual((str(okr.date_start), str(okr.date_end)), WEEK)
        self.assertEqual((target.scope, target.warehouse_id, target.state, target.range_max), ("branch", self.b1, "approved", 5.0))
        self.assertEqual(payload()["okr"][0]["key_results"][0]["id"], kr.id)

        setup.evaluate_now()
        evaluation = kr.evaluation_id
        self.assertEqual((evaluation.target_id, evaluation.status, evaluation.actual_value), (target, "valid", 2.0))
        self.assertEqual(payload()["current"], "2 L")

        # Reopening and re-evaluating changes nothing; the locked choice cannot be swapped.
        self.Setup.get_goals("inventory_waste")
        setup.evaluate_now()
        with self.assertRaises(UserError):
            self._configure_waste(setup, "waste_cost_reduction", 5.0)
        with self.assertRaises(UserError):
            setup.approve_target()
        after = self._counts()
        self.assertEqual({model: after[model] - before[model] for model in after}, {
            "strategic.kpi.definition": 1, "strategic.kpi.target": 1, "strategic.okr.objective": 1,
            "strategic.key.result": 1, "strategic.kpi.evaluation": 1})
        self.assertEqual(kr.evaluation_id, evaluation)

        # The cost of the same waste is a separate goal with its own money KPI, from valuation.
        cost = self._goal("waste_cost_reduction", 10.0, product=self.milk, warehouse=self.b1)
        _goal, cost_evaluation = self._final(cost)
        self.assertEqual((cost.kpi_id.calculation_type, cost.kpi_id.unit),
                         ("inventory_recorded_waste_cost", self.company.currency_id.symbol))
        self.assertNotEqual(cost.kpi_id, kpi)
        self.assertAlmostEqual(cost_evaluation.actual_value, 4.0)
        self.assertEqual(cost.key_result_id.primary_kpi_id, cost.kpi_id)

    def _configure_waste(self, setup, template, target):
        return setup.configure_measurement({
            "template_key": template, "product_id": self.milk.id, "target_value": target,
            "period_start": WEEK[0], "period_end": WEEK[1],
        })
