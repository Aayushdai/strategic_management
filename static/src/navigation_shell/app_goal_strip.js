/** @odoo-module **/

// The active goals of a business area, shown above Odoo's own Purchase orders, Inventory transfers and POS orders
// (lists and forms), so people see them where they work. Server: strategic.goal.setup.get_app_goals (same goals and access
// as the dashboards). A list whose user sees no goal shows nothing extra.

import { Component, onWillStart, proxy, t, useProps } from "@odoo/owl";
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { ControlPanel } from "@web/search/control_panel/control_panel";

const MODELS = ["purchase.order", "stock.picking", "pos.order"];
const SHOWN = 3;

export class AppGoalStrip extends Component {
    static template = "strategic_management.AppGoalStrip";
    props = useProps({ resModel: t.string() });

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = proxy({ goals: [] });
        onWillStart(async () => {
            try {
                this.state.goals = await this.orm.silent.call("strategic.goal.setup", "get_app_goals", [this.props.resModel]);
            } catch {
                this.state.goals = []; // the list works the same without its goals
            }
        });
    }

    get shown() {
        return this.state.goals.slice(0, SHOWN);
    }

    get more() {
        return Math.max(0, this.state.goals.length - SHOWN);
    }

    measured(goal) {
        return Boolean(goal.bar && goal.bar.kind !== "waiting");
    }

    barClass(goal) {
        return { good: "bg-success", warn: "bg-warning", bad: "bg-danger", exceeded: "bg-danger" }[goal.bar?.tone] || "bg-primary";
    }

    barStyle(goal) {
        return `width:${this.measured(goal) ? goal.bar.bar : 0}%`;
    }

    openGoal(goal) {
        this.action.doAction({ type: "ir.actions.act_window", res_model: "strategic.objective", res_id: goal.objective_id,
                               views: [[false, "form"]], target: "current" });
    }

    openAll() {
        this.action.doAction("strategic_management.action_business_goals");
    }
}

ControlPanel.components = { ...ControlPanel.components, AppGoalStrip };

patch(ControlPanel.prototype, {
    /** The model when it is one of the three that carry goals: their lists, and their forms, where an order is written. */
    get smGoalModel() {
        const model = this.env.searchModel?.resModel;
        return ["list", "kanban", "form"].includes(this.env.config.viewType) && MODELS.includes(model) ? model : false;
    },
});
