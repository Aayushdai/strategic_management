/** @odoo-module **/

import { Component, onWillStart, proxy } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const PERSPECTIVE_LABELS = {
    financial: "Financial",
    customer: "Customer",
    internal_process: "Internal Process",
    learning_growth: "Learning & Growth",
};

const STATE_LABELS = {
    draft: "Draft",
    pending_approval: "Pending approval",
    active: "Active",
    completed: "Completed",
    closed: "Closed",
    archived: "Archived",
    approved: "Approved",
};

const LINK_LABELS = {
    "B>A": "Breakthrough → Annual",
    "I>A": "Initiative → Annual",
    "I>M": "Initiative → KPI / target",
    "B>M": "Breakthrough → KPI / target",
    "I>O": "Initiative → Owner",
};

export class StrategicHoshinXMatrix extends Component {
    static template = "strategic_management.HoshinXMatrix";

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.state = proxy({
            loading: true,
            error: "",
            plans: [],
            selectedPlanId: false,
            selectedPlan: false,
            breakthroughs: [],
            annualObjectives: [],
            initiatives: [],
            alignments: [],
            targets: [],
        });
        onWillStart(() => this.loadPlansAndMatrix());
    }

    async loadPlansAndMatrix() {
        this.state.loading = true;
        this.state.error = "";
        try {
            const plans = await this.orm.searchRead(
                "strategic.plan",
                [["active", "=", true]],
                ["name", "company_id", "date_start", "date_end", "active"],
                { order: "date_start desc, name", limit: 200 }
            );
            this.state.plans = plans;
            if (!plans.length) {
                this.clearMatrix();
                this.state.selectedPlanId = false;
                this.state.selectedPlan = false;
                return;
            }
            const hasSelectedPlan = plans.some(
                (plan) => plan.id === Number(this.state.selectedPlanId)
            );
            if (!hasSelectedPlan) {
                this.state.selectedPlanId = plans[0].id;
            }
            await this.loadMatrix();
        } catch (error) {
            this.state.error = error.message || "Unable to load the Hoshin planning workspace.";
            this.notification.add(this.state.error, { type: "danger" });
        } finally {
            this.state.loading = false;
        }
    }

    clearMatrix() {
        this.state.breakthroughs = [];
        this.state.annualObjectives = [];
        this.state.initiatives = [];
        this.state.alignments = [];
        this.state.targets = [];
    }

    async loadMatrix() {
        const planId = Number(this.state.selectedPlanId);
        const plan = this.state.plans.find((item) => item.id === planId);
        this.state.selectedPlan = plan || false;
        if (!planId || !plan) {
            this.clearMatrix();
            return;
        }
        this.state.loading = true;
        this.state.error = "";
        try {
            const [breakthroughs, annualObjectives, initiatives, alignments] = await Promise.all([
                this.orm.searchRead(
                    "strategic.objective",
                    [["plan_id", "=", planId], ["scope", "=", "organization"], ["hoshin_level", "=", "breakthrough"]],
                    ["name", "plan_id", "perspective", "deadline", "state", "hoshin_level"],
                    { order: "deadline, name", limit: 500 }
                ),
                this.orm.searchRead(
                    "strategic.objective",
                    [["plan_id", "=", planId], ["scope", "=", "organization"], ["hoshin_level", "=", "annual"]],
                    ["name", "plan_id", "perspective", "deadline", "state", "hoshin_level"],
                    { order: "deadline, name", limit: 500 }
                ),
                this.orm.searchRead(
                    "strategic.initiative",
                    [["plan_id", "=", planId]],
                    ["name", "plan_id", "date_start", "date_end", "owner_id", "state"],
                    { order: "date_start, name", limit: 500 }
                ),
                this.orm.searchRead(
                    "strategic.xmatrix.alignment",
                    [
                        "&", ["state", "=", "active"],
                        "|", "|",
                        ["breakthrough_objective_id.plan_id", "=", planId],
                        ["annual_objective_id.plan_id", "=", planId],
                        ["initiative_id.plan_id", "=", planId],
                    ],
                    [
                        "name", "link_type", "relationship_type", "breakthrough_objective_id",
                        "annual_objective_id", "initiative_id", "kpi_id", "target_id", "owner_id",
                        "effective_date", "state",
                    ],
                    { order: "link_type, relationship_type, id", limit: 1000 }
                ),
            ]);
            const targetIds = [...new Set(
                alignments
                    .filter((alignment) => ["I>M", "B>M"].includes(alignment.link_type))
                    .map((alignment) => alignment.target_id && alignment.target_id[0])
                    .filter(Boolean)
            )];
            const targets = targetIds.length
                ? await this.orm.searchRead(
                    "strategic.kpi.target",
                    [["id", "in", targetIds]],
                    ["name", "kpi_id", "target_value", "unit", "date_start", "date_end", "state", "scope"],
                    { limit: 1000 }
                )
                : [];

            this.state.breakthroughs = breakthroughs;
            this.state.annualObjectives = annualObjectives;
            this.state.initiatives = initiatives;
            this.state.alignments = alignments;
            this.state.targets = targets;
        } catch (error) {
            this.clearMatrix();
            this.state.error = error.message || "Unable to load this plan's X-Matrix data.";
            this.notification.add(this.state.error, { type: "danger" });
        } finally {
            this.state.loading = false;
        }
    }

    onPlanChanged(event) {
        this.state.selectedPlanId = event.target.value ? Number(event.target.value) : false;
        return this.loadMatrix();
    }

    perspectiveLabel(value) {
        return PERSPECTIVE_LABELS[value] || value || "BSC perspective not set";
    }

    stateLabel(value) {
        return STATE_LABELS[value] || value || "Unknown";
    }

    relationName(value) {
        return Array.isArray(value) ? value[1] : "Not linked";
    }

    targetById(targetRelation) {
        if (!Array.isArray(targetRelation)) {
            return false;
        }
        return this.state.targets.find((target) => target.id === targetRelation[0]) || false;
    }

    targetLabel(targetRelation) {
        const target = this.targetById(targetRelation);
        if (!target) {
            return this.relationName(targetRelation);
        }
        const amount = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(target.target_value || 0);
        return `${this.relationName(target.kpi_id)} · target ${amount}${target.unit ? ` ${target.unit}` : ""}`;
    }

    targetPeriod(targetRelation) {
        const target = this.targetById(targetRelation);
        return target ? `${target.date_start} – ${target.date_end}` : "Approved target";
    }

    linksFor(linkType) {
        return this.state.alignments.filter((alignment) => alignment.link_type === linkType);
    }

    alignmentOriginLabel(alignment) {
        switch (alignment.link_type) {
            case "B>A":
            case "B>M":
                return this.relationName(alignment.breakthrough_objective_id);
            case "I>A":
            case "I>M":
            case "I>O":
                return this.relationName(alignment.initiative_id);
            default:
                return this.relationName(alignment.name);
        }
    }

    alignmentDestinationLabel(alignment) {
        switch (alignment.link_type) {
            case "B>A":
            case "I>A":
                return this.relationName(alignment.annual_objective_id);
            case "I>M":
            case "B>M":
                return this.targetLabel(alignment.target_id);
            case "I>O":
                return this.relationName(alignment.owner_id);
            default:
                return "Endpoint missing";
        }
    }

    ownerFor(initiativeId) {
        const primary = this.state.alignments.find(
            (alignment) => alignment.link_type === "I>O"
                && alignment.initiative_id?.[0] === initiativeId
                && alignment.relationship_type === "primary"
        );
        if (primary) {
            return this.relationName(primary.owner_id);
        }
        const initiative = this.state.initiatives.find((item) => item.id === initiativeId);
        return initiative && initiative.owner_id ? this.relationName(initiative.owner_id) : "Owner alignment missing";
    }

    linkLabel(linkType) {
        return LINK_LABELS[linkType] || linkType;
    }

    async openRecord(model, recordId) {
        if (!recordId) {
            return;
        }
        return this.action.doAction({
            type: "ir.actions.act_window",
            res_model: model,
            res_id: recordId,
            views: [[false, "form"]],
            target: "current",
        });
    }

    async openCreatePlan() {
        return this.action.doAction({
            type: "ir.actions.act_window",
            name: "New Strategic Plan",
            res_model: "strategic.plan",
            views: [[false, "form"]],
            target: "current",
        });
    }

    async openCreateObjective(level) {
        const plan = this.state.selectedPlan;
        if (!plan) {
            this.notification.add("Create or select a strategic plan first.", { type: "warning" });
            return;
        }
        return this.action.doAction({
            type: "ir.actions.act_window",
            name: level === "breakthrough" ? "New Breakthrough Objective" : "New Annual Objective",
            res_model: "strategic.objective",
            views: [[false, "form"]],
            target: "current",
            context: {
                default_plan_id: plan.id,
                default_scope: "organization",
                default_hoshin_level: level,
            },
        });
    }

    async openCreateInitiative() {
        const plan = this.state.selectedPlan;
        if (!plan) {
            this.notification.add("Create or select a strategic plan first.", { type: "warning" });
            return;
        }
        return this.action.doAction({
            type: "ir.actions.act_window",
            name: "New Improvement Initiative",
            res_model: "strategic.initiative",
            views: [[false, "form"]],
            target: "current",
            context: {
                default_plan_id: plan.id,
                default_date_start: plan.date_start,
                default_date_end: plan.date_end,
            },
        });
    }

    async openCreateAlignment(linkType) {
        if (!this.state.selectedPlan) {
            this.notification.add("Create or select a strategic plan first.", { type: "warning" });
            return;
        }
        return this.action.doAction({
            type: "ir.actions.act_window",
            name: `Add ${this.linkLabel(linkType)} alignment`,
            res_model: "strategic.xmatrix.alignment",
            views: [[false, "form"]],
            target: "new",
            context: {
                default_link_type: linkType,
                default_relationship_type: "primary",
            },
        }, {
            onClose: () => this.loadMatrix(),
        });
    }

    async openTargets() {
        return this.action.doAction("strategic_management.action_strategic_kpi_target");
    }

    async openAlignmentRegister() {
        return this.action.doAction("strategic_management.action_strategic_xmatrix_alignment");
    }

    async refresh() {
        if (!this.state.plans.length) {
            return this.loadPlansAndMatrix();
        }
        return this.loadMatrix();
    }
}

registry.category("actions").add("strategic_management.hoshin_xmatrix", StrategicHoshinXMatrix);
