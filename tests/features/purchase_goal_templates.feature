Feature: Purchasing goal templates
  The owner picks a plain-language Purchasing goal; the system maps it to a measure, a target rule
  (ceiling, floor, price reduction, price-rise limit) and the existing objective / KPI / Key Result chain.
  Implemented by tests/test_purchase_goal_templates.py (TestPurchaseGoalTemplates); budget scenarios
  are in purchase_budget_goal.feature.

  Rule: Ceilings and floors are inclusive hard limits: Achieved or Not achieved, never Almost Met.
  Rule: Values come from dated records (confirmations, receipts, payments), not from today's state.
  Rule: Options that cannot be measured yet stay visible with the reason, and cannot be set up.

  Scenario: The catalogue is complete and honest
    Given the Purchasing goal options
    Then all 19 are listed with a status: available, requires setup or not supported yet
    And choosing an unavailable option is refused with its reason, creating no target

  Scenario: Limit how much of a product we buy
    Given 30 kg and 500 g of coffee beans ordered in the week
    Then the ordered quantity is 30.5 kg
    And with a 30 kg limit the final result is Not achieved, over the limit by 0.5 kg
    And a week with no orders is 0 kg and Achieved

  Scenario: Reduce the price we pay
    Given coffee beans cost 1,000 per kg last week and 950 per kg this week
    Then the price change is -5%
    And a "reduce by at least 5%" goal is Achieved and "at least 6%" is Not achieved
    And a product with no purchases last week has no comparison, not a saving

  Scenario: Alert me when prices rise
    Given the price rose from 1,000 to 1,060 per kg
    Then a "rise no more than 5%" goal is Not achieved

  Scenario: Avoid relying on one supplier
    Given 700 bought from one supplier and 300 from another
    Then the largest supplier's share is 70% and the supplier rows add up to 1,000
    And a 60% ceiling is Not achieved
    And a week with no purchasing has no share rather than 0%

  Scenario: Track orders still waiting for delivery
    Given a confirmed 1,000 order of which 40% was received during the week and the rest after it
    And a draft RFQ
    Then 600 was still awaiting delivery at the end of the week and the RFQ is not counted

  Scenario: Receive goods faster after ordering
    Given one order fully received 2 days after confirmation and one still waiting
    Then the average is 2 days and the waiting order is reported, not averaged

  Scenario: Get deliveries on time and reduce delays
    Given three deliveries due in the week: one complete on time, one complete 2 days late,
      and one half on time and completed 2 days late
    Then 1 of 3 deliveries were on time (33.3%)
    And the average delay of completed deliveries is 1.33 days

  Scenario: Keep overdue supplier bills under control
    Given a 1,000 bill due before the week ends, 400 paid during the week and the rest after it
    And a draft bill
    Then 600 was overdue and unpaid on the last day of the week and the draft is not counted
    And the goal can only be set for the whole company

  Scenario: Records are reused and linked
    When two goals use the same option
    Then they share one approved measure, each with its own target and Key Result
    And the final evaluation is linked to the goal's Key Result
