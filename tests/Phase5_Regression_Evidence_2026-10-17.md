# Phase 5 Regression Evidence — 2026-10-17

**Odoo version:** 20.0
**Addon:** `strategic_management`
**Database:** `strategic_management_test` only
**Disposition:** Focused purchase/receipt acceptance and full addon regression gates passed. Phase 5 remains open pending reconciliation against native operational records.

## 1. Full addon regression run

Command executed:

```powershell
docker exec odoo20-web python3 odoo-bin --config=/etc/odoo/odoo.conf -d strategic_management_test -u strategic_management --test-enable --test-tags /strategic_management --http-port=8070 --stop-after-init
```

The captured log ended with:

```text
186 post-tests in 60.89s
strategic_management: 256 tests
0 failed, 0 error(s) of 186 tests when loading database 'strategic_management_test'
```

This is the authoritative latest full-suite result for this evidence record. Odoo's `186 post-tests` count and `256 tests` statistics line are distinct counters and must not be conflated.

One accounting test was skipped because the configured test chart had no account of type **Other Expenses**: `TestStrategicKpiAccountingSourceAdapter.test_other_expense_account_type_is_included_in_net_profit`. The run had zero test failures and zero test errors, but this particular assertion was not exercised in that environment.

## 2. Focused purchase and receipt acceptance

The focused acceptance class `TestPurchaseDashboardAcceptance` completed with four post-install test methods and no failures or errors. The broader test run also executed the class with no failure or error.

The covered acceptance scenarios include:

- confirming a 20 kg PO refreshes organization and branch ordered-quantity evaluations without a manual evaluation action;
- validating a completed 20 kg supplier receipt refreshes received-quantity targets;
- dashboard landing configuration displays actual values and units with analysis views retained; and
- tested Head Office, branch and employee evaluation visibility follows the existing access rules.

The dedicated receipt test previously exposed the missing refresh hook, so the zero-failure result after the receipt implementation is evidence that the tested flow now passes in `strategic_management_test`.

## 3. Focused source-adapter regression

Command executed:

```powershell
docker exec odoo20-web python3 odoo-bin --config=/etc/odoo/odoo.conf -d strategic_management_test -u strategic_management --test-enable --test-tags /strategic_management:TestStrategicKpiPurchaseInventorySourceAdapter --http-port=8070 --stop-after-init
```

Captured result:

```text
7 post-tests in 4.37s
strategic_management: 9 tests
0 failed, 0 error(s) of 7 tests when loading database 'strategic_management_test'
```

The executed tests covered confirmed purchase quantity, employee restrictions on source evaluation, UoM/source contract validation, no completed receipt remaining `not_evaluated`, and waste-recording behavior. Keep the distinction between this focused run and the full-suite run above.

## 4. Scope and interpretation

These test runs provide regression evidence for the current code in the stated test database. They do **not** establish that operational KPIs reconcile to a real café's native Odoo records, and they do not justify declaring Phase 5 accepted for release.

The purchase quantities remain semantically distinct:

- **Ordered quantity** comes from confirmed purchase-order lines in the selected company, branch and reporting period.
- **Received quantity** comes from completed inbound supplier-to-internal stock moves, using the move completion period.
- Ordered quantity must not be presented as received stock; unvalidated pickings are not receipts.
- Dashboard access must remain restricted by the existing role rules. Employees must not gain visibility into aggregate organizational or branch purchase quantities through this work.

The root-user notice, unknown `password`/`email` configuration options, and unrelated DMS `ormcache` deprecation warning appeared in the logs but did not cause these test runs to fail.

## 5. Remaining operational acceptance gates

The following remain open and require read-only reconciliation against native Odoo records for a selected completed period, with source IDs and any unmatched differences recorded:

1. **POS sold quantity:** compare the adapter's eligible paid/completed `pos.order` / `pos.order.line` cohort and per-product net quantities with the native POS UI/report.
2. **Recipe-based estimated consumption:** verify the BoM effective on each sale date, line quantities/UoM conversion, refunds, combo products and missing/ambiguous recipe mappings. Keep estimated consumption separate from completed stock movements.
3. **Purchase and receipt quantities:** reconcile the selected PO-line cohort and completed supplier-to-internal move cohort, including partial receipts and compatible UoMs.
4. **Waste and inventory flow:** compare recorded and validated waste by the approved categories (**Spoilage/Expiry** and **Damaged Goods**), then separately reconcile opening stock, receipts, consumption, waste, transfers, adjustments and closing stock/variance.
5. **Supplier OTD:** manually inspect a small PO/receipt cohort against the snapshotted original due date and quantity, including partial, late and over-receipts and unresolved cancellation/return policy.
6. **Financial KPI gate:** Phase 6 remains blocked until net profit and margin are reconciled to Odoo's native accrual-basis P&L with verified account/dimension mapping, period, scope, currency and a declared tolerance. Do not approximate net profit as sales minus purchases.

## 6. Current gate disposition

| Gate | Status | Evidence / remaining work |
|---|---|---|
| Full addon automated regression | **PASS** | 186 post-tests; 0 failures; 0 errors; Odoo statistics report 256 tests |
| Purchase/receipt dashboard acceptance | **PASS** | Four focused acceptance methods pass; order and receipt refresh paths exercised |
| Purchase/inventory source-adapter regression | **PASS** | 7 post-tests; 0 failures; 0 errors |
| POS source reconciliation | **OPEN** | Reconcile a representative period and product cohort against native POS records |
| Recipe consumption reconciliation | **OPEN** | Verify effective BoMs and estimated-vs-actual distinction |
| Stock-flow/waste reconciliation | **OPEN** | Match source records and closing stock; waste remains distinct from variance |
| Supplier OTD reconciliation and edge-policy acceptance | **OPEN / PARTIAL** | Reconcile cohort; confirm cancellation, return and boundary policy |
| Financial source mapping / P&L reconciliation | **OPEN — BLOCKS PHASE 6** | Validate accounting dimensions and compare against native accrual P&L |

## 7. Recommended next action

Begin with a **read-only POS source reconciliation** in `strategic_management_test`: identify a completed reporting period with usable paid/completed POS orders, select one product present in the period, and compare the eligible order/line cohort plus signed line quantity to the adapter output. Record the period, company, POS configuration/warehouse, product/UoM, order and line IDs, source total, adapter total, and difference. If there are no suitable POS records, record that as a data-availability blocker rather than fabricate an example result.

Do not use the protected `odoo` database for this work.
