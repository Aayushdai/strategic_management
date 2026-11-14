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
                this.state.purchasingLoadedAt = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
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

    // ------------------------------------------------------------------ calm page (design D)
    setPurTab(tab) {
        this.state.purTab = tab;
    },

    setPurHeroMetric(metric) {
        this.state.purHeroMetric = metric;
    },

    /** Headline: the period's running total (spend or orders) against the previous period's total, as SVG paths. */
    get purchasingHero() {
        const pd = this.state.purchasingData;
        if (!pd || !pd.available) return null;
        const orders = this.state.purHeroMetric === "orders";
        const days = pd.daily_spend || [];
        let running = 0;
        const series = days.map((day) => (running += orders ? Number(day.orders) || 0 : Number(day.amount) || 0));
        const total = orders ? pd.purchase_order_count : pd.purchase_order_value;
        const previous = pd.previous_period ? (orders ? pd.previous_period.orders : pd.previous_period.spend) : false;
        const hasPrevious = previous !== false && previous !== null && previous !== undefined;
        const top = Math.max(running, hasPrevious ? Number(previous) : 0, Number(total) || 0) * 1.12 || 1;
        const x = (index) => (days.length > 1 ? (index * 600) / (days.length - 1) : 300);
        const y = (value) => 200 - (value / top) * 190;
        const points = series.map((value, index) => `${x(index).toFixed(1)} ${y(value).toFixed(1)}`);
        const line = points.length ? `M${points.join(" L")}` : "";
        const format = (value) => (orders ? this.formatNumber(value) : this.formatPurchaseCurrency(value));
        let change = "";
        let compare = "";
        if (hasPrevious && Number(previous) > 0) {
            const pct = ((Number(total) - Number(previous)) / Number(previous)) * 100;
            change = `${pct > 0 ? "+" : pct < 0 ? "−" : ""}${Math.abs(pct).toFixed(1)}%`;
            compare = `vs ${format(previous)} previous period`;
        } else if (hasPrevious) {
            compare = orders ? "No orders in the previous period" : "No spend in the previous period";
        }
        const middle = days[Math.floor((days.length - 1) / 2)];
        return {
            title: orders ? "Confirmed orders" : "Purchase spend",
            value: format(total),
            change,
            compare,
            line,
            area: line ? `${line} L${x(points.length - 1).toFixed(1)} 200 L${x(0).toFixed(1)} 200 Z` : "",
            ref: hasPrevious && Number(previous) > 0 ? `M0 ${y(Number(previous)).toFixed(1)} L600 ${y(Number(previous)).toFixed(1)}` : "",
            axis: days.length ? [days[0].date, middle && middle.date, days[days.length - 1].date].filter(Boolean) : [],
            label: `${orders ? "Running count of confirmed orders" : "Running purchase spend"} over the period: ${format(total)}${compare ? ", " + compare : ""}`,
        };
    },

    /** The one thing that needs attention first: overdue deliveries, with the oldest named. */
    get purchasingAlert() {
        const pd = this.state.purchasingData;
        if (!pd || !pd.available || !pd.overdue_delivery_count) return null;
        const oldest = (pd.open_deliveries || []).find((row) => row.status === "Overdue");
        const count = pd.overdue_delivery_count;
        return {
            headline: `${count} of ${pd.pending_delivery_count} open deliveries ${count === 1 ? "is" : "are"} overdue`,
            detail: oldest ? `Oldest is ${oldest.name} from ${oldest.supplier}, ${oldest.days_late} day${oldest.days_late === 1 ? "" : "s"} late at ${oldest.branch}.` : "",
        };
    },

    get overdueDeliveries() {
        return (this.state.purchasingData?.open_deliveries || []).filter((row) => row.status === "Overdue");
    },

    onTimePendingCount() {
        const pd = this.state.purchasingData;
        return pd ? Math.max(0, (pd.pending_delivery_count || 0) - (pd.overdue_delivery_count || 0)) : 0;
    },

    oldestOverdueDays() {
        const first = this.overdueDeliveries[0];
        return first ? first.days_late : 0;
    },

    purBadge(statusClass) {
        return { o_sm_pur_status_good: "is-good", o_sm_pur_status_warn: "is-warn", o_sm_pur_status_bad: "is-bad", o_sm_pur_status_info: "is-info" }[statusClass] || "";
    },

    deliveryBadge(row) {
        if (row.status === "Overdue") return { cls: "is-bad", label: row.days_late ? `${row.days_late} day${row.days_late === 1 ? "" : "s"} late` : "Overdue" };
        if (row.status === "Partial") return { cls: "is-warn", label: "Partly received" };
        return { cls: "", label: "Awaiting" };
    },

    goalBadge(tone) {
        return { good: "is-good", warn: "is-warn", bad: "is-bad" }[tone] || "";
    },

    /** Dash length for the on-time ring (circumference of r=15 is 94.25). */
    ringDash(rate) {
        const value = Math.max(0, Math.min(100, Number(rate) || 0));
        return `${((value * 94.25) / 100).toFixed(2)} 94.25`;
    },

    shareWidth(value, rows) {
        const top = Math.max(0, ...(rows || []).map((row) => Number(row.value) || 0));
        return top ? `width:${Math.max(0, (Number(value) || 0) / top) * 100}%` : "width:0";
    },

    goalSplitStyle(part) {
        const summary = this.purchasingGoalSummary;
        return summary && summary.total ? `flex:${summary[part] || 0}` : "flex:0";
    },
};
