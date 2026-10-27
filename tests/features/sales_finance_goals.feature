Feature: Sales & Finance goals measured from real POS and Accounting data
  As a café-chain owner
  I want to pick sales, profit, product, cost and branch goals in plain language
  So that each goal is linked to its OKR, key result, KPI, target and evaluation without writing formulas

  Background:
    Given branch 1 sold 2 lattes for 10.00 + 1.50 tax and 1 muffin for 4.00 + 0.60 tax in the week
    And branch 1 refunded 1 latte for 5.00 + 0.75 tax in the week
    And branch 1 has a draft order and an order after the week, which are not sales of the week
    And branch 2 sold 5 muffins for 20.00 + 3.00 tax in the week

  Scenario: Net sales exclude tax and subtract refunds
    When "Increase a branch's net sales" for branch 1 with at least 8.00 is evaluated after the week
    Then net sales are 9.00 and the goal is achieved
    And the company-wide net sales are 29.00
    And the Business Overview shows the same 9.00 and 29.00 for the same week and branch

  Scenario: No sales data is not zero sales
    Given a week without any POS order
    Then net sales have no result and the objective stays Not Evaluated
    And a branch without a linked Point of Sale fails the readiness check

  Scenario: Average order value
    Then branch 1's average order value is 9.00 / 2 customer orders = 4.50; the refund is not an order
    And a week with only a refund has no average

  Scenario: Sales growth
    Given branch 1 sold 6.00 in the week before and more before that
    Then growth is (9.00 - 6.00) / 6.00 = +50.0% against "Growth of at least 40.0%"
    And a branch with no sales in the week before has no growth result
    And growth for a week that has not ended is not evaluated and not shown as a partial figure

  Scenario: Product performance
    Then latte sales at branch 1 are 10.00 - 5.00 = 5.00 and the quantity sold is 2 - 1 = 1
    And muffin sales for the company are 24.00
    And an existing approved quantity KPI is reused and re-evaluation keeps one evaluation and one key result

  Scenario: Profit and costs from posted Accounting entries
    Given posted income of 1,000, posted expenses of 300 and direct costs of 200 in the week
    And a draft expense and an expense after the week
    Then total expenses and direct costs are 500, within a 600 limit
    And expenses are 50% of income, above a 40% limit
    And net profit is 500 and the margin is 50%
    And without income the expense ratio has no result; without expenses the total has no result

  Scenario: Scope, access and unsupported goals
    Then profit goals are company-wide only and the branch sales goal is branch-only
    And branch profit and product margin are not supported yet and are refused with the reason
    And a branch manager evaluates only their own branch and cannot prepare targets
    And a measured goal is submitted for approval, not activated

  Scenario: A configured goal without data shows "Waiting for data"
    Given "Sell more of a product" for lattes at branch 1 with at least 100 for the current week
    And no paid or posted POS order in that week
    When the measurement is saved
    Then the data check reads "Data source checked — no matching records found" and setup can continue
    When the target is approved
    Then the goal shows its target "At least 100" with an empty neutral bar labelled "Waiting for data" and the reason
    And it shows no 0%, no value, no evaluation, and it is not ranked

  Scenario: A configured goal with data shows its progress
    Given 40 lattes sold at branch 1 today
    Then the goal shows "40" so far against "At least 100", a 40% bar, the same in the dashboard row and the details
