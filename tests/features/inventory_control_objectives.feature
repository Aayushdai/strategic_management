Feature: Inventory-control objectives measured from real Odoo data
  As a café-chain owner
  I want to know whether recipes exist, stock counts happen, waste is recorded and ingredient use adds up
  So that I can find operational problems instead of guessing

  # Implemented by tests/test_inventory_control_objectives.py

  Scenario: Recipe coverage counts each sold menu item that needs a recipe once
    Given Branch 1 sold lattes with a recipe, cappuccinos without one, mochas whose recipe changed after the sale
    And bottled water, which is not marked "Needs a recipe"
    And Branch 2 sold only lattes
    When recipe coverage is measured per branch
    Then Branch 1 shows 33.3% (1 of 3 items) and names the cappuccino and the mocha
    And the bottled water is not counted
    And Branch 2 shows 100%

  Scenario: No recipe items sold
    Given only bottled water was sold in the period
    Then recipe coverage is not evaluated instead of 0% or 100%

  Scenario: Planned counts are done only when applied in Inventory on time
    Given Branch 1 plans a count that was applied on time
    And a count whose quantities were entered but never applied
    And a count applied only after its count-by date
    And a plan cancelled before its date and a plan cancelled after its date
    When planned count completion is measured for Branch 1
    Then it is 25% (1 of 4): the plan cancelled in time is left out, the late cancellation counts as missed
    And a Branch 1 count does not complete a Branch 2 plan, so Branch 2 shows 50%

  Scenario: Periods without due plans are not scored
    Given no counts were planned in the period
    Then planned count completion is not evaluated
    And a plan not yet due shows "Not due yet" and can still be changed

  Scenario: Past plans and branch access
    Given a plan past its count-by date
    Then it cannot be changed or deleted
    And a branch manager reads only their own branch's plans and cannot create plans

  Scenario: Stock counts reveal unrecorded loss and unexplained ingredient use
    Given 10 kg of coffee counted on Monday, 5 kg received and 100 lattes sold at 18 g each
    And 0.5 kg recorded as spoiled, and a Friday count 0.5 kg short
    When waste recording and ingredient use are measured
    Then waste recording is 50% (0.5 kg of 1 kg identified loss was recorded)
    And the counts show 2.3 kg used against 1.8 kg explained by sales and recipes: a difference of +0.5 kg
    And Branch 2, which has no counts, is not evaluated

  Scenario: Waste without an approved reason is not properly recorded
    Given 0.3 kg spoiled with a reason, 0.2 kg scrapped without one and 0.5 kg found missing at a count
    Then waste recording is 30%

  Scenario: Nothing is guessed without evidence
    Given waste was recorded but no stock count was applied
    Then waste recording is not evaluated
    And ingredient use with a count on only one day is not evaluated
    And ingredient use while a menu item that needs a recipe was sold without one is a data-quality issue

  Scenario: The planned-count goal is measured through Business Goals
    Given two counts due in the week across branches and one applied on time
    When the owner sets "Complete planned stock counts" with a target of at least 80%
    Then the setup check shows 50.0% and the goal result is 50%, not achieved
