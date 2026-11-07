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

    // The X-Matrix laid out on a CSS grid, as in the X-Matrix builder: initiatives on top, breakthrough objectives
    // below, annual objectives left, metrics and owners right, and the links in the four corners.
    hoshinMatrix() {
        const x = this.state.hoshin?.xmatrix;
        if (!x) return null;
        const it = x.items, n = (q) => it[q].length;
        const nA = n("A"), nI = n("I"), nM = n("M"), nB = n("B");
        const colMid = nA + 1, colM = nA + 2, colO = nA + 2 + nM, rowMid = nI + 1, rowB = nI + 2;
        const at = (row, col) => `grid-row: ${row}; grid-column: ${col};`;
        const cells = [];
        const corner = (set, rows, cols, row0, col0) => {
            it[rows].forEach((r, i) => it[cols].forEach((c, j) => {
                const link = x.links[`${set}:${i}:${j}`];
                cells.push({ key: `${set}:${i}:${j}`, cls: "o_sm_xm_cell", style: at(row0 + i, col0 + j),
                    dot: link === 1 ? "is-primary" : link === 2 ? "is-supporting" : "",
                    title: `${rows}${i + 1} ${r.name} · ${cols}${j + 1} ${c.name}${link ? (link === 1 ? " · primary" : " · supporting") : ""}` });
            }));
        };
        corner("IA", "I", "A", 1, 1);
        corner("IM", "I", "M", 1, colM);
        corner("IO", "I", "O", 1, colO);
        corner("BA", "B", "A", rowB, 1);
        corner("BM", "B", "M", rowB, colM);
        const band = (q, i, style, vertical) => {
            const item = it[q][i];
            cells.push({ key: `${q}${i}`, style, label: `${q}${i + 1}: ${item.name}`, title: item.name,
                cls: `o_sm_xm_band${vertical ? " is-vertical" : ""}${item.linked ? "" : " is-unlinked"}${item.tone ? " is-" + item.tone : ""}` });
        };
        it.I.forEach((_, i) => band("I", i, at(1 + i, colMid)));
        it.B.forEach((_, i) => band("B", i, at(rowB + i, colMid)));
        it.A.forEach((_, i) => band("A", i, at(rowMid, 1 + i), true));
        it.M.forEach((_, i) => band("M", i, at(rowMid, colM + i), true));
        it.O.forEach((_, i) => band("O", i, at(rowMid, colO + i), true));
        const track = (count, size) => (count ? `repeat(${count}, ${size}) ` : "");
        return {
            plan: x.plan, findings: x.findings, cells, centre: at(rowMid, colMid),
            grid: `grid-template-columns: ${track(nA, "2.4rem")}minmax(18rem, 1fr) ${track(nM + n("O"), "2.4rem")}; `
                + `grid-template-rows: ${track(nI, "2.6rem")}19rem ${track(nB, "2.6rem")};`,
        };
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
