Feature: Product quantity goal shows the right progress
  "Limit how much of a product we buy" counts the chosen product's quantity on purchase orders
  confirmed in the goal's period, in the product's base unit, for the goal's company and branch.
  Implemented by tests/test_purchase_quantity_goal.py (TestPurchaseQuantityGoal).

  Scenario: Only the selected product counts
    Given a Milk goal and confirmed orders for 20 L of Milk and 50 kg of Coffee Beans
    Then the goal measures 20 L; the coffee is not counted

  Scenario: Only purchases confirmed inside the period count
    Given Milk confirmed before, inside and after the period
    Then only the Milk confirmed inside the period counts

  Scenario: Unconfirmed orders and other branches do not count
    Given a Milk RFQ that is not confirmed and Milk ordered for another branch
    Then neither counts for a Branch 1 goal

  Scenario: Purchase units are converted to the base unit
    Given Milk bought as 5,000 mL
    Then it counts as 5 L

  Scenario: The period must end by the goal deadline
    Given a goal due on the 24th and a period ending on the 31st
    Then readiness explains the period must end by the deadline and no target can be prepared

  Scenario: Progress during an open period
    Given an approved 200 L Milk limit for the current week and 20 L confirmed this week
    When the owner opens the goal
    Then it shows 20 L so far, 10% of the limit used, "Within the limit so far", not a final result
    And readiness already shows the result so far for the goal's own period

  Scenario: Re-evaluating creates no duplicates
    When a closed period's goal is evaluated twice
    Then there is one evaluation, linked to the goal's Key Result

  Scenario: Zero is a result; a broken source is not
    Given no Milk orders in the period, the result is 0 L and valid
    Given the company has no timezone, the data source cannot bound the period
    Then the goal shows Data unavailable instead of 0
