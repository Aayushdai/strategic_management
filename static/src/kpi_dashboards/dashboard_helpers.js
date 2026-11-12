/** @odoo-module **/

// Helpers shared by the three KPI dashboards (Head Office, Branch, Employee). Each dashboard adds them to its class
// with Object.assign(Dashboard.prototype, dashboardHelpers); a dashboard keeps its own loadDashboard().

export const dashboardHelpers = {
    asDateInput(value) {
        const year = value.getFullYear();
        const month = String(value.getMonth() + 1).padStart(2, "0");
        const day = String(value.getDate()).padStart(2, "0");
        return `${year}-${month}-${day}`;
    },

    onStartDateChanged(event) {
        this.state.dateStart = event.target.value;
        return this.loadDashboard();
    },

    onEndDateChanged(event) {
        this.state.dateEnd = event.target.value;
        return this.loadDashboard();
    },

    formatNumber(value, digits = 1) {
        return typeof value === "number" && Number.isFinite(value)
            ? value.toLocaleString(undefined, { maximumFractionDigits: digits })
            : "—";
    },

    formatPercent(value) {
        return typeof value === "number" && Number.isFinite(value) ? `${this.formatNumber(value)}%` : "—";
    },

    progressWidth(value) {
        if (typeof value !== "number" || !Number.isFinite(value)) return 0;
        return Math.min(100, Math.max(0, value));
    },

    /** Bar height in percent for the monthly trend; a non-zero month is always visible. */
    trendHeight(value) {
        const points = this.state.data?.monthly_trend || [];
        const maximum = Math.max(100, ...points.map((point) => Number(point.average_attainment) || 0));
        const numeric = Number(value) || 0;
        return Math.min(100, Math.max(numeric > 0 ? 3 : 0, (numeric / maximum) * 100));
    },

    prettyDate(value) {
        if (!value) return "—";
        const date = new Date(`${value}T12:00:00`);
        if (Number.isNaN(date.getTime())) return value;
        return date.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
    },

    statusClass(value) {
        return (value || "not_evaluated").replaceAll("_", "-");
    },
};
