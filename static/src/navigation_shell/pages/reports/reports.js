/** @odoo-module **/

// Reports and Settings.
// These functions become methods of StrategicNavigationShell (see navigation_shell.js); this page's
// template is reports.xml and its styles reports.scss, next to this file.

import { SECTION_LINKS, REPORT_MODEL, REPORT_SECTIONS, CONFIG_GROUPS } from "../../constants";

export const reportsPage = {
    // ------------------------------------------------------------ Branch reports
    get canUseReports() {
        return ["branch_manager", "head_office"].includes(this.state.navContext?.role);
    },

    async loadReports({ silent = false } = {}) {
        if (!this.canUseReports || this.branchError) return;
        try {
            this.state.reports = await this.orm.silent.call(REPORT_MODEL, "get_branch_reports", []);
            this.state.reportsError = "";
        } catch (error) {
            if (!silent) this.state.reportsError = error.data?.message || error.message || "Reports could not be loaded.";
        }
        if (this.state.navContext?.role === "head_office") {
            try {
                this.state.openBlockers = await this.orm.silent.call("strategic.performance.dashboard", "get_open_blockers", []);
            } catch {
                this.state.openBlockers = null;
            }
        }
    },

    showReport(data) {
        this.state.reportDetail = data;
        this.state.reportComment = data.comment || "";
        this.state.reportNote = "";
    },

    async newReport() {
        if (this.state.reportBusy) return;
        this.state.reportBusy = true;
        try {
            this.showReport(await this.orm.call(REPORT_MODEL, "create_branch_report", [this.state.dateStart, this.state.dateEnd]));
            await this.loadReports({ silent: true });
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "The report could not be prepared.", { type: "danger" });
        } finally {
            this.state.reportBusy = false;
        }
    },

    async openReport(row) {
        try {
            this.showReport(await this.orm.call(REPORT_MODEL, "get_branch_report", [row.id]));
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "This report could not be opened.", { type: "danger" });
        }
    },

    onReportComment(event) {
        this.state.reportComment = event.target.value;
    },

    onReportNote(event) {
        this.state.reportNote = event.target.value;
    },

    closeReport() {
        if (!this.state.reportBusy) this.state.reportDetail = null;
    },

    /** One workflow step on the server; the list and the report reload from the server afterwards. */
    async runReportAction(method, args, done) {
        if (this.state.reportBusy) return;
        this.state.reportBusy = true;
        try {
            await this.orm.call(REPORT_MODEL, method, [[this.state.reportDetail.id], ...args]);
            this.notification.add(done, { type: "success" });
            this.state.reportDetail = null;
            await this.loadReports({ silent: true });
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "The report could not be updated.", { type: "danger" });
        } finally {
            this.state.reportBusy = false;
        }
    },

    submitReport() {
        return this.runReportAction("action_submit", [this.state.reportComment], "Report submitted to Head Office.");
    },

    reviewReport(decision) {
        const note = this.state.reportNote.trim();
        if (decision === "changes" && !note) {
            this.notification.add("Explain what needs to change.", { type: "warning" });
            return;
        }
        return decision === "changes"
            ? this.runReportAction("action_request_changes", [note], "Changes requested.")
            : this.runReportAction("action_mark_reviewed", [note], "Report marked as reviewed.");
    },

    /** The saved (or previewed) figures as labelled rows; a missing figure is said to be missing, never zero. */
    reportSections(snapshot) {
        return REPORT_SECTIONS.map(([title, key, fields]) => {
            const data = snapshot?.[key] || {};
            return {
                title, key, available: Boolean(data.available), messages: data.messages || [],
                rows: fields.map(([label, field, kind]) => [label,
                    kind === "money" ? this.formatOpsMoney(data[field], data) : this.formatOpsCount(data[field])]),
            };
        });
    },

    /** Settings: the configuration links of this role, grouped. */
    get configGroups() {
        const links = this.sectionLinks("configuration");
        return CONFIG_GROUPS.map((group) => ({ ...group, links: links.filter((link) => link.group === group.key) })).filter((group) => group.links.length);
    },

    sectionLinks(sectionId) {
        const role = this.state.data?.role || "employee";
        return (SECTION_LINKS[sectionId] || []).filter((link) => link.roles.includes(role));
    },
};
