Feature: Purchasing, Inventory and POS as one data chain
  As head office of a café chain
  I want goals and KPIs to read the same purchase, stock and POS records
  So that ordered, received, sold, used, wasted and paid figures agree and are never mixed up

  Background:
    Given a branch with its own POS, real-time POS stock deduction, and coffee beans in kg costed at 200 per kg
    And a latte is a POS kit whose approved recipe uses 18 g of coffee

  Scenario: Purchase order, partial receipt and stock
    Given 10 kg of coffee in stock before the week
    When 20 kg are ordered at 200 per kg and only 15 kg are received in the week
    Then the ordered quantity is 20 kg and the received quantity is 15 kg
    And opening stock is 10 kg and closing stock is 25 kg
    And the purchase commitment is 4,000 and the purchase price is 200 per kg
    And the stock-flow check finds no unexplained difference

  Scenario: A receipt that is not validated
    When 20 kg are ordered but nothing is received
    Then the ordered quantity is 20 kg, there is no received quantity (not zero), and stock stays at 10 kg

  Scenario: POS sales, recipe use, waste and stock flow
    Given 10 kg of coffee in stock
    When 100 lattes are sold and paid, an order of 50 lattes is cancelled and 0.5 kg of coffee is spoiled
    Then Odoo itself deducts 1.8 kg of coffee through the POS kit
    And the expected recipe use is 1.8 kg (100 x 18 g, grams converted to kg; the cancelled order excluded)
    And waste is 0.5 kg, separate from recipe use, and closing stock is 7.7 kg
    And the stock-flow check explains the POS deduction and finds no unexplained difference
    And production consumption stays "not evaluated" because no manufacturing order used coffee

  Scenario: Missing recipes and missing history
    When only products without a recipe are sold
    Then there is no recipe estimate rather than a zero
    And a week without sales has no sold quantity rather than zero
