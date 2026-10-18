/** @odoo-module **/

import { Component, onWillStart, proxy } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

export class StrategicEmployeeDashboard extends Component {
    static template = "strategic_management.EmployeeDashboard";

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        const today = this.asDateInput(new Date());
        const rangeStart = new Date(new Date().getFullYear(), new Date().getMonth() - 5, 1);
        this.state = proxy({
            loading: true,
            error: "",
            dateStart: this.asDateInput(rangeStart),
            dateEnd: today,
            data: null,
        });
        onWillStart(() => this.loadDashboard());
    }

    asDateInput(value) {
        const year = value.getFullYear();
        const month = String(value.getMonth() + 1).padStart(2, "0");
        const day = String(value.getDate()).padStart(2, "0");
        return `${year}-${month}-${day}`;
    }

    async loadDashboard() {
        if (this.state.dateStart && this.state.dateEnd && this.state.dateStart > this.state.dateEnd) {
            this.state.error = "The start date must be on or before the end date.";
            this.state.data = null;
            return;
        }
        this.state.loading = true;
        this.state.error = "";
        try {
            this.state.data = await this.orm.call(
                "strategic.performance.dashboard",
                "get_employee_data",
                [this.state.dateStart || false, this.state.dateEnd || false]
            );
        } catch (error) {
            this.state.data = null;
            this.state.error = error.message || "Unable to load your personal KPI dashboard.";
            this.notification.add(this.state.error, { type: "danger" });
        } finally {
            this.state.loading = false;
        }
    }

    onStartDateChanged(event) {
        this.state.dateStart = event.target.value;
        return this.loadDashboard();
    }

    onEndDateChanged(event) {
        this.state.dateEnd = event.target.value;
        return this.loadDashboard();
    }

    formatNumber(value, digits = 1) {
        return typeof value === "number" && Number.isFinite(value)
            ? value.toLocaleString(undefined, { maximumFractionDigits: digits })
            : "—";
    }

    formatPercent(value) {
        return typeof value === "number" && Number.isFinite(value)
            ? `${this.formatNumber(value)}%`
            : "—";
    }

    progressWidth(value) {
        if (typeof value !== "number" || !Number.isFinite(value)) return 0;
        return Math.min(100, Math.max(0, value));
    }

    trendHeight(value) {
        const points = this.state.data?.monthly_trend || [];
        const maximum = Math.max(100, ...points.map((point) => Number(point.average_attainment) || 0));
        const numeric = Number(value) || 0;
        return Math.min(100, Math.max(numeric > 0 ? 3 : 0, (numeric / maximum) * 100));
    }

    prettyDate(value) {
        if (!value) return "—";
        const date = new Date(`${value}T12:00:00`);
        if (Number.isNaN(date.getTime())) return value;
        return date.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
    }

    statusClass(value) {
        return (value || "not_evaluated").replaceAll("_", "-");
    }

    openMyEvaluations() {
        const userId = this.state.data?.user_id;
        return this.action.doAction({
            type: "ir.actions.act_window",
            name: "My KPI Evaluations",
            res_model: "strategic.kpi.evaluation",
            view_mode: "list,graph,pivot,form",
            domain: [
                ["scope", "=", "user"],
                ["user_id", "=", userId],
                ["date_start", "<=", this.state.dateEnd],
                ["date_end", ">=", this.state.dateStart],
            ],
            target: "current",
        });
    }

    openMyMeasurements() {
        const userId = this.state.data?.user_id;
        return this.action.doAction({
            type: "ir.actions.act_window",
            name: "My KPI Measurement Submissions",
            res_model: "strategic.kpi.measurement",
            view_mode: "list,form",
            domain: [
                ["scope", "=", "user"],
                ["user_id", "=", userId],
                ["date_start", "<=", this.state.dateEnd],
                ["date_end", ">=", this.state.dateStart],
            ],
            target: "current",
        });
    }
}

registry.category("actions").add(
    "strategic_management.employee_dashboard",
    StrategicEmployeeDashboard
);
