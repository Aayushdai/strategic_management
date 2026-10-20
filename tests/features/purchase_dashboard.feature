Feature: Purchase activity is reflected in KPI dashboards

  Rule: Confirmed purchase quantities and completed receipts are different measures.

  Scenario: A confirmed 20 kg coffee order is reflected on organization and branch dashboards
    Given an approved organization KPI target for ordered coffee-bean quantity
    And an approved Branch A KPI target for ordered coffee-bean quantity
    And coffee beans use kilograms as their base unit
    When a buyer confirms a purchase order for 20 kg of coffee beans for Branch A
    Then the organization-level ordered quantity is refreshed to 20 kg
    And Branch A's ordered quantity is refreshed to 20 kg
    And the dashboard shows the actual quantity and unit, not only attainment percentage
    And no manual "Evaluate from Operational Source" action is required

  Scenario: Branch-level purchase results respect role boundaries
    Given a valid Branch A purchase KPI evaluation
    When the Head Office Manager opens the organization dashboard
    Then the Head Office Manager can see the organization and authorized branch results
    When the Branch A Manager opens Branch Performance
    Then the manager can see Branch A's result
    And the manager cannot see Branch B's restricted result
    When an Employee opens My KPI Dashboard
    Then the employee cannot see branch-wide or organization-wide purchase aggregates
    And the employee can continue to see only their own authorized KPI evaluations

  Scenario: Purchase ordered and supplier received quantities are not conflated
    Given a confirmed purchase order for 20 kg of coffee beans
    And the supplier receipt has not yet been validated
    Then the ordered-quantity KPI can report 20 kg
    And the received-quantity KPI must not report 20 kg before a completed inbound stock move exists
    When the incoming receipt is validated for 20 kg
    Then the received-quantity KPI can report 20 kg
