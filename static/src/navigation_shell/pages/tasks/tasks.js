/** @odoo-module **/

// Tasks: My Tasks, Task Management, Tasks & Issues and the task dialog.
// These functions become methods of StrategicNavigationShell (see navigation_shell.js); this page's
// template is tasks.xml and its styles tasks.scss, next to this file.

import { TASK_MODEL } from "../../constants";

export const tasksPage = {
    // ------------------------------------------------------------ branch tasks
    async loadMyTasks({ silent = false } = {}) {
        if (!silent) this.state.tasksLoading = true;
        try {
            this.state.myTasks = await this.orm.silent.call(TASK_MODEL, "get_my_tasks", []);
            this.state.tasksError = "";
        } catch (error) {
            if (!silent) this.state.tasksError = error.data?.message || error.message || "Tasks could not be loaded.";
        } finally {
            this.state.tasksLoading = false;
        }
    },

    async loadTaskManagement({ silent = false } = {}) {
        if (!this.isBranchManager || this.branchError) return;
        if (!silent) this.state.tasksLoading = true;
        try {
            this.state.taskManagement = await this.orm.silent.call(TASK_MODEL, "get_task_management", []);
            this.state.tasksError = "";
        } catch (error) {
            if (!silent) this.state.tasksError = error.data?.message || error.message || "Tasks could not be loaded.";
        } finally {
            this.state.tasksLoading = false;
        }
    },

    async loadTaskSummary() {
        try {
            this.state.taskSummary = await this.orm.silent.call(TASK_MODEL, "get_branch_task_summary", []);
        } catch (error) {
            this.state.taskSummary = null;
            console.error("Task summary could not be loaded", error);
        }
    },

    /** Task Management's tasks, narrowed to one employee when opened from Employee Management. */
    get scopedTasks() {
        const tasks = this.state.taskManagement?.tasks || [];
        const employee = this.state.taskEmployee;
        return employee ? tasks.filter((task) => task.assigned_user_id === employee.id) : tasks;
    },

    get taskFilters() {
        const tasks = this.scopedTasks;
        const open = (task) => ["assigned", "in_progress", "changes_requested", "submitted"].includes(task.state);
        return [
            { key: "open", label: "Open", match: open },
            { key: "overdue", label: "Overdue", match: (task) => task.is_overdue },
            { key: "review", label: "Awaiting review", match: (task) => task.state === "submitted" },
            { key: "all", label: "All", match: () => true },
        ].map((filter) => ({ ...filter, count: tasks.filter(filter.match).length }));
    },

    get filteredTasks() {
        const filter = this.taskFilters.find((item) => item.key === this.state.taskFilter) || this.taskFilters[0];
        return this.scopedTasks.filter(filter.match);
    },

    clearTaskEmployee() {
        this.state.taskEmployee = null;
        this.state.pages.task_management = 0;
    },

    setTaskFilter(key) {
        this.state.taskFilter = key;
        this.state.pages.task_management = 0;
    },

    /** The task open in the dialog, from whichever list holds it. */
    get taskDetail() {
        const id = this.state.taskDetailId;
        if (!id) return null;
        return [...(this.state.taskManagement?.tasks || []), ...(this.state.myTasks?.tasks || [])].find((task) => task.id === id) || null;
    },

    openTask(task) {
        this.state.taskDetailId = task.id;
        this.state.taskNote = "";
        this.state.taskPanel = null;
    },

    /** From the dashboard: open the task in Task Management. */
    async openManagedTask(taskId, employee = null) {
        this.state.taskEmployee = employee;
        this.state.activeSection = "task_management";
        await this.loadTaskManagement();
        this.state.taskFilter = "all";
        this.state.taskDetailId = taskId;
        this.state.taskNote = "";
        this.state.taskPanel = null;
    },

    closeTask() {
        if (!this.state.taskBusy) {
            this.state.taskDetailId = null;
        }
    },

    onTaskNoteInput(event) {
        this.state.taskNote = event.target.value;
    },

    /** Run one workflow step on the server, then reload; nothing changes on screen before the server confirms. */
    async runTaskAction(task, method, withNote = false) {
        if (this.state.taskBusy) return;
        const note = this.state.taskNote.trim();
        if (withNote && !note) {
            this.notification.add(method === "action_submit" ? "Describe what was done." : "Explain what needs to change.", { type: "warning" });
            return;
        }
        this.state.taskBusy = true;
        try {
            await this.orm.call(TASK_MODEL, method, withNote ? [[task.id], note] : [[task.id]]);
            this.state.taskNote = "";
            const labels = { action_start: "Task started.", action_submit: "Submitted for review.", action_accept: "Task completed.",
                             action_request_changes: "Changes requested.", action_cancel: "Task cancelled." };
            this.notification.add(labels[method], { type: "success" });
            await this.refreshTasks();
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "The task could not be updated.", { type: "danger" });
        } finally {
            this.state.taskBusy = false;
        }
    },

    refreshTasks() {
        return Promise.all([
            this.state.activeSection === "employee_dashboard" ? this.loadEmployeeWorkspace() : null,
            this.state.myTasks ? this.loadMyTasks({ silent: true }) : null,
            this.isBranchManager ? this.loadTaskManagement({ silent: true }) : null,
            this.isBranchManager ? this.loadTaskSummary() : null,
        ]);
    },

    openTaskPanel(kind) {
        this.state.taskPanel = { kind, first: "", second: "" };
    },

    /** Report or resolve a blocker, or extend the due date; the server decides who may and keeps the record. */
    async saveTaskPanel(task) {
        const panel = this.state.taskPanel;
        const { method, args, done } = {
            blocker: { method: "action_report_blocker", args: [panel.first, panel.second], done: "Blocker sent to your manager." },
            resolve: { method: "action_resolve_blocker", args: [panel.first], done: "Blocker resolved." },
            extend: { method: "action_extend_deadline", args: [panel.first, panel.second], done: "Due date extended." },
        }[panel.kind];
        if (this.state.taskBusy) return;
        this.state.taskBusy = true;
        try {
            await this.orm.call(TASK_MODEL, method, [[task.id], ...args]);
            this.state.taskPanel = null;
            this.notification.add(done, { type: "success" });
            await this.refreshTasks();
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "The task could not be updated.", { type: "danger" });
        } finally {
            this.state.taskBusy = false;
        }
    },

    openTaskForm() {
        this.state.taskForm = { name: "", description: "", assigned_user_id: "", date_deadline: "", priority: "0", objective_id: "" };
    },

    closeTaskForm() {
        if (!this.state.taskBusy) this.state.taskForm = null;
    },

    onTaskFormInput(field, event) {
        this.state.taskForm[field] = event.target.value;
    },

    get taskFormReady() {
        const form = this.state.taskForm;
        return Boolean(form && form.name.trim() && form.description.trim() && form.assigned_user_id && form.date_deadline);
    },

    async saveTask() {
        const form = this.state.taskForm;
        if (!this.taskFormReady || this.state.taskBusy) return;
        this.state.taskBusy = true;
        try {
            await this.orm.call(TASK_MODEL, "create_branch_task", [{
                name: form.name.trim(), description: form.description.trim(), assigned_user_id: Number(form.assigned_user_id),
                date_deadline: form.date_deadline, priority: form.priority, objective_id: form.objective_id ? Number(form.objective_id) : false,
            }]);
            this.state.taskForm = null;
            this.notification.add("Task assigned.", { type: "success" });
            await Promise.all([this.loadTaskManagement({ silent: true }), this.loadTaskSummary()]);
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "The task could not be created.", { type: "danger" });
        } finally {
            this.state.taskBusy = false;
        }
    },

    /** Tasks & Issues reuses the Overview attention list and the People task data. */
    loadTasksIssues(options = {}) {
        return Promise.all([this.loadOverviewExtras(options), this.loadPeople(options)]);
    },

    issueCount(severity) {
        const items = this.state.overviewExtras?.attention;
        if (!items) return "—";
        return items.filter((item) => (severity === "critical") === (item.severity === "critical")).length;
    },

    /** Open Issues, narrowed by the Critical / Warnings card when one is selected. */
    get visibleIssues() {
        const items = this.state.overviewExtras?.attention;
        if (!items) return null;
        const severity = this.state.issueSeverity;
        return severity ? items.filter((item) => (severity === "critical") === (item.severity === "critical")) : items;
    },

    toggleIssueSeverity(severity) {
        this.state.issueSeverity = this.state.issueSeverity === severity ? null : severity;
        this.setPage("task_issues", 1);
    },

    onIssueSeverityKey(event, severity) {
        if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            this.toggleIssueSeverity(severity);
        }
    },

    openTaskCount() {
        const rows = this.state.peopleData?.workload;
        return rows ? rows.reduce((sum, row) => sum + row.open, 0) : "—";
    },

    async openIssueRecord(item) {
        try {
            await this.action.doAction({
                type: "ir.actions.act_window",
                res_model: item.res_model,
                res_id: item.res_id,
                views: [[false, "form"]],
                target: "current",
            });
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "This record is not available with your access rights.", { type: "warning" });
        }
    },

    /** Assign a follow-up activity on the issue's record; live refresh then updates its status. */
    async createIssueFollowup(item) {
        try {
            const result = await this.orm.call(
                "strategic.performance.dashboard",
                "create_attention_followup",
                [item.res_model, item.res_id, item.title, item.evidence || false, item.deadline || false]
            );
            const people = (result.assigned_to || []).join(", ") || "the responsible manager";
            this.notification.add(
                result.created ? `Follow-up assigned to ${people}.` : `A follow-up is already open for ${people}.`,
                { type: result.created ? "success" : "info" }
            );
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "The follow-up could not be created.", { type: "danger" });
        }
    },

    async loadOverviewExtras({ silent = false } = {}) {
        // Comparisons, section summaries, strategy, alerts and activity for the Overview.
        const requestId = this.overviewExtrasRequestId = (this.overviewExtrasRequestId || 0) + 1;
        try {
            const selectedWarehouse = this.state.warehouseId === "all" ? false : Number(this.state.warehouseId);
            const data = await this.orm.silent.call(
                "strategic.performance.dashboard",
                "get_overview_extras",
                [this.state.dateStart || false, this.state.dateEnd || false, selectedWarehouse]
            );
            if (requestId === this.overviewExtrasRequestId) {
                this.state.overviewExtras = data;
            }
        } catch (error) {
            if (!silent && requestId === this.overviewExtrasRequestId) {
                this.state.overviewExtras = null;
            }
            console.error("Overview sections could not be loaded", error);
        }
    },
};
