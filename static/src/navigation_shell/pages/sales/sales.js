/** @odoo-module **/

// Sales.
// These functions become methods of StrategicNavigationShell (see navigation_shell.js); this page's
// template is sales.xml and its styles sales.scss, next to this file.

import { CHART_COLORS } from "../../constants";

export const salesPage = {
    setTrendGranularity(granularity) {
        if (!["day", "week", "month"].includes(granularity)) {
            return;
        }
        if (this.state.trendGranularity === granularity) {
            return;
        }
        this.state.trendGranularity = granularity;
        return this.loadOverview();
    },

    onBranchMetricChanged(event) {
        const allowed = ["sales", "orders", "average_order_value"];
        if (allowed.includes(event.target.value)) {
            this.state.branchMetric = event.target.value;
        }
    },

    onTopProductLimitChanged(event) {
        const limit = Number(event.target.value);
        if (![5, 10].includes(limit) || this.state.topProductLimit === limit) {
            return;
        }
        this.state.topProductLimit = limit;
        return this.loadOverview();
    },

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
    },

    salesTrendMax() {
        const values = (this.state.data?.sales_trend || [])
            .map((point) => Number(point.sales) || 0);
        const maximum = Math.max(0, ...values);

        if (maximum <= 0) {
            return 1;
        }

        const magnitude = 10 ** Math.floor(Math.log10(maximum));
        return Math.ceil(maximum / magnitude) * magnitude;
    },

    salesTrendX(index, count) {
        return count <= 1 ? 319 : 48 + index * (542 / (count - 1));
    },

    salesTrendY(value) {
        const number = Math.max(0, Number(value) || 0);
        return 160 - (number / this.salesTrendMax()) * 125;
    },

    salesTrendPoints() {
        const points = this.state.data?.sales_trend || [];
        return points.map((point, index) =>
            `${this.salesTrendX(index, points.length)},${this.salesTrendY(point.sales)}`
        ).join(" ");
    },

    trendLabelStep() {
        const count = this.state.data?.sales_trend?.length || 0;
        return Math.max(1, Math.ceil(count / 8));
    },

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
    },

    formatBranchMetric(branch) {
        const value = this.branchMetricValue(branch);
        if (value === false) {
            return "—";
        }
        return this.state.branchMetric === "orders"
            ? this.formatNumber(value)
            : this.formatCurrency(value);
    },

    branchBarWidth(branch) {
        const rows = this.state.data?.branch_sales || [];
        const max = Math.max(1, ...rows.map((row) => Number(this.branchMetricValue(row)) || 0));
        const value = Number(this.branchMetricValue(branch)) || 0;
        return Math.max(0, Math.min(100, (value / max) * 100));
    },

    categoryTotal() {
        const rows = this.state.data?.sales_by_category || [];
        if (!rows.length) return false;
        return rows.reduce((sum, row) => sum + (Number(row.sales) || 0), 0);
    },

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
    },

    categoryColor(index) {
        return CHART_COLORS[index % CHART_COLORS.length];
    },

    salesHourMax() {
        const rows = this.state.data?.sales_by_hour || [];
        return Math.max(1, ...rows.map((row) => Number(row.sales) || 0));
    },

    salesHourBarHeight(value) {
        const ratio = (Number(value) || 0) / this.salesHourMax();
        return Math.max(0, Math.min(100, ratio * 100));
    },

    salesHourAxisLabel(fraction) {
        return this.formatNumber(this.salesHourMax() * fraction);
    },

    /** Headline: sales over the period as a running total, against the same-length previous period. */
    get salesHero() {
        const data = this.state.data;
        if (!data || data.role === "employee" || data.sales === false || data.sales === undefined) return null;
        const previous = data.previous_period ? data.previous_period.sales : false;
        const chart = this.runningChart(data.sales_trend, "sales", data.sales, previous);
        const rows = data.sales_trend || [];
        let compare = "";
        if (chart.change !== null) {
            compare = `vs ${this.formatCurrency(previous)} previous period`;
        } else if (previous === 0) {
            compare = "No sales in the previous period";
        }
        const middle = rows[Math.floor((rows.length - 1) / 2)];
        return {
            ...chart,
            value: this.formatCurrency(data.sales),
            changeText: this.formatChange(chart.change),
            compare,
            axis: rows.length ? [rows[0].label, middle && middle.label, rows[rows.length - 1].label].filter(Boolean) : [],
            previousOrders: data.previous_period ? data.previous_period.orders : false,
            label: `Running sales over the period: ${this.formatCurrency(data.sales)}${compare ? ", " + compare : ""}`,
        };
    },

    /** What needs attention first on Sales: a drop against the previous period, else goals below target. */
    get salesAlert() {
        const hero = this.salesHero;
        if (hero && hero.change !== null && hero.change <= -10) {
            return { headline: `Sales are down ${Math.abs(hero.change).toFixed(0)}% on the previous period`, detail: `${hero.value} ${hero.compare}.` };
        }
        const missed = (this.state.areaGoals.sales_finance || []).filter((goal) => goal.tone === "bad");
        if (missed.length) {
            return { headline: `${missed.length} sales goal${missed.length === 1 ? " is" : "s are"} below target`, detail: missed.map((goal) => goal.title).slice(0, 3).join(", ") };
        }
        return null;
    },
};
