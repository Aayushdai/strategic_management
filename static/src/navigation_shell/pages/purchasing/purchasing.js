/** @odoo-module **/

// Purchasing.
// These functions become methods of StrategicNavigationShell (see navigation_shell.js); this page's
// template is purchasing.xml and its styles purchasing.scss, next to this file.

import { SECTION_LINKS, PURCHASE_COLORS } from "../../constants";

export const purchasingPage = {
    /** Active goals on track among those with an evaluated result; goals without a result are reported apart. */
    get goalSummary() {
        const counts = this.state.overviewExtras?.strategy?.objectives;
        if (!counts) {
            return null;
        }
        const onTrack = counts.on_track || 0;
        const evaluated = onTrack + (counts.at_risk || 0);
        return { onTrack, evaluated, notEvaluated: counts.not_evaluated || 0, pct: evaluated ? Math.round((onTrack * 100) / evaluated) : null };
    },

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
    },

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
    },

    formatPurchaseRate(value) {
        return value === false || value === null || value === undefined ? "Not available" : `${Number(value).toLocaleString("en-US", { maximumFractionDigits: 1 })}%`;
    },

    purchasingTrendMax() {
        const rows = this.state.purchasingData?.expenditure_trend || [];
        const max = Math.max(0, ...rows.map((row) => Number(row.current) || 0), ...rows.map((row) => Number(row.previous) || 0));
        if (max <= 0) return 1;
        const magnitude = 10 ** Math.floor(Math.log10(max));
        return Math.ceil(max / magnitude) * magnitude;
    },

    purchasingTrendX(index, count) {
        return count <= 1 ? 300 : index * (600 / (count - 1));
    },

    purchasingTrendY(value) {
        return 178 - ((Number(value) || 0) / this.purchasingTrendMax()) * 160;
    },

    purchasingTrendPoints(series) {
        const rows = this.state.purchasingData?.expenditure_trend || [];
        return rows.map((row, index) => `${this.purchasingTrendX(index, rows.length)},${this.purchasingTrendY(row[series])}`).join(" ");
    },

    /** "▲ 12% spend vs previous period", or "" when the previous period had no spend to compare with. */
    purchasingSpendChange() {
        const rows = this.state.purchasingData?.expenditure_trend || [];
        const current = rows.reduce((sum, row) => sum + (Number(row.current) || 0), 0);
        const previous = rows.reduce((sum, row) => sum + (Number(row.previous) || 0), 0);
        if (!previous) return "";
        const pct = Math.round(((current - previous) / previous) * 100);
        return `${pct > 0 ? "▲" : pct < 0 ? "▼" : "="} ${Math.abs(pct)}% spend vs previous period`;
    },

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
    },

    purchasingCategoryColor(index) {
        return PURCHASE_COLORS[index % PURCHASE_COLORS.length];
    },

    purchaseSupplierBarClass(rate) {
        const value = Number(rate) || 0;
        if (value >= 85) return "o_sm_pur_bar_good";
        if (value >= 65) return "o_sm_pur_bar_medium";
        return "o_sm_pur_bar_bad";
    },

    togglePurchasing(key) {
        this.state.purExpanded[key] = !this.state.purExpanded[key];
    },

    /** Purchasing goals grouped by the status tone the Business Goals page already gives them; null while loading. */
    get purchasingGoalSummary() {
        const rows = this.state.areaGoals.purchasing;
        if (!rows) {
            return null;
        }
        const count = (tones) => rows.filter((goal) => tones.includes(goal.tone)).length;
        return { total: rows.length, onTrack: count(["good"]), attention: count(["warn", "bad"]), other: count(["neutral"]) };
    },

    /** Head Office only: Odoo's standard Purchase screens are not branch-restricted by this module (see SECTION_LINKS). */
    get canOpenPurchaseOrders() {
        return this.state.data?.role === "head_office";
    },

    openPurchaseOrder(order) {
        return this.action.doAction(order.id ? {
            type: "ir.actions.act_window", res_model: "purchase.order", res_id: order.id, views: [[false, "form"]], target: "current",
        } : {
            type: "ir.actions.act_window", name: order.name, res_model: "purchase.order", domain: [["name", "=", order.name]],
            views: [[false, "list"], [false, "form"]], target: "current",
        });
    },

    purchaseStatusClass(status) {
        return status || "o_sm_pur_status_info";
    },
};
