/** @odoo-module **/

import { Component, onWillStart, proxy } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { dashboardHelpers } from "./dashboard_helpers";
import { BackButton } from "@strategic_management/navigation_shell/back_button";
import { useService } from "@web/core/utils/hooks";

export class StrategicEmployeeDashboard extends Component {
    static components = { BackButton };
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

Object.assign(StrategicEmployeeDashboard.prototype, dashboardHelpers);

registry.category("actions").add(
    "strategic_management.employee_dashboard",
    StrategicEmployeeDashboard
);
