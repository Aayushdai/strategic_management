"""Purchasing goal templates: what a café owner can choose, mapped to a measure and an acceptance rule.

Rules (all map onto the existing inclusive range target, so results are Achieved / Not achieved, never "Almost Met"):
  ceiling      inclusive maximum: 0 .. X               (budget, quantity, days, share, value awaiting delivery)
  floor        inclusive minimum: X .. 100 %           (on-time and full-quantity delivery)
  reduce_by    price falls by at least X %: -100 .. -X
  rise_at_most price rises by no more than X %: -100 .. X
  tolerance    a signed difference stays within plus or minus X: -X .. X
kind decides how values are shown: money, quantity (product unit), percent, change (+/- %), days.
status: available = implemented and tested; setup = needs a configuration change first; unsupported = not built yet.
goal_type: optimization (default; a target to reach), compliance (a rule or required process is followed) or
monitoring (values and trends, no target, never achieved or failed).
Apps are ids from the App Dependencies page (models/app_dependencies.py).
"""

GOAL_TYPES = [("monitoring", "Monitoring"), ("optimization", "Optimization"), ("compliance", "Compliance")]

_PURCHASE = ["purchasing"]
_PURCHASE_STOCK = ["purchasing", "inventory"]

TEMPLATES = [
    {
        # Monitoring: one configurable option; what to show, the period and the filters are settings.
        "key": "purchase_monitor", "group": "Monitor purchasing", "title": "Monitor purchasing",
        "description": "Confirmed order value, quantities ordered or received, orders awaiting delivery, overdue orders or the "
                       "price paid per product, by supplier, category, product and branch. No target.",
        "goal_type": "monitoring", "kind": "monitor", "monitor_source": "purchase", "measure": "purchase_mon_value",
        "metrics": ["purchase_mon_value", "purchase_mon_ordered", "purchase_mon_received", "purchase_mon_outstanding",
                    "purchase_mon_overdue", "purchase_mon_unit_price", "purchase_mon_on_time", "purchase_mon_delay",
                    "purchase_mon_price_comparison"],
        "apps": _PURCHASE_STOCK, "status": "available",
        "how": "Confirmed purchase orders only (RFQs and cancelled orders are not commitments), dated by confirmation; "
               "amounts exclude tax, after discounts, in your currency (other currencies are converted at the confirmation "
               "date, or the goal says the rate is missing). This is what was committed to suppliers, not money paid or "
               "booked as an expense. Quantities received come from completed receipts, dated when they were received, "
               "minus goods returned to the supplier. Orders awaiting delivery and overdue orders are the position at the "
               "end of the period (or now). Branch = the warehouse the order is received into. Supplier delivery "
               "performance judges each order line on the promised date recorded when the order was confirmed, by "
               "calendar day, shipment by shipment; price comparison lists the price actually paid per supplier.",
    },
    # ---------------------------------------------------------------- A. Spending and purchase quantities
    {
        "key": "budget", "group": "Spending and quantities", "title": "Keep purchasing within budget",
        "description": "Confirmed purchase orders in the period stay within a budget.",
        "measure": "purchase_confirmed_amount", "rule": "ceiling", "kind": "money", "target_label": "Budget limit",
        "apps": _PURCHASE, "status": "available",
        "how": "Adds up purchase orders confirmed in the period, excluding tax, in company currency. Draft quotations "
               "do not count, and receiving the goods later does not count an order twice.",
    },
    {
        "key": "unit_purchase_price", "group": "Spending and quantities", "title": "Track purchase price per unit",
        "description": "The average price paid per unit of a product stays at or below a maximum.",
        "measure": "purchase_average_unit_price", "rule": "ceiling", "kind": "unit_price",
        "target_label": "Highest price allowed per unit", "apps": ["purchasing"], "status": "available",
        "how": "Confirmed purchase amounts excluding tax divided by the quantity ordered, in the product's unit; order "
               "lines in other compatible units are converted. This is the purchase cost, never the selling price.",
    },
    {
        "key": "price_reduction", "group": "Spending and quantities", "title": "Reduce the price we pay",
        "description": "The average price paid for one product falls by at least a chosen percentage.",
        "measure": "purchase_unit_price_change_pct", "rule": "reduce_by", "kind": "change",
        "target_label": "Reduce the price by at least (%)", "apps": _PURCHASE, "status": "available",
        "how": "Compares the product's average unit price (excluding tax, per its base unit, weighted by quantity) in "
               "this period with the period of the same length just before it. No purchases before means no comparison.",
    },
    {
        "key": "quantity_limit", "group": "Spending and quantities", "title": "Limit how much of a product we buy",
        "description": "The quantity ordered of one product stays within a limit.",
        "measure": "purchase_product_quantity_limit", "rule": "ceiling", "kind": "quantity",
        "target_label": "Maximum quantity", "apps": _PURCHASE, "status": "available",
        "how": "Adds up the product's quantity on purchase orders confirmed in the period, converted to its base unit.",
    },
    # ---------------------------------------------------------------- B. Supplier delivery performance
    {
        "key": "on_time_deliveries", "group": "Supplier deliveries", "title": "Get deliveries on time",
        "description": "A minimum share of deliveries arrive in full by the promised date.",
        "measure": "purchase_on_time_delivery_count_pct", "rule": "floor", "kind": "percent",
        "target_label": "At least (% of deliveries)", "apps": _PURCHASE_STOCK, "status": "available",
        "how": "A delivery counts as on time when its full quantity arrives by the date promised when the order was "
               "confirmed. Deliveries not yet due are not counted; a partly on-time delivery counts as late.",
    },
    {
        "key": "full_quantity", "group": "Supplier deliveries", "title": "Receive the full quantity ordered",
        "description": "A minimum share of the ordered quantity arrives by the promised date.",
        "measure": "supplier_on_time_delivery_pct", "rule": "floor", "kind": "percent",
        "target_label": "At least (% of quantity)", "apps": _PURCHASE_STOCK, "status": "available",
        "how": "Quantity received by the promised date divided by the quantity due in the period. A partial delivery "
               "counts for the part that arrived on time.",
    },
    {
        "key": "delivery_delay", "group": "Supplier deliveries", "title": "Reduce delivery delays",
        "description": "Completed deliveries are, on average, no more than a set number of days late.",
        "measure": "purchase_average_delivery_delay_days", "rule": "ceiling", "kind": "days",
        "target_label": "At most (days late on average)", "apps": _PURCHASE_STOCK, "status": "available",
        "how": "Average days late when a delivery became complete (early counts as 0). Only completed deliveries are "
               "averaged; outstanding ones are reported separately.",
    },
    {
        "key": "awaiting_delivery", "group": "Supplier deliveries", "title": "Track orders still waiting for delivery",
        "description": "The value of goods ordered but not yet received stays below a limit.",
        "measure": "purchase_outstanding_amount", "rule": "ceiling", "kind": "money",
        "target_label": "Keep below", "apps": _PURCHASE_STOCK, "status": "available",
        "how": "Value (excluding tax) of the not-yet-received part of confirmed orders at the end of the period, or "
               "today if earlier, using receipts recorded by that moment.",
    },
    {
        "key": "receipt_lead_time", "group": "Supplier deliveries", "title": "Receive goods faster after ordering",
        "description": "Orders are fully received within a set number of days of confirmation, on average.",
        "measure": "purchase_average_receipt_lead_days", "rule": "ceiling", "kind": "days",
        "target_label": "At most (days on average)", "apps": _PURCHASE_STOCK, "status": "available",
        "how": "Average days from order confirmation to the receipt that completes the order, for orders confirmed in "
               "the period and fully received. Orders still waiting are reported, not averaged.",
    },
    # ---------------------------------------------------------------- C. Purchasing workflow speed
    {
        "key": "approval_speed", "group": "Purchasing workflow", "title": "Approve purchase orders faster",
        "description": "Time from an approval request to the approval.",
        "apps": _PURCHASE, "status": "setup",
        "needs": "Purchase orders in your company are confirmed in one step (Purchase > Configuration > Settings > "
                 "Order Approval is off), so there is no approval request to time. Odoo records each order status change, "
                 "but the calculation from that history is also not built yet.",
    },
    {
        "key": "quotation_speed", "group": "Purchasing workflow", "title": "Make quotation decisions faster",
        "description": "Time from sending a request for quotation to confirming or cancelling it.",
        "apps": _PURCHASE, "status": "unsupported",
        "needs": "Odoo records when a quotation is sent and when it is confirmed or cancelled, but the calculation "
                 "that turns that history into decision times is not built yet.",
    },
    # ---------------------------------------------------------------- D. Supplier prices and dependence
    {
        "key": "supplier_price_comparison", "group": "Supplier prices", "title": "Compare supplier prices",
        "description": "Unit prices for the same product from different suppliers, side by side.",
        "apps": _PURCHASE, "status": "unsupported",
        "needs": "This is a side-by-side comparison rather than a single goal number, and it is not built yet. For one "
                 "product, use 'Reduce the price we pay' or 'Alert me when prices rise'.",
    },
    {
        "key": "price_rise_alert", "group": "Supplier prices", "title": "Alert me when prices rise",
        "description": "The price of one product rises by no more than a chosen percentage.",
        "measure": "purchase_unit_price_change_pct", "rule": "rise_at_most", "kind": "change",
        "target_label": "Alert if the price rises more than (%)", "apps": _PURCHASE, "status": "available",
        "how": "Compares the product's average unit price in this period with the period of the same length just "
               "before it. No purchases before means no comparison, not a 0% change.",
    },
    {
        "key": "supplier_concentration", "group": "Supplier prices", "title": "Avoid relying on one supplier",
        "description": "No single supplier takes more than a set share of purchasing.",
        "measure": "purchase_top_supplier_share_pct", "rule": "ceiling", "kind": "percent",
        "target_label": "Largest supplier at most (% of purchasing)", "apps": _PURCHASE, "status": "available",
        "how": "The largest supplier's share of confirmed purchasing in the period. It measures concentration only, "
               "not supplier quality or reliability.",
    },
    # ---------------------------------------------------------------- E. Stock availability and replenishment
    {
        "key": "stockouts", "group": "Stock availability", "title": "Prevent important items from running out",
        "description": "Count separate stock-outs of critical products.",
        "apps": _PURCHASE_STOCK, "status": "unsupported",
        "needs": "Needs stock levels over time to find when a product reached zero and when it came back. Odoo keeps "
                 "the stock moves, but the calculation that rebuilds that history and counts separate stock-outs is not built yet.",
    },
    {
        "key": "in_stock_time", "group": "Stock availability", "title": "Keep critical products in stock",
        "description": "Share of time critical products were available.",
        "apps": _PURCHASE_STOCK, "status": "unsupported",
        "needs": "Availability over time needs stock history, not today's balance. The history calculation is not built yet.",
    },
    {
        "key": "restock_speed", "group": "Stock availability", "title": "Restock critical products faster",
        "description": "Time from a low-stock alert until the stock is replenished.",
        "apps": _PURCHASE_STOCK, "status": "setup",
        "needs": "Needs the moment each low-stock alert started; Odoo's reorder rules do not record it, so low-stock "
                 "event tracking must be added first. To measure order-to-receipt time today, use 'Receive goods faster after ordering'.",
    },
    # ---------------------------------------------------------------- F. Supplier bills and payments
    {
        "key": "overdue_bills", "group": "Supplier bills", "title": "Keep overdue supplier bills under control",
        "description": "The unpaid amount of supplier bills past their due date stays below a limit.",
        "measure": "accounting_overdue_supplier_bills_amount", "rule": "ceiling", "kind": "money",
        "target_label": "Keep overdue bills below", "apps": ["accounting"], "status": "available", "company_only": True,
        "how": "Posted supplier bills past their due date and still unpaid on the last day of the period (or today), "
               "counting payments recorded by that date. Draft and fully paid bills are not included. Company-wide only.",
    },
    {
        "key": "order_bill_differences", "group": "Supplier bills", "title": "Find differences between orders and bills",
        "description": "Differences between what was ordered and what the supplier billed.",
        "apps": ["purchasing", "accounting"], "status": "unsupported",
        "needs": "Needs each bill line matched to its purchase order line with the same tax treatment, so legitimate "
                 "tax and rounding differences are not flagged. This matching is not built yet.",
    },
    {
        "key": "bills_paid_on_time", "group": "Supplier bills", "title": "Pay supplier bills on time",
        "description": "A minimum share of supplier bills paid in full by their due date.",
        "apps": ["accounting"], "status": "unsupported",
        "needs": "Needs the date each bill was fully settled from payment reconciliations, with rules for partial "
                 "payments and credit notes. This calculation is not built yet.",
    },
]
for _template in TEMPLATES:
    _template.setdefault("area", "purchasing")

_INVENTORY = ["inventory"]
# Inventory & Waste options (from inventory_waste_objective_okr_kpi.patch, without its illustrative figures).
# Options with a working, tested calculation are "available"; the rest say exactly what is missing.
INVENTORY_TEMPLATES = [
    {
        # Monitoring: Odoo's current stock (a snapshot, never a past period's balance); measure and filters are settings.
        "key": "inventory_monitor", "group": "Monitor inventory", "title": "Monitor inventory",
        "description": "Current stock per branch and product: on hand, available, forecast, below minimum, negative stock, "
                       "and stock held by unfinished POS deliveries. No target.",
        "goal_type": "monitoring", "kind": "monitor", "monitor_source": "inventory", "measure": "inv_mon_on_hand",
        "metrics": ["inv_mon_on_hand", "inv_mon_available", "inv_mon_forecast", "inv_mon_below_minimum", "inv_mon_negative",
                    "inv_mon_pos_reserved"],
        "apps": _INVENTORY, "status": "available",
        "how": "Odoo's own stock now, for each branch's stock locations and each product in its own unit (kilograms, litres "
               "and pieces are never added together). Available = on hand minus reserved; forecast = on hand plus incoming "
               "minus outgoing pending moves, with no date horizon. Below minimum uses your reordering rules only. Negative "
               "stock and unfinished POS deliveries are shown for investigation and never corrected here. Stock value, "
               "waste cost, count differences and recipe-based use are not part of this option.",
    },
    {
        "key": "waste_quantity", "group": "Waste", "title": "Track recorded waste quantity",
        "description": "Measure how much stock is being wasted.",
        "measure": "inventory_validated_waste_quantity", "rule": "ceiling", "kind": "quantity",
        "target_label": "Most waste allowed", "apps": _INVENTORY, "status": "available",
        "how": "Adds up the product's validated scrap with reason Spoilage/Expiry or Damaged Goods, in its base unit. "
               "Scrap without one of these reasons stops the measurement instead of being counted or skipped.",
    },
    {
        "key": "waste_cost_reduction", "group": "Waste", "title": "Reduce recorded waste cost",
        "description": "Measure the inventory cost of stock being wasted.",
        "measure": "inventory_recorded_waste_cost", "rule": "ceiling", "kind": "money",
        "target_label": "Most waste cost allowed", "apps": ["inventory", "valuation"], "status": "available",
        "how": "Adds up the inventory cost Odoo's stock valuation recorded for each validated waste of the product "
               "(Spoilage/Expiry or Damaged Goods), in your currency. It never uses the sales price. Needs Inventory "
               "Valuation and a cost on the product; otherwise the goal says so instead of showing a cost. The waste "
               "quantity can still be tracked with 'Track recorded waste quantity'.",
    },
    {
        "key": "waste_to_sales", "group": "Waste", "title": "Keep waste cost small compared with sales",
        "description": "Waste cost as a percentage of net sales stays below a limit.",
        "measure": "inventory_waste_cost_to_sales_pct", "rule": "ceiling", "kind": "percent",
        "target_label": "Waste cost at most (% of net sales)", "apps": ["inventory", "valuation", "pos"], "status": "available",
        "how": "Waste cost divided by net POS sales excluding tax, for the same period and the same branch (or the whole "
               "company). A period without sales has no result rather than 0%.",
    },
    {
        "key": "branch_waste_rate", "group": "Waste", "title": "Improve a branch's waste rate",
        "description": "One branch's waste cost as a percentage of its own sales falls below a limit.",
        "measure": "inventory_waste_cost_to_sales_pct", "rule": "ceiling", "kind": "percent", "branch_only": True,
        "target_label": "Branch waste cost at most (% of its net sales)", "apps": ["inventory", "valuation", "pos"],
        "status": "available",
        "how": "Uses waste as a share of the branch's own sales, so small and large branches are compared fairly. Pick the "
               "branch with the highest rate (Branches page) and set its limit below today's rate.",
    },
    {
        "key": "excess_stock", "group": "Stock levels", "title": "Reduce excess stock (quantity)",
        "description": "Stock of one product at the end of the period stays at or below an approved maximum.",
        "measure": "inventory_closing_stock_quantity", "rule": "ceiling", "kind": "quantity",
        "target_label": "Maximum stock at the end of the period", "apps": _INVENTORY, "status": "available",
        "how": "Closing stock is rebuilt from today's on-hand quantity and all completed moves since the period ended; it "
               "is measured once the period is over. For money, use 'Track stock value'.",
    },
    {
        "key": "minimum_stock", "group": "Stock levels", "title": "Track stock quantity",
        "description": "Stock of one product at the end of the period stays at or above a minimum quantity.",
        "measure": "inventory_closing_stock_quantity", "rule": "at_least", "kind": "quantity",
        "target_label": "Minimum stock at the end of the period", "apps": _INVENTORY, "status": "available",
        "how": "Closing stock in the product's unit, rebuilt from on-hand quantity and completed moves; measured once "
               "the period is over.",
    },
    {
        "key": "stock_value", "group": "Stock levels", "title": "Track stock value",
        "description": "The value of one product's stock at the end of the period stays at or below a limit.",
        "measure": "inventory_closing_stock_value", "rule": "ceiling", "kind": "money",
        "target_label": "Most stock value at the end of the period", "apps": ["inventory", "valuation"], "status": "available",
        "how": "Odoo's inventory valuation of the product on the last day of the period, in your currency (never the "
               "sales price). For a branch, Odoo values the branch's share of the quantity. A product with stock but "
               "no cost gives no result instead of zero.",
    },
    {
        "key": "stock_coverage", "group": "Stock levels", "title": "Keep stock coverage within a sensible range",
        "description": "Days of stock, based on recent usage, stay inside a product-specific range.",
        "apps": ["inventory", "pos"], "status": "unsupported",
        "needs": "Needs one agreed definition of daily usage. Cafés that deduct ingredients through POS recipes and those "
                 "that use manufacturing orders record usage differently, and a range needs two limits. Not built yet.",
    },
    {
        "key": "count_accuracy", "group": "Inventory accuracy", "title": "Improve the accuracy of stock counts",
        "description": "Physical counts of one product differ from the system by no more than a limit.",
        "measure": "inventory_count_difference_quantity", "rule": "ceiling", "kind": "quantity",
        "target_label": "Most total count difference allowed", "apps": _INVENTORY, "status": "available",
        "how": "Adds up the differences between counted and system quantity from stock counts applied in Odoo (Inventory > "
               "Physical Inventory). A count with no difference counts as accurate; a period without counts has no result.",
    },
    {
        "key": "stock_flow_variance", "group": "Inventory accuracy", "title": "Reduce unexplained stock differences",
        "description": "The difference between system stock and recorded movements stays within a tolerance for one product.",
        "measure": "inventory_unexplained_variance_quantity", "rule": "tolerance", "kind": "quantity",
        "target_label": "Allowed difference (plus or minus)", "apps": _INVENTORY, "status": "available",
        "how": "For the chosen product: closing stock minus what opening stock, receipts, consumption, transfers, adjustments "
               "and recorded waste explain. This is a stock-flow check, not a physical count. Measured after the period ends.",
    },
    {
        "key": "count_completion", "group": "Inventory accuracy", "title": "Complete planned stock counts",
        "description": "Planned stock counts are done on time.",
        "measure": "inventory_count_plan_completion_pct", "rule": "floor", "kind": "percent", "goal_type": "compliance",
        "target_label": "At least (% of planned counts)", "apps": _INVENTORY, "status": "available",
        "how": "Plan counts under Strategic Management > Strategy & KPIs > Planned Stock Counts (branch, products, date). A "
               "count is done only when every planned product has a count applied in Inventory > Physical Inventory between "
               "the planned date and its count-by date; entering quantities without applying them is not a count. Counts not "
               "yet due and plans cancelled before their date are left out; a plan cancelled later counts as missed. A "
               "period with no count due has no result.",
    },
    {
        "key": "consumption_variance", "group": "Ingredient use", "title": "Reduce unexplained ingredient-use differences",
        "description": "What stock counts show was used stays close to what sales and recipes expect.",
        "measure": "inventory_ingredient_use_difference_quantity", "rule": "tolerance", "kind": "quantity",
        "target_label": "Allowed difference (plus or minus)", "apps": ["inventory", "pos", "recipes"], "status": "available",
        "how": "For the chosen ingredient, between the first and last days it was counted in the period: use shown by the "
               "stock counts (opening count + receipts + transfers in - transfers out - recorded waste - closing count) "
               "minus the use expected from POS sales and the recipe in effect on each sale date. The stock moves POS "
               "recipes create are not the evidence; the counts are. Needs counts on two different days covering all the "
               "branch's locations of the ingredient. Menu items sold without a valid recipe, manufacturing orders or "
               "uncompleted POS deliveries stop the calculation instead of guessing. A difference is a reason to "
               "investigate, not proof of waste or theft.",
    },
    {
        "key": "recipe_coverage", "group": "Ingredient use", "title": "Ensure key menu items have approved recipes",
        "description": "Menu items that need a recipe had a valid recipe every time they were sold.",
        "measure": "inventory_recipe_coverage_pct", "rule": "floor", "kind": "percent", "goal_type": "compliance",
        "target_label": "At least (% of menu items sold)", "apps": ["pos", "recipes"], "status": "available",
        "how": "Of the menu items marked 'Needs a recipe' (Point of Sale > Products, Point of Sale tab) and sold in the "
               "period, the share that had an active recipe (Bill of Materials) with ingredients on every sale date. Each "
               "item counts once, whatever the quantity sold. Only Manufacturing administrators can create or change "
               "recipes, so an active recipe is the approved one. Items sold without a valid recipe are named in the result.",
    },
    {
        "key": "waste_record_completeness", "group": "Ingredient use", "title": "Record required waste consistently",
        "description": "Stock losses are recorded as waste with a reason, not discovered later at a stock count.",
        "measure": "inventory_waste_recording_pct", "rule": "floor", "kind": "percent", "goal_type": "compliance",
        "target_label": "At least (% of losses recorded)", "apps": _INVENTORY, "status": "available",
        "how": "For the chosen product: waste recorded with reason Spoilage/Expiry or Damaged Goods, as a share of all "
               "identified losses, i.e. recorded waste plus stock found missing at the period's applied stock counts. Waste "
               "without one of these reasons counts as not properly recorded. Counts are the independent evidence, so a "
               "period without a count has no result. Missing stock may also be a counting error or an unrecorded transfer.",
    },
]
for _template in INVENTORY_TEMPLATES:
    _template["area"] = "inventory_waste"

_POS = ["pos"]
_ACCOUNTING = ["accounting"]
# Sales & Finance options. "at_least" goals keep the KPI's higher-is-better direction with a single target value.
SALES_TEMPLATES = [
    {
        # Monitoring: one configurable option; the metric, reporting period, daily time window and filters are settings.
        "key": "sales_time_window", "group": "Monitor sales", "title": "Monitor sales in a time window",
        "description": "Sales, orders and quantities between two times of day (e.g. 17:00-21:00), with daily trend, "
                       "previous period and branch comparison. No target.",
        "goal_type": "monitoring", "kind": "monitor", "monitor_source": "pos", "measure": "pos_window_sales",
        "metrics": ["pos_window_sales", "pos_window_orders", "pos_window_quantity", "pos_window_average_order_value"],
        "apps": _POS, "status": "available",
        "how": "Paid and posted POS orders whose time, in the company's time zone, falls from the start time up to (not "
               "including) the end time, on every day of the reporting period. Sales are order lines excluding tax, after "
               "discounts; refunds are their own POS orders and reduce sales and quantities on the day they were made, "
               "and are not counted as orders. Draft and cancelled orders are left out. Average order value = sales / "
               "orders. Quantities in different units are shown per unit, never added together.",
    },
    {
        "key": "net_sales", "group": "Sales", "title": "Increase net sales",
        "description": "Net sales excluding tax reach at least a target for the period.",
        "measure": "pos_net_sales", "rule": "at_least", "kind": "money", "target_label": "Net sales of at least",
        "apps": _POS, "status": "available",
        "how": "Adds up paid and posted POS orders excluding tax; refunds are subtracted. A period without any POS "
               "order has no result rather than zero.",
    },
    {
        "key": "sales_growth", "group": "Sales", "title": "Grow sales compared with the previous period",
        "description": "Net sales grow by at least a percentage compared with the period just before.",
        "measure": "pos_sales_growth_pct", "rule": "at_least", "kind": "change", "target_label": "Growth of at least (%)",
        "apps": _POS, "status": "available",
        "how": "Compares net sales with the period of the same length just before, with the same branch and rules. "
               "Measured once the period has ended; a previous period without sales has no result.",
    },
    {
        "key": "average_order_value", "group": "Sales", "title": "Increase the average order value",
        "description": "Net sales per customer order reach at least a target.",
        "measure": "pos_average_order_value", "rule": "at_least", "kind": "money", "target_label": "Average order of at least",
        "apps": _POS, "status": "available",
        "how": "Net sales excluding tax divided by the number of customer orders; refunds reduce sales but are not "
               "counted as orders.",
    },
    {
        "key": "net_profit", "group": "Profitability", "title": "Reach a net profit",
        "description": "Posted net profit of the company reaches at least a target.",
        "measure": "accounting_net_profit", "rule": "at_least", "kind": "money", "target_label": "Net profit of at least",
        "apps": _ACCOUNTING, "status": "available", "company_only": True,
        "how": "Posted income minus posted expenses, direct costs and depreciation. Missing income or expense entries "
               "give no result instead of zero. Company-wide only.",
    },
    {
        "key": "profit_margin", "group": "Profitability", "title": "Improve the profit margin",
        "description": "Net profit as a percentage of income reaches at least a target.",
        "measure": "accounting_net_profit_margin_pct", "rule": "at_least", "kind": "percent",
        "target_label": "Net profit margin of at least (%)", "apps": _ACCOUNTING, "status": "available", "company_only": True,
        "how": "Net profit divided by posted income, from the same posted entries. Company-wide only.",
    },
    {
        "key": "product_quantity", "group": "Products", "title": "Sell more of a product",
        "description": "The quantity of one product sold through POS reaches at least a target.",
        "measure": "pos_sold_quantity", "rule": "at_least", "kind": "quantity", "target_label": "Quantity sold of at least",
        "apps": _POS, "status": "available",
        "how": "Net quantity sold in paid and posted POS orders, in the product's unit; refunded quantities are subtracted.",
    },
    {
        "key": "product_revenue", "group": "Products", "title": "Increase sales of a product",
        "description": "Net sales of one product reach at least a target.",
        "measure": "pos_product_net_sales", "rule": "at_least", "kind": "money", "target_label": "Product sales of at least",
        "apps": _POS, "status": "available",
        "how": "Adds up the product's POS lines excluding tax; refund lines are subtracted.",
    },
    {
        "key": "unit_selling_price", "group": "Products", "title": "Track selling price per unit",
        "description": "The average price customers pay per unit of a product stays at or above a minimum.",
        "measure": "pos_average_unit_price", "rule": "at_least", "kind": "unit_price",
        "target_label": "Selling price of at least, per unit", "apps": _POS, "status": "available",
        "how": "POS sales of the product excluding tax divided by the quantity sold, refunds subtracted from both. "
               "This is the selling price, never the purchase cost.",
    },
    {
        "key": "product_margin", "group": "Products", "title": "Improve a product's margin",
        "description": "A product's sales minus its cost reach a target.", "apps": ["pos", "valuation"], "status": "unsupported",
        "needs": "Not built yet: a product's margin needs its verified cost at the time of each sale, including recipe "
                 "ingredients. Use 'Increase sales of a product' meanwhile.",
    },
    {
        "key": "total_expenses", "group": "Costs", "title": "Keep expenses within a limit",
        "description": "Posted expenses, direct costs and depreciation stay at or below a limit.",
        "measure": "accounting_total_expenses", "rule": "ceiling", "kind": "money", "target_label": "Most expenses allowed",
        "apps": _ACCOUNTING, "status": "available", "company_only": True,
        "how": "Posted expense, other expense, direct cost and depreciation entries. It includes cost of goods sold and "
               "depreciation, so it is all expenses, not operating expenses only. Company-wide only.",
    },
    {
        "key": "expense_ratio", "group": "Costs", "title": "Keep expenses small compared with income",
        "description": "Expenses and direct costs as a percentage of income stay below a limit.",
        "measure": "accounting_expense_to_income_pct", "rule": "ceiling", "kind": "percent",
        "target_label": "Expenses at most (% of income)", "apps": _ACCOUNTING, "status": "available", "company_only": True,
        "how": "Expenses and direct costs divided by posted income, with the same entries as net profit. No result when "
               "income is zero or negative. Company-wide only.",
    },
    {
        "key": "branch_sales", "group": "Branches", "title": "Increase a branch's net sales",
        "description": "Net sales of one branch reach at least a target.",
        "measure": "pos_net_sales", "rule": "at_least", "kind": "money", "target_label": "Branch net sales of at least",
        "apps": _POS, "status": "available", "branch_only": True,
        "how": "Net sales excluding tax of the Points of Sale linked to the branch's warehouse; refunds are subtracted.",
    },
    {
        "key": "branch_profit", "group": "Branches", "title": "Improve a branch's profit",
        "description": "Net profit of one branch reaches a target.", "apps": _ACCOUNTING, "status": "unsupported",
        "needs": "Accounting entries are not tagged by branch for both income and expenses, so branch profit cannot be "
                 "measured. Every sale and expense would need a verified branch analytic account first.",
    },
]
for _template in SALES_TEMPLATES:
    _template["area"] = "sales_finance"
TEMPLATES = TEMPLATES + INVENTORY_TEMPLATES + SALES_TEMPLATES
TEMPLATE_BY_KEY = {template["key"]: template for template in TEMPLATES}
TEMPLATE_AREAS = {template["area"] for template in TEMPLATES}


def target_bounds(rule, value):
    """(range_min, range_max) of the inclusive range target for a rule and the owner's number."""
    return {
        "ceiling": (0.0, value),
        "floor": (value, 100.0),
        "reduce_by": (-100.0, -value),
        "rise_at_most": (-100.0, value),
        "tolerance": (-value, value),
    }[rule]


# ---------------------------------------------------------------- progress bar, colour and ranking
# Single source for colour bands; the UI only maps the returned tone to a colour.
#   usage (lower is better):      < 70 good, 70..<90 warn, 90..100 bad, > 100 exceeded
#   attainment (higher is better): >= 100 good, 90..<100 warn, < 90 bad
USAGE_WARN_AT, USAGE_BAD_AT = 70.0, 90.0
ATTAINMENT_WARN_AT = 90.0


def usage_tone(pct):
    if pct > 100.0:
        return "exceeded"
    if pct >= USAGE_BAD_AT:
        return "bad"
    return "warn" if pct >= USAGE_WARN_AT else "good"


def attainment_tone(pct):
    if pct >= 100.0:
        return "good"
    return "warn" if pct >= ATTAINMENT_WARN_AT else "bad"


def measure_bar(rule, value, range_min, range_max):
    """What the progress bar shows for one result, and its ranking margin.

    kind  usage      share of an allowance used (ceiling; price rise against the allowed rise)
          attainment share of a required result reached (floor; price reduction against the required cut)
    pct   the real percentage, not capped (110 means 10% over the allowance)
    bar   pct clamped to 0..100 for the bar width
    margin  distance from the target edge as a % of the target: 0 = exactly at the limit/minimum,
            positive = favourable headroom, negative = failing. Comparable across units, used for ranking.
    """
    if rule == "ceiling":
        limit = range_max
        pct = value / limit * 100.0 if limit else 0.0
        kind, tone = "usage", usage_tone(pct)
        margin = (limit - value) / limit * 100.0 if limit else 0.0
    elif rule == "floor":
        minimum = range_min
        pct = value / minimum * 100.0 if minimum else 100.0
        kind, tone = "attainment", attainment_tone(pct)
        margin = (value - minimum) / minimum * 100.0 if minimum else 0.0
    elif rule == "reduce_by":
        required = -range_max  # e.g. 5 for "down at least 5%"
        pct = (-value) / required * 100.0 if required else 100.0
        kind, tone = "attainment", attainment_tone(pct)
        margin = ((-value) - required) / required * 100.0 if required else 0.0
    elif rule == "at_least":
        # Higher is better against one target value (range_min carries it): attainment of the target.
        target = range_min
        pct = value / target * 100.0 if target else 0.0
        kind, tone = "attainment", attainment_tone(pct)
        margin = (value - target) / abs(target) * 100.0 if target else 0.0
    elif rule == "tolerance":
        # Signed difference allowed within +/- tolerance: usage of the tolerance by its size.
        allowed = range_max
        pct = abs(value) / allowed * 100.0 if allowed else 0.0
        kind, tone = "usage", usage_tone(pct)
        margin = (allowed - abs(value)) / allowed * 100.0 if allowed else 0.0
    elif rule == "rise_at_most":
        allowed = range_max
        pct = max(value, 0.0) / allowed * 100.0 if allowed else 0.0
        kind, tone = "usage", usage_tone(pct)
        margin = (allowed - value) / allowed * 100.0 if allowed else 0.0
    else:
        raise ValueError("Unknown target rule: %s" % rule)
    return {"kind": kind, "pct": round(pct, 1), "bar": round(max(0.0, min(100.0, pct)), 1),
            "tone": tone, "margin": round(margin, 2)}
