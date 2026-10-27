Feature: Weekly purchasing budget goal
  As a café-chain owner I set a spending limit for a week and see whether
  confirmed purchase orders stay within it, without configuring KPIs myself.

  Rule: Spending = purchase orders confirmed in the period (date_approve), excluding tax, in company currency.
  Rule: The budget is a hard ceiling, inclusive: equal to the limit is achieved, one unit over is not.
  Rule: While the week is open the goal shows spending so far; only the closed week gets a final result.

  Implemented by tests/test_purchase_budget_goal.py (TestPurchaseBudgetGoal), one test per scenario.

  Scenario: A - Create a goal
    Given a Head Office Manager can create business goals
    When they choose Purchasing, a budget of 10,000, one week and a branch
    Then the goal, its KPI target and Key Result are saved
    And no KPI formula is entered by hand
    And reopening the goals list returns the same goal

  Scenario: B - Spending stays below the limit
    Given confirmed purchase orders totalling 8,500 excluding tax in the closed week
    When the week is evaluated
    Then 8,500 is committed and 1,500 remains
    And the final status is Achieved

  Scenario: C - Spending equals the limit
    Given confirmed purchase orders totalling exactly 10,000
    When the week is evaluated
    Then the goal is Achieved

  Scenario: D - Spending exceeds the limit
    Given confirmed purchase orders totalling 10,001
    When the week is evaluated
    Then the final status is Not achieved, over budget by 1
    And it is never reported as Almost Met

  Scenario: E - The week is still open
    Given the current week has 6,000 of confirmed orders
    When the owner opens the goal
    Then it shows 6,000 committed so far and 4,000 remaining, "Within budget so far"
    And no final result can be recorded before the week ends

  Scenario: F - RFQs and receipts
    Given a draft RFQ and a confirmed 4,000 order whose goods have not arrived
    Then the RFQ is excluded and the 4,000 is included
    When the goods arrive and the calculation runs again
    Then the order is not counted twice

  Scenario: G - Period boundaries
    Given orders confirmed just before, at the start of, at the end of and just after the week
    Then only confirmations from the first second of day one to the last second of day seven count

  Scenario: H - Branch scope and access
    Given a goal for Branch 1
    Then orders received into Branch 2 are excluded
    And Branch 2's manager can neither see nor evaluate Branch 1's goal
    And Branch 1 plus Branch 2 totals reconcile to the company total

  Scenario: I - No orders and invalid data
    Given no confirmed orders in the week, the committed amount is 0 and the result is valid
    Given an order in a currency without an exchange rate
    Then the goal shows Data unavailable and is not Achieved

  Scenario: J - Repeated evaluation
    When the goal is refreshed and evaluated again
    Then no duplicate goal, KPI, target, Key Result or evaluation is created

  Scenario: K - Currency and tax
    Given a foreign-currency order and a taxed order
    Then the foreign amount is converted at the confirmation-date rate
    And the untaxed amount is used

  Scenario: L - Approvals and linked status
    Given the target is not yet approved
    Then the goal cannot be evaluated and a Branch Manager cannot approve it
    When the final evaluation exists it is linked to the goal's Key Result

  Scenario: Purchasing page and goal agree
    Given the same branch and week
    Then the Purchasing overview's confirmed order value equals the goal's committed amount

  Scenario: M - A budget goal set up with the quantity option and a too-early deadline
    Given the goal "budget control" was created with its deadline on the first day of the week
    And "Limit how much of a product we buy" with 200 of a product was chosen for the whole week
    Then the readiness check names the chosen option and its "At most 200" target
    And it says the period must end by the goal deadline: choose an earlier "Measure until" date or extend the deadline
    When the owner chooses "Keep purchasing within budget" (measured in money) with 10,000
    Then the product is dropped and the goal measures confirmed purchasing in the company currency
    When Head Office extends the deadline to the end of the week
    Then the change is recorded as a deadline revision, a branch manager cannot do it, and it cannot pass the plan's end
    And the goal is ready, and its Key Result links the money KPI and the "Up to 10,000" target
    Given an order of 6,000 confirmed in the week and an unconfirmed quotation of 9,000
    Then the final result is 6,000, 4,000 remaining, 60% of the budget used: "Achieved: within budget"
