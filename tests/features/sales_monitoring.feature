Feature: Goal types and configurable sales monitoring
  As a café-chain owner
  I want to see sales between two times of day, for any period, branch, product or category
  So that I understand evening (or morning) trade without asking for a new objective each time

  # Implemented by tests/test_sales_monitoring.py. The café is in Asia/Kathmandu (UTC+5:45).
  # Window rule: start time included, end time excluded, company time zone, every day of the period.

  Scenario: The window is applied in local time on every day, end time excluded
    Given orders at 17:30, 20:59, 21:00, 16:59 and midday on Monday, 18:00 on Tuesday and a refund on Wednesday
    And a cancelled order, a draft order and an order the day after the period
    When sales are measured for 17:00-21:00 from Monday to Sunday
    Then sales are 20.60 excluding tax and after discounts
    And there are 3 orders: a two-line order counts once and the refund is not an order
    And quantities stay apart per unit: 3 Units and 0.5 kg
    And the daily trend lists all 7 days, with the refund reducing Wednesday

  Scenario: Product, category and branch filters
    Then the latte alone sold 7.60 in 2 orders, 2 Units
    And the Drinks category includes its Hot drinks sub-category
    And Branch 2 sold 40.00 and the whole business 60.60, each order counted once

  Scenario: Missing data is never zero sales
    Then a branch without a Point of Sale is a data problem
    And a period without POS orders is empty, not zero
    And a window without orders while other hours had sales is a genuine zero

  Scenario: A monitoring goal needs no target and is never achieved or failed
    Given "Monitor sales in a time window" for the whole business, 17:00-21:00, 8-14 January
    Then it is ready without a target and cannot get one
    And it shows 60.60, both branches, the daily trend and +203.0% against the previous week
    And once approved its status is "Monitoring", with no progress bar

  Scenario: Monitoring settings are validated
    Then a window across midnight is refused with an explanation
    And average order value with a product filter is refused
    And a time that is not HH:MM is refused
    And average order value with no orders shows "No orders", not a division by zero

  Scenario: Rolling periods and empty states
    Given a muffin sale today
    Then "Today" for 00:00-24:00 shows 2 Units
    And a period without POS orders shows "No POS data"
    And a branch without a Point of Sale fails the data check

  Scenario: Branch managers see their own branch only
    Then a branch manager sees their branch's sales without a branch comparison
    And company-wide sales are not shown to them
    And they cannot read another branch's goal; employees cannot read goal setups

  Scenario: Optimization and compliance keep the existing rules
    Then an optimization goal still needs a target above zero
    And a compliance goal with no evidence is "Not evaluated", not a failed requirement
