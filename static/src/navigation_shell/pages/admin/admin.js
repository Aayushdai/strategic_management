/** @odoo-module **/

// Admin console: overview, people and access, branches, work records, system health and the audit log.
// These functions become methods of StrategicNavigationShell (see navigation_shell.js); this page's
// template is admin.xml and its styles admin.scss, next to this file.
// The server (models/admin_console.py) returns no money, personal details, ratings or notes.

const CONSOLE = "strategic.admin.console";
const ADMIN_PAGE_SIZE = 15;
const WORK_KINDS = [
    { key: "objectives", label: "Objectives" },
    { key: "goals", label: "Business goals" },
    { key: "tasks", label: "Tasks" },
    { key: "reports", label: "Branch reports" },
    { key: "staffing", label: "Staffing requests" },
];
const SECTION_LOADS = {
    admin_home: ["overview", "get_admin_overview", []],
    admin_people: ["people", "get_admin_people", []],
    admin_branches: ["branches", "get_admin_branches", []],
    admin_health: ["health", "get_admin_health", []],
    admin_audit: ["audit", "get_admin_audit", []],
};

export const adminPage = {
    get adminWorkKinds() {
        return WORK_KINDS;
    },

    /** Load the open Admin section; `silent` keeps the current figures on screen while refreshing. */
    async loadAdminSection({ silent = false } = {}) {
        const admin = this.state.admin;
        const section = this.state.activeSection;
        if (!silent) admin.error = "";
        try {
            if (section === "admin_work") {
                const data = await this.orm.call(CONSOLE, "get_admin_work", [admin.workKind]);
                admin.work = data;
                return;
            }
            const load = SECTION_LOADS[section];
            if (!load) return;
            admin[load[0]] = await this.orm.call(CONSOLE, load[1], load[2]);
            if (section === "admin_people" && !admin.peopleOptions) admin.peopleOptions = admin.people;
        } catch (error) {
            admin.error = error.data?.message || error.message || "This page could not be loaded.";
        }
    },

    adminGo(section) {
        this.selectSection(section);
    },

    setAdminWorkKind(kind) {
        this.state.admin.workKind = kind;
        this.state.admin.work = null;
        this.state.admin.workStatus = "all";
        this.state.pages.admin_work = 1;
        return this.loadAdminSection();
    },

    adminSetFilter(key, event) {
        this.state.admin[key] = event.target.type === "checkbox" ? event.target.checked : event.target.value;
        const page = { search: "admin_people", roleFilter: "admin_people", showInactive: "admin_people", workSearch: "admin_work",
            workStatus: "admin_work", auditAction: "admin_audit" }[key];
        if (page) this.state.pages[page] = 1;
    },

    get adminPeopleRows() {
        const admin = this.state.admin;
        const term = (admin.search || "").trim().toLowerCase();
        return (admin.people?.people || []).filter((person) => (admin.showInactive || person.active)
            && (admin.roleFilter === "all" || (admin.roleFilter === "none" ? !person.role : person.role === admin.roleFilter))
            && (!term || `${person.name} ${person.login} ${person.branch || ""}`.toLowerCase().includes(term)));
    },

    get adminRoleMax() {
        return Math.max(1, ...(this.state.admin.overview?.people.roles || []).map((role) => role.count));
    },

    get adminWorkStatuses() {
        const seen = new Map();
        for (const row of this.state.admin.work?.rows || []) seen.set(row.status_key, row.status);
        return [...seen].map(([key, label]) => ({ key, label }));
    },

    get adminWorkRows() {
        const admin = this.state.admin;
        const term = (admin.workSearch || "").trim().toLowerCase();
        return (admin.work?.rows || []).filter((row) => (admin.workStatus === "all" || (admin.workStatus === "overdue" ? row.overdue : row.status_key === admin.workStatus))
            && (!term || `${row.title} ${row.branch || ""} ${row.owner || ""}`.toLowerCase().includes(term)));
    },

    get adminAuditRows() {
        const action = this.state.admin.auditAction;
        return (this.state.admin.audit?.entries || []).filter((entry) => action === "all" || entry.action_key === action);
    },

    // ------------------------------------------------------------ paging (15 rows; the shared pager shows 5)
    adminPageCount(rows) {
        return Math.max(1, Math.ceil((rows || []).length / ADMIN_PAGE_SIZE));
    },

    adminPageOf(key, rows) {
        return Math.min(this.state.pages[key] || 1, this.adminPageCount(rows));
    },

    adminPageRows(key, rows) {
        const page = this.adminPageOf(key, rows);
        return (rows || []).slice((page - 1) * ADMIN_PAGE_SIZE, page * ADMIN_PAGE_SIZE);
    },

    adminDateTime(value) {
        if (!value) return "—";
        const date = new Date(value.replace(" ", "T") + "Z");
        return Number.isNaN(date.getTime()) ? value : date.toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
    },

    adminDate(value) {
        if (!value) return "—";
        const date = new Date(value + "T00:00:00");
        return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
    },

    adminStaffWidth(branch) {
        if (!branch.planned) return branch.staff ? 100 : 0;
        return Math.min(100, Math.round((branch.staff / branch.planned) * 100));
    },

    /** Server refusals carry a plain sentence; show that, never a stack trace. */
    adminErrorText(error) {
        return error.data?.message || error.message || "This change could not be saved.";
    },

    // ------------------------------------------------------------ people
    openAdminEdit(person) {
        this.state.admin.edit = { person, role: person.role && person.role !== "head_office" ? person.role : "employee", branchId: person.branch_id || "",
            departmentIds: [...person.department_ids], error: "" };
    },

    openAdminCreate() {
        this.state.admin.create = { name: "", email: "", role: "employee", branchId: "", departmentIds: [], error: "", result: null };
    },

    closeAdminDialog() {
        Object.assign(this.state.admin, { edit: null, create: null, confirm: null, branchEdit: null });
    },

    adminFormValue(form, key, event) {
        form[key] = event.target.value;
        if (key === "role" && event.target.value !== "employee") form.departmentIds = [];
    },

    adminToggleDepartment(form, id, event) {
        form.departmentIds = event.target.checked ? [...new Set([...form.departmentIds, id])] : form.departmentIds.filter((value) => value !== id);
    },

    async adminSave(method, args, done) {
        const admin = this.state.admin;
        if (admin.busy) return;
        admin.busy = true;
        try {
            const result = await this.orm.call(CONSOLE, method, args);
            await done(result);
            await this.loadAdminSection({ silent: true });
        } catch (error) {
            const form = admin.edit || admin.create || admin.branchEdit || admin.confirm;
            if (form) form.error = this.adminErrorText(error);
            else this.notification.add(this.adminErrorText(error), { type: "danger" });
        } finally {
            admin.busy = false;
        }
    },

    saveAdminEdit() {
        const form = this.state.admin.edit;
        const branch = form.role === "admin" ? false : Number(form.branchId) || false;
        return this.adminSave("admin_set_user_access", [form.person.id, form.role, branch, form.departmentIds], () => {
            this.notification.add(`${form.person.name}'s access was updated.`, { type: "success" });
            this.closeAdminDialog();
        });
    },

    saveAdminCreate() {
        const form = this.state.admin.create;
        const branch = form.role === "admin" ? false : Number(form.branchId) || false;
        return this.adminSave("admin_create_user", [form.name, form.email, form.role, branch, form.departmentIds], (result) => {
            form.result = result;
        });
    },

    askAdminActive(person, active) {
        this.state.admin.confirm = { person, active, error: "" };
    },

    confirmAdminActive() {
        const { person, active } = this.state.admin.confirm;
        return this.adminSave("admin_set_user_active", [person.id, active], () => {
            this.notification.add(`${person.name} was ${active ? "activated" : "deactivated"}.`, { type: "success" });
            this.closeAdminDialog();
        });
    },

    sendAdminReset(person) {
        return this.adminSave("admin_send_reset_link", [person.id], (result) => {
            this.notification.add(result.message, { type: result.sent ? "success" : "warning", sticky: !result.sent });
        });
    },

    // ------------------------------------------------------------ branches
    openAdminBranch(branch) {
        this.state.admin.branchEdit = { branch, name: branch.name, code: branch.code, planned: String(branch.planned), error: "" };
    },

    saveAdminBranch() {
        const form = this.state.admin.branchEdit;
        const planned = Number(form.planned);
        if (!Number.isInteger(planned) || planned < 0) {
            form.error = "Planned staff is a whole number, 0 or more.";
            return;
        }
        return this.adminSave("admin_update_branch", [form.branch.id, form.name, form.code, planned], () => {
            this.notification.add("Branch saved.", { type: "success" });
            this.closeAdminDialog();
        });
    },
};
