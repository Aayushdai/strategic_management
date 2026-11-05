/** @odoo-module **/

// Branch Manager dashboard.
// These functions become methods of StrategicNavigationShell (see navigation_shell.js); this page's
// template is branch_manager.xml and its styles branch_manager.scss, next to this file.

export const branchManagerPage = {
    // ------------------------------------------------------------ Branch Manager dashboard
    /** Role first, then data: a Branch Manager without a valid branch keeps their navigation and sees why. */
    async loadStart() {
        try {
            this.state.navContext = await this.orm.call("strategic.performance.dashboard", "get_navigation_context", []);
        } catch (error) {
            console.error("Navigation context could not be loaded", error);
        }
        const context = this.state.navContext;
        if (context && context.role === "admin") {
            this.state.activeSection = "admin_home";
            return;
        }
        if (context && context.role === "branch_manager") {
            // A Branch Manager lands on their own dashboard.
            this.state.activeSection = "bm_dashboard";
            if (context.branch_error) return;
            await this.loadOverview();
            return this.loadBranchObjectives();
        }
        if (context && context.role === "employee") {
            // Employees land on their own tasks.
            this.state.activeSection = "employee_dashboard";
            await this.loadEmployeeContext();
            await this.loadOverview();
            return this.loadEmployeeHome();
        }
        return this.loadOverview();
    },

    async loadBranchObjectives({ silent = false } = {}) {
        if (!this.isBranchManager || this.branchError) return;
        if (!silent) {
            this.state.branchObjectivesLoading = true;
        }
        this.loadTaskSummary();
        this.loadBranchOps({ silent });
        try {
            this.state.branchObjectives = await this.orm.silent.call("strategic.performance.dashboard", "get_branch_objective_summary", []);
            this.state.branchObjectivesError = "";
            // Instructions carry no deadline of their own; show their objective's. A failure only hides the deadline.
            if (this.state.branchObjectives.instructions?.length) {
                const goals = await this.orm.silent.call("strategic.goal.setup", "get_goals", []).catch(() => []);
                this.state.objectiveDeadlines = Object.fromEntries(goals.map((goal) => [goal.objective_id, goal.deadline]));
            }
        } catch (error) {
            if (!silent) {
                this.state.branchObjectives = null;
                this.state.branchObjectivesError = error.data?.message || error.message || "Objectives could not be loaded.";
            }
        } finally {
            this.state.branchObjectivesLoading = false;
        }
    },

    /** Business Goals, opened on one objective's details (Branch Manager dashboard). */
    openBranchObjective(objectiveId) {
        return this.action.doAction("strategic_management.action_business_goals", { additionalContext: { open_objective_id: objectiveId } });
    },

    /** Dashboard sales chart: the server's trend on a round axis, positions in percent; null when the period has no sales. */
    get opsTrendChart() {
        const sales = this.state.branchOps?.sales;
        if (!sales?.trend?.length || !(sales.net_sales || sales.orders)) return null;
        const values = sales.trend.map((row) => row.sales);
        const scale = this.niceScale(Math.min(0, ...values), Math.max(0, ...values));
        const y = (value) => ((value - scale.low) / (scale.high - scale.low)) * 100;
        const step = Math.ceil(values.length / 8);
        return {
            ticks: this.axisTicks(scale, y),
            bars: sales.trend.map((row, index) => ({
                key: row.key, label: row.label, title: `${row.label}: ${this.formatOpsMoney(row.sales, sales)}`,
                bottom: Math.min(y(0), y(row.sales)), height: Math.abs(y(row.sales) - y(0)),
                showLabel: index === values.length - 1 || (index % step === 0 && values.length - 1 - index >= step / 2),
            })),
        };
    },

    /** Dashboard: one status row per operational area; an unreadable area says so instead of showing zero. */
    get opsHealth() {
        const { inventory: inv, purchasing: pur } = this.state.branchOps;
        const row = (key, label, section, count, status) => ({ key, label, section, value: this.formatOpsCount(count), status,
            tone: count ? "warn" : "ok" });
        const missing = (key, label, section, status = "Not available") => ({ key, label, section, value: "—", status, tone: "none" });
        return [
            inv.available ? row("low", "Low stock", "inventory_waste", inv.low_stock_count, inv.low_stock_count ? "Below minimum" : "All above minimum")
                : missing("low", "Low stock", "inventory_waste"),
            !inv.available ? missing("expiry", "Expiring or expired", "inventory_waste")
                : !inv.expiry_tracking_available ? missing("expiry", "Expiring or expired", "inventory_waste", "Expiry not tracked")
                : row("expiry", "Expiring or expired", "inventory_waste", inv.expiring_count, inv.expiring_count ? "Check lots" : "Nothing expiring soon"),
            !pur.available ? missing("deliveries", "Outstanding deliveries", "purchasing")
                : { ...row("deliveries", "Outstanding deliveries", "purchasing", pur.awaiting_count, pur.overdue_count ? `${this.formatOpsCount(pur.overdue_count)} overdue` : pur.awaiting_count ? "None overdue" : "Nothing outstanding"),
                    tone: pur.overdue_count ? "bad" : "ok" },
        ];
    },

    /** Dashboard: what needs action now (low stock, expiring lots, overdue deliveries), at most five. */
    get opsAlerts() {
        const ops = this.state.branchOps;
        if (!ops || !ops.sales) return null;
        const alerts = [];
        const inv = ops.inventory?.available ? ops.inventory : null;
        const pur = ops.purchasing?.available ? ops.purchasing : null;
        for (const row of inv?.low_stock || []) {
            alerts.push({ key: "low-" + row.product_id, kind: "stock", title: `${row.status}: ${row.name}`,
                          detail: `${this.formatInventoryQuantity(row.available, row.unit)} on hand · minimum ${this.formatInventoryQuantity(row.minimum, row.unit)}` });
        }
        for (const row of inv?.expiring || []) {
            alerts.push({ key: "exp-" + row.lot_id + row.expiry_date, kind: "expiry", title: `${row.days_left < 0 ? "Expired" : "Expiring"}: ${row.product}`,
                          detail: `${this.formatInventoryQuantity(row.quantity, row.unit)} · ${this.formatPurchaseDate(row.expiry_date)}` });
        }
        for (const row of (pur?.deliveries || []).filter((item) => item.status === "Overdue")) {
            alerts.push({ key: "po-" + row.name, kind: "purchase", title: `Overdue delivery: ${row.name}`,
                          detail: `${row.supplier} · expected ${this.formatPurchaseDate(row.expected_date)}` });
        }
        return alerts.slice(0, 5);
    },

    /** The manager's branch as the server pinned it (res.users.strategic_branch_warehouse_id); never chosen in the browser. */
    get managerBranch() {
        if (!this.isBranchManager || this.branchError) return null;
        if (this.state.navContext?.role) return this.state.navContext.branch || null;
        const data = this.state.data;
        return data ? (data.branch_options || []).find((branch) => branch.id === data.selected_warehouse_id) || null : null;
    },

    get reportingPeriodLabel() {
        const period = this.state.data?.period;
        return period ? `${this.formatPurchaseDate(period.start)} – ${this.formatPurchaseDate(period.end)}` : "";
    },

    openNotifications() {
        return this.action.doAction("mail.action_discuss");
    },

    /** Round axis bounds and step covering [low, high]. */
    niceScale(low, high) {
        const span = high - low || Math.abs(high) || 1;
        const rough = span / 4;
        const magnitude = 10 ** Math.floor(Math.log10(rough));
        const step = [1, 2, 2.5, 5, 10].find((factor) => factor * magnitude >= rough) * magnitude;
        return { low: Math.floor(low / step) * step, high: Math.max(step, Math.ceil(high / step) * step), step };
    },

    axisTicks(scale, y) {
        const ticks = [];
        for (let value = scale.low; value <= scale.high + scale.step / 2; value += scale.step) {
            ticks.push({ key: String(value), y: y(value), label: this.formatCompactCurrency(value) });
        }
        return ticks;
    },
};
