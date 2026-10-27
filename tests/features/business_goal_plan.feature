Feature: Business goals belong to a strategic plan
  Every goal is a strategic objective, and every objective belongs to a strategic plan
  (its company comes from the plan). A draft therefore needs a plan that covers its deadline;
  the plan is checked again before the goal goes for approval.

  Implemented by tests/test_business_goals.py (TestBusinessGoals, test_plan_* tests).

  Scenario: A - Draft creation without a covering plan
    Given an owner enters a valid goal but no active plan covers its deadline
    When they save the draft
    Then no goal is created and nothing is activated
    And the message says the goal was not saved, until when the current plan runs, and where Head Office fixes it

  Scenario: B - Approval when the plan stops covering the goal
    Given a measured draft goal whose plan is later archived or shortened before the deadline
    When it is submitted, or approved after submission
    Then the step is refused and the goal keeps its current state

  Scenario: C - Valid plan
    Given an active plan covers the deadline
    When the goal is saved and goes through the normal workflow
    Then it is linked to that plan

  Scenario: D - Invalid chosen plan
    Given a plan that is archived or whose dates do not include the deadline
    When the goal is saved with that plan
    Then it is rejected with a clear explanation

  Scenario: E - Plain-language guidance
    Given an owner unfamiliar with strategic planning
    When a plan problem stops them
    Then the message uses business words, names the date that works, and points to Strategy & KPIs > Strategy > Strategic Plans
    And the form shows the latest allowed deadline before saving
