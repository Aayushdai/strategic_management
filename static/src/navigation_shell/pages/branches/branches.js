/** @odoo-module **/

// Branches, and the branch operations panel other pages reuse.
// These functions become methods of StrategicNavigationShell (see navigation_shell.js); this page's
// template is branches.xml and its styles branches.scss, next to this file.

export const branchesPage = {
    // ------------------------------------------------------------ Branch operations
    /** Sales, inventory and purchasing for the manager's own branch; the server decides the branch, the shared dates the period. */
    async loadBranchOps({ silent = false } = {}) {
        const code = this.departmentCode;
        if ((!this.isBranchManager && !code) || this.branchError) return;
        const requestId = (this.branchOpsRequestId = (this.branchOpsRequestId || 0) + 1);
        if (!silent) this.state.branchOpsLoading = true;
        try {
            const period = [this.state.dateStart || false, this.state.dateEnd || false];
            const data = await this.orm.silent.call("strategic.performance.dashboard", code ? "get_department_operations" : "get_branch_operations",
                code ? [code, ...period, this.state.departmentProductId] : period);
            if (requestId === this.branchOpsRequestId) {
                this.state.branchOps = data;
                this.state.branchOpsError = "";
            }
        } catch (error) {
            // A failed background refresh keeps the last good figures on screen.
            // Except a department: a refusal means the access is gone, so its figures must not stay on screen.
            if (requestId === this.branchOpsRequestId && (!silent || code)) {
                this.state.branchOps = null;
                this.state.branchOpsError = error.data?.message || error.message || "Branch figures could not be loaded.";
            }
        } finally {
            if (requestId === this.branchOpsRequestId) this.state.branchOpsLoading = false;
        }
    },

    setOpsTab(tab) {
        this.state.opsTab = tab;
    },

    /** Money in the section's own currency; a missing figure says so instead of showing zero. */
    formatOpsMoney(value, section) {
        if (value === false || value === null || value === undefined || !Number.isFinite(Number(value))) return "Not available";
        const currency = section?.currency || {};
        const digits = Number.isInteger(currency.decimal_places) ? Math.max(0, Math.min(6, currency.decimal_places)) : 2;
        const formatted = Number(value).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
        const symbol = currency.symbol || currency.name || "";
        return currency.position === "after" ? `${formatted} ${symbol}`.trim() : `${symbol}${formatted}`;
    },

    formatOpsCount(value) {
        return value === false || value === null || value === undefined ? "Not available" : Number(value).toLocaleString("en-US");
    },

    /** One bar per day, week or month of the period; heights relative to the best bucket (refunds can make one negative). */
    get opsTrendBars() {
        const trend = this.state.branchOps?.sales?.trend || [];
        const max = Math.max(0, ...trend.map((row) => row.sales));
        const step = Math.ceil(trend.length / 8);
        return trend.map((row, index) => ({ ...row, height: max > 0 ? Math.max(0, Math.round((row.sales / max) * 100)) : 0,
                                             showLabel: index % step === 0 || index === trend.length - 1 }));
    },

    async loadBranches({ silent = false } = {}) {
        if (this.isBranchManager) {
            this.loadBranchOps({ silent });
        }
        // Ignore responses that arrive after a newer request (filter change or live refresh).
        const requestId = ++this.branchesRequestId;
        if (!silent) {
            this.state.branchesLoading = true;
        }
        try {
            const selectedWarehouse = this.state.warehouseId === "all" ? false : Number(this.state.warehouseId);
            const data = await this.orm.silent.call(
                "strategic.performance.dashboard",
                "get_branches_overview_data",
                [this.state.dateStart || false, this.state.dateEnd || false, selectedWarehouse]
            );
            if (requestId === this.branchesRequestId) {
                this.state.branchesData = data;
            }
        } catch (branchesError) {
            // A failed background refresh keeps the last good figures on screen.
            if (requestId === this.branchesRequestId && !silent) {
                this.state.branchesData = { available: false, messages: [branchesError.data?.message || branchesError.message || "Branch data could not be loaded."] };
            }
            console.error("Branches dashboard data could not be loaded", branchesError);
        } finally {
            if (requestId === this.branchesRequestId) {
                this.state.branchesLoading = false;
            }
        }
    },

    /** Rows for one branch comparison chart; missing values render as "Not Evaluated". */
    branchChartRows(field) {
        const rows = this.state.branchesData?.branches || [];
        const isMissing = (value) => value === false || value === null || value === undefined;
        const values = rows.map((row) => (isMissing(row[field]) ? 0 : Math.abs(Number(row[field]) || 0)));
        const max = Math.max(1, ...values);
        return rows.map((row, index) => ({
            id: row.warehouse_id,
            name: row.name,
            value: row[field],
            width: Math.min(100, (values[index] / max) * 100),
        }));
    },

    formatBranchValue(value, kind) {
        if (value === false || value === null || value === undefined) return "Not Evaluated";
        if (kind === "currency") {
            const currency = this.state.branchesData?.currency || {};
            const formatted = Number(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
            const symbol = currency.symbol || "";
            return currency.position === "after" ? `${formatted} ${symbol}`.trim() : `${symbol}${formatted}`;
        }
        if (kind === "percent") return `${Number(value).toLocaleString("en-US", { maximumFractionDigits: 1 })}%`;
        return Number(value).toLocaleString("en-US", { maximumFractionDigits: 2 });
    },

    toggleBranches(key) {
        this.state.brExpanded[key] = !this.state.brExpanded[key];
    },

    /** Sales chart rows, largest first; branches without readable sales stay listed as "Not Evaluated". */
    get branchSalesRows() {
        const missing = (value) => value === false || value === null || value === undefined;
        return this.branchChartRows("sales").sort((a, b) => (missing(b.value) ? -1 : b.value) - (missing(a.value) ? -1 : a.value));
    },

    /** Attainment is already a percentage of target: draw it on a 0-100 scale, not relative to the best branch. */
    branchAttainmentWidth(row) {
        return row.attainment === false || row.attainment === null || row.attainment === undefined ? 0 : Math.max(0, Math.min(100, Number(row.attainment) || 0));
    },

    /** First sentence of a data note; the full text stays one click away. */
    branchNoteHeadline(message) {
        const text = message || "";
        const cut = text.search(/\.(\s|$)/);
        return cut > 0 ? text.slice(0, cut + 1) : text;
    },

    scrollToBranchTable() {
        const table = document.querySelector(".o_sm_br_table_panel");
        if (table) {
            table.scrollIntoView({ behavior: "smooth", block: "start" });
        }
    },
};
