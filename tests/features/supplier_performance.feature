Feature: Purchasing Phase 2 — supplier performance
  As a café-chain owner
  I want to see which suppliers deliver on time, how late deliveries are, and what each supplier charges
  So that I can judge suppliers on facts, one measure at a time

  # Implemented by tests/test_supplier_performance.py. Café time zone Asia/Kathmandu (UTC+5:45).
  # Promised day = the promised date recorded when the order was confirmed, as a local calendar day.
  # On-time rate and delay judge order lines due in the period once the due day has passed; price comparison uses
  # orders confirmed in the period.

  Background:
    Given Branch 1 orders due 8-14 January 2001:
      | supplier | quantity | due    | deliveries                    |
      | Fast     | 10 kg    | 9 Jan  | 10 kg on 8 Jan (1 day early)  |
      | Fast     | 10 kg    | 10 Jan | 10 kg on 10 Jan               |
      | Slow     | 100 kg   | 10 Jan | 60 kg on 9 Jan, 40 kg 13 Jan  |
      | Slow     | 20 kg    | 11 Jan | receipt cancelled, nothing    |
      | Slow     | 10 kg    | 12 Jan | 10 kg on 15 Jan               |
      | Fast     | 500 g    | 12 Jan | 0.5 kg on 12 Jan              |
    And a cancelled order, an RFQ and an order due on 20 January
    And Branch 2: 50 L of milk due 10 January, received 11 January

  Scenario: On-time rate is weighted by quantity, shipment by shipment
    Then Branch 1 delivered 80.5 of 150.5 kg on time: 53.5 %
    And the split order counts 60 of 100 kg on time, the undelivered 20 kg count as not on time and are shown apart
    And Fast is 100.0 %, Slow 46.2 %
    And for the whole business litres and kilograms are reported apart: "L: 0.0% · kg: 53.5%"

  Scenario: Average delay is weighted by quantity received
    Then Branch 1's average delay is +0.6 days: (-10 + 0 - 60 + 120 + 30 + 0) / 130.5 kg
    And Fast is -0.5 days (early), Slow +0.8 days (late)
    And a delivery on its promised day is "0.0 days (on time)", a genuine zero

  Scenario: Missing promised dates and returns
    Given a line confirmed before promised dates were recorded
    Then it is listed as not judged with its reason, and the rest is still measured
    And a later return does not change when goods arrived

  Scenario: Days follow the company time zone
    Then a promise at 21 Jan 19:00 UTC is due on 22 Jan locally, and a receipt at 22 Jan 18:45 UTC is one day late

  Scenario: No deliveries due is not 0 %
    Then a week with nothing due shows no rate and no delay, with an explanation
    And a line due but not yet received has no delay yet: "Nothing received yet"

  Scenario: Price comparison per supplier
    Given in 5-11 February: Fast 10 kg at 500 less 10 % and 1,000 g at 0.50/g, Slow 5 kg at 520,
      Import 4 kg at 1,000 BGX with 2 BGX = 1
    Then Fast paid 454.55/kg over 11 kg in 2 purchases (450.00-500.00), Import 500.00/kg (BGX), Slow 520.00/kg
    And tax, a cancelled order, an RFQ, the dark-roast variant and an order outside the week are not compared
    And the comparison says it is not a supplier ranking

  Scenario: Price comparisons that cannot be made are refused
    Then without a product the setup asks for one
    And a kg product ordered in litres, or an order in a currency without a rate, is a data problem
    And a zero quantity gives no price, not a price of zero

  Scenario: Branch managers and Phase 1
    Then a branch manager sees Branch 1's 53.5 % only; company-wide results are not shown to them
    And the six Phase 1 measures still answer from the same orders
