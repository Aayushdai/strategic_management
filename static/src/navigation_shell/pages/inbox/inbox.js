/** @odoo-module **/

// Inbox: Head Office's branch requests (staffing and objectives).
// These functions become methods of StrategicNavigationShell (see navigation_shell.js); this page's
// template is inbox.xml and its styles inbox.scss, next to this file.

export const inboxPage = {
    /** Head Office's Overview lists the staffing requests waiting on Head Office: a decision, or employees to register. */
    async loadPendingStaffing() {
        if (!this.isHeadOffice) return;
        try {
            const data = await this.orm.silent.call("strategic.staffing.request", "get_staffing_requests", [{}]);
            this.state.staffingWork = data.requests.filter((row) => row.state === "submitted" || (row.state === "approved" && row.remaining));
        } catch {
            this.state.staffingWork = [];
        }
    },

    // ------------------------------------------------------------ objective requests
    async loadObjectiveRequests({ silent = false } = {}) {
        const requestId = this.requestsRequestId = (this.requestsRequestId || 0) + 1;
        if (!silent) {
            this.state.requestsLoading = true;
        }
        try {
            const selectedWarehouse = this.state.warehouseId === "all" ? false : Number(this.state.warehouseId);
            const data = await this.orm.silent.call("strategic.performance.dashboard", "get_objective_requests", [selectedWarehouse]);
            if (requestId === this.requestsRequestId) {
                this.state.requestsData = data;
            }
        } catch (error) {
            if (!silent && requestId === this.requestsRequestId) {
                this.state.requestsData = { available: false, pending: [], history: [], messages: [error.data?.message || error.message || "Objective requests could not be loaded."] };
            }
            console.error("Objective requests could not be loaded", error);
        } finally {
            if (requestId === this.requestsRequestId) {
                this.state.requestsLoading = false;
            }
        }
    },

    toggleRequests(key) {
        this.state.reqExpanded[key] = !this.state.reqExpanded[key];
    },

    get filteredRequestHistory() {
        const rows = this.state.requestsData?.history || [];
        return this.state.historyStatus === "all" ? rows : rows.filter((row) => row.decision === this.state.historyStatus);
    },

    onHistoryStatusChanged(event) {
        this.state.historyStatus = event.target.value;
        this.state.pages.req_history = 0;
    },

    // Pending rows are keyed by objective, history rows by decision ("p:12" / "h:7"), so the two never collide.
    get reviewRequest() {
        const key = this.state.reviewRequestId;
        const data = this.state.requestsData;
        if (!key || !data) return null;
        const id = Number(key.slice(2));
        return key.startsWith("p:")
            ? (data.pending || []).find((row) => row.objective_id === id) || null
            : (data.history || []).find((row) => row.review_id === id) || null;
    },

    openRequestReview(row) {
        this.state.reviewRequestId = row.review_id ? `h:${row.review_id}` : `p:${row.objective_id}`;
    },

    closeRequestReview() {
        this.state.reviewRequestId = null;
    },

    openRejectRequest(row) {
        this.state.rejectRequestId = row.objective_id;
        this.state.rejectReason = "";
    },

    closeRejectRequest() {
        if (!this.state.deciding) {
            this.state.rejectRequestId = null;
        }
    },

    get rejectRequest() {
        return (this.state.requestsData?.pending || []).find((row) => row.objective_id === this.state.rejectRequestId) || null;
    },

    onRejectReasonInput(event) {
        this.state.rejectReason = event.target.value;
    },

    /** Approve or reject on the server; the lists refresh from its answer, so nothing changes before it confirms. */
    async decideRequest(row, decision) {
        if (this.state.deciding) return;
        const reason = decision === "reject" ? this.state.rejectReason.trim() : false;
        if (decision === "reject" && !reason) {
            this.notification.add("Enter a reason for rejecting this objective.", { type: "warning" });
            return;
        }
        this.state.deciding = true;
        try {
            const selectedWarehouse = this.state.warehouseId === "all" ? false : Number(this.state.warehouseId);
            const data = await this.orm.call("strategic.performance.dashboard", "decide_objective_request", [row.objective_id, decision, reason, selectedWarehouse]);
            this.state.requestsData = data;
            this.state.rejectRequestId = null;
            this.state.reviewRequestId = null;
            this.notification.add(`"${row.name}" ${decision === "approve" ? "approved and activated" : "rejected"}.`, { type: "success" });
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "The decision could not be saved.", { type: "danger" });
            await this.loadObjectiveRequests({ silent: true });
        } finally {
            this.state.deciding = false;
        }
    },

    async openRequestObjective(objectiveId) {
        try {
            await this.action.doAction({
                type: "ir.actions.act_window",
                res_model: "strategic.objective",
                res_id: objectiveId,
                views: [[false, "form"]],
                target: "current",
            });
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "This objective is not available with your access rights.", { type: "warning" });
        }
    },

    /** Server datetimes are UTC ("2026-10-27 14:55:58"); show the local calendar date. */
    formatRequestDate(value) {
        if (!value) return "—";
        const date = new Date(String(value).replace(" ", "T") + (String(value).length > 10 ? "Z" : "T00:00:00"));
        return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
    },
};
