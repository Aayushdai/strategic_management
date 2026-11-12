/** @odoo-module **/

// Fixed lists shared by the navigation shell and its pages: sidebar layout, page links, colours and sizes.

export const NAVIGATION_ITEMS = [
    { id: "overview", label: "Inbox", icon: "home", description: "Branch requests waiting for Head Office: staffing and objectives." },
    { id: "sales_finance", label: "Sales & Finance", icon: "bar_chart", description: "Review sales, revenue trends, orders, and financial results." },
    { id: "inventory_waste", label: "Inventory & Waste", icon: "inventory_2", description: "Monitor stock, ingredient consumption, waste, and discrepancies." },
    { id: "purchasing", label: "Purchasing", icon: "shopping_cart", description: "Review supplier orders, deliveries, and purchasing activity." },
    { id: "branches", label: "Branches", icon: "storefront", description: "Compare locations and investigate branch-level performance." },
    { id: "people", label: "People", icon: "group", description: "Review employees and workforce-related information." },
    { id: "tasks_issues", label: "Tasks & Issues", icon: "checklist", description: "Track assigned issues, deadlines, and follow-up actions." },
    { id: "reports", label: "Reports", icon: "description", description: "Open business reports and historical analysis." },
    { id: "app_dependencies", label: "App Dependencies", icon: "widgets", description: "See which Odoo apps Strategic Management relies on and what each enables." },
    { id: "configuration", label: "Configuration", icon: "tune", description: "Access authorized module configuration and management tools.", dividerBefore: true },
];

// Reports and Configuration open Odoo's own screens. Standard POS, purchase, stock and
// accounting analyses are not branch-restricted by this module, so they are offered
// to Head Office only; strategic records carry their own branch record rules.
// Settings groups the configuration links; each group has its own colour (see .o_sm_cfg_group.is-<key>).
export const CONFIG_GROUPS = [
    { key: "strategy", label: "Strategy", description: "Plans, objectives and how they line up." },
    { key: "kpi", label: "KPIs", description: "What is measured, the targets and the recorded results." },
    { key: "operations", label: "Operations", description: "Branches, recipes and stock levels the figures come from." },
    { key: "access", label: "Access", description: "Who can use the app and for which branch." },
];
// Reports groups its analysis links the same way, with the same card look.
export const REPORT_GROUPS = [
    { key: "sales", label: "Sales & Finance", description: "What was sold and what it earned." },
    { key: "stock", label: "Purchasing & Stock", description: "What was bought, received and moved." },
    { key: "kpi", label: "KPIs", description: "KPI results over time, and the ones that could not be measured." },
    { key: "achievements", label: "Achievements", description: "What was achieved when an objective was completed, ready to print." },
];
export const SECTION_LINKS = {
    reports: [
        { xmlid: "point_of_sale.action_report_pos_order_all", group: "sales", icon: "M4 20V10M9 20V4M14 20v-8M19 20v-5", label: "POS Sales Analysis", description: "Sales, margin and quantities by product, branch and period.", roles: ["head_office"] },
        { xmlid: "purchase.action_purchase_order_report_all", group: "stock", icon: "M3 4h2l2.4 11.2a2 2 0 0 0 2 1.6h8.2a2 2 0 0 0 2-1.5L21 8H6M10 21h.01M18 21h.01", label: "Purchase Analysis", description: "Purchasing spend, suppliers and delivery performance.", roles: ["head_office"] },
        { xmlid: "stock.stock_move_action", group: "stock", icon: "M4 7h13l-3-3M20 17H7l3 3", label: "Stock Movements", description: "Receipts, consumption, scrap and adjustments by location.", roles: ["head_office"] },
        { xmlid: "account.action_account_moves_all", group: "sales", icon: "M6 3h12v18l-3-2-3 2-3-2-3 2zM9 8h6M9 12h6M9 16h3", label: "Journal Items", description: "Posted income and expenses behind net profit.", roles: ["head_office"] },
        { xmlid: "strategic_management.action_strategic_kpi_analysis", group: "kpi", icon: "M4 19l5-6 4 3 7-9M15 7h5v5", label: "KPI Analysis", description: "KPI evaluations over time, by branch and status.", roles: ["head_office", "branch_manager"] },
        { xmlid: "strategic_management.action_strategic_achievement_report", group: "achievements", icon: "M8 21h8M12 17v4M7 4h10v5a5 5 0 0 1-10 0V4ZM7 6H4v1a3 3 0 0 0 3 3M17 6h3v1a3 3 0 0 1-3 3", label: "Achievement Reports", description: "Completed objectives: key results achieved, tasks done and who confirmed them.", roles: ["head_office", "branch_manager"] },
        { xmlid: "strategic_management.action_strategic_kpi_exceptions", group: "kpi", icon: "M12 3 2 20h20L12 3ZM12 10v4M12 17h.01", label: "KPI Exceptions", description: "Targets that could not be evaluated, with reasons.", roles: ["head_office", "branch_manager"] },
    ],
    configuration: [
        { xmlid: "strategic_management.action_strategic_plan", group: "strategy", icon: "M4 5h16M4 12h10M4 19h7M17 15l3 3-3 3", label: "Strategic Plans", description: "Plan periods that group objectives.", roles: ["head_office"] },
        { xmlid: "strategic_management.action_strategic_objective", group: "strategy", icon: "M12 3a9 9 0 1 0 9 9M12 7a5 5 0 1 0 5 5M12 11a1 1 0 1 0 1 1M21 3l-9 9", label: "Objectives", description: "Company and branch objectives, owners and deadlines.", roles: ["head_office", "branch_manager"] },
        { xmlid: "strategic_management.action_strategic_okr", group: "strategy", icon: "M5 4v16M5 5h11l-2 4 2 4H5", label: "OKR Objectives", description: "Key results linked to objectives.", roles: ["head_office", "branch_manager"] },
        { xmlid: "strategic_management.action_strategic_kpi_definition", group: "kpi", icon: "M5 20V10M10 20V4M15 20v-7M20 20v-4", label: "KPI Definitions", description: "What is measured and from which Odoo source.", roles: ["head_office"] },
        { xmlid: "strategic_management.action_strategic_kpi_target", group: "kpi", icon: "M12 21a9 9 0 1 1 0-18 9 9 0 0 1 0 18ZM12 16a4 4 0 1 1 0-8 4 4 0 0 1 0 8Z", label: "KPI Targets", description: "Approved targets per period, branch or employee.", roles: ["head_office", "branch_manager", "employee"] },
        { xmlid: "strategic_management.action_strategic_kpi_evaluation", group: "kpi", icon: "M9 11l3 3 8-8M20 12v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h9", label: "KPI Evaluations", description: "Recorded KPI results and their evidence.", roles: ["head_office", "branch_manager", "employee"] },
        { xmlid: "stock.action_warehouse_form", group: "operations", icon: "M3 21V9l9-6 9 6v12M9 21v-6h6v6", label: "Branches (Warehouses)", description: "Branch details and Planned Staff for coverage.", roles: ["head_office"] },
        { xmlid: "mrp.mrp_bom_form_action", group: "operations", icon: "M6 3h12M8 3v5l-4 9a3 3 0 0 0 3 4h10a3 3 0 0 0 3-4l-4-9V3M7 14h10", label: "Recipes (BoMs)", description: "Set recipes to Kit so POS sales consume ingredients.", roles: ["head_office"] },
        { xmlid: "stock.action_orderpoint", group: "operations", icon: "M3 12a9 9 0 0 1 15-6.7L21 8M21 3v5h-5M21 12a9 9 0 0 1-15 6.7L3 16M3 21v-5h5", label: "Reorder Rules", description: "Minimum stock levels behind low-stock alerts.", roles: ["head_office"] },
        { xmlid: "base.action_res_users", group: "access", icon: "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM19 8v6M22 11h-6", label: "Users & Branch Assignment", description: "Roles and each user's branch (administrators).", roles: ["head_office"] },
    ],
};

// Sidebar: short labels in the Overview design's order. Business Goals is in the top bar, so it is not repeated here; the sections the
// design does not list stay one click away under "More", so no existing page becomes unreachable.
export const SIDEBAR_PRIMARY = [
    { id: "overview", label: "Inbox" },
    { id: "sales_finance", label: "Sales" },
    { id: "purchasing", label: "Purchasing" },
    { id: "inventory_waste", label: "Inventory" },
    { id: "reports", label: "Reports" },
    { id: "configuration", label: "Settings" },
];
export const SIDEBAR_MORE = NAVIGATION_ITEMS.filter((item) => ["branches", "people", "tasks_issues", "app_dependencies"].includes(item.id));
// Head Office's people pages share one sidebar entry and switch between themselves with tabs.
export const HEAD_OFFICE_PEOPLE_TABS = [{ id: "employee_management", label: "Employees" }, { id: "staffing_requests", label: "Staffing Requests" },
    { id: "employee_performance", label: "Performance" }, { id: "department_assignment", label: "Department Assignment" }];
export const HEAD_OFFICE_SIDEBAR_MORE = [...SIDEBAR_MORE, { id: "employee_management", label: "Employee Management" }];
// Branch Manager sidebar. Items with a section or action open pages that already exist and are scoped to the
// manager's branch on the server; "planned" items belong to later phases and are shown disabled, never as working pages.
// Objectives stay reachable from the dashboard's objective cards and links; notifications from the header bell.
export const BRANCH_SIDEBAR = [
    { id: "bm_dashboard", label: "Dashboard" },
    { id: "sales_finance", label: "Sales" },
    { id: "purchasing", label: "Purchasing" },
    { id: "inventory_waste", label: "Inventory" },
    { id: "branches", label: "Branch Performance" },
    { id: "my_tasks", label: "My Tasks" },
    { id: "employee_management", label: "Employee Management" },
    { id: "task_management", label: "Task Management" },
    // Proposals use the Business Goals form and approval workflow; the context opens the form on the Proposals list.
    { id: "propose_objective", label: "Propose Objective", action: "strategic_management.action_business_goals", context: { propose_objective: true } },
    { id: "reports", label: "Reports" },
];
// Employees: their dashboard and tasks; assigned departments come from the server (see sidebarMore).
// Admin console: people, access, branches, work status, system health and the audit log; never money, personal details or ratings.
export const ADMIN_SIDEBAR = [
    { id: "admin_home", label: "Overview" },
    { id: "admin_people", label: "People & Access" },
    { id: "admin_branches", label: "Branches" },
    { id: "admin_work", label: "Work Records" },
    { id: "admin_health", label: "System Health" },
    { id: "admin_requests", label: "KPI & OKR Requests" },
    { id: "admin_audit", label: "Audit Log" },
];
export const EMPLOYEE_SIDEBAR = [{ id: "employee_dashboard", label: "Dashboard" }, { id: "my_tasks", label: "My Tasks" },
    { id: "my_performance", label: "My Performance" }];
export const TASK_MODEL = "strategic.branch.task";
export const REPORT_MODEL = "strategic.branch.report";
// Report sections, as saved by the server; "(when taken)" figures describe the moment the report's figures were taken.
export const REPORT_SECTIONS = [
    ["Sales (period)", "sales", [["Net sales", "net_sales", "money"], ["Completed orders", "orders"], ["Average order value", "average_order_value", "money"]]],
    ["Inventory", "inventory", [["Recorded waste (period)", "waste_cost", "money"], ["Receipts completed (period)", "receipts_done"],
        ["Below minimum stock (when taken)", "low_stock_count"], ["Expiring soon (when taken)", "expiring_count"]]],
    ["Purchasing", "purchasing", [["Confirmed purchases (period)", "confirmed_value", "money"], ["Open deliveries (when taken)", "awaiting_count"],
        ["Overdue deliveries (when taken)", "overdue_count"]]],
    ["Employee tasks (when taken)", "tasks", [["Open", "open"], ["Overdue", "overdue"], ["Awaiting review", "review"]]],
];
// Pages Branch Managers can already open stay reachable under "More".
export const BRANCH_SIDEBAR_MORE = [
    { id: "people", label: "People" },
    { id: "tasks_issues", label: "Tasks & Issues" },
    { id: "app_dependencies", label: "App Dependencies" },
    { id: "configuration", label: "Settings" },
];
export const PERIOD_PRESETS = [
    { key: "this_week", label: "This Week" },
    { key: "this_month", label: "This Month" },
    { key: "last_month", label: "Last Month" },
    { key: "last_3_months", label: "Last 3 Months" },
    { key: "this_year", label: "This Year" },
    { key: "custom", label: "Custom dates" },
];
// Purchasing category colours, purple first like the Overview (donut and legend share them).
export const PURCHASE_COLORS = ["#6f5bd3", "#35b56f", "#f5a524", "#4a8fe7", "#a39bc8", "#e86a5a"];

export const CHART_COLORS = ["#2e9cf0", "#25a56a", "#f5a23d", "#9673c9", "#91a0ae"];
export const PAGE_SIZE = 5;

// Sent by models/live_refresh.py after POS, Purchase, Inventory or Strategic changes commit.
export const DATA_CHANGED = "strategic_management.data_changed";
export const LIVE_REFRESH_DEBOUNCE_MS = 1500;
// Fallback in case the websocket is unavailable (e.g. blocked proxy).
export const LIVE_REFRESH_POLL_MS = 60000;
