/** @odoo-module **/

// KPI and OKR work requests: Head Office asks an Admin for a KPI or OKR, the Admin proposes one, Head Office
// reviews it. Head Office's card sits on the Inbox; the Admin's list is the Admin console's "KPI & OKR Requests".
// These functions become methods of StrategicNavigationShell (see navigation_shell.js); this page's template is
// kpi_requests.xml and its styles kpi_requests.scss. Server: models/kpi_work_request.py.

const WORK_REQUEST_MODEL = "strategic.kpi.work.request";

export const kpiRequestsPage = {
    // ------------------------------------------------------------ KPI/OKR work requests (assigned Admin)
    async loadAdminRequests() {
        if (!this.isAdminRole) return;
        try {
            this.state.adminRequests = await this.orm.silent.call(WORK_REQUEST_MODEL, "get_my_work_requests", []);
            this.state.adminRequestsError = "";
        } catch (error) {
            this.state.adminRequestsError = error.data?.message || error.message || "Your requests could not be loaded.";
        }
    },

    openAdminRequest(row) {
        const proposal = row.proposal || {};
        const blank = { kpi_name: "", kpi_description: "", source_type: "", calculation_type: "", measurement_method: "", unit: "", direction: "", frequency: "", level: "", target_value: "",
                        objective_title: "", objective_description: "", perspective: "", key_results: [] };
        // Period and window start from what Head Office asked for.
        this.state.adminForm = { ...blank, ...proposal, row, proposal_type: proposal.proposal_type || (row.request_type === "okr" ? "okr" : "kpi"),
                                 date_start: proposal.date_start || row.date_start, date_end: proposal.date_end || row.date_end,
                                 time_window_start: proposal.time_window_start || row.time_start, time_window_end: proposal.time_window_end || row.time_end,
                                 key_results: (proposal.key_results || []).map((line) => ({ ...line })) };
    },

    // ------------------------------------------------------------ the Admin's own proposals
    openAdminStart() {
        this.state.adminStart = { name: "", description: "", request_type: "kpi", department_id: "", scope: "all", warehouse_ids: [], date_start: "", date_end: "", error: "" };
    },

    toggleAdminStartBranch(branchId, event) {
        const form = this.state.adminStart;
        form.warehouse_ids = event.target.checked ? [...form.warehouse_ids, branchId] : form.warehouse_ids.filter((value) => value !== branchId);
    },

    /** Create the proposal on the server, then go straight to writing it. */
    async saveAdminStart() {
        const form = this.state.adminStart;
        if (this.state.adminSaving) return;
        this.state.adminSaving = true;
        try {
            const { error, ...values } = form;
            const row = await this.orm.call(WORK_REQUEST_MODEL, "admin_start_proposal", [{ ...values, department_id: Number(values.department_id) || false }]);
            this.state.adminStart = null;
            await this.loadAdminRequests();
            this.openAdminRequest(row);
        } catch (error) {
            form.error = error.data?.message || error.message || "The proposal could not be started.";
        } finally {
            this.state.adminSaving = false;
        }
    },

    async withdrawOwnProposal(row) {
        if (await this.adminCall("action_withdraw_own_proposal", row, [], "Proposal withdrawn.")) {
            this.state.adminForm = null;
            this.state.adminWithdraw = null;
        }
    },

    calculationsFor(source) {
        const options = this.state.adminRequests?.calculations || [];
        return options.filter((option) => option.source === source || (source === "other" && option.id === "manual_value"));
    },

    get adminWindowAllowed() {
        return this.calculationsFor(this.state.adminForm?.source_type).some((option) => option.id === this.state.adminForm?.calculation_type && option.window);
    },

    addProposalKeyResult() {
        this.state.adminForm.key_results.push({ name: "", measurement_method: "", unit: "", baseline: "", target: "", source_type: "", calculation_type: "" });
    },

    /** One server step, then a fresh list; nothing changes on screen before the server confirms. */
    async adminCall(method, row, args, message) {
        if (this.state.adminSaving) return false;
        this.state.adminSaving = true;
        try {
            await this.orm.call(WORK_REQUEST_MODEL, method, [[row.id], ...args]);
            this.notification.add(message, { type: "success" });
            await this.loadAdminRequests();
            return true;
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "This could not be saved.", { type: "danger" });
            return false;
        } finally {
            this.state.adminSaving = false;
        }
    },

    async acceptAdminRequest(row) {
        if (await this.adminCall("action_accept_start", row, [], "Work started. The requirement is now locked.")) {
            this.openAdminRequest(this.state.adminRequests.requests.find((item) => item.id === row.id));
        }
    },

    async saveProposal(submit) {
        const { row, ...values } = this.state.adminForm;
        if (await this.adminCall("save_proposal", row, [values, submit], submit ? "Proposal sent to Head Office." : "Draft saved.")) this.state.adminForm = null;
    },

    adminPeriodLabel(row) {
        const parts = [row.scope === "all" ? row.scope_label : row.branches.join(", ")];
        if (row.date_start) parts.push(`${this.formatPurchaseDate(row.date_start)} – ${this.formatPurchaseDate(row.date_end)}`);
        if (row.time_start) parts.push(`${row.time_start}–${row.time_end}`);
        return parts.join(" · ");
    },

    // ------------------------------------------------------------ KPI/OKR work requests (Head Office)
    async loadWorkRequests() {
        if (!this.isHeadOffice) return;
        try {
            this.state.workRequests = await this.orm.silent.call(WORK_REQUEST_MODEL, "get_work_requests", []);
            this.state.workRequestsError = "";
        } catch (error) {
            this.state.workRequestsError = error.data?.message || error.message || "Work requests could not be loaded.";
        }
    },

    openWorkForm(row = null) {
        this.state.workForm = row
            ? { id: row.id, is_draft: row.state === "draft", name: row.name, description: row.description, request_type: row.request_type, admin_user_id: row.admin_user_id ? String(row.admin_user_id) : "", scope: row.scope,
                warehouse_ids: [...row.warehouse_ids], date_start: row.date_start, date_end: row.date_end, time_start: row.time_start, time_end: row.time_end, instructions: row.instructions }
            : { id: false, is_draft: true, name: "", description: "", request_type: "kpi", admin_user_id: "", scope: "all", warehouse_ids: [], date_start: "", date_end: "", time_start: "", time_end: "", instructions: "" };
    },

    toggleWorkBranch(branchId, event) {
        const form = this.state.workForm;
        form.warehouse_ids = event.target.checked ? [...form.warehouse_ids, branchId] : form.warehouse_ids.filter((value) => value !== branchId);
    },

    /** One server step, then a fresh list; nothing changes on screen before the server confirms. */
    async workRequestCall(method, args, message) {
        if (this.state.workSaving) return false;
        this.state.workSaving = true;
        try {
            await this.orm.call(WORK_REQUEST_MODEL, method, args);
            this.notification.add(message, { type: "success" });
            await this.loadWorkRequests();
            return true;
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "The request could not be saved.", { type: "danger" });
            return false;
        } finally {
            this.state.workSaving = false;
        }
    },

    async saveWorkForm(submit) {
        const form = this.state.workForm;
        const values = { ...form, admin_user_id: form.admin_user_id ? Number(form.admin_user_id) : false };
        const message = !form.is_draft ? "Changes saved." : submit ? "Request submitted." : "Draft saved.";
        if (await this.workRequestCall("save_work_request", [values, submit], message)) this.state.workForm = null;
    },

    openWorkAssign(row) {
        this.state.workAssign = { row, admin_user_id: row.admin_user_id ? String(row.admin_user_id) : "" };
    },

    async saveWorkAssign() {
        const form = this.state.workAssign;
        if (await this.workRequestCall("action_assign_admin", [[form.row.id], Number(form.admin_user_id)], "Admin assigned.")) this.state.workAssign = null;
    },

    async confirmWorkWithdraw() {
        if (await this.workRequestCall("action_withdraw", [[this.state.workWithdraw.id]], "Request withdrawn.")) this.state.workWithdraw = null;
    },

    /** Server times are UTC; show them in the viewer's own time. */
    get workReview() {
        return (this.state.workRequests?.requests || []).find((row) => row.id === this.state.workReviewId) || null;
    },

    openWorkReview(row) {
        this.state.workReviewId = row.id;
        this.state.workReviewComment = "";
    },

    async reviewWorkRequest(decision) {
        const messages = { approve: "Proposal approved and activated.", changes: "Changes requested.", reject: "Proposal rejected." };
        if (await this.workRequestCall("action_review", [[this.state.workReviewId], decision, this.state.workReviewComment], messages[decision])) this.state.workReviewComment = "";
    },

    async saveWorkRecover() {
        const form = this.state.workRecover;
        if (await this.workRequestCall("action_recover_abandoned", [[form.row.id], Number(form.admin_user_id), form.reason], "Request reassigned. Any draft is kept.")) this.state.workRecover = null;
    },

    formatWorkDateTime(value) {
        return new Date(`${value.replace(" ", "T")}Z`).toLocaleString(undefined, { day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit" });
    },

    deleteWorkDraft(row) {
        return this.workRequestCall("action_delete_draft", [[row.id]], "Draft deleted.");
    },

    workPeriodLabel(row) {
        const parts = [row.scope === "all" ? row.scope_label : row.branches.join(", ")];
        if (row.date_start) parts.push(`${this.formatPurchaseDate(row.date_start)} – ${this.formatPurchaseDate(row.date_end)}`);
        if (row.time_start) parts.push(`${row.time_start}–${row.time_end}`);
        return parts.join(" · ");
    },
};
