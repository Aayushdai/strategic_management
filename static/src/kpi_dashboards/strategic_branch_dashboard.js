/** @odoo-module **/

import { Component, onWillStart, proxy } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

export class StrategicBranchDashboard extends Component {
    static template = "strategic_management.BranchDashboard";

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        const today = this.asDateInput(new Date());
        const currentMonth = new Date();
        const rangeStart = new Date(currentMonth.getFullYear(), currentMonth.getMonth() - 5, 1);
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
                "get_branch_data",
                [this.state.dateStart || false, this.state.dateEnd || false]
            );
        } catch (error) {
            this.state.data = null;
            this.state.error = error.message || "Unable to load the Branch Manager dashboard.";
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
        if (typeof value !== "number" || !Number.isFinite(value)) {
            return "—";
        }
        return value.toLocaleString(undefined, { maximumFractionDigits: digits });
    }

    formatPercent(value) {
        return typeof value === "number" && Number.isFinite(value)
            ? `${this.formatNumber(value)}%`
            : "—";
    }

    progressWidth(value) {
        if (typeof value !== "number" || !Number.isFinite(value)) {
            return 0;
        }
        return Math.min(100, Math.max(0, value));
    }

    trendHeight(value) {
        const points = this.state.data?.monthly_trend || [];
        const maximum = Math.max(100, ...points.map((point) => Number(point.average_attainment) || 0));
        const numeric = Number(value) || 0;
        return Math.min(100, Math.max(numeric > 0 ? 3 : 0, (numeric / maximum) * 100));
    }

    prettyDate(value) {
        if (!value) {
            return "—";
        }
        const date = new Date(`${value}T12:00:00`);
        if (Number.isNaN(date.getTime())) {
            return value;
        }
        return date.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
    }

    statusClass(value) {
        return (value || "not_evaluated").replaceAll("_", "-");
    }

    openObjective(objectiveId) {
        return this.action.doAction({
            type: "ir.actions.act_window",
            name: "Branch Objective",
            res_model: "strategic.objective",
            views: [[false, "form"]],
            res_id: objectiveId,
            target: "current",
        });
    }

    openKpiAnalysis() {
        const branchId = this.state.data?.branch?.warehouse_id;
        return this.action.doAction({
            type: "ir.actions.act_window",
            name: "Branch KPI Evaluations",
            res_model: "strategic.kpi.evaluation",
            view_mode: "graph,pivot,list,form",
            domain: [
                ["company_id", "=", this.state.data.company_id],
                ["date_start", "<=", this.state.dateEnd],
                ["date_end", ">=", this.state.dateStart],
                "|",
                "&", ["scope", "=", "branch"], ["warehouse_id", "=", branchId],
                "&", ["scope", "=", "user"], ["user_id.strategic_branch_warehouse_id", "=", branchId],
            ],
            context: { search_default_group_by_kpi: 1, search_default_group_by_period: 1 },
            target: "current",
        });
    }
}

registry.category("actions").add(
    "strategic_management.branch_dashboard",
    StrategicBranchDashboard
);
