# Strategic Performance Management — Hoshin Validation Checkpoint

**Date:** 16 October 2026  
**Environment:** Odoo 20 Community  
**Addon:** `strategic_management`  
**Validation database:** `strategic_management_test`  
**Checkpoint status:** Hoshin vertical workflow passed the current automated suite and the documented manual checks below. This is **not** final module acceptance or production readiness.

## 1. Automated test evidence

The latest reported test command was run against `strategic_management_test`:

```powershell
docker exec odoo20-web python3 odoo-bin `
  --config=/etc/odoo/odoo.conf `
  -d strategic_management_test `
  -u strategic_management `
  --test-enable `
  --test-tags /strategic_management `
  --stop-after-init
```

The final log summary reported:

- **101 tests total**
- **59 post-tests**
- **0 failures and 0 errors**

The Hoshin tests logged in that run included the new-alignment-to-active-breakthrough regression test, revision wizard defaults, controlled revision/audit history, initiative activation prerequisites, endpoint mismatch, primary/supporting B>A relationships, and role-access checks. Existing KPI and Strategy regression tests also ran.

Some `ERROR`-level SQL log entries came from tests intentionally attempting invalid records (for example, a branch objective without its required parent and a Key Result without its required primary KPI). The final test summary still reported zero test failures/errors. The HTTP port 8069 address-in-use warning occurred during the test runner's HTTP server startup and did not prevent the tests from completing. Other displayed warnings (root execution, config options, markdown2, and the DMS ormcache deprecation) were not reported as test failures.

## 2. Manual workflow evidence

### Hoshin objective lifecycle

- Created and activated the organization-level Breakthrough Objective **Improve overall café-chain profitability**.
- Created and activated the organization-level Annual Objective **Improve annual net profit margin**.
- Kept the Financial BSC perspective distinct from the Hoshin horizon field.
- Created an active primary `B>A` relationship connecting those two objectives.

### Improvement initiative and required links

Created **Reduce Café Operating Costs** under **UI Test Strategic Plan 2026** and successfully activated it after creating three active primary relationships:

| Link | Relationship |
|---|---|
| `I>A` | Initiative → Improve annual net profit margin |
| `I>M` | Initiative → existing Net Profit Margin KPI definition |
| `I>O` | Initiative → Administrator as accountable owner |

After activation, the initiative displayed **Active**, **Administrator** as Accountable Owner, and the activation user/time. The X-Matrix links remained visible and active.

### Controlled revision and audit history

- Revised the `I>A` relationship using **Revise Alignment** with a reason.
- The original alignment appeared as **Superseded** and the replacement as **Active**.
- The original record's Revision History showed the change time, Administrator, replacement alignment, and reason.
- The automated tests also exercised preservation of old/new snapshots and immutability of history records.

### Role and employee record visibility

- **Head Office Manager:** Hoshin initiative, X-Matrix alignment, revision history, and branch-summary menu entries were visible.
- **Branch Manager:** Strategy navigation showed Objectives, OKR Objectives, and Breakthrough–Annual Alignment; Head Office-only initiative, full X-Matrix, and history menus were hidden.
- **Employee:** Strategy/Hoshin menus and KPI Definitions were hidden; KPI Targets and Evaluations remained accessible.
- **Employee KPI self-visibility:** UI Test Employee A could see their own Net Profit Margin evaluation (target 10, actual 8, attainment 80%, status Almost Met).
- **Employee isolation check:** An approved employee-specific target was created for `ui_test_employee_b`. The user reported that Employee A did not see Employee B's record(s). This was a manual UI check; retain the automated record-rule tests as the stronger repeatable evidence.

## 3. What this checkpoint proves — and what it does not

### Verified for this test slice

- The tested Hoshin horizons can use the existing Strategic Objective model while retaining BSC perspective and lifecycle.
- The tested `B>A`, `I>A`, `I>M`, and `I>O` links can be created with the expected endpoints and primary relationship.
- Initiative activation enforces the required active links and derives its owner from the primary owner link.
- Controlled revision replaces an active relationship without silently overwriting the original; the original is superseded and an audit-history row is recorded.
- The tested menu restrictions behave as intended for Head Office, Branch Manager, and Employee roles.
- The latest full test run passes with the count/result above.

### Still unverified or only partially verified

1. **`B>M` complete UI workflow:** the model supports this link type, but it was not manually exercised end to end in this checkpoint.
2. **Supporting links in the UI:** primary/supporting B>A behavior is covered by automated tests; a supporting relationship has not been manually walked through the full interface here.
3. **Direct-record security across every Hoshin model:** menu visibility and the automated security tests passed, but this checkpoint is not a substitute for a complete direct-URL/record-ID authorization matrix for all roles and models.
4. **KPI truth/provenance:** the X-Matrix `I>M` link references the existing Net Profit Margin KPI definition whose source type is controlled manual submission. This validates the relationship, **not** a reconciled accounting-derived Net Profit Margin calculation.
5. **Operational source adapters:** no evidence in this checkpoint proves POS sales, purchase commitments/receipts, inventory flows, recipe/BoM consumption, waste, stock variance, or supplier delivery adapters are implemented and reconciled against authoritative Odoo data.
6. **Financial KPI source contract:** revenue, net profit, and margin must not be treated as trusted operational results until accounting/source mappings, period semantics, returns/refunds, expenses/costs, currency, and reconciliation are validated.
7. **Full release/production readiness:** dashboards, complete reporting, notifications/scheduled jobs, remote-access deployment, HTTPS/reverse proxy, mail delivery, backup/restore, logging/recovery, and full UAT have not been established by this checkpoint.

## 4. Requirements and phase comparison

The project README defines Phase 2 as strategy, Hoshin deployment, X-Matrix links, and lifecycle; Phase 3 as the KPI contract/evaluation core; Phases 5–6 as operational adapters and financial KPIs; Phase 7 as native views/reporting/notifications; and Phase 8 as full acceptance and deployment readiness.

| Area | Current status | Evidence / remaining gate |
|---|---|---|
| Hoshin objectives and core initiative flow (Phase 2 slice) | **Tested slice passed** | Manual workflow plus 101-test full run; do not interpret as all Phase 2 acceptance closed. |
| X-Matrix link integrity and revision | **Tested slice passed; remaining link/UI coverage** | `B>A`, `I>A`, `I>M`, `I>O` and controlled revision tested; add/manual-check `B>M` and supporting UI journeys. |
| Role navigation and employee isolation | **Partially verified** | Observed all three role menus; employee A/B list isolation checked manually. Complete direct-ID security matrix remains a separate acceptance item. |
| KPI contract/evaluation core (Phase 3) | **Existing test coverage passed; full requirements gate still open** | Scoring and role tests passed; KPI target/source/version/provenance contract and full requirement traceability still require review. |
| Manual governance/corrections/audit (Phase 4) | **Partial** | Target approval, evaluation, and alignment revision history exercised; submission/return/reject, self-approval prevention, correction paths and all history rules need separate acceptance evidence. |
| POS/purchase/inventory/recipe/waste/supplier source adapters (Phase 5) | **Not demonstrated here** | Source feasibility, idempotency, unit conversion, exception handling and reconciliation evidence remain needed. |
| Financial KPIs (Phase 6) | **Not validated operationally** | Net Profit Margin used here is a manual-source KPI; accounting truth and formula/source reconciliation remain gated. |
| Dashboards/reporting/notifications (Phase 7) | **Not validated by this checkpoint** | Native list/form menus were used; dashboard, period, branch comparison, scheduled evaluation and notification acceptance remain. |
| Full regression/UAT/deployment (Phase 8) | **Open** | Full role journeys, operational-source reconciliation, mail/scheduled jobs, remote access, HTTPS, backups and recovery remain. |

## 5. Design-gate note

The v1.1 technical specification labels the detailed Hoshin model mapping and final ERD/cardinality choices as subject to design-gate review. The code and tests now provide implementation evidence, but a passing test run does not itself mean the documented design gate was formally approved. Before treating the Hoshin scope as fully closed, reconcile the implemented models, required fields, link uniqueness/cardinality, revision semantics, and BDD/TDD acceptance cases against the approved requirements and update the design record explicitly.

## 6. Recommended next action

Do not add unrelated features yet. Perform a requirements-to-implementation review for the Phase 2/Phase 3 boundary:

1. Record the actual implemented model/field/link map, including all five link categories (`B>A`, `I>A`, `I>M`, `B>M`, `I>O`).
2. Trace each relevant requirement to a test ID and observed outcome; mark uncovered cases instead of assuming they pass.
3. Confirm what KPI target/source/version behavior is already implemented versus still pending.
4. Keep operational KPI sources and financial metrics marked unverified until a source contract and reconciliation evidence exist.
5. Update the README/specification and close the applicable design/readiness gate before authorizing the next development phase.

All testing and manual records described here belong to `strategic_management_test`. The protected `odoo` database is not an authorized development/test target.
