Feature: Inventory monitoring, first release (current stock)
  As a café-chain owner
  I want to see the stock each branch has now, what is free to use, what is expected, and what needs attention
  So that I can trust the stock figures without being shown unsupported history, value, waste or recipe figures

  # Implemented by tests/test_inventory_monitoring.py. All six measures are a current snapshot (no reporting period).

  Background:
    Given Branch 1 holds 20 kg of beans and 10 L of lot-tracked milk, and no cups at all
    And Branch 2 holds 5 kg of beans and -3 L of milk

  Scenario: On hand is a current snapshot per branch and unit
    When the owner monitors current stock for the whole business
    Then it is labelled "Current snapshot" and not a balance for a past period
    And cups without any stock record are listed at 0 Units, not dropped
    And totals stay per unit: 7 L · 0 Units · 25 kg, and Branch 1 alone 10 L · 0 Units · 20 kg
    And the negative milk balance is marked "(negative)"

  Scenario: Available and forecast
    Given a confirmed delivery reserves 4 kg of beans and a pending receipt brings 10 kg
    Then available beans are 20 on hand, 4 reserved, 16 available
    And forecast beans are 20 on hand + 10 incoming - 4 outgoing = 26 kg, explained as not a demand forecast

  Scenario: Below minimum uses reordering rules only
    Given rules: beans minimum 30, milk minimum 5, cups without a rule
    Then only beans are below minimum: forecast 26 kg, shortfall 4 kg
    And 2 rules were checked and 1 product without a rule is not judged
    And a second rule for the same product and location is refused by Odoo
    And with no rule at all the result says nothing can be judged, not that stock is sufficient

  Scenario: Negative stock is flagged, not corrected
    Then Branch 2 milk at -3 L is listed for investigation, without naming a cause
    And the stock is unchanged

  Scenario: Unfinished POS deliveries are surfaced, not repaired
    Given a POS sale of 4 L of lot-tracked milk without a lot number
    Then its unfinished delivery is listed with Odoo's reserved quantity, 4 L still to take out, its state,
      "lot number missing" and the POS order
    And available and forecast stock carry a warning that matches what Odoo actually reserved
    And the delivery is left exactly as it was

  Scenario: Filters, units and empty data
    Then a product category filter includes only its products
    And an empty selection is "Nothing to show", not zero
    And a renamed "Units" unit is warned about

  Scenario: Branch permissions
    Then a branch manager sees only their branch and no branch comparison
    And company-wide stock is not shown to them, another branch's goal cannot be read,
      and a branch sent by the client is replaced by their own branch
    And Head Office compares all branches
