"""Regression tests for posted-ledger financial KPI calculations and cutover safety."""

from datetime import date
from unittest import SkipTest

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.strategic_management.services.kpi_sources.accounting import (
    collect_accounting_result,
)
from odoo.addons.strategic_management.models.kpi_source_adapter import (
    _SOURCE_ADAPTERS,
    _SOURCE_COLLECTORS,
)
from odoo.addons.strategic_management.models import kpi_scheduler


@tagged("post_install", "-at_install")
class TestStrategicKpiAccountingSourceAdapter(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.currency_unit = cls.company.currency_id.symbol or cls.company.currency_id.name
        cls.GroupUser = cls.env.ref("base.group_user")
        cls.GroupHead = cls.env.ref("strategic_management.group_strategic_head_office_manager")
        cls.head = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Phase 6 Accounting KPI Head Office",
            "login": "phase6_accounting_kpi_head",
            "email": "phase6_accounting_kpi_head@example.com",
            "company_id": cls.company.id,
            "company_ids": [(6, 0, [cls.company.id])],
            "group_ids": [(6, 0, [cls.GroupUser.id, cls.GroupHead.id])],
        })

        Account = cls.env["account.account"].sudo()
        accounts = Account.search([("account_type", "in", [
            "income", "expense", "asset_current", "asset_cash",
        ])])
        if "company_ids" in Account._fields:
            accounts = accounts.filtered(lambda account: cls.company in account.company_ids)
        elif "company_id" in Account._fields:
            accounts = accounts.filtered(lambda account: account.company_id == cls.company)

        cls.income_account = accounts.filtered(lambda account: account.account_type == "income")[:1]
        cls.expense_account = accounts.filtered(lambda account: account.account_type == "expense")[:1]
        cls.offset_account = accounts.filtered(
            lambda account: account.account_type in ("asset_current", "asset_cash")
        )[:1]
        cls.journal = cls.env["account.journal"].sudo().search([
            ("company_id", "=", cls.company.id),
            ("type", "=", "general"),
        ], limit=1)
        if not cls.income_account or not cls.expense_account or not cls.offset_account or not cls.journal:
            raise SkipTest(
                "Phase 6 accounting adapter tests require income, expense, asset accounts and a general journal."
            )

    def _make_kpi(self, code, calculation_type, unit=None, source_type="accounting"):
        if unit is None:
            unit = "%" if calculation_type == "accounting_net_profit_margin_pct" else self.currency_unit
        return self.env["strategic.kpi.definition"].with_user(self.head).create({
            "name": "%s KPI" % code,
            "code": code,
            "company_id": self.company.id,
            "unit": unit,
            "direction": "higher",
            "source_type": source_type,
            "calculation_type": calculation_type,
            "description": "Test fixture for posted-ledger financial KPI evaluation.",
        })

    def _make_target(self, code, calculation_type, start, end, scope="organization"):
        kpi = self._make_kpi(code, calculation_type)
        values = {
            "kpi_id": kpi.id,
            "date_start": start,
            "date_end": end,
            "scope": scope,
            "target_value": 50.0,
        }
        if scope == "branch":
            warehouse = self.env["stock.warehouse"].search([
                ("company_id", "=", self.company.id),
            ], limit=1)
            if not warehouse:
                self.skipTest("A warehouse is required to exercise branch-scope fail-closed behavior.")
            values["warehouse_id"] = warehouse.id
        target = self.env["strategic.kpi.target"].with_user(self.head).create(values)
        target.with_user(self.head).action_approve()
        return kpi, target

    def _post_pnl_entry(self, posting_date, account, debit=0.0, credit=0.0):
        """Post a balanced general journal entry with one P&L line."""
        signed_balance = debit - credit
        offset_debit = max(-signed_balance, 0.0)
        offset_credit = max(signed_balance, 0.0)
        move = self.env["account.move"].sudo().create({
            "move_type": "entry",
            "company_id": self.company.id,
            "journal_id": self.journal.id,
            "date": posting_date,
            "line_ids": [
                (0, 0, {
                    "name": "Phase 6 test P&L line",
                    "account_id": account.id,
                    "debit": debit,
                    "credit": credit,
                }),
                (0, 0, {
                    "name": "Phase 6 test balancing line",
                    "account_id": self.offset_account.id,
                    "debit": offset_debit,
                    "credit": offset_credit,
                }),
            ],
        })
        move.action_post()
        return move

    def _evaluate(self, target):
        action = target.with_user(self.head).action_evaluate_source()
        return self.env["strategic.kpi.evaluation"].browse(action["res_id"])

    def test_manual_and_scheduled_dispatchers_register_accounting_calculations(self):
        expected = {"accounting_net_profit", "accounting_net_profit_margin_pct", "accounting_overdue_supplier_bills_amount",
                    "accounting_total_expenses", "accounting_expense_to_income_pct"}
        self.assertEqual(_SOURCE_ADAPTERS.get("accounting"), expected)
        self.assertIs(_SOURCE_COLLECTORS.get("accounting"), collect_accounting_result)
        self.assertIs(kpi_scheduler._SOURCE_COLLECTORS.get("accounting"), collect_accounting_result)

    def test_accounting_calculation_contract_rejects_wrong_units_and_source(self):
        with self.assertRaises(ValidationError):
            self._make_kpi("P6_BAD_NET_PROFIT_UNIT", "accounting_net_profit", unit="%")
        with self.assertRaises(ValidationError):
            self._make_kpi("P6_BAD_MARGIN_UNIT", "accounting_net_profit_margin_pct", unit=self.currency_unit)
        with self.assertRaises(ValidationError):
            self._make_kpi(
                "P6_BAD_ACCOUNTING_SOURCE", "accounting_net_profit",
                unit=self.currency_unit, source_type="manual",
            )

    def test_posted_income_and_expenses_produce_net_profit_and_margin(self):
        self._post_pnl_entry(date(2098, 1, 5), self.income_account, credit=100.0)
        self._post_pnl_entry(date(2098, 1, 9), self.expense_account, debit=40.0)
        # A large next-period posting must not leak into January's evaluation.
        self._post_pnl_entry(date(2098, 2, 3), self.income_account, credit=900.0)

        _profit_kpi, profit_target = self._make_target(
            "P6_NET_PROFIT_VALID", "accounting_net_profit", date(2098, 1, 1), date(2098, 1, 31)
        )
        profit = self._evaluate(profit_target)
        self.assertEqual(profit.status, "valid", profit.reason or profit.source_note)
        self.assertTrue(profit.has_actual_value)
        self.assertAlmostEqual(profit.actual_value, 60.0, places=2)
        self.assertEqual(profit.source_model, "account.move.line")

        _margin_kpi, margin_target = self._make_target(
            "P6_NET_MARGIN_VALID", "accounting_net_profit_margin_pct", date(2098, 1, 1), date(2098, 1, 31)
        )
        margin = self._evaluate(margin_target)
        self.assertEqual(margin.status, "valid", margin.reason or margin.source_note)
        self.assertTrue(margin.has_actual_value)
        self.assertAlmostEqual(margin.actual_value, 60.0, places=2)

    def test_other_expense_account_type_is_included_in_net_profit(self):
        other_expense = self.env["account.account"].sudo().search([
            ("account_type", "=", "expense_other"),
            ("company_ids", "in", [self.company.id]),
        ], limit=1)
        if not other_expense:
            self.skipTest("The test chart has no Other Expenses account.")

        self._post_pnl_entry(date(2098, 5, 5), self.income_account, credit=200.0)
        self._post_pnl_entry(date(2098, 5, 6), other_expense, debit=25.0)
        _kpi, target = self._make_target(
            "P6_NET_PROFIT_OTHER_EXPENSE", "accounting_net_profit",
            date(2098, 5, 1), date(2098, 5, 31),
        )
        result = self._evaluate(target)
        self.assertEqual(result.status, "valid", result.reason or result.source_note)
        self.assertAlmostEqual(result.actual_value, 175.0, places=2)

    def test_missing_expense_lines_are_not_assumed_to_be_zero(self):
        self._post_pnl_entry(date(2098, 2, 5), self.income_account, credit=90.0)
        _kpi, target = self._make_target(
            "P6_NET_PROFIT_NO_EXPENSES", "accounting_net_profit", date(2098, 2, 1), date(2098, 2, 28)
        )
        result = collect_accounting_result(
            target, target.kpi_id._get_definition_version_for_period(target.date_start, target.date_end)
        )
        self.assertEqual(result["status"], "not_evaluated")
        self.assertFalse(result["has_value"])
        self.assertIn("missing expenses are not assumed", result["reason"])

    def test_zero_or_negative_revenue_does_not_produce_margin(self):
        self._post_pnl_entry(date(2098, 3, 5), self.income_account, credit=100.0)
        self._post_pnl_entry(date(2098, 3, 6), self.income_account, debit=100.0)
        self._post_pnl_entry(date(2098, 3, 7), self.expense_account, debit=10.0)
        _kpi, target = self._make_target(
            "P6_NET_MARGIN_ZERO_REVENUE", "accounting_net_profit_margin_pct", date(2098, 3, 1), date(2098, 3, 31)
        )
        result = collect_accounting_result(
            target, target.kpi_id._get_definition_version_for_period(target.date_start, target.date_end)
        )
        self.assertEqual(result["status"], "not_evaluated")
        self.assertFalse(result["has_value"])
        self.assertIn("zero or negative", result["reason"])

    def test_branch_financial_kpis_fail_closed_without_verified_account_mapping(self):
        _kpi, target = self._make_target(
            "P6_NET_PROFIT_BRANCH_UNMAPPED", "accounting_net_profit",
            date(2098, 4, 1), date(2098, 4, 30), scope="branch",
        )
        result = collect_accounting_result(
            target, target.kpi_id._get_definition_version_for_period(target.date_start, target.date_end)
        )
        self.assertEqual(result["status"], "not_evaluated")
        self.assertFalse(result["has_value"])
        self.assertIn("no verified accounting-dimension mapping", result["reason"])

    def test_accounting_calculation_version_effective_date_preserves_manual_october(self):
        margin = self.env["strategic.kpi.definition"].with_user(self.head).create({
            "name": "P6 Cutover Margin KPI",
            "code": "P6_CUTOVER_MARGIN",
            "company_id": self.company.id,
            "unit": "%",
            "direction": "higher",
            "source_type": "manual",
            "calculation_type": "manual_value",
        })
        original = margin._get_definition_version_for_period("2026-10-01", "2026-10-31")
        revision = self.env["strategic.kpi.definition.version"].with_user(self.head).create({
            "kpi_id": margin.id,
            "unit": "%",
            "direction": "higher",
            "source_type": "accounting",
            "calculation_type": "accounting_net_profit_margin_pct",
            "effective_from": "2026-11-01",
            "change_reason": "Phase 6 automated accounting KPI cutover.",
        })
        revision.with_user(self.head).action_approve()

        original.invalidate_recordset()
        self.assertEqual(str(original.effective_to), "2026-10-31")
        october = margin._get_definition_version_for_period("2026-10-01", "2026-10-31")
        november = margin._get_definition_version_for_period("2026-11-01", "2026-11-30")
        self.assertEqual(october.calculation_type, "manual_value")
        self.assertEqual(october.source_type, "manual")
        self.assertEqual(november.calculation_type, "accounting_net_profit_margin_pct")
        self.assertEqual(november.source_type, "accounting")
