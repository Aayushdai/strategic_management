"""Posted-ledger financial KPI adapters.

The calculations intentionally fail closed when the posted P&L data needed for a
meaningful result is absent. They do not infer missing expenses as zero and do
not infer branch attribution from journals, names, or POS closing references.
"""

import math

from .common import DATA_QUALITY, NOT_EVALUATED, VALID, source_result

SUPPORTED_CALCULATIONS = {
    "accounting_net_profit",
    "accounting_net_profit_margin_pct",
}

_INCOME_TYPES = {"income", "income_other"}
_EXPENSE_TYPES = {"expense", "expense_other", "expense_direct_cost", "expense_depreciation"}


def collect_accounting_result(target, version):
    """Evaluate Net Profit or Net Profit Margin from posted P&L journal lines.

    The initial implementation supports organization scope only. Branch-level
    financial KPIs need a verified mapping for both income and expenses; the
    current module/database has no direct branch dimension on accounting lines.
    """
    Line = target.env["account.move.line"].sudo()
    empty_lines = Line.browse()

    if (
        version.source_type != "accounting"
        or version.calculation_type not in SUPPORTED_CALCULATIONS
    ):
        return source_result(
            target, version, DATA_QUALITY, None,
            "The approved KPI definition version does not select a supported accounting calculation.",
            "account.move.line", empty_lines,
        )

    if version.source_product_id:
        return source_result(
            target, version, DATA_QUALITY, None,
            "Accounting financial KPIs must not select a tracked source product.",
            "account.move.line", empty_lines,
        )

    company_currency = target.company_id.currency_id
    if version.calculation_type == "accounting_net_profit_margin_pct":
        if (version.unit or "").strip() != "%":
            return source_result(
                target, version, DATA_QUALITY, None,
                "Net Profit Margin must use the % unit.",
                "account.move.line", empty_lines,
            )
    else:
        expected_units = {
            (company_currency.symbol or "").strip(),
            (company_currency.name or "").strip(),
        }
        expected_units.discard("")
        if (version.unit or "").strip() not in expected_units:
            return source_result(
                target, version, DATA_QUALITY, None,
                "Net Profit must use the KPI company's currency unit (%s)."
                % (company_currency.symbol or company_currency.name),
                "account.move.line", empty_lines,
            )

    if not target.date_start or not target.date_end or target.date_start > target.date_end:
        return source_result(
            target, version, DATA_QUALITY, None,
            "A valid financial reporting period is required.",
            "account.move.line", empty_lines,
        )

    if target.scope != "organization":
        return source_result(
            target, version, NOT_EVALUATED, None,
            "Branch and employee financial KPIs are not evaluated because this release has "
            "no verified accounting-dimension mapping for attributing both income and "
            "expenses to that scope.",
            "account.move.line", empty_lines,
        )

    required_line_fields = {"date", "balance", "company_id", "account_id", "move_id"}
    if not required_line_fields.issubset(Line._fields):
        # Keep the required line schema explicit so unsupported installations fail closed.
        return source_result(
            target, version, DATA_QUALITY, None,
            "The installed Accounting model does not expose the fields required for posted P&L calculation.",
            "account.move.line", empty_lines,
        )
    Account = target.env["account.account"].sudo()
    if "account_type" not in Account._fields:
        return source_result(
            target, version, DATA_QUALITY, None,
            "The installed account model does not expose account types; financial classification cannot be verified.",
            "account.move.line", empty_lines,
        )

    pnl_types = sorted(_INCOME_TYPES | _EXPENSE_TYPES)
    lines = Line.search([
        ("company_id", "=", target.company_id.id),
        ("date", ">=", target.date_start),
        ("date", "<=", target.date_end),
        ("move_id.state", "=", "posted"),
        ("account_id.account_type", "in", pnl_types),
    ], order="date, move_id, id")
    income_lines = lines.filtered(lambda line: line.account_id.account_type in _INCOME_TYPES)
    expense_lines = lines.filtered(lambda line: line.account_id.account_type in _EXPENSE_TYPES)

    if not income_lines:
        return source_result(
            target, version, NOT_EVALUATED, None,
            "No posted income lines were found for this company and period; the adapter "
            "will not assume income is zero.",
            "account.move.line", lines,
        )
    if not expense_lines:
        return source_result(
            target, version, NOT_EVALUATED, None,
            "No posted expense or direct-cost lines were found for this company and period; "
            "missing expenses are not assumed to be zero.",
            "account.move.line", lines,
        )

    amounts = [line.balance for line in lines]
    if any(not isinstance(amount, (int, float)) or not math.isfinite(amount) for amount in amounts):
        return source_result(
            target, version, DATA_QUALITY, None,
            "Posted P&L lines contain a non-finite company-currency balance.",
            "account.move.line", lines,
        )

    # Odoo stores income credits as negative balances and expense debits as
    # positive balances. Convert both classes to positive accounting amounts.
    revenue = sum(-line.balance for line in income_lines)
    expenses = sum(line.balance for line in expense_lines)
    net_profit = revenue - expenses

    if not all(math.isfinite(value) for value in (revenue, expenses, net_profit)):
        return source_result(
            target, version, DATA_QUALITY, None,
            "The posted P&L balances did not produce finite financial totals.",
            "account.move.line", lines,
        )

    if version.calculation_type == "accounting_net_profit":
        value = net_profit
        explanation = (
            "Net Profit = posted income (%s) minus posted expenses/direct costs (%s), "
            "in company currency. Calculation uses posted P&L account lines only; "
            "the accounting period must be complete and all applicable income and expenses posted."
        ) % (round(revenue, 2), round(expenses, 2))
    else:
        if revenue <= 0.0:
            return source_result(
                target, version, NOT_EVALUATED, None,
                "Net Profit Margin cannot be calculated because net posted income is zero "
                "or negative; no valid revenue denominator is available.",
                "account.move.line", lines,
            )
        value = (net_profit / revenue) * 100.0
        explanation = (
            "Net Profit Margin = (posted income (%s) minus posted expenses/direct costs (%s)) / posted income * 100. "
            "Calculation uses posted P&L account lines only; the accounting period must "
            "be complete and all applicable income and expenses posted."
        ) % (round(revenue, 2), round(expenses, 2))

    return source_result(
        target, version, VALID, float(value), explanation,
        "account.move.line", lines,
    )
