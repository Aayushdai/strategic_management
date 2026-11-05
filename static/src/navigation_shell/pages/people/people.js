/** @odoo-module **/

// People overview and roles.
// These functions become methods of StrategicNavigationShell (see navigation_shell.js); this page's
// template is people.xml and its styles people.scss, next to this file.

export const peoplePage = {
    async loadPeople({ silent = false } = {}) {
        // Ignore responses that arrive after a newer request (filter change or live refresh).
        const requestId = ++this.peopleRequestId;
        if (!silent) {
            this.state.peopleLoading = true;
        }
        try {
            const selectedWarehouse = this.state.warehouseId === "all" ? false : Number(this.state.warehouseId);
            const data = await this.orm.silent.call(
                "strategic.performance.dashboard",
                "get_people_overview_data",
                [this.state.dateStart || false, this.state.dateEnd || false, selectedWarehouse]
            );
            if (requestId === this.peopleRequestId) {
                this.state.peopleData = data;
            }
        } catch (peopleError) {
            // A failed background refresh keeps the last good figures on screen.
            if (requestId === this.peopleRequestId && !silent) {
                this.state.peopleData = { messages: [peopleError.data?.message || peopleError.message || "People data could not be loaded."] };
            }
            console.error("People dashboard data could not be loaded", peopleError);
        } finally {
            if (requestId === this.peopleRequestId) {
                this.state.peopleLoading = false;
            }
        }
    },

    async loadRoles() {
        if (!this.isHeadOffice) return;
        try {
            this.state.roles = await this.orm.silent.call("strategic.performance.dashboard", "get_role_assignments", []);
            this.state.rolesError = "";
        } catch (error) {
            this.state.rolesError = error.data?.message || error.message || "Roles could not be loaded.";
        }
    },

    openRoleForm(person) {
        this.state.roleForm = { person, role: person.role || "", warehouse_id: person.branch_id ? String(person.branch_id) : "" };
    },

    async saveRoleForm() {
        const form = this.state.roleForm;
        if (this.state.roleSaving) return;
        this.state.roleSaving = true;
        try {
            await this.orm.call("strategic.performance.dashboard", "set_user_role", [form.person.id, form.role, form.warehouse_id ? Number(form.warehouse_id) : false]);
            this.notification.add(`Role saved for ${form.person.name}. It applies the next time they open the app.`, { type: "success" });
            this.state.roleForm = null;
            await this.loadRoles();
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "The role could not be saved.", { type: "danger" });
        } finally {
            this.state.roleSaving = false;
        }
    },

    peopleBarWidth(value, rows, ...fields) {
        const max = Math.max(1, ...(rows || []).flatMap((row) => fields.map((field) => Number(row[field]) || 0)));
        return Math.max(0, Math.min(100, ((Number(value) || 0) / max) * 100));
    },

    peopleStaffingClass(status) {
        return { covered: "o_sm_people_fill_green", understaffed: "o_sm_people_fill_red" }[status] || "o_sm_people_fill_amber";
    },

    peopleTaskClass(status) {
        return { Overdue: "o_sm_people_task_open", "Due today": "o_sm_people_task_progress", Done: "o_sm_people_coverage_good" }[status] || "o_sm_people_badge_low";
    },
};
