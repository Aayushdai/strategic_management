/** @odoo-module **/

// Our Plan: the Branch Manager's part of the Hoshin plan (catchball). The company's annual objectives for the year,
// and under each the branch's own objectives and their tasks. These functions become methods of
// StrategicNavigationShell (see navigation_shell.js); template branch_plan.xml. Server: models/hoshin_cascade.py.

export const branchPlanPage = {
    async loadBranchPlan(year = false) {
        this.state.branchPlanError = "";
        try {
            this.state.branchPlan = await this.orm.call("strategic.performance.dashboard", "get_branch_cascade", [year || this.state.branchPlan?.year || false]);
        } catch (error) {
            this.state.branchPlanError = error.data?.message || error.message || "Your branch's plan could not be loaded.";
        }
    },

    onBranchPlanYear(event) {
        return this.loadBranchPlan(Number(event.target.value));
    },

    proposeBranchObjective() {
        return this.action.doAction("strategic_management.action_business_goals", { additionalContext: { propose_objective: true } });
    },
};
