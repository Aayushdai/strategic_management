# Strategic Management — Phase 5 Operational Acceptance Status

**Date:** 2026-10-16<br>
**Database tested:** `strategic_management_test`<br>
**Module:** `strategic_management`<br>
**Decision:** Automated regression gate passed; Phase 5 is **not yet fully accepted for release** because live-source reconciliation and several operational edge-case validations remain open.

## 1. Latest test evidence

The run at 2026-10-16 14:12 completed module upgrade and post-tests successfully:

- **160 post-tests executed**
- **0 failures**
- **0 errors**
- Odoo's module statistics line reports **220 tests**; this is a separate statistic and should not be described as 220 post-tests.
- The output shows nine supplier-delivery test methods ran, with no reported failures or errors.
- The recurring `127.0.0.1:8069` bind warning did not stop the run.

This proves the code present in the test container passed the executed automated suite. It does not prove the KPI outputs reconcile to real café operations or accounting records.

## 2. Supplier On-Time Delivery contract now tested

The adapter's intended calculation is:

`OTD (%) = total quantity received by the original committed due date / total quantity due in the elapsed baseline-date cohort × 100`

The implementation caps on-time quantity at the snapshotted due quantity, credits partial receipts proportionately, snapshots the original promised date and quantity at PO confirmation, converts quantities to the product's base UoM, and excludes future-due lines from being prematurely treated as late.

The latest Odoo run includes tests for:

- quantity-weighted proportional credit for partial receipt;
- over-receipt capping;
- no receipt against a due cohort returning valid 0%;
- future-due cohort returning Not Evaluated rather than being prematurely scored;
- invalid/empty due quantity handling;
- immutable original date and quantity snapshot;
- rejection of attempts to forge/rewrite baseline values;
- KPI definition contract validation; and
- employee access restriction.

## 3. Remaining Phase 5 release gates

### A. Supplier OTD edge policies

The implementation currently fails closed with `data_quality_issue` rather than silently scoring cohorts containing unresolved cases:

- canceled PO lines;
- supplier returns or rejected-delivery moves;
- post-confirmation changes to quantity, product, supplier, company, or warehouse;
- candidate confirmed lines lacking an original baseline snapshot.

These are deliberate safety behaviors, but the business owner still needs to approve the policies. In particular:

- **Backorders/multiple partial receipts:** the adapter reads completed stock moves linked to the PO line, but a dedicated integration scenario should verify the actual Odoo 20 relationship and quantity aggregation.
- **UoM conversion:** conversion is implemented, but a focused test should cover a receipt and PO expressed in different compatible units.
- **Due-date changes:** the initial baseline remains authoritative; changing the currently displayed promise date does not change the cohort. The source note records both dates, but a controlled approved due-date revision/history workflow is not demonstrated by the current tests.
- **Timezone/period boundary:** the adapter converts the selected reporting period to UTC; boundary tests are still needed to verify committed-date and receipt timestamps immediately before/at/after the cutoff.
- **Cancellations and returns:** the current result is a data-quality issue, not an assumed inclusion or exclusion. This behavior needs explicit acceptance.

### B. Operational-source reconciliation

The charter requires a sampled reconciliation against native Odoo records/UI, beyond fixture-based automated tests. These checks remain open:

1. POS completed/paid order totals versus the configured sales source.
2. Estimated recipe consumption using the recipe/BoM effective at sale time, kept separate from actual stock movements and never double-counted.
3. Confirmed purchase-order commitment quantities versus PO lines; received quantities versus completed inbound stock moves.
4. Recorded waste versus validated waste; permitted categories remain **Spoilage/Expiry** and **Damaged Goods**. Inventory variance must remain separate from waste.
5. Inventory-flow totals and expected closing stock versus native system stock/movement records for a selected completed period.
6. Supplier OTD numerator/denominator against a small manually inspected PO cohort, including partial and late receipts.

## 4. Financial KPI gate — do not skip source validation

The project charter explicitly makes Phase 6 conditional on validated mappings. Earlier environment inspection found no usable accounting-move records, so this needs revalidation in the current test database. Model/module availability alone does not establish that financial KPIs are derivable.

Before implementing/activating revenue, net profit, or margin, investigate and document:

- company/branch/warehouse/accounting dimensions;
- accrual-basis Profit and Loss mapping reconciled for the same period, scope, currency and filters;
- revenue treatment for taxes, discounts, returns/refunds and selected order/accounting states;
- expense/cost/account mapping and currency conversion rules;
- a known reporting period that reconciles to Odoo's native P&L; and
- a declared tolerance and data-quality behavior where values do not reconcile.

Net profit must not be approximated as sales minus purchases.

## 5. Recommended next batch

Complete one **Phase 5 acceptance and reconciliation batch**, not another general rerun:

1. Add focused Odoo integration tests for backorders/multiple receipts, compatible-UoM conversion, changed promise dates, cutoff boundaries, and the currently fail-closed cancellation/return/reassignment cases.
2. Run a read-only source-reconciliation check against `strategic_management_test` for representative POS, PO, stock-move, waste/scrap, recipe/BoM and accounting records. Record when a source has no usable records instead of manufacturing an example result.
3. Update the requirements traceability/gate record with each result and unresolved decision.

Do not run experiments on the protected `odoo` database. Keep the reconciliation read-only and use `strategic_management_test` only.

## 6. Current gate disposition

| Gate | Status | Evidence / remaining work |
|---|---|---|
| Full module test suite | **PASS** | 160 post-tests; 0 failures; 0 errors |
| Supplier OTD formula regression tests | **PASS** | Nine supplier-delivery test methods shown in run log |
| OTD source-data reconciliation | **OPEN** | Compare calculated cohort against actual PO/stock move records |
| OTD edge-case contract | **PARTIAL** | Fail-closed policies exist; business acceptance and focused edge tests remain |
| POS/recipe/inventory/waste reconciliation | **OPEN** | Automated tests passed, but sampled native-record reconciliation not evidenced by this run |
| Financial source mapping and P&L reconciliation | **OPEN / BLOCKING Phase 6** | Revalidate available records and reconcile to native accrual P&L |
