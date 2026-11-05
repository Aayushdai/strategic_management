/** @odoo-module **/

// Employee home: dashboard, My Performance and department pages.
// These functions become methods of StrategicNavigationShell (see navigation_shell.js); this page's
// template is employee_home.xml and its styles employee_home.scss, next to this file.

import { TASK_MODEL, PERIOD_PRESETS } from "../../constants";

export const employeeHomePage = {
    // ------------------------------------------------------------ employee departments
    /** The department page being shown; the server re-checks membership on every request. */
    get departmentCode() {
        const section = this.state.activeSection;
        return section.startsWith("department_") && section !== "department_assignment" ? section.slice("department_".length) : null;
    },

    get departmentName() {
        return (this.state.employeeContext?.departments || []).find((row) => row.code === this.departmentCode)?.name || "";
    },

    async loadEmployeeContext() {
        try {
            this.state.employeeContext = await this.orm.silent.call("strategic.performance.dashboard", "get_employee_context", []);
        } catch (error) {
            this.state.employeeContext = { name: "", branch: false, departments: [], access_error: error.data?.message || error.message || "Your access could not be checked." };
        }
    },

    onDepartmentProductChanged(event) {
        this.state.departmentProductId = Number(event.target.value) || false;
        return this.loadBranchOps();
    },

    /** One row per ordered product, keeping each product's own unit. */
    departmentOrderLines(orders) {
        return (orders || []).flatMap((order) => order.lines.map((line, index) => ({ ...line, key: `${order.name}-${index}`, order: order.name, supplier: order.supplier })));
    },

    onMyPerformancePeriod(field, event) {
        if (!event.target.value) return;
        this.state[field] = event.target.value;
        return this.loadMyPerformance();
    },

    async loadMyPerformance() {
        const requestId = (this.myPerformanceRequestId = (this.myPerformanceRequestId || 0) + 1);
        try {
            const data = await this.orm.silent.call("strategic.performance.dashboard", "get_my_performance", [this.state.myPerformanceStart, this.state.myPerformanceEnd]);
            if (requestId !== this.myPerformanceRequestId) return;
            this.state.myPerformance = data;
            this.state.myPerformanceError = "";
        } catch (error) {
            if (requestId !== this.myPerformanceRequestId) return;
            this.state.myPerformance = null;
            this.state.myPerformanceError = error.data?.message || error.message || "Your performance could not be loaded.";
        }
    },

    /** The task dialog reads My Tasks, so both load together. */
    loadEmployeeHome() {
        return Promise.all([this.loadMyTasks({ silent: true }), this.loadEmployeeWorkspace()]);
    },

    async loadEmployeeWorkspace() {
        try {
            this.state.employeeWorkspace = await this.orm.silent.call(TASK_MODEL, "get_employee_workspace", []);
            this.state.employeeWorkspaceError = "";
        } catch (error) {
            this.state.employeeWorkspaceError = error.data?.message || error.message || "Your work could not be loaded.";
        }
    },

    /** [start, end] of a period preset as date inputs, ending today at the latest; null for custom dates. */
    presetRange(preset, today = new Date()) {
        const year = today.getFullYear();
        const month = today.getMonth();
        const range = {
            this_month: [new Date(year, month, 1), today],
            last_month: [new Date(year, month - 1, 1), new Date(year, month, 0)],
            this_week: [new Date(year, month, today.getDate() - ((today.getDay() + 6) % 7)), today],
            last_3_months: [new Date(year, month - 2, 1), today],
            this_year: [new Date(year, 0, 1), today],
        }[preset];
        return range ? range.map((date) => this.asDateInput(date)) : null;
    },

    /** Daily buckets up to about a month, weekly up to about four months, monthly beyond. */
    granularityFor(start, end) {
        const days = (new Date(`${end}T12:00:00`) - new Date(`${start}T12:00:00`)) / 86400000 + 1;
        return days <= 45 ? "day" : days <= 125 ? "week" : "month";
    },

    onPeriodPresetChanged(event) {
        const preset = event.target.value;
        if (!PERIOD_PRESETS.some((option) => option.key === preset)) {
            return;
        }
        this.state.periodPreset = preset;
        const range = this.presetRange(preset);
        if (!range) {
            return; // Custom dates: the date fields appear and load on change.
        }
        [this.state.dateStart, this.state.dateEnd] = range;
        this.state.trendGranularity = this.granularityFor(...range);
        return this.loadOverview();
    },
};
