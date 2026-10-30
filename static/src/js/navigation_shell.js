/** @odoo-module **/

import { Component, onMounted, onWillStart, onWillUnmount, proxy } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { user } from "@web/core/user";
import { useService } from "@web/core/utils/hooks";

const NAVIGATION_ITEMS = [
    { id: "overview", label: "Overview", icon: "home", description: "See overall business performance and important changes." },
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
const SECTION_LINKS = {
    reports: [
        { xmlid: "point_of_sale.action_report_pos_order_all", label: "POS Sales Analysis", description: "Sales, margin and quantities by product, branch and period.", roles: ["head_office"] },
        { xmlid: "purchase.action_purchase_order_report_all", label: "Purchase Analysis", description: "Purchasing spend, suppliers and delivery performance.", roles: ["head_office"] },
        { xmlid: "stock.stock_move_action", label: "Stock Movements", description: "Receipts, consumption, scrap and adjustments by location.", roles: ["head_office"] },
        { xmlid: "account.action_account_moves_all", label: "Journal Items", description: "Posted income and expenses behind net profit.", roles: ["head_office"] },
        { xmlid: "strategic_management.action_strategic_kpi_analysis", label: "KPI Analysis", description: "KPI evaluations over time, by branch and status.", roles: ["head_office", "branch_manager"] },
        { xmlid: "strategic_management.action_strategic_kpi_exceptions", label: "KPI Exceptions", description: "Targets that could not be evaluated, with reasons.", roles: ["head_office", "branch_manager"] },
    ],
    configuration: [
        { xmlid: "strategic_management.action_strategic_plan", label: "Strategic Plans", description: "Plan periods that group objectives.", roles: ["head_office"] },
        { xmlid: "strategic_management.action_strategic_objective", label: "Objectives", description: "Company and branch objectives, owners and deadlines.", roles: ["head_office", "branch_manager"] },
        { xmlid: "strategic_management.action_strategic_okr", label: "OKR Objectives", description: "Key results linked to objectives.", roles: ["head_office", "branch_manager"] },
        { xmlid: "strategic_management.action_strategic_kpi_definition", label: "KPI Definitions", description: "What is measured and from which Odoo source.", roles: ["head_office"] },
        { xmlid: "strategic_management.action_strategic_kpi_target", label: "KPI Targets", description: "Approved targets per period, branch or employee.", roles: ["head_office", "branch_manager", "employee"] },
        { xmlid: "strategic_management.action_strategic_kpi_evaluation", label: "KPI Evaluations", description: "Recorded KPI results and their evidence.", roles: ["head_office", "branch_manager", "employee"] },
        { xmlid: "strategic_management.action_strategic_hoshin_xmatrix_workspace", label: "Hoshin X-Matrix", description: "Strategy alignment matrix.", roles: ["head_office"] },
        { xmlid: "stock.action_warehouse_form", label: "Branches (Warehouses)", description: "Branch details and Planned Staff for coverage.", roles: ["head_office"] },
        { xmlid: "mrp.mrp_bom_form_action", label: "Recipes (BoMs)", description: "Set recipes to Kit so POS sales consume ingredients.", roles: ["head_office"] },
        { xmlid: "stock.action_orderpoint", label: "Reorder Rules", description: "Minimum stock levels behind low-stock alerts.", roles: ["head_office"] },
        { xmlid: "base.action_res_users", label: "Users & Branch Assignment", description: "Roles and each user's branch (administrators).", roles: ["head_office"] },
    ],
};

// Sidebar: short labels in the Overview design's order. Business Goals opens its own client action; the sections the
// design does not list stay one click away under "More", so no existing page becomes unreachable.
const SIDEBAR_PRIMARY = [
    { id: "overview", label: "Overview" },
    { id: "sales_finance", label: "Sales" },
    { id: "purchasing", label: "Purchasing" },
    { id: "inventory_waste", label: "Inventory" },
    { id: "business_goals", label: "Business Goals", action: "strategic_management.action_business_goals" },
    { id: "reports", label: "Reports" },
    { id: "configuration", label: "Settings" },
];
const SIDEBAR_MORE = NAVIGATION_ITEMS.filter((item) => ["branches", "people", "tasks_issues", "app_dependencies"].includes(item.id));
// Branch Manager sidebar (Phase 1). Items with a section or action open pages that already exist and are scoped to the
// manager's branch on the server; "planned" items belong to later phases and are shown disabled, never as working pages.
// Objectives stay reachable from the dashboard's objective cards and links; notifications from the header bell.
const BRANCH_SIDEBAR = [
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
// Employees: their own tasks first, then the pages they could already open.
const EMPLOYEE_SIDEBAR = [{ id: "my_tasks", label: "My Tasks" }, ...SIDEBAR_PRIMARY];
const TASK_MODEL = "strategic.branch.task";
const REPORT_MODEL = "strategic.branch.report";
// Report sections, as saved by the server; "(when taken)" figures describe the moment the report's figures were taken.
const REPORT_SECTIONS = [
    ["Sales (period)", "sales", [["Net sales", "net_sales", "money"], ["Completed orders", "orders"], ["Average order value", "average_order_value", "money"]]],
    ["Inventory", "inventory", [["Recorded waste (period)", "waste_cost", "money"], ["Receipts completed (period)", "receipts_done"],
        ["Below minimum stock (when taken)", "low_stock_count"], ["Expiring within 7 days (when taken)", "expiring_count"]]],
    ["Purchasing", "purchasing", [["Confirmed purchases (period)", "confirmed_value", "money"], ["Open deliveries (when taken)", "awaiting_count"],
        ["Overdue deliveries (when taken)", "overdue_count"]]],
    ["Employee tasks (when taken)", "tasks", [["Open", "open"], ["Overdue", "overdue"], ["Awaiting review", "review"]]],
];
// Pages Branch Managers can already open stay reachable under "More".
const BRANCH_SIDEBAR_MORE = [
    { id: "people", label: "People" },
    { id: "tasks_issues", label: "Tasks & Issues" },
    { id: "app_dependencies", label: "App Dependencies" },
    { id: "configuration", label: "Settings" },
];
const PERIOD_PRESETS = [
    { key: "this_month", label: "This Month" },
    { key: "last_month", label: "Last Month" },
    { key: "last_3_months", label: "Last 3 Months" },
    { key: "this_year", label: "This Year" },
    { key: "custom", label: "Custom dates" },
];
const BAR_COLORS = ["#7b68d9", "#8b7add", "#9d8fe3", "#b0a5ea"];
// Purchasing category colours, purple first like the Overview (donut and legend share them).
const PURCHASE_COLORS = ["#6f5bd3", "#35b56f", "#f5a524", "#4a8fe7", "#a39bc8", "#e86a5a"];

const CHART_COLORS = ["#2e9cf0", "#25a56a", "#f5a23d", "#9673c9", "#91a0ae"];
const PAGE_SIZE = 5;

// Sent by models/live_refresh.py after POS, Purchase, Inventory or Strategic changes commit.
const DATA_CHANGED = "strategic_management.data_changed";
const LIVE_REFRESH_DEBOUNCE_MS = 1500;
// Fallback in case the websocket is unavailable (e.g. blocked proxy).
const LIVE_REFRESH_POLL_MS = 60000;

export class StrategicNavigationShell extends Component {
    static template = "strategic_management.NavigationShell";

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.action = useService("action");
        this.items = NAVIGATION_ITEMS;
        this.periodPresets = PERIOD_PRESETS;
        // The Overview opens on This Month (shared by every section's period filter).
        const today = new Date();
        const [monthStart, todayInput] = [this.asDateInput(new Date(today.getFullYear(), today.getMonth(), 1)), this.asDateInput(today)];
        this.state = proxy({
            items: NAVIGATION_ITEMS,
            activeSection: "overview",
            loading: true,
            error: "",
            dateStart: monthStart,
            dateEnd: todayInput,
            todayDate: todayInput,
            periodPreset: "this_month",
            warehouseId: "all",
            trendGranularity: "day",
            branchMetric: "sales",
            topProductLimit: 5,
            data: null,
            // Role and own branch assignment from the server, known even when branch data cannot be loaded.
            navContext: null,
            // Branch Manager dashboard: objectives needing attention and Head Office instructions (Phase 2).
            branchObjectives: null,
            branchObjectivesLoading: false,
            branchObjectivesError: "",
            // Branch tasks (Phase 3): the user's own tasks, the manager's workspace, the dashboard summary and dialogs.
            myTasks: null,
            taskManagement: null,
            taskSummary: null,
            tasksLoading: false,
            tasksError: "",
            taskFilter: "open",
            taskDetailId: null,
            taskNote: "",
            taskForm: null,
            taskBusy: false,
            // Task Management narrowed to one employee (from Employee Management); cleared from the page or the sidebar.
            taskEmployee: null,
            // Employee Management (Phase 4): the branch's people with their task counts, and one person's tasks.
            employees: null,
            employeesLoading: false,
            employeesError: "",
            employeeDetail: null,
            employeeDetailLoading: false,
            employeeDetailError: "",
            // Branch operations (Phase 6): the manager's own branch sales, inventory and purchasing for the reporting period.
            branchOps: null,
            branchOpsLoading: false,
            branchOpsError: "",
            opsTab: "sales",
            // Branch reports (Phase 7): the list, the open report, and Head Office's open blockers.
            reports: null,
            reportsError: "",
            reportDetail: null,
            reportComment: "",
            reportNote: "",
            reportBusy: false,
            openBlockers: null,
            inventoryData: null,
            inventoryLoading: false,
            inventoryError: "",
            appDependenciesData: null,
            appDependenciesLoading: false,
            appDependenciesError: "",
            expandedDependencyId: "inventory",
            inventoryProductId: "all",
            purchasingData: null,
            purchasingLoading: false,
            branchesData: null,
            branchesLoading: false,
            peopleData: null,
            peopleLoading: false,
            overviewExtras: null,
            // Purchasing: compact previews; "See more" expands the existing full lists in place.
            purExpanded: {},
            // Inventory & Waste: compact previews; "See all" and the detail toggles expand the existing sections in place.
            invExpanded: {},
            brExpanded: {},
            // Overview: objective requests from branch managers and the decisions already taken.
            requestsData: null,
            requestsLoading: false,
            reqExpanded: {},
            historyStatus: "all",
            reviewRequestId: null,
            rejectRequestId: null,
            rejectReason: "",
            deciding: false,
            pages: {},
            areaGoals: {},
        });
        this.purchasingRequestId = 0;
        this.branchesRequestId = 0;
        this.peopleRequestId = 0;
        this.liveRefreshTimer = null;
        this.livePollTimer = null;
        this.busService = useService("bus_service");
        this.onDataChanged = () => this.scheduleLiveRefresh();
        this.onVisibilityChanged = () => {
            if (document.visibilityState === "visible") {
                this.scheduleLiveRefresh();
            }
        };
        onWillStart(() => this.loadStart());
        onMounted(() => {
            this.busService.subscribe(DATA_CHANGED, this.onDataChanged);
            if (!this.busService.isActive) {
                this.busService.start();
            }
            document.addEventListener("visibilitychange", this.onVisibilityChanged);
            this.livePollTimer = setInterval(() => this.refreshActiveSectionSilently(), LIVE_REFRESH_POLL_MS);
        });
        onWillUnmount(() => {
            this.busService.unsubscribe(DATA_CHANGED, this.onDataChanged);
            document.removeEventListener("visibilitychange", this.onVisibilityChanged);
            clearTimeout(this.liveRefreshTimer);
            clearInterval(this.livePollTimer);
        });
    }

    scheduleLiveRefresh() {
        clearTimeout(this.liveRefreshTimer);
        this.liveRefreshTimer = setTimeout(() => this.refreshActiveSectionSilently(), LIVE_REFRESH_DEBOUNCE_MS);
    }

    refreshActiveSectionSilently() {
        // Hidden sections load on demand when opened, so only the visible live section is refreshed.
        const loaders = {
            bm_dashboard: "loadBranchObjectives",
            my_tasks: "loadMyTasks",
            task_management: "loadTaskManagement",
            employee_management: "refreshEmployeeManagement",
            reports: "loadReports",
            overview: "loadOverview",
            sales_finance: "loadOverview",
            inventory_waste: "loadInventory",
            purchasing: "loadPurchasing",
            branches: "loadBranches",
            people: "loadPeople",
            tasks_issues: "loadTasksIssues",
            app_dependencies: "loadAppDependencies",
        };
        const loader = loaders[this.state.activeSection];
        if (!loader || this.branchError || document.visibilityState === "hidden") {
            return;
        }
        // Keep an open "up to today" period current across midnight.
        // shortcut: rolls over at the browser's midnight, not the Odoo user's; fetch "today" from the server if they differ in practice.
        const today = this.asDateInput(new Date());
        if (today !== this.state.todayDate) {
            if (this.state.dateEnd === this.state.todayDate) {
                this.state.dateEnd = today;
            }
            this.state.todayDate = today;
        }
        return this[loader]({ silent: true });
    }

    asDateInput(value) {
        const year = value.getFullYear();
        const month = String(value.getMonth() + 1).padStart(2, "0");
        const day = String(value.getDate()).padStart(2, "0");
        return `${year}-${month}-${day}`;
    }

    async loadOverview({ silent = false } = {}) {
        if (this.state.dateStart && this.state.dateEnd && this.state.dateStart > this.state.dateEnd) {
            this.state.error = "The start date must be on or before the end date.";
            return;
        }
        // Ignore responses that arrive after a newer request (filter change or live refresh).
        const requestId = this.overviewRequestId = (this.overviewRequestId || 0) + 1;
        if (silent) {
            try {
                const selectedWarehouse = this.state.warehouseId === "all" ? false : Number(this.state.warehouseId);
                const data = await this.orm.silent.call(
                    "strategic.performance.dashboard",
                    "get_business_overview_data",
                    [this.state.dateStart || false, this.state.dateEnd || false, selectedWarehouse, this.state.trendGranularity, this.state.topProductLimit]
                );
                if (requestId === this.overviewRequestId) {
                    this.state.data = data;
                }
            } catch (error) {
                // A failed background refresh keeps the last good figures on screen.
                console.error("Overview data could not be refreshed", error);
            }
            if (this.state.activeSection === "overview") {
                await this.loadObjectiveRequests({ silent: true });
            }
            return;
        }
        this.state.loading = true;
        this.state.error = "";
        try {
            const selectedWarehouse = this.state.warehouseId === "all" ? false : Number(this.state.warehouseId);
            const data = await this.orm.call(
                "strategic.performance.dashboard",
                "get_business_overview_data",
                [this.state.dateStart || false, this.state.dateEnd || false, selectedWarehouse, this.state.trendGranularity, this.state.topProductLimit]
            );
            if (requestId !== this.overviewRequestId) {
                return;
            }
            this.state.data = data;
            if (!this.state.todayDate && data.period) {
                Object.assign(this.state, { dateStart: data.period.start, dateEnd: data.period.end, todayDate: data.period.end });
            }
            if (this.state.activeSection === "overview") { await this.loadObjectiveRequests(); }
            if (this.state.activeSection === "purchasing") { await this.loadPurchasing(); }
            if (this.state.activeSection === "branches") { await this.loadBranches(); }
            if (this.state.activeSection === "people") { await this.loadPeople(); }
            if (this.state.activeSection === "tasks_issues") { await this.loadTasksIssues(); }
            if (this.state.activeSection === "inventory_waste") { await this.loadInventory(); }
        } catch (error) {
            this.state.data = null;
            this.state.error = error.message || "The business overview could not be loaded.";
            this.notification.add(this.state.error, { type: "danger" });
        } finally {
            this.state.loading = false;
        }
    }

        onStartDateChanged(event) {
        let value = event.target.value || "";
        const today = this.state.todayDate || this.asDateInput(new Date());
        if (value && value > today) value = today;
        this.state.dateStart = value;
        if (this.state.dateEnd && this.state.dateEnd > today) {
            this.state.dateEnd = today;
        }
        if (this.state.dateStart && this.state.dateEnd && this.state.dateStart > this.state.dateEnd) {
            this.state.dateEnd = this.state.dateStart;
        }
        if (event.target.value !== value) event.target.value = value;
        return this.loadOverview();
    }

        onEndDateChanged(event) {
        let value = event.target.value || "";
        const today = this.state.todayDate || this.asDateInput(new Date());
        if (value && value > today) value = today;
        this.state.dateEnd = value;
        if (this.state.dateStart && this.state.dateEnd && this.state.dateEnd < this.state.dateStart) {
            this.state.dateStart = this.state.dateEnd;
        }
        if (event.target.value !== value) event.target.value = value;
        return this.loadOverview();
    }

    onWarehouseChanged(event) {
        this.state.warehouseId = event.target.value;
        return this.loadOverview();
    }

    setTrendGranularity(granularity) {
        if (!["day", "week", "month"].includes(granularity)) {
            return;
        }
        if (this.state.trendGranularity === granularity) {
            return;
        }
        this.state.trendGranularity = granularity;
        return this.loadOverview();
    }

    onBranchMetricChanged(event) {
        const allowed = ["sales", "orders", "average_order_value"];
        if (allowed.includes(event.target.value)) {
            this.state.branchMetric = event.target.value;
        }
    }

    onTopProductLimitChanged(event) {
        const limit = Number(event.target.value);
        if (![5, 10].includes(limit) || this.state.topProductLimit === limit) {
            return;
        }
        this.state.topProductLimit = limit;
        return this.loadOverview();
    }

    selectSection(sectionId) {
        this.state.activeSection = sectionId;
        if (this.branchError) {
            // Without a valid branch assignment the server refuses branch data; nothing is requested.
            return;
        }
        if (["sales_finance", "inventory_waste", "purchasing"].includes(sectionId)) {
            this.loadAreaGoals(sectionId);
        }
        if (sectionId === "overview") {
            return this.loadObjectiveRequests();
        }
        if (sectionId === "bm_dashboard" && this.isBranchManager) {
            return this.loadBranchObjectives();
        }
        if (sectionId === "my_tasks") {
            return this.loadMyTasks();
        }
        if (sectionId === "task_management" && this.isBranchManager) {
            this.state.taskEmployee = null;
            return this.loadTaskManagement();
        }
        if (sectionId === "reports") {
            return this.loadReports();
        }
        if (sectionId === "employee_management" && this.isBranchManager) {
            this.state.employeeDetail = null;
            this.state.employeeDetailError = "";
            return this.loadEmployeeManagement();
        }
        if (sectionId === "purchasing") {
            return this.loadPurchasing();
        }
        if (sectionId === "branches") {
            return this.loadBranches();
        }
        if (sectionId === "people") {
            return this.loadPeople();
        }
        if (sectionId === "tasks_issues") {
            return this.loadTasksIssues();
        }
        if (sectionId === "inventory_waste") {
            return this.loadInventory();
        }
        if (sectionId === "app_dependencies") {
            return this.loadAppDependencies();
        }
    }

    // ------------------------------------------------------------ Overview home ("Welcome back!")
    openNavItem(item) {
        if (item.planned) {
            return;
        }
        if (item.action) {
            return item.context ? this.action.doAction(item.action, { additionalContext: item.context }) : this.action.doAction(item.action);
        }
        return this.selectSection(item.id);
    }

    // ------------------------------------------------------------ Branch Manager dashboard (Phase 1 foundation)
    /** Role first, then data: a Branch Manager without a valid branch keeps their navigation and sees why. */
    async loadStart() {
        try {
            this.state.navContext = await this.orm.call("strategic.performance.dashboard", "get_navigation_context", []);
        } catch (error) {
            console.error("Navigation context could not be loaded", error);
        }
        const context = this.state.navContext;
        if (context && context.role === "branch_manager") {
            // A Branch Manager lands on their own dashboard.
            this.state.activeSection = "bm_dashboard";
            if (context.branch_error) {
                this.state.loading = false;
                return;
            }
            await this.loadOverview();
            return this.loadBranchObjectives();
        }
        if (context && context.role === "employee") {
            // Employees land on their own tasks.
            this.state.activeSection = "my_tasks";
            await this.loadOverview();
            return this.loadMyTasks();
        }
        return this.loadOverview();
    }

    async loadBranchObjectives({ silent = false } = {}) {
        if (!this.isBranchManager || this.branchError) return;
        if (!silent) {
            this.state.branchObjectivesLoading = true;
        }
        this.loadTaskSummary();
        this.loadBranchOps({ silent });
        try {
            this.state.branchObjectives = await this.orm.silent.call("strategic.performance.dashboard", "get_branch_objective_summary", []);
            this.state.branchObjectivesError = "";
        } catch (error) {
            if (!silent) {
                this.state.branchObjectives = null;
                this.state.branchObjectivesError = error.data?.message || error.message || "Objectives could not be loaded.";
            }
        } finally {
            this.state.branchObjectivesLoading = false;
        }
    }

    /** Business Goals, opened on one objective's details (Branch Manager dashboard). */
    openBranchObjective(objectiveId) {
        return this.action.doAction("strategic_management.action_business_goals", { additionalContext: { open_objective_id: objectiveId } });
    }

    get isBranchManager() {
        const role = this.state.navContext?.role || this.state.data?.role;
        return role === "branch_manager";
    }

    get branchError() {
        return (this.isBranchManager && this.state.navContext?.branch_error) || false;
    }

    get sidebarPrimary() {
        if (this.isBranchManager) return BRANCH_SIDEBAR;
        return this.state.navContext?.role === "employee" ? EMPLOYEE_SIDEBAR : SIDEBAR_PRIMARY;
    }

    // ------------------------------------------------------------ branch tasks (Phase 3)
    async loadMyTasks({ silent = false } = {}) {
        if (!silent) this.state.tasksLoading = true;
        try {
            this.state.myTasks = await this.orm.silent.call(TASK_MODEL, "get_my_tasks", []);
            this.state.tasksError = "";
        } catch (error) {
            if (!silent) this.state.tasksError = error.data?.message || error.message || "Tasks could not be loaded.";
        } finally {
            this.state.tasksLoading = false;
        }
    }

    async loadTaskManagement({ silent = false } = {}) {
        if (!this.isBranchManager || this.branchError) return;
        if (!silent) this.state.tasksLoading = true;
        try {
            this.state.taskManagement = await this.orm.silent.call(TASK_MODEL, "get_task_management", []);
            this.state.tasksError = "";
        } catch (error) {
            if (!silent) this.state.tasksError = error.data?.message || error.message || "Tasks could not be loaded.";
        } finally {
            this.state.tasksLoading = false;
        }
    }

    async loadTaskSummary() {
        try {
            this.state.taskSummary = await this.orm.silent.call(TASK_MODEL, "get_branch_task_summary", []);
        } catch (error) {
            this.state.taskSummary = null;
            console.error("Task summary could not be loaded", error);
        }
    }

    /** Task Management's tasks, narrowed to one employee when opened from Employee Management. */
    get scopedTasks() {
        const tasks = this.state.taskManagement?.tasks || [];
        const employee = this.state.taskEmployee;
        return employee ? tasks.filter((task) => task.assigned_user_id === employee.id) : tasks;
    }

    get taskFilters() {
        const tasks = this.scopedTasks;
        const open = (task) => ["assigned", "in_progress", "changes_requested", "submitted"].includes(task.state);
        return [
            { key: "open", label: "Open", match: open },
            { key: "overdue", label: "Overdue", match: (task) => task.is_overdue },
            { key: "review", label: "Awaiting review", match: (task) => task.state === "submitted" },
            { key: "all", label: "All", match: () => true },
        ].map((filter) => ({ ...filter, count: tasks.filter(filter.match).length }));
    }

    get filteredTasks() {
        const filter = this.taskFilters.find((item) => item.key === this.state.taskFilter) || this.taskFilters[0];
        return this.scopedTasks.filter(filter.match);
    }

    clearTaskEmployee() {
        this.state.taskEmployee = null;
        this.state.pages.task_management = 0;
    }

    setTaskFilter(key) {
        this.state.taskFilter = key;
        this.state.pages.task_management = 0;
    }

    /** The task open in the dialog, from whichever list holds it. */
    get taskDetail() {
        const id = this.state.taskDetailId;
        if (!id) return null;
        return [...(this.state.taskManagement?.tasks || []), ...(this.state.myTasks?.tasks || [])].find((task) => task.id === id) || null;
    }

    openTask(task) {
        this.state.taskDetailId = task.id;
        this.state.taskNote = "";
    }

    /** From the dashboard: open the task in Task Management. */
    async openManagedTask(taskId, employee = null) {
        this.state.taskEmployee = employee;
        this.state.activeSection = "task_management";
        await this.loadTaskManagement();
        this.state.taskFilter = "all";
        this.state.taskDetailId = taskId;
        this.state.taskNote = "";
    }

    // ------------------------------------------------------------ Employee Management (Phase 4): read-only workload view
    async loadEmployeeManagement({ silent = false } = {}) {
        if (!this.isBranchManager || this.branchError) return;
        if (!silent) this.state.employeesLoading = true;
        try {
            this.state.employees = await this.orm.silent.call(TASK_MODEL, "get_employee_management", []);
            this.state.employeesError = "";
        } catch (error) {
            if (!silent) {
                this.state.employees = null;
                this.state.employeesError = error.data?.message || error.message || "Employees could not be loaded.";
            }
        } finally {
            this.state.employeesLoading = false;
        }
    }

    async openEmployee(employee, { silent = false } = {}) {
        if (!silent) {
            this.state.employeeDetail = { employee, current: null, history: null };
            this.state.employeeDetailError = "";
            this.state.employeeDetailLoading = true;
        }
        try {
            this.state.employeeDetail = await this.orm.silent.call(TASK_MODEL, "get_employee_tasks", [employee.id]);
        } catch (error) {
            if (!silent) this.state.employeeDetailError = error.data?.message || error.message || "This employee's tasks could not be loaded.";
        } finally {
            this.state.employeeDetailLoading = false;
        }
    }

    closeEmployee() {
        this.state.employeeDetail = null;
        this.state.employeeDetailError = "";
        return this.loadEmployeeManagement({ silent: true });
    }

    refreshEmployeeManagement() {
        const detail = this.state.employeeDetail;
        return detail && detail.current ? this.openEmployee(detail.employee, { silent: true }) : this.loadEmployeeManagement({ silent: true });
    }

    /** Task Management stays the place to act on tasks: open it narrowed to this employee. */
    async viewEmployeeTasks(employee) {
        this.state.activeSection = "task_management";
        this.state.taskEmployee = { id: employee.id, name: employee.name };
        this.state.taskFilter = "all";
        this.state.pages.task_management = 0;
        await this.loadTaskManagement();
    }

    // ------------------------------------------------------------ Branch reports (Phase 7)
    get canUseReports() {
        return ["branch_manager", "head_office"].includes(this.state.navContext?.role);
    }

    async loadReports({ silent = false } = {}) {
        if (!this.canUseReports || this.branchError) return;
        try {
            this.state.reports = await this.orm.silent.call(REPORT_MODEL, "get_branch_reports", []);
            this.state.reportsError = "";
        } catch (error) {
            if (!silent) this.state.reportsError = error.data?.message || error.message || "Reports could not be loaded.";
        }
        if (this.state.navContext?.role === "head_office") {
            try {
                this.state.openBlockers = await this.orm.silent.call("strategic.performance.dashboard", "get_open_blockers", []);
            } catch {
                this.state.openBlockers = null;
            }
        }
    }

    showReport(data) {
        this.state.reportDetail = data;
        this.state.reportComment = data.comment || "";
        this.state.reportNote = "";
    }

    async newReport() {
        if (this.state.reportBusy) return;
        this.state.reportBusy = true;
        try {
            this.showReport(await this.orm.call(REPORT_MODEL, "create_branch_report", [this.state.dateStart, this.state.dateEnd]));
            await this.loadReports({ silent: true });
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "The report could not be prepared.", { type: "danger" });
        } finally {
            this.state.reportBusy = false;
        }
    }

    async openReport(row) {
        try {
            this.showReport(await this.orm.call(REPORT_MODEL, "get_branch_report", [row.id]));
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "This report could not be opened.", { type: "danger" });
        }
    }

    onReportComment(event) {
        this.state.reportComment = event.target.value;
    }

    onReportNote(event) {
        this.state.reportNote = event.target.value;
    }

    closeReport() {
        if (!this.state.reportBusy) this.state.reportDetail = null;
    }

    /** One workflow step on the server; the list and the report reload from the server afterwards. */
    async runReportAction(method, args, done) {
        if (this.state.reportBusy) return;
        this.state.reportBusy = true;
        try {
            await this.orm.call(REPORT_MODEL, method, [[this.state.reportDetail.id], ...args]);
            this.notification.add(done, { type: "success" });
            this.state.reportDetail = null;
            await this.loadReports({ silent: true });
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "The report could not be updated.", { type: "danger" });
        } finally {
            this.state.reportBusy = false;
        }
    }

    submitReport() {
        return this.runReportAction("action_submit", [this.state.reportComment], "Report submitted to Head Office.");
    }

    reviewReport(decision) {
        const note = this.state.reportNote.trim();
        if (decision === "changes" && !note) {
            this.notification.add("Explain what needs to change.", { type: "warning" });
            return;
        }
        return decision === "changes"
            ? this.runReportAction("action_request_changes", [note], "Changes requested.")
            : this.runReportAction("action_mark_reviewed", [note], "Report marked as reviewed.");
    }

    /** The saved (or previewed) figures as labelled rows; a missing figure is said to be missing, never zero. */
    reportSections(snapshot) {
        return REPORT_SECTIONS.map(([title, key, fields]) => {
            const data = snapshot?.[key] || {};
            return {
                title, key, available: Boolean(data.available), messages: data.messages || [],
                rows: fields.map(([label, field, kind]) => [label,
                    kind === "money" ? this.formatOpsMoney(data[field], data) : this.formatOpsCount(data[field])]),
            };
        });
    }

    // ------------------------------------------------------------ Branch operations (Phase 6)
    /** Sales, inventory and purchasing for the manager's own branch; the server decides the branch, the shared dates the period. */
    async loadBranchOps({ silent = false } = {}) {
        if (!this.isBranchManager || this.branchError) return;
        const requestId = (this.branchOpsRequestId = (this.branchOpsRequestId || 0) + 1);
        if (!silent) this.state.branchOpsLoading = true;
        try {
            const data = await this.orm.silent.call("strategic.performance.dashboard", "get_branch_operations",
                [this.state.dateStart || false, this.state.dateEnd || false]);
            if (requestId === this.branchOpsRequestId) {
                this.state.branchOps = data;
                this.state.branchOpsError = "";
            }
        } catch (error) {
            // A failed background refresh keeps the last good figures on screen.
            if (requestId === this.branchOpsRequestId && !silent) {
                this.state.branchOps = null;
                this.state.branchOpsError = error.data?.message || error.message || "Branch figures could not be loaded.";
            }
        } finally {
            if (requestId === this.branchOpsRequestId) this.state.branchOpsLoading = false;
        }
    }

    setOpsTab(tab) {
        this.state.opsTab = tab;
    }

    /** Money in the section's own currency; a missing figure says so instead of showing zero. */
    formatOpsMoney(value, section) {
        if (value === false || value === null || value === undefined || !Number.isFinite(Number(value))) return "Not available";
        const currency = section?.currency || {};
        const digits = Number.isInteger(currency.decimal_places) ? Math.max(0, Math.min(6, currency.decimal_places)) : 2;
        const formatted = Number(value).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
        const symbol = currency.symbol || currency.name || "";
        return currency.position === "after" ? `${formatted} ${symbol}`.trim() : `${symbol}${formatted}`;
    }

    formatOpsCount(value) {
        return value === false || value === null || value === undefined ? "Not available" : Number(value).toLocaleString("en-US");
    }

    /** One bar per day, week or month of the period; heights relative to the best bucket (refunds can make one negative). */
    get opsTrendBars() {
        const trend = this.state.branchOps?.sales?.trend || [];
        const max = Math.max(0, ...trend.map((row) => row.sales));
        const step = Math.ceil(trend.length / 8);
        return trend.map((row, index) => ({ ...row, height: max > 0 ? Math.max(0, Math.round((row.sales / max) * 100)) : 0,
                                             showLabel: index % step === 0 || index === trend.length - 1 }));
    }

    /** Dashboard: what needs action now (low stock, expiring lots, overdue deliveries), at most five. */
    get opsAlerts() {
        const ops = this.state.branchOps;
        if (!ops || !ops.sales) return null;
        const alerts = [];
        const inv = ops.inventory?.available ? ops.inventory : null;
        const pur = ops.purchasing?.available ? ops.purchasing : null;
        for (const row of inv?.low_stock || []) {
            alerts.push({ key: "low-" + row.product_id, kind: "stock", title: `${row.status}: ${row.name}`,
                          detail: `${this.formatInventoryQuantity(row.available, row.unit)} on hand · minimum ${this.formatInventoryQuantity(row.minimum, row.unit)}` });
        }
        for (const row of inv?.expiring || []) {
            alerts.push({ key: "exp-" + row.lot_id + row.expiry_date, kind: "expiry", title: `${row.days_left < 0 ? "Expired" : "Expiring"}: ${row.product}`,
                          detail: `${this.formatInventoryQuantity(row.quantity, row.unit)} · ${this.formatPurchaseDate(row.expiry_date)}` });
        }
        for (const row of (pur?.deliveries || []).filter((item) => item.status === "Overdue")) {
            alerts.push({ key: "po-" + row.name, kind: "purchase", title: `Overdue delivery: ${row.name}`,
                          detail: `${row.supplier} · expected ${this.formatPurchaseDate(row.expected_date)}` });
        }
        return alerts.slice(0, 5);
    }

    closeTask() {
        if (!this.state.taskBusy) {
            this.state.taskDetailId = null;
        }
    }

    onTaskNoteInput(event) {
        this.state.taskNote = event.target.value;
    }

    /** Run one workflow step on the server, then reload; nothing changes on screen before the server confirms. */
    async runTaskAction(task, method, withNote = false) {
        if (this.state.taskBusy) return;
        const note = this.state.taskNote.trim();
        if (withNote && !note) {
            this.notification.add(method === "action_submit" ? "Describe what was done." : "Explain what needs to change.", { type: "warning" });
            return;
        }
        this.state.taskBusy = true;
        try {
            await this.orm.call(TASK_MODEL, method, withNote ? [[task.id], note] : [[task.id]]);
            this.state.taskNote = "";
            const labels = { action_start: "Task started.", action_submit: "Submitted for review.", action_accept: "Task completed.",
                             action_request_changes: "Changes requested.", action_cancel: "Task cancelled." };
            this.notification.add(labels[method], { type: "success" });
            await Promise.all([
                this.state.myTasks ? this.loadMyTasks({ silent: true }) : null,
                this.isBranchManager ? this.loadTaskManagement({ silent: true }) : null,
                this.isBranchManager ? this.loadTaskSummary() : null,
            ]);
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "The task could not be updated.", { type: "danger" });
        } finally {
            this.state.taskBusy = false;
        }
    }

    openTaskForm() {
        this.state.taskForm = { name: "", description: "", assigned_user_id: "", date_deadline: "", priority: "0", objective_id: "" };
    }

    closeTaskForm() {
        if (!this.state.taskBusy) this.state.taskForm = null;
    }

    onTaskFormInput(field, event) {
        this.state.taskForm[field] = event.target.value;
    }

    get taskFormReady() {
        const form = this.state.taskForm;
        return Boolean(form && form.name.trim() && form.description.trim() && form.assigned_user_id && form.date_deadline);
    }

    async saveTask() {
        const form = this.state.taskForm;
        if (!this.taskFormReady || this.state.taskBusy) return;
        this.state.taskBusy = true;
        try {
            await this.orm.call(TASK_MODEL, "create_branch_task", [{
                name: form.name.trim(), description: form.description.trim(), assigned_user_id: Number(form.assigned_user_id),
                date_deadline: form.date_deadline, priority: form.priority, objective_id: form.objective_id ? Number(form.objective_id) : false,
            }]);
            this.state.taskForm = null;
            this.notification.add("Task assigned.", { type: "success" });
            await Promise.all([this.loadTaskManagement({ silent: true }), this.loadTaskSummary()]);
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "The task could not be created.", { type: "danger" });
        } finally {
            this.state.taskBusy = false;
        }
    }

    get sidebarMore() {
        return this.isBranchManager ? BRANCH_SIDEBAR_MORE : SIDEBAR_MORE;
    }

    /** The manager's branch as the server pinned it (res.users.strategic_branch_warehouse_id); never chosen in the browser. */
    get managerBranch() {
        if (!this.isBranchManager || this.branchError) return null;
        if (this.state.navContext?.role) return this.state.navContext.branch || null;
        const data = this.state.data;
        return data ? (data.branch_options || []).find((branch) => branch.id === data.selected_warehouse_id) || null : null;
    }

    get userName() {
        return user.name;
    }

    get reportingPeriodLabel() {
        const period = this.state.data?.period;
        return period ? `${this.formatPurchaseDate(period.start)} – ${this.formatPurchaseDate(period.end)}` : "";
    }

    openNotifications() {
        return this.action.doAction("mail.action_discuss");
    }

    get avatarUrl() {
        return `/web/image/res.users/${user.userId}/avatar_128`;
    }

    /** [start, end] of a period preset as date inputs, ending today at the latest; null for custom dates. */
    presetRange(preset, today = new Date()) {
        const year = today.getFullYear();
        const month = today.getMonth();
        const range = {
            this_month: [new Date(year, month, 1), today],
            last_month: [new Date(year, month - 1, 1), new Date(year, month, 0)],
            last_3_months: [new Date(year, month - 2, 1), today],
            this_year: [new Date(year, 0, 1), today],
        }[preset];
        return range ? range.map((date) => this.asDateInput(date)) : null;
    }

    /** Daily buckets up to about a month, weekly up to about four months, monthly beyond. */
    granularityFor(start, end) {
        const days = (new Date(`${end}T12:00:00`) - new Date(`${start}T12:00:00`)) / 86400000 + 1;
        return days <= 45 ? "day" : days <= 125 ? "week" : "month";
    }

    onPeriodPresetChanged(event) {
        const preset = event.target.value;
        if (!PERIOD_PRESETS.some((option) => option.key === preset)) {
            return;
        }
        this.state.periodPreset = preset;
        const range = this.presetRange(preset);
        if (!range) {
            return; // Custom dates: the date fields appear and load on change.
        }
        [this.state.dateStart, this.state.dateEnd] = range;
        this.state.trendGranularity = this.granularityFor(...range);
        return this.loadOverview();
    }

    onCustomDateChanged(which, event) {
        const start = which === "start" ? event.target.value : this.state.dateStart;
        const end = which === "end" ? event.target.value : this.state.dateEnd;
        if (start && end && start <= end) {
            this.state.trendGranularity = this.granularityFor(start, end);
        }
        return which === "start" ? this.onStartDateChanged(event) : this.onEndDateChanged(event);
    }

    get periodPhrase() {
        return {
            this_month: "this month", last_month: "last month", last_3_months: "over the last three months", this_year: "this year",
        }[this.state.periodPreset] || "in the selected period";
    }

    get trendCaption() {
        const unit = { day: "Daily", week: "Weekly", month: "Monthly" }[this.state.trendGranularity] || "";
        return `${unit} · ${this.formatDate(this.state.dateStart)} – ${this.formatDate(this.state.dateEnd)}`;
    }

    /** Change versus the previous equal period, or null when no truthful comparison exists (missing data, zero base). */
    changeInfo(current, previous) {
        const missing = (value) => value === false || value === null || value === undefined || !Number.isFinite(Number(value));
        if (missing(current) || missing(previous) || Number(previous) === 0) {
            return null;
        }
        const change = ((Number(current) - Number(previous)) / Math.abs(Number(previous))) * 100;
        return { up: change >= 0, text: `${change >= 0 ? "+" : ""}${change.toLocaleString("en-US", { maximumFractionDigits: 1 })}%` };
    }

    /** Small trend line of one series of the sales trend (sales or profit); null unless two real points exist. */
    sparkline(key) {
        const values = (this.state.data?.sales_trend || [])
            .map((point) => point[key])
            .filter((value) => value !== undefined && value !== null && value !== false && Number.isFinite(Number(value)))
            .map(Number);
        if (values.length < 2) {
            return null;
        }
        const low = Math.min(...values);
        const span = Math.max(...values) - low || 1;
        const line = values.map((value, index) => `${((index * 100) / (values.length - 1)).toFixed(1)},${(34 - ((value - low) / span) * 28).toFixed(1)}`).join(" ");
        return { line, area: `0,40 ${line} 100,40` };
    }

    /** Round axis bounds and step covering [low, high]. */
    niceScale(low, high) {
        const span = high - low || Math.abs(high) || 1;
        const rough = span / 4;
        const magnitude = 10 ** Math.floor(Math.log10(rough));
        const step = [1, 2, 2.5, 5, 10].find((factor) => factor * magnitude >= rough) * magnitude;
        return { low: Math.floor(low / step) * step, high: Math.max(step, Math.ceil(high / step) * step), step };
    }

    axisTicks(scale, y) {
        const ticks = [];
        for (let value = scale.low; value <= scale.high + scale.step / 2; value += scale.step) {
            ticks.push({ key: String(value), y: y(value), label: this.formatCompactCurrency(value) });
        }
        return ticks;
    }

    /** Sales over the selected period from the existing sales trend; null when no POS order was completed. */
    get salesTrendChart() {
        const data = this.state.data;
        const points = data?.sales_trend || [];
        if (!points.length || !data.order_count) {
            return null;
        }
        const [left, right, top, bottom] = [58, 620, 12, 178];
        const values = points.map((point) => Number(point.sales) || 0);
        const scale = this.niceScale(Math.min(0, ...values), Math.max(0, ...values));
        const y = (value) => bottom - ((value - scale.low) / (scale.high - scale.low)) * (bottom - top);
        const x = (index) => (points.length === 1 ? (left + right) / 2 : left + (index * (right - left)) / (points.length - 1));
        const last = points.length - 1;
        const step = Math.max(1, Math.ceil(points.length / 8));
        const dots = points.map((point, index) => ({
            key: point.month_key || String(index), x: x(index), y: y(values[index]),
            title: `${point.label || ""}: ${this.formatCurrency(values[index])}`,
            label: index === last || (index % step === 0 && last - index >= step / 2) ? point.label : "",
        }));
        const line = dots.map((dot) => `${dot.x.toFixed(1)},${dot.y.toFixed(1)}`).join(" ");
        const base = y(Math.max(scale.low, 0)).toFixed(1);
        return {
            dots, line, ticks: this.axisTicks(scale, y), left, right, bottom,
            area: `${dots[0].x.toFixed(1)},${base} ${line} ${dots[last].x.toFixed(1)},${base}`,
        };
    }

    /** Sales per branch from the existing branch performance (already limited to the user's branches). */
    get branchChart() {
        const rows = this.state.data?.branch_performance || [];
        if (!rows.length) {
            return null;
        }
        const [left, right, top, bottom] = [52, 620, 22, 176];
        const values = rows.map((row) => Number(row.sales) || 0);
        const scale = this.niceScale(0, Math.max(0, ...values));
        const y = (value) => bottom - ((Math.max(value, 0) - scale.low) / (scale.high - scale.low)) * (bottom - top);
        const slot = (right - left) / rows.length;
        const width = Math.min(70, slot * 0.58);
        const bars = rows.map((row, index) => {
            const center = left + slot * (index + 0.5);
            const barTop = y(values[index]);
            const name = row.name || "";
            return {
                key: String(row.branch_id || index), x: center - width / 2, center, y: barTop, width, height: Math.max(0, bottom - barTop),
                color: BAR_COLORS[index % BAR_COLORS.length], value: this.formatCompactCurrency(values[index]),
                name: name.length > 16 ? `${name.slice(0, 15)}…` : name, title: `${name}: ${this.formatCurrency(values[index])}`,
            };
        });
        return { bars, ticks: this.axisTicks(scale, y), left, right, bottom };
    }

    /** Active goals on track among those with an evaluated result; goals without a result are reported apart. */
    get goalSummary() {
        const counts = this.state.overviewExtras?.strategy?.objectives;
        if (!counts) {
            return null;
        }
        const onTrack = counts.on_track || 0;
        const evaluated = onTrack + (counts.at_risk || 0);
        return { onTrack, evaluated, notEvaluated: counts.not_evaluated || 0, pct: evaluated ? Math.round((onTrack * 100) / evaluated) : null };
    }

    ringDash(pct, radius) {
        const circumference = 2 * Math.PI * radius;
        return `${((circumference * (pct || 0)) / 100).toFixed(1)} ${circumference.toFixed(1)}`;
    }

    /** The three most important alerts (already sorted by severity on the server), without duplicates. */
    get topAttention() {
        const seen = new Set();
        return (this.state.overviewExtras?.attention || []).filter((alert) => {
            const key = `${alert.title}|${alert.branch || ""}|${alert.evidence || ""}`;
            if (seen.has(key)) {
                return false;
            }
            seen.add(key);
            return true;
        }).slice(0, 3);
    }

    attentionActionLabel(alert) {
        return { Inventory: "View Stock", Branches: "View Branch", People: "View Details" }[alert.source] || "View";
    }

    openAttention(alert) {
        if (alert.res_model) {
            return this.openIssueRecord(alert);
        }
        return this.selectSection({ Inventory: "inventory_waste", Branches: "branches", People: "people" }[alert.source] || "tasks_issues");
    }

    get activeItem() {
        return this.items.find((item) => item.id === this.state.activeSection) || this.items[0];
    }

    get branchSelectValue() {
        const id = this.state.data?.selected_warehouse_id;
        return id ? String(id) : this.state.warehouseId;
    }

    async loadInventory({ silent = false } = {}) {
        if (this.state.dateStart && this.state.dateEnd && this.state.dateStart > this.state.dateEnd) {
            this.state.inventoryError = "The start date must be on or before the end date.";
            this.state.inventoryData = { available: false, messages: [this.state.inventoryError] };
            return;
        }
        // Ignore responses that arrive after a newer request (filter change or live refresh).
        const requestId = this.inventoryRequestId = (this.inventoryRequestId || 0) + 1;
        if (!silent) {
            this.state.inventoryLoading = true;
            this.state.inventoryError = "";
        }
        try {
            const selectedWarehouse = this.state.warehouseId === "all" ? false : Number(this.state.warehouseId);
            const selectedProduct = this.state.inventoryProductId && this.state.inventoryProductId !== "all"
                ? Number(this.state.inventoryProductId)
                : false;
            const response = await this.orm.silent.call(
                "strategic.performance.dashboard",
                "get_inventory_waste_data",
                [this.state.dateStart || false, this.state.dateEnd || false, selectedWarehouse, selectedProduct]
            );
            if (requestId !== this.inventoryRequestId) {
                return;
            }
            this.state.inventoryData = response;
            const returnedProductId = response && response.selected_product_id ? String(response.selected_product_id) : "all";
            if ((this.state.inventoryProductId || "all") !== returnedProductId) {
                this.state.inventoryProductId = returnedProductId;
            }
        } catch (error) {
            // A failed background refresh keeps the last good figures on screen.
            if (silent || requestId !== this.inventoryRequestId) {
                console.error("Inventory data could not be refreshed", error);
                return;
            }
            this.state.inventoryError = error.message || "Inventory data could not be loaded.";
            this.state.inventoryData = { available: false, messages: [this.state.inventoryError] };
            this.notification.add(this.state.inventoryError, { type: "danger" });
        } finally {
            if (requestId === this.inventoryRequestId) {
                this.state.inventoryLoading = false;
            }
        }
    }

    async loadAppDependencies({ silent = false } = {}) {
        if (!silent) {
            this.state.appDependenciesLoading = true;
            this.state.appDependenciesError = "";
        }
        try {
            this.state.appDependenciesData = await this.orm.silent.call(
                "strategic.performance.dashboard",
                "get_app_dependencies_data",
                []
            );
            this.state.appDependenciesError = "";
        } catch (error) {
            if (silent) {
                console.warn("App dependency status could not be refreshed", error);
                return;
            }
            this.state.appDependenciesError = error.message || "App dependency status could not be loaded.";
        } finally {
            if (!silent) {
                this.state.appDependenciesLoading = false;
            }
        }
    }

    toggleAppDependency(id) {
        this.state.expandedDependencyId = this.state.expandedDependencyId === id ? "" : id;
    }

    onInventoryProductChanged(event) {
        this.state.inventoryProductId = event.target.value || "all";
        return this.loadInventory();
    }

    formatInventoryCurrency(value) {
        if (value === false || value === null || value === undefined || !Number.isFinite(Number(value))) {
            return "Not available";
        }
        const currency = this.state.inventoryData && this.state.inventoryData.currency
            ? this.state.inventoryData.currency
            : { name: "USD", symbol: "$", position: "before", decimal_places: 2 };
        const digits = Number.isInteger(Number(currency.decimal_places))
            ? Math.max(0, Math.min(4, Number(currency.decimal_places)))
            : 2;
        const formatted = Number(value).toLocaleString("en-US", {
            minimumFractionDigits: digits,
            maximumFractionDigits: digits,
        });
        const symbol = currency.symbol || currency.name || "";
        return currency.position === "after"
            ? (formatted + (symbol ? " " + symbol : ""))
            : (symbol + formatted);
    }

    formatInventoryQuantity(value, unit) {
        if (value === false || value === null || value === undefined || !Number.isFinite(Number(value))) {
            return "Not available";
        }
        const formatted = Number(value).toLocaleString("en-US", { maximumFractionDigits: 3 });
        return (unit ? formatted + " " + unit : formatted);
    }

    inventoryMetricValue(value) {
        if (value === false || value === null || value === undefined || !Number.isFinite(Number(value))) {
            return "Not available";
        }
        return Number(value).toLocaleString("en-US", { maximumFractionDigits: 0 });
    }

    pageCount(rows) {
        return Math.max(1, Math.ceil((rows || []).length / PAGE_SIZE));
    }

    pageOf(key, rows) {
        // Clamped so a filter change that shrinks the list never leaves an empty page.
        return Math.min(this.state.pages[key] || 1, this.pageCount(rows));
    }

    pageRows(key, rows) {
        const page = this.pageOf(key, rows);
        return (rows || []).slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
    }

    setPage(key, page) {
        this.state.pages[key] = page;
    }

    inventoryWasteWidth(waste) {
        const rows = (this.state.inventoryData && this.state.inventoryData.waste_by_product) || [];
        const max = rows.reduce((top, row) => Math.max(top, Number(row.cost) || 0), 0);
        return max > 0 ? Math.max(0, Number(waste.cost) || 0) / max * 100 : 0;
    }

    inventoryColor(index) {
        return CHART_COLORS[index % CHART_COLORS.length];
    }

    inventoryStockWidth(item) {
        const minimum = Math.max(0, Number(item && item.minimum_quantity) || 0);
        const available = Math.max(0, Number(item && item.available_quantity) || 0);
        if (!minimum) {
            return 0;
        }
        return Math.max(0, Math.min(100, (available / minimum) * 100));
    }

    inventoryBranchWidth(branch) {
        const rows = (this.state.inventoryData && this.state.inventoryData.branch_comparison) || [];
        const max = rows.reduce((top, row) => Math.max(top, Number(row.low_stock_count) || 0), 0);
        return max > 0 ? Math.max(0, Number(branch.low_stock_count) || 0) / max * 100 : 0;
    }

    get inventoryExpiredLotCount() {
        const lots = (this.state.inventoryData && this.state.inventoryData.expiring_stock) || [];
        return lots.filter((lot) => lot.days_left < 0).length;
    }
    async loadPurchasing({ silent = false } = {}) {
        // Ignore responses that arrive after a newer request (filter change or live refresh).
        const requestId = ++this.purchasingRequestId;
        if (!silent) {
            this.state.purchasingLoading = true;
        }
        try {
            const selectedWarehouse = this.state.warehouseId === "all" ? false : Number(this.state.warehouseId);
            const data = await this.orm.silent.call(
                "strategic.performance.dashboard",
                "get_purchasing_overview_data",
                [this.state.dateStart || false, this.state.dateEnd || false, selectedWarehouse]
            );
            if (requestId === this.purchasingRequestId) {
                this.state.purchasingData = data;
            }
        } catch (purchasingError) {
            // A failed background refresh keeps the last good figures on screen.
            if (requestId === this.purchasingRequestId && !silent) {
                this.state.purchasingData = null;
            }
            console.error("Purchasing dashboard data could not be loaded", purchasingError);
        } finally {
            if (requestId === this.purchasingRequestId) {
                this.state.purchasingLoading = false;
            }
        }
    }

    async loadBranches({ silent = false } = {}) {
        if (this.isBranchManager) {
            this.loadBranchOps({ silent });
        }
        // Ignore responses that arrive after a newer request (filter change or live refresh).
        const requestId = ++this.branchesRequestId;
        if (!silent) {
            this.state.branchesLoading = true;
        }
        try {
            const selectedWarehouse = this.state.warehouseId === "all" ? false : Number(this.state.warehouseId);
            const data = await this.orm.silent.call(
                "strategic.performance.dashboard",
                "get_branches_overview_data",
                [this.state.dateStart || false, this.state.dateEnd || false, selectedWarehouse]
            );
            if (requestId === this.branchesRequestId) {
                this.state.branchesData = data;
            }
        } catch (branchesError) {
            // A failed background refresh keeps the last good figures on screen.
            if (requestId === this.branchesRequestId && !silent) {
                this.state.branchesData = { available: false, messages: [branchesError.data?.message || branchesError.message || "Branch data could not be loaded."] };
            }
            console.error("Branches dashboard data could not be loaded", branchesError);
        } finally {
            if (requestId === this.branchesRequestId) {
                this.state.branchesLoading = false;
            }
        }
    }

    /** Tasks & Issues reuses the Overview attention list and the People task data. */
    loadTasksIssues(options = {}) {
        return Promise.all([this.loadOverviewExtras(options), this.loadPeople(options)]);
    }

    issueCount(severity) {
        const items = this.state.overviewExtras?.attention;
        if (!items) return "—";
        return items.filter((item) => (severity === "critical") === (item.severity === "critical")).length;
    }

    openTaskCount() {
        const rows = this.state.peopleData?.workload;
        return rows ? rows.reduce((sum, row) => sum + row.open, 0) : "—";
    }

    sectionLinks(sectionId) {
        const role = this.state.data?.role || "employee";
        return (SECTION_LINKS[sectionId] || []).filter((link) => link.roles.includes(role));
    }

    /** Open the Odoo record behind a dashboard issue. */
    // "Goals in this area": the same strategic.objective records Business Goals shows.
    async loadAreaGoals(area) {
        try {
            this.state.areaGoals[area] = await this.orm.silent.call("strategic.goal.setup", "get_goals", [], { area });
        } catch (error) {
            console.warn("Area goals could not be loaded", error);
            this.state.areaGoals[area] = [];
        }
    }

    openObjective(objectiveId) {
        return this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "strategic.objective",
            res_id: objectiveId,
            views: [[false, "form"]],
            target: "current",
        });
    }

    async openIssueRecord(item) {
        try {
            await this.action.doAction({
                type: "ir.actions.act_window",
                res_model: item.res_model,
                res_id: item.res_id,
                views: [[false, "form"]],
                target: "current",
            });
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "This record is not available with your access rights.", { type: "warning" });
        }
    }

    /** Assign a follow-up activity on the issue's record; live refresh then updates its status. */
    async createIssueFollowup(item) {
        try {
            const result = await this.orm.call(
                "strategic.performance.dashboard",
                "create_attention_followup",
                [item.res_model, item.res_id, item.title, item.evidence || false, item.deadline || false]
            );
            const people = (result.assigned_to || []).join(", ") || "the responsible manager";
            this.notification.add(
                result.created ? `Follow-up assigned to ${people}.` : `A follow-up is already open for ${people}.`,
                { type: result.created ? "success" : "info" }
            );
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "The follow-up could not be created.", { type: "danger" });
        }
    }

    async openAction(xmlid) {
        try {
            await this.action.doAction(xmlid);
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "This screen is not available with your access rights.", { type: "warning" });
        }
    }

    // ------------------------------------------------------------ objective requests (Overview)
    async loadObjectiveRequests({ silent = false } = {}) {
        const requestId = this.requestsRequestId = (this.requestsRequestId || 0) + 1;
        if (!silent) {
            this.state.requestsLoading = true;
        }
        try {
            const selectedWarehouse = this.state.warehouseId === "all" ? false : Number(this.state.warehouseId);
            const data = await this.orm.silent.call("strategic.performance.dashboard", "get_objective_requests", [selectedWarehouse]);
            if (requestId === this.requestsRequestId) {
                this.state.requestsData = data;
            }
        } catch (error) {
            if (!silent && requestId === this.requestsRequestId) {
                this.state.requestsData = { available: false, pending: [], history: [], messages: [error.data?.message || error.message || "Objective requests could not be loaded."] };
            }
            console.error("Objective requests could not be loaded", error);
        } finally {
            if (requestId === this.requestsRequestId) {
                this.state.requestsLoading = false;
            }
        }
    }

    toggleRequests(key) {
        this.state.reqExpanded[key] = !this.state.reqExpanded[key];
    }

    get filteredRequestHistory() {
        const rows = this.state.requestsData?.history || [];
        return this.state.historyStatus === "all" ? rows : rows.filter((row) => row.decision === this.state.historyStatus);
    }

    onHistoryStatusChanged(event) {
        this.state.historyStatus = event.target.value;
        this.state.pages.req_history = 0;
    }

    // Pending rows are keyed by objective, history rows by decision ("p:12" / "h:7"), so the two never collide.
    get reviewRequest() {
        const key = this.state.reviewRequestId;
        const data = this.state.requestsData;
        if (!key || !data) return null;
        const id = Number(key.slice(2));
        return key.startsWith("p:")
            ? (data.pending || []).find((row) => row.objective_id === id) || null
            : (data.history || []).find((row) => row.review_id === id) || null;
    }

    openRequestReview(row) {
        this.state.reviewRequestId = row.review_id ? `h:${row.review_id}` : `p:${row.objective_id}`;
    }

    closeRequestReview() {
        this.state.reviewRequestId = null;
    }

    openRejectRequest(row) {
        this.state.rejectRequestId = row.objective_id;
        this.state.rejectReason = "";
    }

    closeRejectRequest() {
        if (!this.state.deciding) {
            this.state.rejectRequestId = null;
        }
    }

    get rejectRequest() {
        return (this.state.requestsData?.pending || []).find((row) => row.objective_id === this.state.rejectRequestId) || null;
    }

    onRejectReasonInput(event) {
        this.state.rejectReason = event.target.value;
    }

    /** Approve or reject on the server; the lists refresh from its answer, so nothing changes before it confirms. */
    async decideRequest(row, decision) {
        if (this.state.deciding) return;
        const reason = decision === "reject" ? this.state.rejectReason.trim() : false;
        if (decision === "reject" && !reason) {
            this.notification.add("Enter a reason for rejecting this objective.", { type: "warning" });
            return;
        }
        this.state.deciding = true;
        try {
            const selectedWarehouse = this.state.warehouseId === "all" ? false : Number(this.state.warehouseId);
            const data = await this.orm.call("strategic.performance.dashboard", "decide_objective_request", [row.objective_id, decision, reason, selectedWarehouse]);
            this.state.requestsData = data;
            this.state.rejectRequestId = null;
            this.state.reviewRequestId = null;
            this.notification.add(`"${row.name}" ${decision === "approve" ? "approved and activated" : "rejected"}.`, { type: "success" });
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "The decision could not be saved.", { type: "danger" });
            await this.loadObjectiveRequests({ silent: true });
        } finally {
            this.state.deciding = false;
        }
    }

    async openRequestObjective(objectiveId) {
        try {
            await this.action.doAction({
                type: "ir.actions.act_window",
                res_model: "strategic.objective",
                res_id: objectiveId,
                views: [[false, "form"]],
                target: "current",
            });
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "This objective is not available with your access rights.", { type: "warning" });
        }
    }

    /** Server datetimes are UTC ("2026-10-27 14:55:58"); show the local calendar date. */
    formatRequestDate(value) {
        if (!value) return "—";
        const date = new Date(String(value).replace(" ", "T") + (String(value).length > 10 ? "Z" : "T00:00:00"));
        return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
    }

    async loadOverviewExtras({ silent = false } = {}) {
        // Comparisons, section summaries, strategy, alerts and activity for the Overview.
        const requestId = this.overviewExtrasRequestId = (this.overviewExtrasRequestId || 0) + 1;
        try {
            const selectedWarehouse = this.state.warehouseId === "all" ? false : Number(this.state.warehouseId);
            const data = await this.orm.silent.call(
                "strategic.performance.dashboard",
                "get_overview_extras",
                [this.state.dateStart || false, this.state.dateEnd || false, selectedWarehouse]
            );
            if (requestId === this.overviewExtrasRequestId) {
                this.state.overviewExtras = data;
            }
        } catch (error) {
            if (!silent && requestId === this.overviewExtrasRequestId) {
                this.state.overviewExtras = null;
            }
            console.error("Overview sections could not be loaded", error);
        }
    }

    /** "+12.5% vs previous period", or why no comparison can be made. */
    comparisonText(current, previous) {
        const missing = (value) => value === false || value === null || value === undefined;
        if (missing(current) || missing(previous)) return "No comparison available";
        const now = Number(current);
        const before = Number(previous);
        if (before === 0) return now === 0 ? "No change vs previous period" : "Previous period: 0";
        const change = ((now - before) / Math.abs(before)) * 100;
        return `${change >= 0 ? "+" : ""}${change.toLocaleString("en-US", { maximumFractionDigits: 1 })}% vs previous period`;
    }

    statusShare(counts, key) {
        const total = Object.values(counts || {}).reduce((sum, value) => sum + (Number(value) || 0), 0);
        return total ? ((Number(counts[key]) || 0) / total) * 100 : 0;
    }

    async loadPeople({ silent = false } = {}) {
        // Ignore responses that arrive after a newer request (filter change or live refresh).
        const requestId = ++this.peopleRequestId;
        if (!silent) {
            this.state.peopleLoading = true;
        }
        try {
            const selectedWarehouse = this.state.warehouseId === "all" ? false : Number(this.state.warehouseId);
            const data = await this.orm.silent.call(
                "strategic.performance.dashboard",
                "get_people_overview_data",
                [this.state.dateStart || false, this.state.dateEnd || false, selectedWarehouse]
            );
            if (requestId === this.peopleRequestId) {
                this.state.peopleData = data;
            }
        } catch (peopleError) {
            // A failed background refresh keeps the last good figures on screen.
            if (requestId === this.peopleRequestId && !silent) {
                this.state.peopleData = { messages: [peopleError.data?.message || peopleError.message || "People data could not be loaded."] };
            }
            console.error("People dashboard data could not be loaded", peopleError);
        } finally {
            if (requestId === this.peopleRequestId) {
                this.state.peopleLoading = false;
            }
        }
    }

    peopleBarWidth(value, rows, ...fields) {
        const max = Math.max(1, ...(rows || []).flatMap((row) => fields.map((field) => Number(row[field]) || 0)));
        return Math.max(0, Math.min(100, ((Number(value) || 0) / max) * 100));
    }

    peopleStaffingClass(status) {
        return { covered: "o_sm_people_fill_green", understaffed: "o_sm_people_fill_red" }[status] || "o_sm_people_fill_amber";
    }

    peopleTaskClass(status) {
        return { Overdue: "o_sm_people_task_open", "Due today": "o_sm_people_task_progress", Done: "o_sm_people_coverage_good" }[status] || "o_sm_people_badge_low";
    }

    /** Rows for one branch comparison chart; missing values render as "Not Evaluated". */
    branchChartRows(field) {
        const rows = this.state.branchesData?.branches || [];
        const isMissing = (value) => value === false || value === null || value === undefined;
        const values = rows.map((row) => (isMissing(row[field]) ? 0 : Math.abs(Number(row[field]) || 0)));
        const max = Math.max(1, ...values);
        return rows.map((row, index) => ({
            id: row.warehouse_id,
            name: row.name,
            value: row[field],
            width: Math.min(100, (values[index] / max) * 100),
        }));
    }

    formatBranchValue(value, kind) {
        if (value === false || value === null || value === undefined) return "Not Evaluated";
        if (kind === "currency") {
            const currency = this.state.branchesData?.currency || {};
            const formatted = Number(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
            const symbol = currency.symbol || "";
            return currency.position === "after" ? `${formatted} ${symbol}`.trim() : `${symbol}${formatted}`;
        }
        if (kind === "percent") return `${Number(value).toLocaleString("en-US", { maximumFractionDigits: 1 })}%`;
        return Number(value).toLocaleString("en-US", { maximumFractionDigits: 2 });
    }

    toggleBranches(key) {
        this.state.brExpanded[key] = !this.state.brExpanded[key];
    }

    /** Sales chart rows, largest first; branches without readable sales stay listed as "Not Evaluated". */
    get branchSalesRows() {
        const missing = (value) => value === false || value === null || value === undefined;
        return this.branchChartRows("sales").sort((a, b) => (missing(b.value) ? -1 : b.value) - (missing(a.value) ? -1 : a.value));
    }

    /** Attainment is already a percentage of target: draw it on a 0-100 scale, not relative to the best branch. */
    branchAttainmentWidth(row) {
        return row.attainment === false || row.attainment === null || row.attainment === undefined ? 0 : Math.max(0, Math.min(100, Number(row.attainment) || 0));
    }

    /** First sentence of a data note; the full text stays one click away. */
    branchNoteHeadline(message) {
        const text = message || "";
        const cut = text.search(/\.(\s|$)/);
        return cut > 0 ? text.slice(0, cut + 1) : text;
    }

    scrollToBranchTable() {
        const table = document.querySelector(".o_sm_br_table_panel");
        if (table) {
            table.scrollIntoView({ behavior: "smooth", block: "start" });
        }
    }

    branchSeverityClass(severity) {
        return severity === "critical" ? "o_sm_br_issue_red" : "o_sm_br_issue_amber";
    }

    formatPurchaseCurrency(value) {
        if (value === false || value === null || value === undefined || !Number.isFinite(Number(value))) {
            return "—";
        }
        const currency = this.state.purchasingData?.currency;
        const digits = Number.isInteger(currency?.decimal_places) ? currency.decimal_places : 2;
        const symbol = currency?.symbol || currency?.name || "";
        const formatted = Number(value).toLocaleString("en-US", {
            minimumFractionDigits: digits,
            maximumFractionDigits: digits,
        });
        return currency?.position === "after" ? `${formatted} ${symbol}`.trim() : `${symbol}${formatted}`;
    }

    formatPurchaseDate(value) {
        if (!value) return "—";
        const date = new Date(`${String(value).slice(0, 10)}T12:00:00`);
        return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
    }

    formatPurchaseRate(value) {
        return value === false || value === null || value === undefined ? "Not available" : `${Number(value).toLocaleString("en-US", { maximumFractionDigits: 1 })}%`;
    }

    purchasingTrendMax() {
        const rows = this.state.purchasingData?.expenditure_trend || [];
        const max = Math.max(0, ...rows.map((row) => Number(row.current) || 0), ...rows.map((row) => Number(row.previous) || 0));
        if (max <= 0) return 1;
        const magnitude = 10 ** Math.floor(Math.log10(max));
        return Math.ceil(max / magnitude) * magnitude;
    }

    purchasingTrendX(index, count) {
        return count <= 1 ? 300 : index * (600 / (count - 1));
    }

    purchasingTrendY(value) {
        return 178 - ((Number(value) || 0) / this.purchasingTrendMax()) * 160;
    }

    purchasingTrendPoints(series) {
        const rows = this.state.purchasingData?.expenditure_trend || [];
        return rows.map((row, index) => `${this.purchasingTrendX(index, rows.length)},${this.purchasingTrendY(row[series])}`).join(" ");
    }

    purchasingCategoryDonutStyle() {
        const rows = this.state.purchasingData?.purchase_categories || [];
        const colors = PURCHASE_COLORS;
        const total = rows.reduce((sum, row) => sum + Math.max(0, Number(row.value) || 0), 0);
        if (!total) return "background: #e7e9ed";
        let offset = 0;
        const parts = rows.map((row, index) => {
            const share = Math.max(0, Number(row.value) || 0) / total * 100;
            const part = `${colors[index % colors.length]} ${offset}% ${offset + share}%`;
            offset += share;
            return part;
        });
        return `background: conic-gradient(${parts.join(", ")})`;
    }

    purchasingCategoryColor(index) {
        return PURCHASE_COLORS[index % PURCHASE_COLORS.length];
    }

    purchasingBarWidth(value, maximum) {
        const max = Math.max(1, Number(maximum) || 0);
        return Math.max(0, Math.min(100, (Number(value) || 0) / max * 100));
    }

    purchaseSupplierBarClass(rate) {
        const value = Number(rate) || 0;
        if (value >= 85) return "o_sm_pur_bar_good";
        if (value >= 65) return "o_sm_pur_bar_medium";
        return "o_sm_pur_bar_bad";
    }

    toggleInventory(key) {
        this.state.invExpanded[key] = !this.state.invExpanded[key];
    }

    togglePurchasing(key) {
        this.state.purExpanded[key] = !this.state.purExpanded[key];
    }

    /** Purchasing goals grouped by the status tone the Business Goals page already gives them; null while loading. */
    get purchasingGoalSummary() {
        const rows = this.state.areaGoals.purchasing;
        if (!rows) {
            return null;
        }
        const count = (tones) => rows.filter((goal) => tones.includes(goal.tone)).length;
        return { total: rows.length, onTrack: count(["good"]), attention: count(["warn", "bad"]), other: count(["neutral"]) };
    }

    /** Head Office only: Odoo's standard Purchase screens are not branch-restricted by this module (see SECTION_LINKS). */
    get canOpenPurchaseOrders() {
        return this.state.data?.role === "head_office";
    }

    openPurchaseOrder(order) {
        return this.action.doAction(order.id ? {
            type: "ir.actions.act_window", res_model: "purchase.order", res_id: order.id, views: [[false, "form"]], target: "current",
        } : {
            type: "ir.actions.act_window", name: order.name, res_model: "purchase.order", domain: [["name", "=", order.name]],
            views: [[false, "list"], [false, "form"]], target: "current",
        });
    }

    purchaseStatusClass(status) {
        return status || "o_sm_pur_status_info";
    }
    formatCurrency(value) {
        if (value === false || value === null || value === undefined || !Number.isFinite(Number(value))) {
            return "—";
        }
        const currency = this.state.data?.currency || {};
        const digits = Number.isInteger(currency.decimal_places)
            ? Math.max(0, Math.min(6, currency.decimal_places))
            : 2;
        const symbol = currency.symbol || currency.name || "";
        const formatted = Number(value).toLocaleString("en-US", {
            minimumFractionDigits: digits,
            maximumFractionDigits: digits,
        });
        if (!symbol) return formatted;
        return currency.position === "after" ? `${formatted} ${symbol}` : `${symbol}${formatted}`;
    }

    formatNumber(value) {
        if (value === false || value === null || value === undefined || !Number.isFinite(Number(value))) {
            return "—";
        }
        return Number(value).toLocaleString("en-NP", { maximumFractionDigits: 1 });
    }

    averageOrderValue() {
        const data = this.state.data;
        if (!data || data.sales === false || data.sales === null ||
            data.sales === undefined) {
            return false;
        }

        const sales = Number(data.sales);
        const count = Number(data.order_count);

        if (!Number.isFinite(sales) || !Number.isFinite(count) || count <= 0) {
            return false;
        }

        return sales / count;
    }

    salesTrendMax() {
        const values = (this.state.data?.sales_trend || [])
            .map((point) => Number(point.sales) || 0);
        const maximum = Math.max(0, ...values);

        if (maximum <= 0) {
            return 1;
        }

        const magnitude = 10 ** Math.floor(Math.log10(maximum));
        return Math.ceil(maximum / magnitude) * magnitude;
    }

    formatCompactCurrency(value) {
        if (value === false || value === null || value === undefined || !Number.isFinite(Number(value))) {
            return "—";
        }
        const number = Number(value);
        const absolute = Math.abs(number);
        const currency = this.state.data?.currency || {};
        const symbol = currency.symbol || currency.name || "";
        const before = currency.position !== "after";
        const compact = (amount, unit) => {
            const formatted = Number(amount).toLocaleString("en-US", { maximumFractionDigits: 1 });
            const valueWithUnit = `${formatted}${unit}`;
            if (!symbol) return valueWithUnit;
            return before ? `${symbol}${valueWithUnit}` : `${valueWithUnit} ${symbol}`;
        };
        if (absolute >= 1000000) return compact(number / 1000000, "M");
        if (absolute >= 1000) return compact(number / 1000, "K");
        return this.formatCurrency(number);
    }

    salesTrendX(index, count) {
        return count <= 1 ? 319 : 48 + index * (542 / (count - 1));
    }

    salesTrendY(value) {
        const number = Math.max(0, Number(value) || 0);
        return 160 - (number / this.salesTrendMax()) * 125;
    }

    salesTrendPoints() {
        const points = this.state.data?.sales_trend || [];
        return points.map((point, index) =>
            `${this.salesTrendX(index, points.length)},${this.salesTrendY(point.sales)}`
        ).join(" ");
    }
    trendLabelStep() {
        const count = this.state.data?.sales_trend?.length || 0;
        return Math.max(1, Math.ceil(count / 8));
    }

    formatDate(value) {
        if (!value) return "—";
        const date = new Date(`${String(value).slice(0, 10)}T12:00:00`);
        return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString(undefined, { day: "numeric", month: "short" });
    }

    chartX(index, count) {
        if (count <= 1) return 40;
        return 40 + (index * 440) / (count - 1);
    }

    chartY(value, key) {
        const points = this.state.data?.sales_trend || [];
        const numericValues = points.map((point) => Number(point[key]) || 0);
        const max = Math.max(1, ...numericValues, ...(key === "sales" ? [] : points.map((point) => Number(point.sales) || 0)));
        return 152 - ((Number(value) || 0) / max) * 122;
    }

    trendPoints(key) {
        const points = this.state.data?.sales_trend || [];
        return points
            .map((point, index) => ({ point, index }))
            .filter(({ point }) => point[key] !== false && point[key] !== null && point[key] !== undefined)
            .map(({ point, index }) => `${this.chartX(index, points.length)},${this.chartY(point[key], key)}`)
            .join(" ");
    }

    branchMetricValue(branch) {
        if (this.state.branchMetric === "orders") {
            return Number(branch.orders) || 0;
        }
        const sales = Number(branch.sales) || 0;
        if (this.state.branchMetric === "average_order_value") {
            const orders = Number(branch.orders) || 0;
            return orders > 0 ? sales / orders : false;
        }
        return sales;
    }

    formatBranchMetric(branch) {
        const value = this.branchMetricValue(branch);
        if (value === false) {
            return "—";
        }
        return this.state.branchMetric === "orders"
            ? this.formatNumber(value)
            : this.formatCurrency(value);
    }

    branchBarWidth(branch) {
        const rows = this.state.data?.branch_sales || [];
        const max = Math.max(1, ...rows.map((row) => Number(this.branchMetricValue(row)) || 0));
        const value = Number(this.branchMetricValue(branch)) || 0;
        return Math.max(0, Math.min(100, (value / max) * 100));
    }

    categoryTotal() {
        const rows = this.state.data?.sales_by_category || [];
        if (!rows.length) return false;
        return rows.reduce((sum, row) => sum + (Number(row.sales) || 0), 0);
    }

    categoryDonutStyle() {
        const rows = this.state.data?.sales_by_category || [];
        const positiveTotal = rows.reduce((sum, row) => sum + Math.max(0, Number(row.sales) || 0), 0);
        if (!positiveTotal) return "background: conic-gradient(#e1e6ec 0 100%)";
        let offset = 0;
        const segments = rows.filter((row) => Number(row.sales) > 0).map((row, index) => {
            const share = (Number(row.sales) / positiveTotal) * 100;
            const segment = `${CHART_COLORS[index % CHART_COLORS.length]} ${offset}% ${offset + share}%`;
            offset += share;
            return segment;
        });
        return `background: conic-gradient(${segments.join(", ")})`;
    }

    categoryColor(index) {
        return CHART_COLORS[index % CHART_COLORS.length];
    }
    salesHourMax() {
        const rows = this.state.data?.sales_by_hour || [];
        return Math.max(1, ...rows.map((row) => Number(row.sales) || 0));
    }

    salesHourBarHeight(value) {
        const ratio = (Number(value) || 0) / this.salesHourMax();
        return Math.max(0, Math.min(100, ratio * 100));
    }

    salesHourAxisLabel(fraction) {
        return this.formatNumber(this.salesHourMax() * fraction);
    }
    expenseBarWidth(value) {
        const rows = this.state.data?.expense_breakdown || [];
        const max = Math.max(1, ...rows.map((row) => Number(row.value) || 0));
        return Math.max(0, Math.min(100, ((Number(value) || 0) / max) * 100));
    }

    expenseDonutStyle() {
        const rows = this.state.data?.expense_breakdown || [];
        const total = rows.reduce((sum, row) => sum + Math.max(0, Number(row.value) || 0), 0);
        if (!total) return "background: #e7e9ed";
        let offset = 0;
        const segments = rows.map((row, index) => {
            const share = (Math.max(0, Number(row.value) || 0) / total) * 100;
            const segment = `${CHART_COLORS[index % CHART_COLORS.length]} ${offset}% ${offset + share}%`;
            offset += share;
            return segment;
        });
        return `background: conic-gradient(${segments.join(", ")})`;
    }

    expenseColor(index) {
        return CHART_COLORS[index % CHART_COLORS.length];
    }

    get expenseTotal() {
        return (this.state.data?.expense_breakdown || []).reduce((sum, row) => sum + (Number(row.value) || 0), 0);
    }

    get needsAttentionCount() {
        return (this.state.data?.alerts || []).length;
    }
}

registry.category("actions").add("strategic_management.navigation_shell", StrategicNavigationShell);
