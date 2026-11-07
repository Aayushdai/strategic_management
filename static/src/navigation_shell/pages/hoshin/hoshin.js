/** @odoo-module **/

// Hoshin: Head Office's monthly review of the Hoshin metrics (the bowling chart), and the way into the X-Matrix.
// These functions become methods of StrategicNavigationShell (see navigation_shell.js); this page's template is
// hoshin.xml and its styles hoshin.scss. Server: models/hoshin_review.py.

export const hoshinPage = {
    async loadHoshinReview(year = false) {
        this.state.hoshinError = "";
        try {
            this.state.hoshin = await this.orm.call("strategic.performance.dashboard", "get_hoshin_review", [year || this.state.hoshin?.year || false]);
        } catch (error) {
            this.state.hoshinError = error.data?.message || error.message || "The Hoshin review could not be loaded.";
        }
    },

    onHoshinYear(event) {
        return this.loadHoshinReview(Number(event.target.value));
    },

    openCountermeasure(row) {
        const c = row.countermeasure;
        this.state.hoshinCountermeasure = { row, id: c.id, cause: c.cause, action: c.action, due_date: c.due_date, error: "", saving: false };
    },

    async saveCountermeasure(done) {
        const form = this.state.hoshinCountermeasure;
        if (form.saving) return;
        form.saving = true;
        try {
            await this.orm.call("strategic.hoshin.countermeasure", "save_countermeasure", [form.id, { cause: form.cause, action: form.action, due_date: form.due_date }, done]);
            this.notification.add(done ? "Countermeasure closed." : "Countermeasure saved.", { type: "success" });
            this.state.hoshinCountermeasure = null;
            await this.loadHoshinReview();
        } catch (error) {
            form.saving = false;
            form.error = error.data?.message || error.message || "The countermeasure could not be saved.";
        }
    },

    openXMatrix() {
        return this.action.doAction("strategic_management.action_strategic_hoshin_xmatrix_workspace");
    },

    hoshinNumber(value) {
        if (value === false || value === undefined || value === null) return "—";
        return Number(value).toLocaleString(undefined, { maximumFractionDigits: Math.abs(value) < 100 ? 1 : 0 });
    },

    hoshinCellTitle(row, cell) {
        if (cell.tone === "future") return "Not reached yet";
        if (cell.target === undefined) return "No valid result this month";
        return `${row.name}: ${this.hoshinNumber(cell.actual)} against ${this.hoshinNumber(cell.target)} ${row.unit} · ${cell.status || ""}`;
    },
};
