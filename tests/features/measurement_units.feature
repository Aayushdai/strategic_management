Feature: Quantity, price per unit and total money are separate measurements
  As a head-office manager
  I choose whether a goal measures a quantity, a price per unit or a total amount of money
  And that choice is kept through the data, the target, the evaluation and the dashboard

  Background:
    Given coffee beans are stocked in kg with a cost of 200 per kg and a sales price of 300 per kg

  Scenario: Purchase price per unit
    Given 10 kg bought at 200 per kg and 1,000 g bought at 0.20 per g in the week
    When "Track purchase price per unit" with a maximum of 220 per kg is evaluated after the week
    Then the price is 200/kg (grams converted to kg), shown as currency per kg against "At most 220/kg"
    And 90.9% of the price limit is used and the goal is achieved

  Scenario: Purchase price and selling price are different measurements
    Given 2 kg sold for 600, 1 kg sold for 300 and 1 kg refunded for 300 in the week
    Then "Track selling price per unit" shows 300/kg and "Track purchase price per unit" shows 200/kg
    And they use different calculations and different KPIs
    And a week without purchases has no purchase price rather than zero

  Scenario: Stock value and stock quantity
    Given 10 kg received in the week and 5 kg after it
    Then "Track stock value" shows 2,000 in currency from Odoo's valuation (not 3,000 at the sales price), 80% of a 2,500 limit
    And "Track stock quantity" shows 10 kg against "At least 8 kg"
    And stock of a product without cost fails the readiness check instead of showing a zero value

  Scenario: Units must match the kind of measurement
    Then a price per unit must use "<currency>/<product unit>", stock value the currency and stock quantity the product's unit
    And grams convert to kilograms, but litres are never converted to kilograms
