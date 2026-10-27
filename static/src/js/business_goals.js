/** @odoo-module **/
/**
 * Business Goals — Odoo 20 OWL client action.
 * Every goal is a strategic.objective; all setup steps run server-side through strategic.goal.setup.
 * Status labels come from the server, so a goal is never shown as active before it is approved.
 */
import { Component, onWillStart, proxy } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const MODEL = "strategic.goal.setup";

// Business Goals has its own page per sidebar item; area pages show only that area's goals.
const SECTIONS = {
    goals: { title: "Business Goals", description: "Keep priorities clear. See what is on track and where a decision is needed." },
    overview: { title: "Goals Overview", description: "How goals are spread across business areas." },
    sales_finance: { title: "Sales & Finance Goals", description: "Goals for sales, profit and financial results.", area: "sales_finance" },
    inventory_waste: { title: "Inventory & Waste Goals", description: "Goals for stock health, waste and expiry.", area: "inventory_waste" },
    purchasing: { title: "Purchasing Goals", description: "Goals for suppliers, deliveries and purchasing.", area: "purchasing" },
    people: { title: "People Goals", description: "Goals for staffing, attendance and team performance.", area: "people" },
    branches: { title: "Branch Goals", description: "Goals for individual branches." },
    tasks_issues: { title: "Goal Tasks & Issues", description: "Follow-up actions for goals." },
    reports: { title: "Goal Reports", description: "Reports on goal progress." },
    app_dependencies: { title: "Goal Data Sources", description: "Odoo apps that goal measurements rely on." },
    configuration: { title: "Goal Settings", description: "Settings for business goals." },
};
// Plain-language next step for each server-side setup state.
const NEXT_STEP = {
    needs_setup: "Choose how this goal will be measured, then check readiness.",
    ready: "All checks passed. Head Office prepares the target next.",
    target_pending: "The target is prepared. Head Office must approve it before the goal can be submitted.",
    measured: "The target is approved and linked. Submit the goal for approval.",
    submitted: "The goal has been submitted through the normal approval process.",
};
// Icon per business area (names from Odoo's icon font, as used in the sidebar).
const AREA_ICONS = {
    sales_finance: "bar_chart", inventory_waste: "inventory_2", purchasing: "shopping_cart",
    people: "group", customers: "storefront", operations: "settings",
};
const formatDate = (iso) => {
    if (!iso) return "—";
    const date = new Date(`${iso}T00:00:00`);
    return Number.isNaN(date.getTime()) ? iso : date.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
};

export class BusinessGoalsAction extends Component {
    static template = "strategic_management.BusinessGoalsAction";

    formatDate(iso) { return formatDate(iso); }

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.action = useService("action");
        this.state = proxy({
            section: "goals", loading: true, options: null, goals: [],
            progressSort: "asc",
            showAllProgress: false, showAllMissed: false, showAllAttention: false,
            selectedGoalId: null, setupForm: null, chooseOption: true, productResults: [], productSearched: false, busy: false,
            createOpen: false, newGoal: this.blankGoal(),
            // Monitoring time breakdown: the open interval per goal, its drill-down path, and stock snapshots shown inline.
            monitorDetail: null, monitorSnapshot: null,
        });
        onWillStart(() => this.load());
    }

    async load() {
        const [options, goals] = await Promise.all([
            this.orm.call(MODEL, "get_form_options", []),
            this.orm.call(MODEL, "get_goals", []),
        ]);
        this.state.options = options;
        this.state.goals = goals;
        this.state.loading = false;
    }

    blankGoal(area = false) {
        return {
            name: "", business_area: area || "sales_finance", desired_result: "", deadline: "", owner_id: false, reason: "", plan_id: false,
            scope: "organization", warehouse_id: false, parent_objective_id: false,
        };
    }

    // ------------------------------------------------------------ sections
    get section() { return SECTIONS[this.state.section]; }
    get sectionArea() { return this.section.area || null; }
    get isGoalList() { return this.state.section === "goals" || Boolean(this.sectionArea); }
    get areas() { return (this.state.options && this.state.options.areas) || []; }
    areaLabel(key) { return (this.areas.find((area) => area.key === key) || {}).label || key; }

    get sectionGoals() {
        return this.sectionArea ? this.state.goals.filter((goal) => goal.areas.includes(this.sectionArea)) : this.state.goals;
    }

    get areaMeasures() {
        if (!this.sectionArea || !this.state.options) return null;
        return {
            supported: this.state.options.measures.filter((measure) => measure.area === this.sectionArea).map((measure) => measure.label),
            missing: this.state.options.area_gaps[this.sectionArea],
        };
    }

    get areaOverview() {
        return this.areas.map((area) => {
            const goals = this.state.goals.filter((goal) => goal.areas.includes(area.key));
            return {
                key: area.key, label: area.label, total: goals.length,
                active: goals.filter((goal) => goal.state === "active").length,
                attention: goals.filter((goal) => ["bad", "warn"].includes(goal.tone)).length,
                sectionId: Object.keys(SECTIONS).find((id) => SECTIONS[id].area === area.key) || null,
            };
        });
    }

    openSection(sectionId) {
        this.state.section = sectionId;
        this.closeGoal();
    }

    // ------------------------------------------------------------ performance panels
    // The server sets each goal's category: measured (ranked), missed (final result below target) or setup.
    goalsIn(category) { return this.sectionGoals.filter((goal) => goal.category === category); }

    // Ranked by the server's margin to target (higher = better business performance), never by bar width.
    ranked(sort) {
        const direction = sort === "asc" ? 1 : -1;
        return [...this.goalsIn("measured")].sort((a, b) => (a.margin - b.margin) * direction || a.title.localeCompare(b.title));
    }

    get progressGoals() {
        const goals = this.ranked(this.state.progressSort);
        return this.state.showAllProgress ? goals : goals.slice(0, 3);
    }

    get progressCount() { return this.goalsIn("measured").length; }

    periodEnd(goal) { return goal.period_end || goal.deadline || ""; }

    get targetMissedGoals() {
        const goals = this.goalsIn("missed").sort((a, b) => this.periodEnd(b).localeCompare(this.periodEnd(a)));
        return this.state.showAllMissed ? goals : goals.slice(0, 3);
    }

    get targetMissedCount() { return this.goalsIn("missed").length; }

    // Goals the server flags (bad, then warn), followed by the rest of the not-measured / setup list.
    get allAttentionGoals() {
        const flagged = this.sectionGoals.filter((goal) => ["bad", "warn"].includes(goal.tone));
        flagged.sort((a, b) => (a.tone === "bad" ? 0 : 1) - (b.tone === "bad" ? 0 : 1));
        return [...flagged, ...this.goalsIn("setup").filter((goal) => !flagged.includes(goal))];
    }

    get attentionGoals() {
        const goals = this.allAttentionGoals;
        return this.state.showAllAttention ? goals : goals.slice(0, 3);
    }

    // ------------------------------------------------------------ monitoring time breakdown
    /** Open an interval; from a detail panel, `drill` keeps the path so the user can go back up. */
    async selectMonitorBucket(goal, bucket, granularity, drill = false) {
        if (!bucket || bucket.future || !goal.setup_id) return;
        const current = this.state.monitorDetail;
        const path = drill && current && current.objectiveId === goal.objective_id ? [...current.path] : [];
        this.state.monitorDetail = { objectiveId: goal.objective_id, key: bucket.key, loading: true, path, data: null };
        try {
            const data = await this.orm.call(MODEL, "get_monitor_interval", [[goal.setup_id], bucket.start, bucket.end, granularity]);
            const detail = this.state.monitorDetail;
            if (detail && detail.objectiveId === goal.objective_id && detail.key === bucket.key) {
                detail.data = data;
                detail.loading = false;
                detail.path = [...path, { key: bucket.key, label: data.range_label || bucket.range_label, bucket, granularity }];
            }
        } catch (error) {
            this.state.monitorDetail = null;
            this.notification.add((error.data && error.data.message) || error.message || "This interval could not be loaded.", { type: "danger" });
        }
    }

    monitorBackTo(goal, index) {
        const step = this.state.monitorDetail && this.state.monitorDetail.path[index];
        if (!step) return;
        this.state.monitorDetail.path = this.state.monitorDetail.path.slice(0, index);
        return this.selectMonitorBucket(goal, step.bucket, step.granularity, true);
    }

    closeMonitorDetail() { this.state.monitorDetail = null; }

    isSelectedBucket(goal, bucket) {
        const detail = this.state.monitorDetail;
        return Boolean(detail && detail.objectiveId === goal.objective_id && detail.path.length <= 1 && detail.key === bucket.key);
    }

    bucketClass(goal, bucket) {
        return "o_sm_bg_tl_bar" + (bucket.future ? " is-future" : bucket.empty ? " is-empty" : bucket.negative ? " is-negative" : "")
            + (bucket.value === 0 ? " is-zero" : "") + (this.isSelectedBucket(goal, bucket) ? " is-selected" : "");
    }

    bucketHeight(bucket) {
        return bucket.height ? Math.max(bucket.height, 4) : 0;
    }

    toggleMonitorSnapshot(goal) {
        this.state.monitorSnapshot = this.state.monitorSnapshot === goal.objective_id ? null : goal.objective_id;
    }

    get canOpenRecords() { return Boolean(this.state.options && this.state.options.is_head_office); }

    async openMonitorRecord(row) {
        try {
            await this.action.doAction({ type: "ir.actions.act_window", res_model: row.model, res_id: row.id, views: [[false, "form"]], target: "current" });
        } catch (error) {
            this.notification.add((error.data && error.data.message) || error.message || "This record is not available with your access rights.", { type: "warning" });
        }
    }

    areaIcon(goal) { return AREA_ICONS[(goal.areas || [])[0]] || "flag"; }

    toggle(flag) { this.state[flag] = !this.state[flag]; }

    get summary() {
        const goals = this.sectionGoals;
        return {
            active: goals.filter((goal) => goal.state === "active").length,
            onTrack: goals.filter((goal) => goal.status === "On track").length,
            attention: goals.filter((goal) => ["bad", "warn"].includes(goal.tone)).length,
            inSetup: goals.filter((goal) => ["draft", "pending_approval"].includes(goal.state)).length,
        };
    }

    get selectedGoal() {
        return this.state.goals.find((goal) => goal.objective_id === this.state.selectedGoalId) || null;
    }

    statusClass(tone) { return `is-${tone || "neutral"}`; }
    barTitle(bar) { return !bar || bar.kind === "waiting" ? "Progress" : bar.kind === "usage" ? "Limit used" : "Target reached"; }

    // OWL 3 t-model only binds signals, so form fields write through this instead.
    setValue(target, key, ev) { target[key] = ev.target.value; }


    // ------------------------------------------------------------ goal drawer and setup
    openGoal(objectiveId) {
        const goal = this.state.goals.find((item) => item.objective_id === objectiveId);
        this.state.selectedGoalId = objectiveId;
        this.state.productResults = [];
        this.state.productSearched = false;
        // A goal with a chosen option opens on that option; the full list is one click away.
        this.state.chooseOption = !(goal && goal.template_key);
        this.state.setupForm = goal && {
            new_deadline: goal.deadline || "",
            template_key: goal.template_key || "",
            measure_key: goal.measure_key || "",
            product_id: goal.product ? goal.product.id : false,
            product_name: goal.product ? goal.product.name : "",
            product_uom: goal.product ? goal.product.uom : "",
            target_value: goal.target_value || "",
            period_start: goal.period_start || "",
            period_end: goal.period_end || goal.deadline || "",
            // Monitoring settings: reporting period, daily time window and optional POS category.
            period_preset: goal.period_preset || "this_week",
            window_start: goal.window_start || "",
            window_end: goal.window_end || "",
            pos_category_id: goal.pos_category ? String(goal.pos_category.id) : "",
            partner_id: goal.partner ? String(goal.partner.id) : "",
            product_category_id: goal.product_category ? String(goal.product_category.id) : "",
        };
    }

    closeGoal() {
        this.state.selectedGoalId = null;
        this.state.setupForm = null;
    }

    measuresFor(goal) {
        const area = goal.areas[0];
        return ((this.state.options && this.state.options.measures) || []).filter((measure) => measure.area === area);
    }

    get selectedMeasure() {
        const form = this.state.setupForm;
        return form && ((this.state.options && this.state.options.measures) || []).find((measure) => measure.key === form.measure_key);
    }

    // Goal options of the selected goal's area, grouped by topic; unavailable ones stay visible with their reason.
    get templateGroups() {
        const groups = [];
        const area = this.selectedGoal && this.selectedGoal.areas[0];
        for (const template of ((this.state.options && this.state.options.templates) || []).filter((item) => item.area === area)) {
            let group = groups.find((item) => item.name === template.group);
            if (!group) {
                group = { name: template.group, templates: [] };
                groups.push(group);
            }
            group.templates.push(template);
        }
        return groups;
    }

    get selectedTemplate() {
        const form = this.state.setupForm;
        return form && ((this.state.options && this.state.options.templates) || []).find((template) => template.key === form.template_key);
    }

    pickTemplate(template) {
        this.state.setupForm.template_key = template.key;
        if (template.status === "available") {
            this.state.chooseOption = false;
        }
        if (!template.needs_product && template.goal_type !== "monitoring") {
            this.state.setupForm.product_id = false;
            this.state.setupForm.product_name = "";
        }
        if (template.goal_type === "monitoring" && !template.metrics.some((metric) => metric.key === this.state.setupForm.measure_key)) {
            this.state.setupForm.measure_key = template.metrics[0].key;
        }
    }

    get isMonitoring() {
        return Boolean(this.isTemplateGoal && this.selectedTemplate && this.selectedTemplate.goal_type === "monitoring");
    }

    get monitoringGoals() { return this.goalsIn("monitoring"); }

    // Sales monitoring ("pos") has a daily time window; purchasing monitoring ("purchase") has supplier filters.
    get monitorSource() { return this.isMonitoring ? this.selectedTemplate.monitor_source : false; }

    // What an option measures, so a money goal is not set up with a quantity option (or the other way round).
    measuredIn(template) {
        const currency = (this.state.options && this.state.options.currency_symbol) || "";
        return {
            money: `Measured in money (${currency})`,
            quantity: "Measured as a quantity (the product's unit)",
            percent: "Measured as a percentage",
            change: "Measured as a % change",
            days: "Measured in days",
            unit_price: `Measured as a price per unit (${currency}/unit)`,
            monitor: "Shown as values and trends, no target",
        }[template.kind] || "";
    }

    changeOption() { this.state.chooseOption = true; }

    // Shown next to the dates and target before saving; the server applies the same rules.
    periodProblem(goal) {
        const form = this.state.setupForm;
        if (!form) return "";
        if (this.isMonitoring) {
            if (this.monitorSource === "pos") {
                if (!form.window_start || !form.window_end) return "Choose the start and end time of the daily window.";
                if (form.window_start >= form.window_end) return "The time window must start before it ends on the same day (windows across midnight are not supported yet).";
            }
            if (this.monitorSource === "inventory") return "";
            if (["purchase_mon_unit_price", "purchase_mon_price_comparison"].includes(form.measure_key) && !form.product_id) {
                return "Choose the product whose purchase price is shown.";
            }
            if (form.period_preset !== "custom") return "";
        }
        if (!form.period_start || !form.period_end) return "Choose both dates of the measurement period.";
        if (form.period_start > form.period_end) return "The period must start before it ends.";
        if (goal.deadline && form.period_end > goal.deadline) {
            return `The period ends after the goal deadline (${formatDate(goal.deadline)}). Choose an earlier date or extend the deadline.`;
        }
        if (!this.isMonitoring && !(parseFloat(form.target_value) > 0)) return "Enter a target above zero.";
        return "";
    }

    extendDeadline() { return this.callGoal("extend_deadline", [this.state.setupForm.new_deadline]); }

    get isTemplateGoal() {
        const area = this.selectedGoal && this.selectedGoal.areas[0];
        return Boolean(area && ((this.state.options && this.state.options.templates) || []).some((template) => template.area === area));
    }

    // Fields appear only once a measurable option or measure is chosen.
    get setupReady() {
        if (this.isTemplateGoal) {
            return Boolean(this.selectedTemplate && this.selectedTemplate.status === "available");
        }
        return Boolean(this.selectedMeasure);
    }

    get needsProduct() {
        return this.isTemplateGoal ? Boolean(this.selectedTemplate && this.selectedTemplate.needs_product)
            : Boolean(this.selectedMeasure && this.selectedMeasure.needs_product);
    }

    get targetLabel() {
        if (this.isTemplateGoal) {
            return this.selectedTemplate.target_label;
        }
        return "Target";
    }

    nextStep(goal) { return goal.next_step || NEXT_STEP[goal.setup_state] || ""; }

    // Lists products as soon as the box is focused; typing narrows the list. Purchasing goals list purchasable products.
    async searchProducts(ev) {
        const query = ev.target.value.trim();
        if (ev.type === "input") {
            this.state.setupForm.product_name = ev.target.value;
            this.state.setupForm.product_id = false;
        }
        const purchasable = Boolean(this.selectedGoal && this.selectedGoal.areas[0] === "purchasing");
        this.state.productResults = await this.orm.call(MODEL, "search_products", [query], { purchasable });
        this.state.productSearched = true;
    }

    pickProduct(product) {
        this.state.setupForm.product_id = product.id;
        this.state.setupForm.product_name = product.name;
        this.state.setupForm.product_uom = product.uom;
        this.state.productResults = [];
        this.state.productSearched = false;
    }

    changeProduct() {
        this.state.setupForm.product_id = false;
        this.state.setupForm.product_name = "";
        this.state.productResults = [];
    }

    // Unit shown beside the target box, so the owner knows what the number means.
    get targetUnit() {
        const template = this.isTemplateGoal && this.selectedTemplate;
        if (!template) return (this.selectedMeasure && this.selectedMeasure.unit) || "";
        if (template.kind === "money") return (this.state.options && this.state.options.currency_symbol) || "";
        if (template.kind === "percent" || template.kind === "change") return "%";
        if (template.kind === "days") return "days";
        if (template.kind === "unit_price") {
            const uom = (this.state.setupForm && this.state.setupForm.product_uom) || "unit";
            return `${(this.state.options && this.state.options.currency_symbol) || ""}/${uom}`;
        }
        return (this.state.setupForm && this.state.setupForm.product_uom) || "";
    }

    replaceGoal(payload) {
        const index = this.state.goals.findIndex((goal) => goal.objective_id === payload.objective_id);
        if (index >= 0) {
            this.state.goals.splice(index, 1, payload);
        } else {
            this.state.goals.unshift(payload);
        }
    }

    async callGoal(method, args = []) {
        const goal = this.selectedGoal;
        if (!goal || !goal.setup_id || this.state.busy) return;
        this.state.busy = true;
        try {
            this.replaceGoal(await this.orm.call(MODEL, method, [[goal.setup_id], ...args]));
        } catch (error) {
            this.notification.add((error.data && error.data.message) || error.message || "The step could not be completed.", { type: "danger" });
        } finally {
            this.state.busy = false;
        }
    }

    saveMeasurement() {
        const form = this.state.setupForm;
        return this.callGoal("configure_measurement", [{
            template_key: form.template_key || false,
            measure_key: form.measure_key,
            product_id: form.product_id,
            target_value: parseFloat(form.target_value) || 0,
            period_start: form.period_start || false,
            period_end: form.period_end || false,
            ...(this.isMonitoring ? Object.assign({ period_preset: form.period_preset || false }, this.monitorSource === "pos" ? {
                window_start: form.window_start || false,
                window_end: form.window_end || false,
                pos_category_id: parseInt(form.pos_category_id) || false,
            } : {
                partner_id: this.monitorSource === "purchase" ? parseInt(form.partner_id) || false : false,
                product_category_id: parseInt(form.product_category_id) || false,
            }) : {}),
        }]);
    }

    checkReadiness() { return this.callGoal("check_readiness"); }
    prepareTarget() { return this.callGoal("prepare_target"); }
    approveTarget() { return this.callGoal("approve_target"); }
    submitGoal() { return this.callGoal("submit_goal"); }
    approveGoal() { return this.callGoal("approve_goal"); }
    evaluateNow() { return this.callGoal("evaluate_now"); }

    // ------------------------------------------------------------ create
    openCreate() {
        this.state.newGoal = this.blankGoal(this.sectionArea);
        this.state.newGoal.owner_id = this.state.options ? this.state.options.current_user_id : false;
        // Branch managers propose goals for their own branch only.
        if (this.state.options && !this.state.options.is_head_office) {
            this.state.newGoal.scope = "branch";
            this.state.newGoal.warehouse_id = (this.state.options.branches[0] || {}).id || false;
        }
        this.state.createOpen = true;
    }

    closeCreate() { this.state.createOpen = false; }

    get deadlineBeyondPlan() {
        const options = this.state.options;
        const deadline = this.state.newGoal.deadline;
        return Boolean(options && options.latest_deadline && deadline && deadline > options.latest_deadline);
    }

    // Plain-language hint about which deadlines a strategic plan currently covers.
    get deadlineHint() {
        const options = this.state.options;
        if (!options) return "";
        const where = "Head Office can extend or add a plan under Strategy & KPIs > Strategy > Strategic Plans.";
        if (!options.has_plan) return `There is no active strategic plan yet, so goals cannot be saved. ${where}`;
        if (!options.latest_deadline) return "";
        if (this.deadlineBeyondPlan) {
            return `This deadline is after the current strategic plan ends (${formatDate(options.latest_deadline)}). Choose an earlier date, or: ${where}`;
        }
        return `Deadlines can be up to ${formatDate(options.latest_deadline)}, when the current strategic plan ends.`;
    }

    async saveDraft() {
        const data = this.state.newGoal;
        if (!data.name.trim() || !data.deadline || this.state.busy) return;
        this.state.busy = true;
        try {
            const payload = await this.orm.call(MODEL, "create_goal", [{
                name: data.name, business_area: data.business_area, desired_result: data.desired_result,
                deadline: data.deadline, owner_id: parseInt(data.owner_id) || false, reason: data.reason,
                plan_id: parseInt(data.plan_id) || false, scope: data.scope,
                warehouse_id: parseInt(data.warehouse_id) || false, parent_objective_id: parseInt(data.parent_objective_id) || false,
            }]);
            this.replaceGoal(payload);
            this.state.createOpen = false;
            this.openGoal(payload.objective_id);
        } catch (error) {
            this.notification.add((error.data && error.data.message) || error.message || "The goal could not be saved.", { type: "danger" });
        } finally {
            this.state.busy = false;
        }
    }
}

registry.category("actions").add("strategic_management.business_goals", BusinessGoalsAction);
