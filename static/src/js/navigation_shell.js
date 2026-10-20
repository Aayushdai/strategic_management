/** @odoo-module **/

import { Component, onWillStart, proxy } from "@odoo/owl";
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
    { id: "settings", label: "Settings", icon: "fa-cog", description: "Manage available module settings." },
];

const CHART_COLORS = ["#2e9cf0", "#25a56a", "#f5a23d", "#9673c9", "#91a0ae"];

export class StrategicNavigationShell extends Component {
    static template = "strategic_management.NavigationShell";

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.items = NAVIGATION_ITEMS;
        const today = new Date();
        const firstMonth = new Date(today.getFullYear(), today.getMonth() - 5, 1);
        this.state = proxy({
            items: NAVIGATION_ITEMS,
            activeSection: "overview",
            loading: true,
            error: "",
            dateStart: this.asDateInput(firstMonth),
            dateEnd: this.asDateInput(today),
            todayDate: this.asDateInput(today),
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
        });
        onWillStart(() => this.loadOverview());
    }

    asDateInput(value) {
        const year = value.getFullYear();
        const month = String(value.getMonth() + 1).padStart(2, "0");
        const day = String(value.getDate()).padStart(2, "0");
        return `${year}-${month}-${day}`;
    }

    async loadOverview() {
        if (this.state.dateStart && this.state.dateEnd && this.state.dateStart > this.state.dateEnd) {
            this.state.error = "The start date must be on or before the end date.";
            return;
        }
        this.state.loading = true;
        this.state.error = "";
        try {
            const selectedWarehouse = this.state.warehouseId === "all" ? false : Number(this.state.warehouseId);
            this.state.data = await this.orm.call(
                "strategic.performance.dashboard",
                "get_business_overview_data",
                [this.state.dateStart || false, this.state.dateEnd || false, selectedWarehouse, this.state.trendGranularity, this.state.topProductLimit]
            );
            if (this.state.activeSection === "purchasing") { await this.loadPurchasing(); }
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
        if (sectionId === "purchasing") {
            return this.loadPurchasing();
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

    async loadInventory() {
        if (this.state.dateStart && this.state.dateEnd && this.state.dateStart > this.state.dateEnd) {
            this.state.inventoryError = "The start date must be on or before the end date.";
            this.state.inventoryData = { available: false, messages: [this.state.inventoryError] };
            return;
        }
        this.state.inventoryLoading = true;
        this.state.inventoryError = "";
        try {
            const selectedWarehouse = this.state.warehouseId === "all" ? false : Number(this.state.warehouseId);
            const selectedProduct = this.state.inventoryProductId && this.state.inventoryProductId !== "all"
                ? Number(this.state.inventoryProductId)
                : false;
            const response = await this.orm.call(
                "strategic.performance.dashboard",
                "get_inventory_waste_data",
                [this.state.dateStart || false, this.state.dateEnd || false, selectedWarehouse, selectedProduct]
            );
            this.state.inventoryData = response;
            const returnedProductId = response && response.selected_product_id ? String(response.selected_product_id) : "all";
            if ((this.state.inventoryProductId || "all") !== returnedProductId) {
                this.state.inventoryProductId = returnedProductId;
            }
        } catch (error) {
            this.state.inventoryError = error.message || "Inventory data could not be loaded.";
            this.state.inventoryData = { available: false, messages: [this.state.inventoryError] };
            this.notification.add(this.state.inventoryError, { type: "danger" });
        } finally {
            this.state.inventoryLoading = false;
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
    async loadPurchasing() {
        this.state.purchasingLoading = true;
        try {
            const selectedWarehouse = this.state.warehouseId === "all" ? false : Number(this.state.warehouseId);
            this.state.purchasingData = await this.orm.call(
                "strategic.performance.dashboard",
                "get_purchasing_overview_data",
                [this.state.dateStart || false, this.state.dateEnd || false, selectedWarehouse]
            );
        } catch (purchasingError) {
            this.state.purchasingData = null;
            console.error("Purchasing dashboard data could not be loaded", purchasingError);
        } finally {
            this.state.purchasingLoading = false;
        }
    }

    formatPurchaseCurrency(value) {
        if (value === false || value === null || value === undefined || !Number.isFinite(Number(value))) {
            return "â€”";
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
        if (!value) return "â€”";
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
            return "â€”";
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
            return "â€”";
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
