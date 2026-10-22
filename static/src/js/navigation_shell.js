/** @odoo-module **/

import { Component, onMounted, onWillStart, onWillUnmount, proxy } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const NAVIGATION_ITEMS = [
    { id: "overview", label: "Overview", icon: "fa-home", description: "See overall business performance and important changes." },
    { id: "sales_finance", label: "Sales & Finance", icon: "fa-bar-chart", description: "Review sales, revenue trends, orders, and financial results." },
    { id: "inventory_waste", label: "Inventory & Waste", icon: "fa-archive", description: "Monitor stock, ingredient consumption, waste, and discrepancies." },
    { id: "purchasing", label: "Purchasing", icon: "fa-shopping-cart", description: "Review supplier orders, deliveries, and purchasing activity." },
    { id: "branches", label: "Branches", icon: "fa-building-o", description: "Compare locations and investigate branch-level performance." },
    { id: "people", label: "People", icon: "fa-users", description: "Review employees and workforce-related information." },
    { id: "tasks_issues", label: "Tasks & Issues", icon: "fa-tasks", description: "Track assigned issues, deadlines, and follow-up actions." },
    { id: "reports", label: "Reports", icon: "fa-file-text-o", description: "Open business reports and historical analysis." },
    { id: "configuration", label: "Configuration", icon: "fa-sliders", description: "Access authorized module configuration and management tools.", dividerBefore: true },
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

const CHART_COLORS = ["#2e9cf0", "#25a56a", "#f5a23d", "#9673c9", "#91a0ae"];

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
        this.state = proxy({
            items: NAVIGATION_ITEMS,
            activeSection: "overview",
            loading: true,
            error: "",
            // Empty until the first load: the server resolves the default period
            // in the user's Odoo timezone and the shell adopts it.
            dateStart: "",
            dateEnd: "",
            todayDate: "",
            warehouseId: "all",
            trendGranularity: "month",
            branchMetric: "sales",
            topProductLimit: 5,
            data: null,
            inventoryData: null,
            inventoryLoading: false,
            inventoryError: "",
            inventoryProductId: "all",
            purchasingData: null,
            purchasingLoading: false,
            branchesData: null,
            branchesLoading: false,
            peopleData: null,
            peopleLoading: false,
            overviewExtras: null,
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
        onWillStart(() => this.loadOverview());
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
            overview: "loadOverview",
            sales_finance: "loadOverview",
            inventory_waste: "loadInventory",
            purchasing: "loadPurchasing",
            branches: "loadBranches",
            people: "loadPeople",
            tasks_issues: "loadTasksIssues",
        };
        const loader = loaders[this.state.activeSection];
        if (!loader || document.visibilityState === "hidden") {
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
                await this.loadOverviewExtras({ silent: true });
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
            if (this.state.activeSection === "overview") { await this.loadOverviewExtras(); }
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
        if (sectionId === "overview") {
            return this.loadOverviewExtras();
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

    inventoryWasteDonutStyle() {
        const rows = (this.state.inventoryData && this.state.inventoryData.waste_by_product) || [];
        const total = rows.reduce((sum, row) => sum + Math.max(0, Number(row.cost) || 0), 0);
        if (!total) {
            return "background: #e2e7ed";
        }
        let offset = 0;
        const segments = rows.map((row, index) => {
            const share = (Math.max(0, Number(row.cost) || 0) / total) * 100;
            const segment = this.inventoryColor(index) + " " + offset + "% " + (offset + share) + "%";
            offset += share;
            return segment;
        });
        return "background: conic-gradient(" + segments.join(", ") + ")";
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
        const colors = ["#2e9cf0", "#25a56a", "#f5a23d", "#9673c9", "#91a0ae", "#e5c343"];
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
        return ["#2e9cf0", "#25a56a", "#f5a23d", "#9673c9", "#91a0ae", "#e5c343"][index % 6];
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
