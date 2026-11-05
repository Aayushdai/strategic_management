/** @odoo-module **/

import { Component, onWillStart, proxy } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const PERSPECTIVE_LABELS = {
    financial: "Financial",
    customer: "Customer",
    internal_process: "Internal Process",
    learning_growth: "Learning & Growth",
};

export class StrategicHeadOfficeDashboard extends Component {
    static template = "strategic_management.HeadOfficeDashboard";

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
                "get_head_office_data",
                [this.state.dateStart || false, this.state.dateEnd || false]
            );
        } catch (error) {
            this.state.data = null;
            this.state.error = error.message || "Unable to load the Head Office dashboard.";
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

    formatPercent(value) {
        if (typeof value !== "number" || !Number.isFinite(value)) {
            return "—";
        }
        return `${value.toLocaleString(undefined, { maximumFractionDigits: 1 })}%`;
    }

    progressWidth(value) {
        if (typeof value !== "number" || !Number.isFinite(value)) {
            return 0;
        }
        return Math.min(100, Math.max(0, value));
    }

    trendHeight(value) {
        const series = this.state.data?.monthly_trend || [];
        const maximum = Math.max(100, ...series.map((point) => Number(point.average_attainment) || 0));
        const numeric = Number(value) || 0;
        return Math.min(100, Math.max(numeric > 0 ? 3 : 0, (numeric / maximum) * 100));
    }

    get periodCaption() {
        const data = this.state.data;
        if (!data?.period?.start || !data?.period?.end) {
            return "Selected reporting period";
        }
        return `${this.prettyDate(data.period.start)} – ${this.prettyDate(data.period.end)}`;
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

    perspectiveLabel(value) {
        return PERSPECTIVE_LABELS[value] || "Strategic";
    }

    statusClass(value) {
        return (value || "not_evaluated").replaceAll("_", "-");
    }

    openKpiAnalysis() {
        return this.action.doAction("strategic_management.action_strategic_kpi_analysis");
    }

    openKpiExceptions() {
        return this.action.doAction("strategic_management.action_strategic_kpi_exceptions");
    }

    openObjective(objectiveId) {
        return this.action.doAction({
            type: "ir.actions.act_window",
            name: "Strategic Objective",
            res_model: "strategic.objective",
            views: [[false, "form"]],
            res_id: objectiveId,
            target: "current",
        });
    }

    openBranchEvaluations(branch) {
        return this.action.doAction({
            type: "ir.actions.act_window",
            name: `${branch.branch_name} KPI Evaluations`,
            res_model: "strategic.kpi.evaluation",
            view_mode: "graph,list,form",
            domain: [
                ["status", "=", "valid"],
                ["scope", "=", "branch"],
                ["warehouse_id", "=", branch.warehouse_id],
                ["date_start", "<=", this.state.dateEnd],
                ["date_end", ">=", this.state.dateStart],
            ],
            context: { search_default_group_by_kpi: 1, search_default_group_by_period: 1 },
            target: "current",
        });
    }
}

registry.category("actions").add(
    "strategic_management.head_office_dashboard",
    StrategicHeadOfficeDashboard
);
