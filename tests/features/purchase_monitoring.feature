Feature: Purchasing monitoring (Phase 1)
  As a café-chain owner
  I want to see what we committed to suppliers, what arrived, what is still open or late, and what we pay per unit
  So that I can follow purchasing without setting a target

  # Implemented by tests/test_purchase_monitoring.py. The café is in Asia/Kathmandu (UTC+5:45); week 8-14 Jan 2001.
  # Dates: order value, quantities ordered and prices by confirmation date; quantities received by receipt
  # completion date; awaiting delivery and overdue as the position at the end of the period (or now).

  Scenario: Confirmed order value and its breakdowns
    Given PO1 20 kg at 500/kg less 10 % with 13 % tax, PO2 100 L of milk, PO3 in Branch 2, PO8 500 g ordered in grams
    And an RFQ, a cancelled order, an order confirmed after the week and last week's order
    When "Monitor purchasing" shows the confirmed order value for the week
    Then the value is 14,800 excluding tax, from 4 orders and 3 suppliers
    And suppliers show 62.8 %, 35.8 % and 1.4 %, categories and branches add up to the same total
    And quantities ordered are listed per unit, with the grams converted: 100 L · 500 Units · 30.5 kg
    And last week (1,920) gives +670.8 %
    And the goal has no target and cannot get one

  Scenario: Filters
    Then the roaster alone is 9,300, the Ingredients category (with its sub-categories) 14,700
    And beans ordered are 30.5 kg, with 15 kg received in the same week

  Scenario: Received quantities use completed receipts, net of returns
    Given 15 of 20 kg received and 3 kg sent back with Odoo's return flow
    Then 12 kg are received, 3 kg returned, from 1 receipt
    And the 3 kg are owed again exactly when Odoo's own received quantity says so

  Scenario: Awaiting delivery and overdue
    Then PO1 (5 kg left), PO3 and PO8 await delivery: 7,850; PO2, received in full, does not
    And only PO1 and PO8 are overdue (2,550), 4 and 5 days late; PO3 is open but not late

  Scenario: Purchase price per product
    Then beans cost 475.41 per kg: (9,000 + 5,200 + 300) / 30.5 kg, quantity-weighted, grams converted
    And Highland Roasters charged 520 per kg
    And the price shown is what was paid, not the supplier's price list
    And the price needs a product

  Scenario: Values that cannot be compared are refused
    Then a kg product ordered in litres is a data problem, not a 1:1 conversion
    And an order in a currency without an exchange rate is a data problem

  Scenario: Zero only when it is real
    Then a week without confirmed orders shows a value of zero
    And a week without purchases of the product shows no price, not a price of zero

  Scenario: Branch managers see their own branch only
    Then a branch manager sees Branch 1's 9,500 without a branch comparison
    And company-wide purchasing is not shown to them, and another branch's goal cannot be read
