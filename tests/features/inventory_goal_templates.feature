Feature: Inventory & Waste goal options and how an objective is achieved
  As a café owner
  I want Inventory & Waste goal ideas that say honestly what can be measured today
  And I want to see, for each goal, the OKR objective, key results and KPIs behind it

  Scenario: The catalogue shows which inventory goals can be measured
    Given the Inventory & Waste goal options
    Then waste quantity, waste cost, waste cost as % of sales, a branch's waste rate, excess stock,
      stock-count accuracy and unexplained stock differences are available
    And stock coverage, planned-count completion, ingredient-use differences, recipe coverage and
      waste-recording completeness are not supported yet, each saying exactly what is missing

  Scenario: Options belong to their own business area
    Given an Inventory & Waste goal
    When I choose a Purchasing option or an unavailable inventory option
    Then the goal is refused and no target is created

  Scenario: Allowed stock difference is a tolerance
    Given an allowed difference of plus or minus 2
    Then a difference of -1 or +1 uses 50% of the tolerance
    And a difference of -3 is outside the tolerance

  Scenario: The goal details show the real OKR chain
    Given an approved "Reduce unexplained stock differences" goal for one product with ±2 L allowed
    Then "How this objective will be achieved" lists its OKR objective and key result
    And the key result shows its KPI, target "Within ±2 L", direction and data source
    And it says "No evaluation yet" until the KPI is evaluated
    When the week has ended and the KPI is evaluated from real stock movements
    Then the key result shows the measured current value

  Scenario: Recorded waste of one product
    Given validated scrap of milk at branch 1 tagged Damaged Goods during the week, on its last day,
      after the week, at branch 2 and of another product
    When the "Reduce recorded waste of one product" goal for milk at branch 1 is evaluated after the week
    Then only branch 1's milk waste inside the week is counted, in litres
    And no waste at all is a valid zero, worded as "zero recorded waste, not proof that nothing was wasted"
    And scrap with a reason other than Spoilage/Expiry or Damaged Goods stops the readiness check

  Scenario: Waste quantity and waste cost are separate measurements
    Given coffee beans with an inventory cost of 200 per kg and a sales price of 999
    And 30 kg and 22 kg of beans scrapped as Damaged Goods at branch 1 during the week
    When "Track recorded waste quantity" with a limit of 100 kg is evaluated after the week
    Then it shows 52 kg against "At most 100 kg", 52% of the limit used
    When "Reduce recorded waste cost" with a limit of 10,000 is evaluated after the week
    Then it shows 10,400 in the company currency, from Odoo's stock valuation and not the sales price
    And 104% of the budget is used and the limit is exceeded
    And the dashboard row and the detail screen show the same value and unit

  Scenario: Waste cost filters and recorded valuation
    Given beans waste at branch 1 and branch 2, after the week, and waste of another product
    Then the branch goal counts only branch 1's beans inside the week and the company goal both branches
    And both goals reuse one approved measure
    And a later change of the beans' cost does not rewrite the recorded waste cost

  Scenario: Waste cost needs real valuation
    Given waste of a product without a cost
    Then the cost goal fails its readiness check with that reason instead of showing zero
    And the quantity of the same waste can still be tracked
    And a move Odoo did not value as stock leaving has no cost rather than a zero cost

  Scenario: Units and limits
    Then a waste-cost KPI must use the company currency and a waste-quantity KPI the product's unit
    And waste cost exactly at the limit is "Achieved: within the limit"
    And only Head Office can prepare a waste target

  Scenario: Waste cost compared with the same branch's sales
    Given waste worth 1.00 at branch 1 and POS sales of 12.00 including 2.00 tax in the week
    Then branch 1's waste rate is 10%
    And a week without sales has no result instead of 0%, so the objective stays Not Evaluated
    And a branch without a linked Point of Sale fails the readiness check
    And the branch-rate option is refused for a company-wide goal

  Scenario: Excess stock and physical count accuracy
    Given 10 L of milk received in the week and 5 L after it
    Then closing stock is 10 L, 2 L over an approved maximum of 8 L
    Given a count finding 8.5 L instead of 10 L, then a second count with no difference
    Then the count difference is 1.5 L over 2 counts, while the stock-flow check shows 0 L
    And a week without counts has no count-accuracy result

  Scenario: Limits are judged on the whole period
    Given an approved waste limit for a week that has not ended
    Then evaluating now is refused and the goal shows the result so far
    And evaluating a finished week twice keeps one evaluation, one OKR and one key result

  Scenario: Selecting, saving and linking an Inventory & Waste goal
    Given a new branch goal "Waste less milk" with nothing chosen yet
    Then it has no Key Result and says "choose what to measure and click 'Save and check readiness'"
    When "Track recorded waste quantity" for milk with at most 5 L is saved
    Then the option, product and period are stored, still without a Key Result,
      and the page says Head Office prepares the target next
    When Head Office prepares and approves the target
    Then one required Key Result links the litre-based waste KPI, the approved branch target and the OKR,
      all for the chosen week
    When the week is evaluated
    Then the Key Result's evaluation shows 2 L
    And reopening, re-evaluating or approving again creates no further KPI, target, OKR, Key Result or evaluation
    And the cost of the same waste is a separate goal with its own currency KPI from valuation (4.00)
