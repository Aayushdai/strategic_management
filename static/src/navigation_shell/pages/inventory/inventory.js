/** @odoo-module **/

// Inventory & Waste.
// These functions become methods of StrategicNavigationShell (see navigation_shell.js); this page's
// template is inventory.xml and its styles inventory.scss, next to this file.

export const inventoryPage = {
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
    },

    onInventoryProductChanged(event) {
        this.state.inventoryProductId = event.target.value || "all";
        return this.loadInventory();
    },

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
    },

    inventoryMetricValue(value) {
        if (value === false || value === null || value === undefined || !Number.isFinite(Number(value))) {
            return "Not available";
        }
        return Number(value).toLocaleString("en-US", { maximumFractionDigits: 0 });
    },

    inventoryWasteWidth(waste) {
        const rows = (this.state.inventoryData && this.state.inventoryData.waste_by_product) || [];
        const max = rows.reduce((top, row) => Math.max(top, Number(row.cost) || 0), 0);
        return max > 0 ? Math.max(0, Number(waste.cost) || 0) / max * 100 : 0;
    },

    inventoryStockWidth(item) {
        const minimum = Math.max(0, Number(item && item.minimum_quantity) || 0);
        const available = Math.max(0, Number(item && item.available_quantity) || 0);
        if (!minimum) {
            return 0;
        }
        return Math.max(0, Math.min(100, (available / minimum) * 100));
    },

    inventoryBranchWidth(branch) {
        const rows = (this.state.inventoryData && this.state.inventoryData.branch_comparison) || [];
        const max = rows.reduce((top, row) => Math.max(top, Number(row.low_stock_count) || 0), 0);
        return max > 0 ? Math.max(0, Number(branch.low_stock_count) || 0) / max * 100 : 0;
    },

    get inventoryExpiredLotCount() {
        const lots = (this.state.inventoryData && this.state.inventoryData.expiring_stock) || [];
        return lots.filter((lot) => lot.days_left < 0).length;
    },

    toggleInventory(key) {
        this.state.invExpanded[key] = !this.state.invExpanded[key];
    },

    formatDate(value) {
        if (!value) return "—";
        const date = new Date(`${String(value).slice(0, 10)}T12:00:00`);
        return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString(undefined, { day: "numeric", month: "short" });
    },

    /** What needs attention first on Inventory, from the page's own alert list: expired lots, stock below minimum,
     * lots about to expire and unreconciled counts, with the most serious one named. */
    get inventoryAlert() {
        const items = this.state.inventoryData?.attention || [];
        if (!items.length) return null;
        const count = (test) => items.filter(test).length;
        const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
        const parts = [
            [count((item) => item.kind === "expiry" && item.severity === "high"), "expired lot", "expired lots"],
            [count((item) => item.kind === "stock"), "product below minimum stock", "products below minimum stock"],
            [count((item) => item.kind === "expiry" && item.severity !== "high"), "lot expiring soon", "lots expiring soon"],
            [count((item) => item.kind === "variance"), "stock count to review", "stock counts to review"],
        ].filter(([n]) => n).map(([n, one, many]) => plural(n, one, many));
        const first = items[0];
        return { headline: parts.join(" · "), detail: `${first.title}: ${first.detail}`, serious: items.some((item) => item.severity === "high") };
    },
};
