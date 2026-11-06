/** @odoo-module **/

// Strategic Management screen: the sidebar, page switching, period and branch filters, live refresh and the
// helpers several pages share. Each page keeps its own functions in pages/<page>/<page>.js (imported below),
// its template in pages/<page>/<page>.xml and its styles in pages/<page>/<page>.scss.

import { Component, onMounted, onWillStart, onWillUnmount, proxy } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { user } from "@web/core/user";
import { useService } from "@web/core/utils/hooks";
import { NAVIGATION_ITEMS, SIDEBAR_PRIMARY, SIDEBAR_MORE, HEAD_OFFICE_PEOPLE_TABS, HEAD_OFFICE_SIDEBAR_MORE, BRANCH_SIDEBAR, ADMIN_SIDEBAR, EMPLOYEE_SIDEBAR, BRANCH_SIDEBAR_MORE, PERIOD_PRESETS, PAGE_SIZE, DATA_CHANGED, LIVE_REFRESH_DEBOUNCE_MS, LIVE_REFRESH_POLL_MS } from "./constants";
import { inboxPage } from "./pages/inbox/inbox";
import { salesPage } from "./pages/sales/sales";
import { purchasingPage } from "./pages/purchasing/purchasing";
import { inventoryPage } from "./pages/inventory/inventory";
import { branchesPage } from "./pages/branches/branches";
import { peoplePage } from "./pages/people/people";
import { employeesPage } from "./pages/employees/employees";
import { tasksPage } from "./pages/tasks/tasks";
import { reportsPage } from "./pages/reports/reports";
import { appDependenciesPage } from "./pages/app_dependencies/app_dependencies";
import { branchManagerPage } from "./pages/branch_manager/branch_manager";
import { employeeHomePage } from "./pages/employee_home/employee_home";
import { adminPage } from "./pages/admin/admin";
import { kpiRequestsPage } from "./pages/kpi_requests/kpi_requests";

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
            activeSection: "overview",
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
            // Branch Manager dashboard: objectives needing attention and Head Office instructions.
            branchObjectives: null,
            branchObjectivesLoading: false,
            branchObjectivesError: "",
            // Branch tasks: the user's own tasks, the manager's workspace, the dashboard summary and dialogs.
            myTasks: null,
            taskManagement: null,
            taskSummary: null,
            tasksLoading: false,
            tasksError: "",
            taskFilter: "open",
            taskDetailId: null,
            taskNote: "",
            taskPanel: null,
            taskForm: null,
            taskBusy: false,
            // Task Management narrowed to one employee (from Employee Management); cleared from the page or the sidebar.
            taskEmployee: null,
            // Employee Management: the branch's people with their task counts, and one person's tasks.
            employees: null,
            employeesLoading: false,
            employeeDepartment: "",
            employeesError: "",
            employeeDetail: null,
            employeeDetailLoading: false,
            employeeDetailError: "",
            // Branch operations: the manager's own branch sales, inventory and purchasing for the reporting period.
            branchOps: null,
            branchOpsLoading: false,
            branchOpsError: "",
            objectiveDeadlines: {},
            employeeContext: null,
            staffing: null,
            staffingError: "",
            staffingFilters: { warehouse_id: "", position: "", state: "" },
            staffingForm: null,
            staffingDecision: null,
            staffingRegister: null,
            staffingBusy: false,
            employeePerformance: null,
            employeePerformanceError: "",
            assessmentForm: null,
            performanceOverview: null,
            performanceOverviewError: "",
            myPerformance: null,
            myPerformanceError: "",
            // Its own period: tasks can be due later this month, which the business reports' "until today" would hide.
            myPerformanceStart: this.asDateInput(new Date(new Date().getFullYear(), new Date().getMonth() - 5, 1)),
            myPerformanceEnd: this.asDateInput(new Date(new Date().getFullYear(), new Date().getMonth() + 1, 0)),
            drillChoice: null,
            staffingWork: [],
            employeeWorkspace: null,
            employeeWorkspaceError: "",
            departmentProductId: false,
            departmentAssignments: null,
            departmentAssignmentsError: "",
            departmentForm: null,
            positionForm: null,
            departmentSearch: "",
            departmentSaving: false,
            opsTab: "sales",
            // Branch reports: the list, the open report, and Head Office's open blockers.
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
            roles: null,
            rolesError: "",
            roleForm: null,
            roleSaving: false,
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
            // KPI/OKR work requests (pages/kpi_requests): Head Office's card on the Inbox and the Admin's list.
            workRequests: null,
            workRequestsError: "",
            workForm: null,
            workAssign: null,
            workWithdraw: null,
            workReviewId: null,
            workReviewComment: "",
            workRecover: null,
            adminRequests: null,
            adminRequestsError: "",
            adminForm: null,
            adminStart: null,
            adminWithdraw: null,
            adminSaving: false,
            workSaving: false,
            // Admin console (pages/admin): loaded data, filters and the open dialog.
            admin: { overview: null, people: null, peopleOptions: null, branches: null, work: null, health: null, audit: null, error: "",
                     workKind: "objectives", workSearch: "", workStatus: "all", search: "", roleFilter: "all", showInactive: false,
                     auditAction: "all", edit: null, create: null, confirm: null, branchEdit: null, busy: false },
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
        if (this.state.navContext?.role === "employee") this.loadEmployeeContext();
        const loaders = {
            employee_dashboard: "loadEmployeeHome",
            my_performance: "loadMyPerformance",
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
        if (this.state.activeSection.startsWith("admin_")) {
            if (document.visibilityState !== "hidden") this.loadAdminSection({ silent: true });
            return;
        }
        const loader = this.departmentCode ? "loadBranchOps" : loaders[this.state.activeSection];
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
            if (this.state.activeSection === "overview") { this.loadPendingStaffing(); this.loadWorkRequests(); await this.loadObjectiveRequests(); }
            // Period change only: on landing the first load comes from loadBranchObjectives.
            if (this.state.activeSection === "bm_dashboard" && this.branchOpsRequestId) { await this.loadBranchOps(); }
            if (this.departmentCode) { await this.loadBranchOps(); }
            if (this.state.activeSection === "employee_performance") { await this.loadPerformanceOverview(); }
            if (this.state.activeSection === "employee_management" && this.state.employeeDetail) { await this.loadEmployeePerformance(this.state.employeeDetail.employee.id); }
            if (this.state.activeSection === "purchasing") { await this.loadPurchasing(); }
            if (this.state.activeSection === "branches") { await this.loadBranches(); }
            if (this.state.activeSection === "people") { await this.loadPeople(); }
            if (this.state.activeSection === "tasks_issues") { await this.loadTasksIssues(); }
            if (this.state.activeSection === "inventory_waste") { await this.loadInventory(); }
        } catch (error) {
            this.state.data = null;
            this.state.error = error.message || "The business overview could not be loaded.";
            this.notification.add(this.state.error, { type: "danger" });
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

    selectSection(sectionId) {
        this.state.activeSection = sectionId;
        if (sectionId.startsWith("admin_")) {
            return this.loadAdminSection();
        }
        if (this.branchError) {
            // Without a valid branch assignment the server refuses branch data; nothing is requested.
            return;
        }
        if (["sales_finance", "inventory_waste", "purchasing"].includes(sectionId)) {
            this.loadAreaGoals(sectionId);
        }
        if (sectionId === "overview") {
            this.loadPendingStaffing();
            this.loadWorkRequests();
            return this.loadObjectiveRequests();
        }
        if (sectionId === "bm_dashboard" && this.isBranchManager) {
            return this.loadBranchObjectives();
        }
        if (sectionId === "staffing_requests") {
            return this.loadStaffing();
        }
        if (sectionId === "employee_performance") {
            return this.loadPerformanceOverview();
        }
        if (sectionId === "department_assignment") {
            return this.loadDepartmentAssignments();
        }
        if (this.state.navContext?.role === "employee") {
            this.loadEmployeeContext();
        }
        if (this.departmentCode) {
            this.state.branchOps = null;
            this.state.branchOpsError = "";
            this.state.opsTab = this.departmentCode;
            this.state.departmentProductId = false;
            return this.loadBranchOps();
        }
        if (sectionId === "employee_dashboard") {
            return this.loadEmployeeHome();
        }
        if (sectionId === "my_performance") {
            return this.loadMyPerformance();
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
        if (sectionId === "employee_management" && (this.isBranchManager || this.isHeadOffice)) {
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
            this.loadRoles();
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

    // ------------------------------------------------------------ sidebar navigation
    openNavItem(item) {
        if (item.planned) {
            return;
        }
        if (item.action) {
            return item.context ? this.action.doAction(item.action, { additionalContext: item.context }) : this.action.doAction(item.action);
        }
        return this.selectSection(item.id);
    }

    get isBranchManager() {
        const role = this.state.navContext?.role || this.state.data?.role;
        return role === "branch_manager";
    }

    /** A summary card opens Odoo's own list of the records behind its number (the server decides which, and whether). */
    openDrill(drill) {
        const options = (Array.isArray(drill) ? drill : [drill]).filter(Boolean);
        if (!options.length) return;
        if (options.length > 1) {
            this.state.drillChoice = options;
            return;
        }
        this.state.drillChoice = null;
        return this.action.doAction({ type: "ir.actions.act_window", name: options[0].name, res_model: options[0].model, domain: options[0].domain,
                                      views: [[false, "list"], [false, "form"]], target: "current" });
    }

    onDrillKey(event, drill) {
        if (drill && (event.key === "Enter" || event.key === " ")) {
            event.preventDefault();
            this.openDrill(drill);
        }
    }

    get isAdminRole() {
        return this.state.navContext?.role === "admin";
    }

    get peopleTabs() {
        return this.isHeadOffice && HEAD_OFFICE_PEOPLE_TABS.some((tab) => tab.id === this.state.activeSection) ? HEAD_OFFICE_PEOPLE_TABS : [];
    }

    isNavActive(item) {
        return this.state.activeSection === item.id || (item.id === "employee_management" && this.peopleTabs.length > 0);
    }

    get isHeadOffice() {
        return (this.state.navContext?.role || this.state.data?.role) === "head_office";
    }

    get branchError() {
        return (this.isBranchManager && this.state.navContext?.branch_error) || false;
    }

    get sidebarPrimary() {
        if (this.isBranchManager) return BRANCH_SIDEBAR;
        if (this.isAdminRole) return ADMIN_SIDEBAR;
        return this.state.navContext?.role === "employee" ? EMPLOYEE_SIDEBAR : SIDEBAR_PRIMARY;
    }

    get sidebarMoreLabel() {
        return this.state.navContext?.role === "employee" ? "Assigned Departments" : "More";
    }

    get sidebarMore() {
        if (this.state.navContext?.role === "employee") {
            return (this.state.employeeContext?.departments || []).map((department) => ({ id: `department_${department.code}`, label: department.name }));
        }
        if (this.isAdminRole) return [];
        if (this.state.navContext?.role === "head_office") return HEAD_OFFICE_SIDEBAR_MORE;
        return this.isBranchManager ? BRANCH_SIDEBAR_MORE : SIDEBAR_MORE;
    }

    get userName() {
        return user.name;
    }

    get avatarUrl() {
        return `/web/image/res.users/${user.userId}/avatar_128`;
    }

    get activeItem() {
        return this.items.find((item) => item.id === this.state.activeSection) || this.items[0];
    }

    get branchSelectValue() {
        const id = this.state.data?.selected_warehouse_id;
        return id ? String(id) : this.state.warehouseId;
    }

    formatInventoryQuantity(value, unit) {
        if (value === false || value === null || value === undefined || !Number.isFinite(Number(value))) {
            return "Not available";
        }
        const formatted = Number(value).toLocaleString("en-US", { maximumFractionDigits: 3 });
        return (unit ? formatted + " " + unit : formatted);
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

    async openAction(xmlid) {
        try {
            await this.action.doAction(xmlid);
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "This screen is not available with your access rights.", { type: "warning" });
        }
    }

    formatPurchaseDate(value) {
        if (!value) return "—";
        const date = new Date(`${String(value).slice(0, 10)}T12:00:00`);
        return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
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
}

// Add every page's functions to the component. A name defined twice would silently replace the other, so it is refused.
for (const page of [inboxPage, salesPage, purchasingPage, inventoryPage, branchesPage, peoplePage, employeesPage, tasksPage, reportsPage, appDependenciesPage, branchManagerPage, employeeHomePage, adminPage, kpiRequestsPage]) {
    for (const [name, descriptor] of Object.entries(Object.getOwnPropertyDescriptors(page))) {
        if (name in StrategicNavigationShell.prototype) {
            throw new Error(`Strategic Management: "${name}" is defined twice in the navigation shell.`);
        }
        Object.defineProperty(StrategicNavigationShell.prototype, name, { ...descriptor, enumerable: false });
    }
}

registry.category("actions").add("strategic_management.navigation_shell", StrategicNavigationShell);
