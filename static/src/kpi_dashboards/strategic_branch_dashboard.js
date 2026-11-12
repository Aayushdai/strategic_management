/** @odoo-module **/

import { Component, onWillStart, proxy } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { dashboardHelpers } from "./dashboard_helpers";
import { BackButton } from "@strategic_management/navigation_shell/back_button";
import { useService } from "@web/core/utils/hooks";

export class StrategicBranchDashboard extends Component {
    static components = { BackButton };
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

Object.assign(StrategicBranchDashboard.prototype, dashboardHelpers);

registry.category("actions").add(
    "strategic_management.branch_dashboard",
    StrategicBranchDashboard
);
