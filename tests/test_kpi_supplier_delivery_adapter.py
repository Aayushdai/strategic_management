"""Requirement-focused tests for supplier on-time delivery KPI calculations."""

from datetime import date, datetime, time, timedelta

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.strategic_management.services.kpi_sources.common import reporting_period_utc
from odoo.addons.strategic_management.services.kpi_sources.supplier_delivery import (
    _weighted_on_time_delivery_percentage,
)


@tagged("post_install", "-at_install")
class TestStrategicKpiSupplierDeliveryAdapter(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        cls.GroupUser = cls.env.ref("base.group_user")
        cls.GroupHead = cls.env.ref("strategic_management.group_strategic_head_office_manager")
        cls.GroupEmployee = cls.env.ref("strategic_management.group_strategic_employee")
        cls.GroupPurchaseUser = cls.env.ref("purchase.group_purchase_user")
        unit = cls.env.ref("uom.product_uom_unit")
        cls.product = cls.env["product.template"].create({
            "name": "Phase 5 Supplier OTD Test Product",
            "uom_id": unit.id,
            "purchase_ok": True,
        }).product_variant_id
        cls.warehouse = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.company.id)], limit=1
        )
        cls.incoming_type = cls.env["stock.picking.type"].search([
            ("code", "=", "incoming"),
            ("warehouse_id", "=", cls.warehouse.id),
        ], limit=1)
        cls.vendor = cls.env["res.partner"].create({
            "name": "Phase 5 Supplier OTD Vendor",
            "supplier_rank": 1,
            "company_id": False,
        })
        cls.head = cls._make_user("phase5_otd_head_fix", cls.GroupHead, purchase=True)
        cls.employee = cls._make_user("phase5_otd_employee_fix", cls.GroupEmployee)
        today = fields.Date.context_today(cls.env.user)
        cls.period_start = today.replace(day=1)
        cls.period_end = (cls.period_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        cls.next_period_start = (cls.period_start + timedelta(days=32)).replace(day=1)
        cls.next_period_end = (cls.next_period_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)

    @classmethod
    def _make_user(cls, login, role_group, purchase=False):
        groups = [cls.GroupUser.id, role_group.id]
        if purchase:
            groups.append(cls.GroupPurchaseUser.id)
        return cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": login,
            "login": login,
            "email": "%s@example.com" % login,
            "company_id": cls.company.id,
            "company_ids": [(6, 0, [cls.company.id])],
            "group_ids": [(6, 0, groups)],
        })

    def _make_target(self, code, start=None, end=None):
        kpi = self.env["strategic.kpi.definition"].with_user(self.head).create({
            "name": "%s KPI" % code,
            "code": code,
            "company_id": self.company.id,
            "unit": "%",
            "direction": "higher",
            "source_type": "purchase",
            "calculation_type": "supplier_on_time_delivery_pct",
            "description": "Quantity-weighted supplier OTD using original PO-confirmation baseline dates.",
        })
        target = self.env["strategic.kpi.target"].with_user(self.head).create({
            "kpi_id": kpi.id,
            "date_start": start or self.period_start,
            "date_end": end or self.period_end,
            "scope": "organization",
            "target_value": 95.0,
        })
        target.with_user(self.head).action_approve()
        return kpi, target

    def _create_due_purchase_order(self, promised_at, quantity=3.0, uom=None):
        if not self.incoming_type:
            self.skipTest("The test company has no incoming operation type for its warehouse.")
        order = self.env["purchase.order"].with_user(self.head).create({
            "partner_id": self.vendor.id,
            "company_id": self.company.id,
            "picking_type_id": self.incoming_type.id,
            "order_line": [(0, 0, {
                "name": self.product.display_name,
                "product_id": self.product.id,
                "product_qty": quantity,
                "uom_id": (uom or self.product.uom_id).id,
                "price_unit": 1.0,
                "date_planned": promised_at,
                "date_promised": promised_at,
            })],
        })
        order.with_user(self.head).button_confirm()
        line = order.order_line.filtered(lambda item: not item.display_type)[:1]
        # The PO-confirmation hook captures the baseline before this routine's
        # later write, which simulates an approved subsequent promise-date change.
        if not line.date_promised:
            line.sudo().write({"date_promised": promised_at})
        return order, line

    def _evaluate(self, target, user=None):
        action = target.with_user(user or self.head).action_evaluate_source()
        return self.env["strategic.kpi.evaluation"].browse(action["res_id"])

    def test_supplier_otd_definition_requires_percent_and_no_tracked_product(self):
        with self.assertRaises(ValidationError):
            self.env["strategic.kpi.definition"].with_user(self.head).create({
                "name": "Bad Supplier OTD Unit Fix",
                "code": "P5_BAD_OTD_UNIT_FIX",
                "company_id": self.company.id,
                "unit": "Units",
                "direction": "higher",
                "source_type": "purchase",
                "calculation_type": "supplier_on_time_delivery_pct",
                "description": "Invalid unit contract.",
            })
        with self.assertRaises(ValidationError):
            self.env["strategic.kpi.definition"].with_user(self.head).create({
                "name": "Bad Supplier OTD Product Fix",
                "code": "P5_BAD_OTD_PRODUCT_FIX",
                "company_id": self.company.id,
                "unit": "%",
                "direction": "higher",
                "source_type": "purchase",
                "calculation_type": "supplier_on_time_delivery_pct",
                "source_product_id": self.product.id,
                "description": "OTD is quantity-weighted across eligible products.",
            })

    def test_weighted_partial_quantity_receives_proportional_credit(self):
        self.assertAlmostEqual(_weighted_on_time_delivery_percentage([(100.0, 80.0)]), 80.0, places=6)
        self.assertAlmostEqual(
            _weighted_on_time_delivery_percentage([(100.0, 80.0), (50.0, 50.0)]),
            100.0 * 130.0 / 150.0,
            places=6,
        )

    def test_over_receipt_is_capped_at_due_quantity(self):
        self.assertAlmostEqual(_weighted_on_time_delivery_percentage([(3.0, 4.0)]), 100.0, places=6)

    def test_invalid_or_empty_due_quantity_does_not_produce_percentage(self):
        self.assertIsNone(_weighted_on_time_delivery_percentage([]))
        self.assertIsNone(_weighted_on_time_delivery_percentage([(0.0, 0.0)]))
        self.assertIsNone(_weighted_on_time_delivery_percentage([(3.0, -1.0)]))

    def test_original_baseline_date_and_quantity_are_snapshotted_at_confirmation(self):
        original = fields.Datetime.to_datetime(fields.Datetime.now()) - timedelta(days=1)
        _order, line = self._create_due_purchase_order(original, quantity=3.0)
        self.assertTrue(line.strategic_otd_baseline_captured)
        self.assertEqual(line.strategic_otd_baseline_date, original)
        self.assertAlmostEqual(line.strategic_otd_baseline_quantity, 3.0, places=6)
        self.assertEqual(line.strategic_otd_baseline_product_id, self.product)
        revised = original + timedelta(days=5)
        line.sudo().write({"date_promised": revised})
        self.assertEqual(line.strategic_otd_baseline_date, original)

    def test_baseline_snapshot_cannot_be_forged_or_rewritten(self):
        _order, line = self._create_due_purchase_order(fields.Datetime.to_datetime(fields.Datetime.now()) - timedelta(days=1))
        with self.assertRaises(UserError):
            line.sudo().write({"strategic_otd_baseline_date": fields.Datetime.now()})

    def test_due_lines_without_receipt_score_zero_percent(self):
        # Use a deliberately historical test period so confirmed PO lines already
        # present in the shared test database cannot contaminate this cohort. The
        # adapter must still fail closed for real in-period POs that lack a captured
        # baseline; this test only intends to score the PO created below.
        period_start = date(2000, 1, 1)
        period_end = date(2000, 1, 31)
        _kpi, target = self._make_target(
            "P5_OTD_NO_RECEIPTS_FIX", period_start, period_end
        )
        promised = datetime(2000, 1, 15, 12, 0, 0)
        self._create_due_purchase_order(promised, quantity=3.0)

        evaluation = self._evaluate(target)
        self.assertEqual(evaluation.status, "valid", evaluation.source_note or evaluation.reason)
        self.assertTrue(evaluation.has_actual_value)
        self.assertAlmostEqual(evaluation.actual_value, 0.0, places=6)
        calculation_note = evaluation.source_note or ""
        self.assertIn("quantity-weighted", calculation_note.lower())
        self.assertIn("original_baseline=", calculation_note)

    def test_future_due_lines_are_not_prematurely_counted_as_late(self):
        _kpi, target = self._make_target(
            "P5_OTD_FUTURE_DUE_FIX", self.next_period_start, self.next_period_end
        )
        future = datetime.combine(self.next_period_start + timedelta(days=3), time(12, 0))
        self._create_due_purchase_order(future, quantity=3.0)
        evaluation = self._evaluate(target)
        self.assertEqual(evaluation.status, "not_evaluated")
        self.assertFalse(evaluation.has_actual_value)
        self.assertIn("Future-due quantities", evaluation.reason)

    def test_promise_date_revision_does_not_change_original_due_cohort(self):
        # Historical fixture dates isolate the cohort from pre-existing PO records.
        period_start = date(2000, 1, 1)
        period_end = date(2000, 1, 31)
        _kpi, target = self._make_target(
            "P5_OTD_PROMISE_REVISION_FIX", period_start, period_end
        )
        original = datetime(2000, 1, 15, 12, 0, 0)
        _order, line = self._create_due_purchase_order(original, quantity=3.0)
        revised = datetime(2050, 2, 1, 12, 0, 0)
        line.sudo().write({"date_promised": revised})

        evaluation = self._evaluate(target)
        self.assertEqual(evaluation.status, "valid", evaluation.source_note or evaluation.reason)
        self.assertAlmostEqual(evaluation.actual_value, 0.0, places=6)
        note = evaluation.source_note or ""
        self.assertIn("original_baseline=2000-01-15 12:00:00", note)
        self.assertIn("current_promise=2050-02-01 12:00:00", note)

    def test_canceled_due_line_fails_closed_until_policy_is_approved(self):
        period_start = date(2000, 1, 1)
        period_end = date(2000, 1, 31)
        _kpi, target = self._make_target(
            "P5_OTD_CANCEL_POLICY_FIX", period_start, period_end
        )
        order, _line = self._create_due_purchase_order(
            datetime(2000, 1, 15, 12, 0, 0), quantity=3.0
        )
        order.button_cancel()

        evaluation = self._evaluate(target)
        self.assertEqual(evaluation.status, "data_quality_issue")
        self.assertFalse(evaluation.has_actual_value)
        reason = evaluation.reason if isinstance(evaluation.reason, str) else ""
        note = evaluation.source_note if isinstance(evaluation.source_note, str) else ""
        self.assertIn("cancelled po lines", (reason + " " + note).lower())

    def test_purchase_uom_quantity_is_snapshotted_in_product_base_unit(self):
        dozen = self.env.ref("uom.product_uom_dozen", raise_if_not_found=False)
        if not dozen:
            self.skipTest("Odoo's standard dozen UoM is not available.")

        converted_one = dozen._compute_quantity(
            1.0, self.product.uom_id, rounding_method="HALF-UP"
        )
        self.assertAlmostEqual(converted_one, 12.0, places=6)

        _order, line = self._create_due_purchase_order(
            datetime(2000, 1, 15, 12, 0, 0), quantity=2.0, uom=dozen
        )
        self.assertEqual(line.uom_id, dozen)
        self.assertTrue(line.strategic_otd_baseline_captured)
        self.assertAlmostEqual(line.strategic_otd_baseline_quantity, 24.0, places=6)

    def test_reporting_period_utc_uses_company_timezone_and_exclusive_end(self):
        period_start = date(2000, 1, 1)
        period_end = date(2000, 1, 31)
        _kpi, target = self._make_target(
            "P5_OTD_TZ_BOUNDARY_FIX", period_start, period_end
        )
        partner = self.company.partner_id.sudo()
        previous_tz = partner.tz
        try:
            partner.write({"tz": "Asia/Kathmandu"})
            start_utc, end_utc, issue = reporting_period_utc(target)
        finally:
            partner.write({"tz": previous_tz or False})

        self.assertFalse(issue)
        self.assertEqual(start_utc, datetime(1999, 12, 31, 18, 15, 0))
        self.assertEqual(end_utc, datetime(2000, 1, 31, 18, 15, 0))

    def test_employee_cannot_trigger_supplier_otd_evaluation(self):
        _kpi, target = self._make_target("P5_OTD_EMPLOYEE_BLOCK_FIX")
        with self.assertRaises(AccessError):
            target.with_user(self.employee).action_evaluate_source()
