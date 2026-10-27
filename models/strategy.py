import psycopg2

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


class StrategicPlan(models.Model):
    _name = "strategic.plan"
    _description = "Strategic Plan"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "date_start desc, name"

    name = fields.Char(required=True, tracking=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    date_start = fields.Date(required=True)
    date_end = fields.Date(required=True)
    active = fields.Boolean(default=True)
    objective_ids = fields.One2many("strategic.objective", "plan_id")

    @api.constrains("date_start", "date_end")
    def _check_dates(self):
        for plan in self:
            if (
                plan.date_start
                and plan.date_end
                and plan.date_start > plan.date_end
            ):
                raise ValidationError(
                    "Strategic plan start date must not be after end date."
                )


class StrategicObjective(models.Model):
    _name = "strategic.objective"
    _description = "Strategic Objective"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "deadline, name"

    name = fields.Char(required=True, tracking=True)
    plan_id = fields.Many2one(
        "strategic.plan",
        required=True,
        ondelete="restrict",
        index=True,
    )
    company_id = fields.Many2one(
        related="plan_id.company_id",
        store=True,
        readonly=True,
    )
    perspective = fields.Selection(
        [
            ("financial", "Financial"),
            ("customer", "Customer"),
            ("internal_process", "Internal Process"),
            ("learning_growth", "Learning & Growth"),
        ],
        required=True,
        index=True,
        tracking=True,
    )
    scope = fields.Selection(
        [
            ("organization", "Organization"),
            ("branch", "Branch"),
        ],
        required=True,
        default="organization",
        index=True,
        tracking=True,
    )
    warehouse_id = fields.Many2one(
        "stock.warehouse",
        string="Branch Warehouse",
        index=True,
    )
    parent_objective_id = fields.Many2one(
        "strategic.objective",
        string="Aligned Organization Objective",
        ondelete="restrict",
        index=True,
    )
    child_objective_ids = fields.One2many(
        "strategic.objective",
        "parent_objective_id",
    )
    owner_id = fields.Many2one(
        "res.users",
        required=True,
        default=lambda self: self.env.user,
    )
    proposer_id = fields.Many2one("res.users", readonly=True, copy=False)
    submitted_at = fields.Datetime(string="Submitted On", readonly=True, copy=False)
    approved_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    completed_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    closed_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    deadline = fields.Date(required=True, tracking=True)
    description = fields.Text()
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("pending_approval", "Pending Approval"),
            ("active", "Active"),
            ("completed", "Completed"),
            ("closed", "Closed"),
            ("archived", "Archived"),
        ],
        required=True,
        default="draft",
        tracking=True,
        index=True,
    )
    okr_objective_ids = fields.One2many(
        "strategic.okr.objective",
        "strategic_objective_id",
    )
    revision_ids = fields.One2many(
        "strategic.objective.revision",
        "objective_id",
    )
    review_ids = fields.One2many(
        "strategic.objective.review",
        "objective_id",
        string="Review Decisions",
        readonly=True,
    )
    is_primary = fields.Boolean(
        string="Current Primary Objective",
        readonly=True,
        copy=False,
        index=True,
    )
    primary_history_ids = fields.One2many(
        "strategic.objective.primary.history",
        "objective_id",
        string="Primary Designation History",
        readonly=True,
    )
    overdue = fields.Boolean(compute="_compute_overdue")
    score = fields.Float(compute="_compute_score")
    scoring_status = fields.Selection(
        [
            ("not_evaluated", "Not Evaluated"),
            ("data_quality_issue", "Data Quality Issue"),
            ("not_achieved", "Not Achieved"),
            ("almost_met", "Almost Met"),
            ("achieved", "Achieved"),
            ("exceeded", "Exceeded"),
        ],
        compute="_compute_score",
    )

    _branch_requires_parent = models.Constraint(
        "CHECK(scope != 'branch' OR parent_objective_id IS NOT NULL)",
        "Branch objectives must be aligned to an organization objective.",
    )

    def init(self):
        """Enforce one current primary organization objective per company."""
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                strategic_objective_one_current_primary_per_company
            ON strategic_objective (company_id)
            WHERE is_primary IS TRUE AND scope = 'organization'
            """
        )

    @api.model_create_multi
    def create(self, vals_list):
        if any(vals.get("is_primary") for vals in vals_list):
            raise ValidationError(
                "The primary objective designation can only be changed "
                "through the Head Office designation workflow."
            )
        vals_list = [
            self._prepare_objective_create_values(vals)
            for vals in vals_list
        ]
        return super().create(vals_list)

    def write(self, vals):
        if "is_primary" in vals:
            raise ValidationError(
                "The primary objective designation can only be changed "
                "through the Head Office designation workflow."
            )

        # Lifecycle state and audit fields can only be changed by lifecycle actions.
        lifecycle_fields = {
            "state",
            "proposer_id",
            "submitted_at",
            "approved_by_id",
            "completed_by_id",
            "closed_by_id",
        }
        if lifecycle_fields.intersection(vals):
            raise ValidationError(
                "Objective lifecycle fields can only be changed through lifecycle actions."
            )

        self._check_write_allowed(vals)
        return super().write(vals)

    def _write_lifecycle(self, vals):
        """Write lifecycle fields after the calling action validates the transition."""
        self._check_write_allowed(vals)
        return super(StrategicObjective, self).write(vals)

    @api.constrains(
        "scope",
        "warehouse_id",
        "parent_objective_id",
        "plan_id",
        "deadline",
    )
    def _check_scope_alignment(self):
        for objective in self:
            if objective.scope == "organization":
                if objective.warehouse_id:
                    raise ValidationError(
                        "Organization objectives cannot be tied to a branch warehouse."
                    )
                if objective.parent_objective_id:
                    raise ValidationError(
                        "Organization objectives cannot be aligned under another objective."
                    )

            if objective.scope == "branch":
                if not objective.warehouse_id:
                    raise ValidationError(
                        "Branch objectives require a branch warehouse."
                    )
                if not objective.parent_objective_id:
                    raise ValidationError(
                        "Branch objectives must be aligned to an organization objective."
                    )
                if objective.parent_objective_id.scope != "organization":
                    raise ValidationError(
                        "Branch objectives can only align to organization objectives."
                    )
                if objective.parent_objective_id.plan_id != objective.plan_id:
                    raise ValidationError(
                        "Branch objective and organization objective must belong to the same plan."
                    )

            if (
                objective.deadline
                and objective.plan_id.date_end
                and objective.deadline > objective.plan_id.date_end
            ):
                raise ValidationError(
                    "Objective deadline cannot be after the strategic plan end date."
                )

    @api.constrains("is_primary", "scope", "state")
    def _check_primary_designation(self):
        for objective in self:
            if objective.is_primary and objective.scope != "organization":
                raise ValidationError(
                    "Only organization-wide objectives can be designated primary."
                )
            if objective.is_primary and objective.state not in {"active", "completed"}:
                raise ValidationError(
                    "Only active or completed objectives can retain the current primary designation."
                )

    @api.depends("deadline", "state")
    def _compute_overdue(self):
        today = fields.Date.context_today(self)
        for objective in self:
            objective.overdue = bool(
                objective.deadline
                and objective.deadline < today
                and objective.state == "active"
            )

    @api.depends(
        "okr_objective_ids.key_result_ids.required",
        "okr_objective_ids.key_result_ids.attainment",
        "okr_objective_ids.key_result_ids.scoring_status",
    )
    def _compute_score(self):
        eval_model = self.env["strategic.kpi.evaluation"]

        for objective in self:
            key_results = (
                objective.okr_objective_ids.key_result_ids.filtered("required")
            )

            if not key_results:
                objective.score = 0.0
                objective.scoring_status = "not_evaluated"
                continue

            if any(kr.scoring_status != "valid" for kr in key_results):
                objective.score = 0.0
                objective.scoring_status = "not_evaluated"
                continue

            objective.score = (
                sum(key_results.mapped("attainment")) / len(key_results)
            )
            objective.scoring_status = eval_model._attainment_status(
                objective.score
            )

    def _write_primary_flag(self, value):
        """Internal write used only after the primary workflow validates access."""
        return super(StrategicObjective, self).write({"is_primary": value})

    def _end_primary_designation(self, reason):
        self.ensure_one()
        if not self.is_primary:
            return

        now = fields.Datetime.now()
        history = self.env["strategic.objective.primary.history"].sudo().search(
            [
                ("objective_id", "=", self.id),
                ("ended_at", "=", False),
            ],
            order="designated_at desc, id desc",
            limit=1,
        )
        if history:
            history.write(
                {
                    "ended_at": now,
                    "ended_by_id": self.env.user.id,
                    "end_reason": reason,
                }
            )
        self._write_primary_flag(False)

    def action_designate_primary(self):
        """Designate this active organization objective as the current primary."""
        self.ensure_one()
        self._require_head_office()

        if self.scope != "organization":
            raise ValidationError(
                "Only organization-wide objectives can be designated primary."
            )
        if self.state != "active":
            raise ValidationError(
                "Only active organization-wide objectives can be designated primary."
            )
        if self.is_primary:
            return True

        current = self.search(
            [
                ("company_id", "=", self.company_id.id),
                ("scope", "=", "organization"),
                ("is_primary", "=", True),
            ],
            limit=1,
        )
        if current:
            current._end_primary_designation(
                "Replaced by primary objective: %s" % self.display_name
            )
            # Flush the previous primary flag before setting the replacement.
            # PostgreSQL checks the partial unique index row-by-row, so making
            # both flags dirty in one ORM flush can attempt to set the new row
            # TRUE before the old row's FALSE update reaches the database.
            current.flush_recordset(["is_primary"])

        now = fields.Datetime.now()
        self._write_primary_flag(True)
        self.env["strategic.objective.primary.history"].sudo().create(
            {
                "name": "Primary designation: %s" % self.display_name,
                "objective_id": self.id,
                "designated_at": now,
                "designated_by_id": self.env.user.id,
            }
        )
        return True

    def action_submit(self):
        for objective in self:
            if objective.state != "draft":
                raise ValidationError(
                    "Only draft objectives can be submitted for approval."
                )

            if (
                objective.scope == "branch"
                and not objective._is_head_office_user()
            ):
                objective._check_branch_user_scope()

            objective._write_lifecycle(
                {
                    "state": "pending_approval",
                    "proposer_id": self.env.user.id,
                    "submitted_at": fields.Datetime.now(),
                }
            )

    def _lock_for_review(self):
        """Lock the rows and re-read their state, so concurrent or repeated decisions cannot both pass the state check."""
        if not self:
            return
        try:
            self.env.cr.execute(
                "SELECT id FROM strategic_objective WHERE id IN %s FOR UPDATE NOWAIT",
                [tuple(self.ids)],
            )
        except psycopg2.errors.LockNotAvailable:
            raise UserError(
                "This objective is being reviewed by someone else. Reload the page and try again."
            )
        self.invalidate_recordset(["state"])

    def action_approve(self):
        self._require_head_office()
        self._lock_for_review()

        for objective in self:
            if objective.state != "pending_approval":
                raise ValidationError(
                    "Only pending objectives can be approved."
                )

            objective._write_lifecycle(
                {
                    "state": "active",
                    "approved_by_id": self.env.user.id,
                }
            )
            objective._log_review("approved")

    def action_reject(self, reason):
        """Head Office rejects a pending proposal with a reason.

        The objective returns to draft so its proposer can revise and resubmit it; the decision itself is kept
        in strategic.objective.review and the reason is posted to the proposer.
        """
        self._require_head_office()
        reason = (reason or "").strip()
        if not reason:
            raise ValidationError("Enter a reason for rejecting this objective.")
        self._lock_for_review()

        for objective in self:
            if objective.state != "pending_approval":
                raise ValidationError(
                    "Only pending objectives can be rejected."
                )

            objective._log_review("rejected", reason)
            objective._write_lifecycle({"state": "draft"})
            proposer = objective.proposer_id
            objective.message_post(
                body="Objective proposal rejected by %s. Reason: %s" % (self.env.user.name, reason),
                partner_ids=proposer.partner_id.ids if proposer else [],
                subtype_xmlid="mail.mt_comment",
            )
        return True

    def _log_review(self, decision, reason=False):
        """Append the Head Office decision to the review log (users have no write access to the log)."""
        self.ensure_one()
        self.env["strategic.objective.review"].sudo().create(
            {
                "objective_id": self.id,
                "objective_name": self.name,
                "company_id": self.company_id.id,
                "warehouse_id": self.warehouse_id.id,
                "scope": self.scope,
                "proposer_id": self.proposer_id.id,
                "submitted_at": self.submitted_at,
                "proposed_target": self._review_target_summary(),
                "decision": decision,
                "reason": reason or False,
                "reviewer_id": self.env.user.id,
                "decided_at": fields.Datetime.now(),
            }
        )

    def _review_target_summary(self):
        """Short text of what was proposed; the goal setup extends it with the measure and target."""
        self.ensure_one()
        return "Deadline %s" % self.deadline.strftime("%d %b %Y") if self.deadline else False

    def action_activate(self):
        self._require_head_office()

        for objective in self:
            if objective.state != "draft":
                raise ValidationError(
                    "Only draft objectives can be activated directly."
                )

            objective._write_lifecycle(
                {
                    "state": "active",
                    "approved_by_id": self.env.user.id,
                }
            )

    def _eligible_primary_replacements(self):
        """Return other active organization objectives eligible to become primary."""
        self.ensure_one()
        return self.search(
            [
                ("company_id", "=", self.company_id.id),
                ("scope", "=", "organization"),
                ("state", "=", "active"),
                ("is_primary", "=", False),
                ("id", "!=", self.id),
            ],
            order="deadline, id",
        )

    def action_open_close_wizard(self):
        """Open replacement selection when closing the current primary objective."""
        self.ensure_one()
        self._require_head_office()
        if self.state != "completed":
            raise ValidationError("Only completed objectives can be closed.")

        if not self.is_primary or not self._eligible_primary_replacements():
            self.action_close()
            return True

        return {
            "type": "ir.actions.act_window",
            "name": "Close Strategic Objective",
            "res_model": "strategic.objective.close.wizard",
            "view_mode": "form",
            "views": [(self.env.ref(
                "strategic_management.view_strategic_objective_close_wizard_form"
            ).id, "form")],
            "target": "new",
            "context": {"default_objective_id": self.id},
        }

    def action_complete(self):
        """Complete an active objective only when all required KRs are valid."""
        self._require_head_office()

        for objective in self:
            if objective.state != "active":
                raise ValidationError(
                    "Only active objectives can be completed."
                )

            required_key_results = (
                objective.okr_objective_ids
                .mapped("key_result_ids")
                .filtered(lambda kr: kr.required)
            )

            if not required_key_results:
                raise ValidationError(
                    "The objective cannot be completed because it has no "
                    "required Key Results. Add the required Key Results "
                    "before requesting completion."
                )

            unresolved = required_key_results.filtered(
                lambda kr: (
                    not kr.evaluation_id
                    or kr.evaluation_id.status != "valid"
                    or kr.scoring_status != "valid"
                )
            )

            if unresolved:
                details = []
                for kr in unresolved:
                    if not kr.evaluation_id:
                        reason = "missing evaluation"
                    elif kr.evaluation_id.status != "valid":
                        reason = "evaluation status: %s" % kr.evaluation_id.status
                    else:
                        reason = "Key Result scoring status: %s" % kr.scoring_status
                    details.append("%s (%s)" % (kr.name, reason))

                raise ValidationError(
                    "The objective cannot be completed because these required "
                    "Key Results do not have valid evaluations: %s. The objective "
                    "remains Active and Not Evaluated; no partial completion "
                    "will be recorded."
                    % ", ".join(details)
                )

            # A completed objective retains its primary designation until it is
            # explicitly closed. Closure performs the required atomic replacement.
            objective._write_lifecycle(
                {
                    "state": "completed",
                    "completed_by_id": self.env.user.id,
                }
            )

    def action_close(self, replacement_primary_id=False):
        """Close completed objectives, atomically replacing/clearing primary status."""
        self._require_head_office()

        if replacement_primary_id and len(self) != 1:
            raise ValidationError(
                "Close one objective at a time when selecting a replacement primary objective."
            )

        for objective in self:
            if objective.state != "completed":
                raise ValidationError(
                    "Only completed objectives can be closed."
                )

            replacement = self.env["strategic.objective"]
            if replacement_primary_id:
                replacement = self.env["strategic.objective"].browse(
                    replacement_primary_id
                ).exists()

            if objective.is_primary:
                candidates = objective._eligible_primary_replacements()
                if candidates and not replacement_primary_id:
                    raise ValidationError(
                        "This is the current primary objective. Select an active "
                        "organization-wide replacement before closing it."
                    )
                if not candidates and replacement_primary_id:
                    raise ValidationError(
                        "No eligible active organization objective remains; "
                        "a replacement must not be supplied."
                    )
                if replacement_primary_id and replacement not in candidates:
                    raise ValidationError(
                        "The replacement must be another active organization-wide "
                        "objective in the same company."
                    )

                if replacement:
                    end_reason = (
                        "Replaced by primary objective: %s" % replacement.display_name
                    )
                else:
                    end_reason = (
                        "No eligible active organization objective remained "
                        "when this objective was closed"
                    )
                objective._end_primary_designation(end_reason)
                # Preserve the partial unique index while switching in one transaction.
                objective.flush_recordset(["is_primary"])

                if replacement:
                    replacement._write_primary_flag(True)
                    self.env["strategic.objective.primary.history"].sudo().create(
                        {
                            "name": "Primary designation: %s" % replacement.display_name,
                            "objective_id": replacement.id,
                            "designated_at": fields.Datetime.now(),
                            "designated_by_id": self.env.user.id,
                        }
                    )
            elif replacement_primary_id:
                raise ValidationError(
                    "A replacement can only be selected when closing the current primary objective."
                )

            objective._write_lifecycle(
                {
                    "state": "closed",
                    "closed_by_id": self.env.user.id,
                }
            )

    def action_archive(self):
        self._require_head_office()

        for objective in self:
            if objective.state != "closed":
                raise ValidationError(
                    "Only closed objectives can be archived."
                )

            objective._write_lifecycle({"state": "archived"})

    def unlink(self):
        protected = self.filtered(
            lambda objective: objective.state
            in {"active", "completed", "closed"}
        )
        if protected:
            raise UserError(
                "Objectives in Active, Completed, or Closed state cannot be deleted. "
                "Archive them first, or contact your Head Office Manager."
            )

        return super().unlink()

    def action_extend_deadline(self, new_deadline, reason):
        if not reason:
            raise ValidationError(
                "A reason is required for an objective deadline revision."
            )

        self._require_head_office()
        new_deadline = fields.Date.to_date(new_deadline)

        for objective in self:
            if not new_deadline or new_deadline <= objective.deadline:
                raise ValidationError(
                    "The revised deadline must extend the current deadline."
                )

            old_deadline = objective.deadline
            objective.with_context(
                allow_objective_core_write=True
            ).write({"deadline": new_deadline})

            self.env["strategic.objective.revision"].create(
                {
                    "objective_id": objective.id,
                    "field_name": "deadline",
                    "old_value": str(old_deadline),
                    "new_value": str(new_deadline),
                    "reason": reason,
                }
            )

            if objective.scope == "organization":
                children = objective.child_objective_ids.filtered(
                    lambda child: child.deadline < new_deadline
                )

                for child in children:
                    child_old_deadline = child.deadline
                    child.with_context(
                        allow_objective_core_write=True
                    ).write({"deadline": new_deadline})

                    self.env["strategic.objective.revision"].create(
                        {
                            "objective_id": child.id,
                            "field_name": "deadline",
                            "old_value": str(child_old_deadline),
                            "new_value": str(new_deadline),
                            "reason": (
                                "Parent objective deadline extension: %s" % reason
                            ),
                        }
                    )

    @api.model
    def _prepare_objective_create_values(self, vals):
        vals = dict(vals)

        if not vals.get("proposer_id"):
            vals["proposer_id"] = self.env.user.id

        if not self._is_head_office_user():
            if vals.get("scope", "organization") != "branch":
                raise AccessError(
                    "Only Head Office Managers can create organization objectives."
                )

            branch_id = self.env.user.strategic_branch_warehouse_id.id
            if not branch_id:
                raise AccessError(
                    "Branch objective proposals require an assigned branch warehouse."
                )

            vals["warehouse_id"] = branch_id
            vals.setdefault("owner_id", self.env.user.id)

        return vals

    def _check_write_allowed(self, vals):
        if self.env.context.get("allow_objective_core_write"):
            return

        core_fields = {
            "name",
            "plan_id",
            "perspective",
            "scope",
            "warehouse_id",
            "parent_objective_id",
            "description",
            "deadline",
        }
        state_flow_fields = {
            "state",
            "proposer_id",
            "submitted_at",
            "approved_by_id",
            "completed_by_id",
            "closed_by_id",
        }

        for objective in self:
            if (
                core_fields.intersection(vals)
                and objective.state
                in {"active", "completed", "closed", "archived"}
            ):
                raise ValidationError(
                    "Active or later objectives require a controlled revision for core changes."
                )

            if not objective._is_head_office_user():
                objective._check_branch_user_scope()

                if objective.scope != "branch":
                    raise AccessError(
                        "Branch Managers cannot modify organization objectives."
                    )

                if vals.keys() - {"name", "description", "owner_id"} - state_flow_fields:
                    raise AccessError(
                        "Branch Managers can only update draft branch proposal details."
                    )

                if (
                    objective.state != "draft"
                    and vals.keys() - state_flow_fields
                ):
                    raise AccessError(
                        "Branch Managers cannot edit submitted branch objective proposals."
                    )

    def _check_branch_user_scope(self):
        self.ensure_one()
        user_branch = self.env.user.strategic_branch_warehouse_id

        if not user_branch or self.warehouse_id != user_branch:
            raise AccessError(
                "You can only access objectives for your assigned branch."
            )

    def _require_head_office(self):
        if not self._is_head_office_user():
            raise AccessError(
                "Only Head Office Managers can perform this objective transition."
            )

    def _is_head_office_user(self):
        return self.env.user.has_group(
            "strategic_management.group_strategic_head_office_manager"
        )


class StrategicObjectiveRevision(models.Model):
    _name = "strategic.objective.revision"
    _description = "Strategic Objective Revision"
    _order = "create_date desc, id desc"

    objective_id = fields.Many2one(
        "strategic.objective",
        required=True,
        ondelete="cascade",
        index=True,
    )
    field_name = fields.Char(required=True)
    old_value = fields.Char()
    new_value = fields.Char(required=True)
    reason = fields.Text(required=True)
    revised_by_id = fields.Many2one(
        "res.users",
        default=lambda self: self.env.user,
        required=True,
        readonly=True,
    )


class StrategicObjectiveReview(models.Model):
    """Append-only log of Head Office decisions on submitted objectives (approved or rejected)."""

    _name = "strategic.objective.review"
    _description = "Strategic Objective Review Decision"
    _order = "decided_at desc, id desc"

    objective_id = fields.Many2one(
        "strategic.objective",
        ondelete="set null",
        index=True,
        readonly=True,
    )
    # Snapshots taken at decision time, so the history stays as decided if the objective is revised later.
    objective_name = fields.Char(required=True, readonly=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True, index=True)
    warehouse_id = fields.Many2one("stock.warehouse", string="Branch Warehouse", readonly=True, index=True)
    scope = fields.Selection(
        [("organization", "Organization"), ("branch", "Branch")],
        readonly=True,
    )
    proposer_id = fields.Many2one("res.users", readonly=True)
    submitted_at = fields.Datetime(readonly=True)
    proposed_target = fields.Char(readonly=True)
    decision = fields.Selection(
        [("approved", "Approved"), ("rejected", "Rejected")],
        required=True,
        readonly=True,
        index=True,
    )
    reason = fields.Text(readonly=True)
    reviewer_id = fields.Many2one("res.users", required=True, readonly=True)
    decided_at = fields.Datetime(required=True, readonly=True, index=True)


class StrategicObjectivePrimaryHistory(models.Model):
    _name = "strategic.objective.primary.history"
    _description = "Strategic Objective Primary Designation History"
    _order = "designated_at desc, id desc"

    name = fields.Char(required=True, readonly=True)
    objective_id = fields.Many2one(
        "strategic.objective",
        required=True,
        ondelete="restrict",
        index=True,
        readonly=True,
    )
    company_id = fields.Many2one(
        related="objective_id.company_id",
        store=True,
        readonly=True,
    )
    designated_at = fields.Datetime(required=True, readonly=True, index=True)
    designated_by_id = fields.Many2one(
        "res.users",
        required=True,
        readonly=True,
    )
    ended_at = fields.Datetime(readonly=True, index=True)
    ended_by_id = fields.Many2one("res.users", readonly=True)
    end_reason = fields.Char(readonly=True)


class StrategicObjectiveCloseWizard(models.TransientModel):
    _name = "strategic.objective.close.wizard"
    _description = "Close Strategic Objective Wizard"

    objective_id = fields.Many2one(
        "strategic.objective",
        string="Objective to Close",
        required=True,
        readonly=True,
        ondelete="cascade",
    )
    company_id = fields.Many2one(
        related="objective_id.company_id",
        readonly=True,
    )
    replacement_required = fields.Boolean(
        compute="_compute_replacement_required",
    )
    replacement_primary_id = fields.Many2one(
        "strategic.objective",
        string="Replacement Primary Objective",
        domain="[('company_id', '=', company_id), ('scope', '=', 'organization'), ('state', '=', 'active'), ('is_primary', '=', False)]",
    )

    @api.depends(
        "objective_id",
        "objective_id.is_primary",
        "objective_id.company_id",
        "objective_id.state",
    )
    def _compute_replacement_required(self):
        for wizard in self:
            wizard.replacement_required = bool(
                wizard.objective_id
                and wizard.objective_id.is_primary
                and wizard.objective_id._eligible_primary_replacements()
            )

    def action_confirm(self):
        self.ensure_one()
        if not self.objective_id:
            raise ValidationError("Select the objective to close.")
        self.objective_id.with_user(self.env.user).action_close(
            replacement_primary_id=self.replacement_primary_id.id or False
        )
        return {"type": "ir.actions.act_window_close"}


class StrategicOkrObjective(models.Model):
    _name = "strategic.okr.objective"
    _description = "OKR Objective"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "date_start desc, name"

    name = fields.Char(required=True, tracking=True)
    strategic_objective_id = fields.Many2one(
        "strategic.objective",
        required=True,
        ondelete="cascade",
        index=True,
    )
    company_id = fields.Many2one(
        related="strategic_objective_id.company_id",
        store=True,
        readonly=True,
    )
    owner_id = fields.Many2one(
        "res.users",
        required=True,
        default=lambda self: self.env.user,
    )
    date_start = fields.Date(required=True)
    date_end = fields.Date(required=True)
    scope = fields.Selection(
        related="strategic_objective_id.scope",
        store=True,
        readonly=True,
    )
    warehouse_id = fields.Many2one(
        related="strategic_objective_id.warehouse_id",
        store=True,
        readonly=True,
    )
    key_result_ids = fields.One2many(
        "strategic.key.result",
        "okr_objective_id",
    )
    score = fields.Float(compute="_compute_score")
    scoring_status = fields.Selection(
        [
            ("not_evaluated", "Not Evaluated"),
            ("data_quality_issue", "Data Quality Issue"),
            ("not_achieved", "Not Achieved"),
            ("almost_met", "Almost Met"),
            ("achieved", "Achieved"),
            ("exceeded", "Exceeded"),
        ],
        compute="_compute_score",
    )

    @api.constrains("date_start", "date_end", "strategic_objective_id")
    def _check_dates(self):
        for okr in self:
            if (
                okr.date_start
                and okr.date_end
                and okr.date_start > okr.date_end
            ):
                raise ValidationError(
                    "OKR objective start date must not be after end date."
                )

            if (
                okr.date_end
                and okr.strategic_objective_id.deadline
                and okr.date_end > okr.strategic_objective_id.deadline
            ):
                raise ValidationError(
                    "OKR objective end date cannot be after the strategic objective deadline."
                )

    @api.depends(
        "key_result_ids.required",
        "key_result_ids.attainment",
        "key_result_ids.scoring_status",
    )
    def _compute_score(self):
        eval_model = self.env["strategic.kpi.evaluation"]

        for okr in self:
            key_results = okr.key_result_ids.filtered("required")

            if (
                not key_results
                or any(kr.scoring_status != "valid" for kr in key_results)
            ):
                okr.score = 0.0
                okr.scoring_status = "not_evaluated"
                continue

            okr.score = (
                sum(key_results.mapped("attainment")) / len(key_results)
            )
            okr.scoring_status = eval_model._attainment_status(okr.score)


class StrategicKeyResult(models.Model):
    _name = "strategic.key.result"
    _description = "Strategic Key Result"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "sequence, id"

    sequence = fields.Integer(default=10)
    name = fields.Char(required=True, tracking=True)
    okr_objective_id = fields.Many2one(
        "strategic.okr.objective",
        required=True,
        ondelete="cascade",
        index=True,
    )
    strategic_objective_id = fields.Many2one(
        related="okr_objective_id.strategic_objective_id",
        store=True,
        readonly=True,
    )
    company_id = fields.Many2one(
        related="okr_objective_id.company_id",
        store=True,
        readonly=True,
    )
    required = fields.Boolean(default=True)
    primary_kpi_id = fields.Many2one(
        "strategic.kpi.definition",
        required=True,
        ondelete="restrict",
        index=True,
    )
    supporting_kpi_ids = fields.Many2many(
        "strategic.kpi.definition",
        "strategic_key_result_supporting_kpi_rel",
        "key_result_id",
        "kpi_id",
        string="Supporting KPIs",
    )
    target_id = fields.Many2one(
        "strategic.kpi.target",
        required=True,
        ondelete="restrict",
        index=True,
    )
    evaluation_id = fields.Many2one(
        "strategic.kpi.evaluation",
        ondelete="restrict",
        index=True,
    )
    date_start = fields.Date(
        related="okr_objective_id.date_start",
        store=True,
        readonly=True,
    )
    date_end = fields.Date(
        related="okr_objective_id.date_end",
        store=True,
        readonly=True,
    )
    scope = fields.Selection(
        related="okr_objective_id.scope",
        store=True,
        readonly=True,
    )
    warehouse_id = fields.Many2one(
        related="okr_objective_id.warehouse_id",
        store=True,
        readonly=True,
    )
    unit = fields.Char(
        related="primary_kpi_id.unit",
        readonly=True,
    )
    attainment = fields.Float(compute="_compute_scoring")
    scoring_status = fields.Selection(
        [
            ("valid", "Valid"),
            ("not_evaluated", "Not Evaluated"),
            ("data_quality_issue", "Data Quality Issue"),
        ],
        compute="_compute_scoring",
    )
    attainment_status = fields.Selection(
        [
            ("not_evaluated", "Not Evaluated"),
            ("data_quality_issue", "Data Quality Issue"),
            ("not_achieved", "Not Achieved"),
            ("almost_met", "Almost Met"),
            ("achieved", "Achieved"),
            ("exceeded", "Exceeded"),
        ],
        compute="_compute_scoring",
    )

    @api.model_create_multi
    def create(self, vals_list):
        key_results = super().create(vals_list)
        # A target must be approved before its Key Result exists, so it may already have
        # evaluations; link the current one for this exact target (same rule as
        # strategic.kpi.evaluation._sync_linked_key_result_evaluation).
        # sudo: discovery only, limited to this Key Result's own target and KPI.
        Evaluation = self.env["strategic.kpi.evaluation"].sudo()
        for key_result in key_results.filtered(lambda kr: not kr.evaluation_id):
            current = Evaluation.search([
                ("target_id", "=", key_result.target_id.id),
                ("kpi_id", "=", key_result.primary_kpi_id.id),
                ("superseded_by_id", "=", False),
            ], order="evaluated_at desc, id desc", limit=1)
            if current:
                key_result.write({"evaluation_id": current.id})
        return key_results

    @api.depends(
        "evaluation_id.status",
        "evaluation_id.attainment",
        "evaluation_id.attainment_status",
    )
    def _compute_scoring(self):
        for key_result in self:
            # Read the linked evaluation only for this computed score.
            # This does not grant the user direct access to evaluation records.
            evaluation = key_result.evaluation_id.sudo()

            if not evaluation:
                key_result.attainment = 0.0
                key_result.scoring_status = "not_evaluated"
                key_result.attainment_status = "not_evaluated"

            elif evaluation.status == "valid":
                key_result.attainment = evaluation.attainment
                key_result.scoring_status = "valid"
                key_result.attainment_status = evaluation.attainment_status

            elif evaluation.status == "data_quality_issue":
                key_result.attainment = 0.0
                key_result.scoring_status = "data_quality_issue"
                key_result.attainment_status = "data_quality_issue"

            else:
                key_result.attainment = 0.0
                key_result.scoring_status = "not_evaluated"
                key_result.attainment_status = "not_evaluated"

    @api.constrains(
        "primary_kpi_id",
        "supporting_kpi_ids",
        "target_id",
        "evaluation_id",
        "okr_objective_id",
    )
    def _check_kpi_target_evaluation(self):
        for key_result in self:
            okr = key_result.okr_objective_id

            if key_result.primary_kpi_id in key_result.supporting_kpi_ids:
                raise ValidationError(
                    "Supporting KPIs cannot include the primary scoring KPI."
                )

            if key_result.target_id.kpi_id != key_result.primary_kpi_id:
                raise ValidationError(
                    "Key Result target must belong to its primary KPI."
                )

            if key_result.target_id.state != "approved":
                raise ValidationError(
                    "Key Result scoring requires an approved KPI target."
                )

            if key_result.target_id.scope != okr.strategic_objective_id.scope:
                raise ValidationError(
                    "Key Result target scope must match its OKR scope."
                )

            if (
                okr.strategic_objective_id.scope == "branch"
                and key_result.target_id.warehouse_id != okr.warehouse_id
            ):
                raise ValidationError(
                    "Branch Key Result target must match the OKR branch."
                )

            if (
                okr.strategic_objective_id.scope == "organization"
                and key_result.target_id.warehouse_id
            ):
                raise ValidationError(
                    "Organization Key Result targets cannot be branch-specific."
                )

            if (
                key_result.target_id.date_start != okr.date_start
                or key_result.target_id.date_end != okr.date_end
            ):
                raise ValidationError(
                    "Key Result target period must match the OKR reporting period."
                )

            if key_result.evaluation_id:
                if key_result.evaluation_id.kpi_id != key_result.primary_kpi_id:
                    raise ValidationError(
                        "Key Result evaluation must belong to its primary KPI."
                    )

                if key_result.evaluation_id.target_id != key_result.target_id:
                    raise ValidationError(
                        "Key Result evaluation must use the Key Result target."
                    )