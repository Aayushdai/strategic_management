Feature: Progress, colours and ranking follow each goal's direction
  A larger percentage is not better performance. Limits show how much of the allowance is used;
  minimums show how much of the target is reached; rankings use the margin to the target.
  Implemented by tests/test_goal_progress_semantics.py.

  Rule: Usage colours: below 70% green, 70% to below 90% amber, 90% to 100% red, above 100% dark red.
  Rule: Attainment colours: 100% or more green, 90% to below 100% amber, below 90% red.
  Rule: Ranking margin = distance from the target edge as a % of the target (0 = at the edge, negative = failing).

  Scenario: A - Lower is better: 50 L of a 100 L limit
    Then usage is 50%, 50 L remains, the bar is green and the goal is "Within the limit so far"

  Scenario: B - Near the limit: 95 L
    Then usage is 95%, the bar is red, the status is "Near the limit so far"
    And it ranks below a goal at 20% usage

  Scenario: C - Exactly at the limit: 100 L
    Then usage is 100%, nothing remains, and the goal is still within the limit

  Scenario: D - Over the limit: 110 L
    Then usage is 110%, "10 L over the limit" is shown, the bar is full width and dark red, and it is not successful

  Scenario: E - Higher is better: on-time deliveries of at least 95%
    Given 80% on time, the bar shows attainment (84.2% of target), red, and the target is not reached
    Given 95% on time, attainment is 100%, green, exactly at the target

  Scenario: F - Dashboard and detail agree
    Then the dashboard bar and the detail panel show the same 50% usage

  Scenario: G - Ranking by business performance
    Given goals at 20%, 95% and 110% usage
    Then the highest-performance order is 20%, 95%, 110%

  Scenario: H - Missing data is not ranked
    Then a goal without a valid result has no bar or margin and is listed under setup

  Scenario: I - Open period is provisional
    Then the status says "so far" and the result is not final

  Scenario: J - Missed target after the period
    Then a final over-limit result is a missed target with a dark red bar

  Scenario: Price rules
    Given a required 5% price cut, a 3% cut is 60% attainment (red) and a price rise is negative attainment shown as an empty bar
    Given a 5% allowed rise, a 3% rise uses 60% of it (green) and a 6% rise exceeds it (dark red)

  Scenario: Colour band boundaries are exact
    Then 69.9% is green, 70% amber, 89.9% amber, 90% red, 100% red and 100.1% dark red
    And 89.9% attainment is red, 90% amber, 99.9% amber and 100% green
