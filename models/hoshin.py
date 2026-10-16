"""Hoshin Kanri horizons, improvement initiatives, and X-Matrix alignments.

This file extends the existing Strategic Objective model. It deliberately reuses
strategic.objective and strategic.kpi.definition rather than creating parallel
objective or KPI stores.
"""

import json

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


class StrategicObjectiveHoshin(models.Model):
    """Add a Hoshin horizon as a dimension independent of BSC perspective."""

    _inherit = "strategic.objective"

    hoshin_level = fields.Selection(
        [
            ("none", "Not a Hoshin Objective"),
            ("breakthrough", "Breakthrough Objective"),
            ("annual", "Annual Objective"),
        ],
        string="Hoshin Horizon",
        required=True,
        default="none",
        index=True,
        tracking=True,
        help=(
            "Hoshin deployment horizon. This is separate from the Balanced "
            "Scorecard perspective. Only organization-level objectives may "
            "be classified as breakthrough or annual objectives."
        ),
    )

    @api.model_create_multi
    def create(self, vals_list):
        # Hoshin annual objectives must go through the validated activation path;
        # otherwise state="active" at create time could bypass B>A alignment.
        for vals in vals_list:
            if (
                vals.get("scope", "organization") == "organization"
                and vals.get("hoshin_level", "none") == "annual"
                and vals.get("state", "draft") in {"active", "completed", "closed", "archived"}
            ):
                raise ValidationError(
                    "Annual Hoshin objectives must be created as drafts and activated "
                    "only after breakthrough alignment is recorded."
                )
        return super().create(vals_list)

    @api.constrains("scope", "hoshin_level")
    def _check_hoshin_objective_scope(self):
        for objective in self:
            if objective.scope != "organization" and objective.hoshin_level != "none":
                raise ValidationError(
                    "Only organization objectives can be classified as Hoshin "
                    "breakthrough or annual objectives."
                )

    def write(self, vals):
        if "hoshin_level" in vals and not self.env.context.get("allow_hoshin_revision"):
            protected = self.filtered(
                lambda objective: objective.state
                in {"active", "completed", "closed", "archived"}
            )
            if protected:
                raise ValidationError(
                    "Hoshin horizon changes on active or finalized objectives "
                    "require a controlled revision."
                )
        return super().write(vals)

    def _check_hoshin_activation_requirements(self):
        """Validate Hoshin prerequisites before an objective becomes active."""
        Alignment = self.env["strategic.xmatrix.alignment"]
        for objective in self:
            if objective.scope == "organization" and objective.hoshin_level == "annual":
                aligned_breakthrough = Alignment.search_count(
                    [
                        ("link_type", "=", "B>A"),
                        ("state", "=", "active"),
                        ("annual_objective_id", "=", objective.id),
                    ]
                )
                if not aligned_breakthrough:
                    raise ValidationError(
                        "An annual Hoshin objective must be aligned to at least "
                        "one breakthrough objective before activation."
                    )

    def action_activate(self):
        self._require_head_office()
        self.filtered(lambda objective: objective.state == "draft")._check_hoshin_activation_requirements()
        return super().action_activate()

    def action_approve(self):
        self._require_head_office()
        self.filtered(
            lambda objective: objective.state == "pending_approval"
        )._check_hoshin_activation_requirements()
        return super().action_approve()


class StrategicInitiative(models.Model):
    _name = "strategic.initiative"
    _description = "Strategic Improvement Initiative"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "date_start, name"

    name = fields.Char(required=True, tracking=True)
    plan_id = fields.Many2one(
        "strategic.plan", required=True, ondelete="restrict", index=True, tracking=True
    )
    company_id = fields.Many2one(
        related="plan_id.company_id", store=True, readonly=True, index=True
    )
    description = fields.Text()
    date_start = fields.Date(required=True, tracking=True, index=True)
    date_end = fields.Date(required=True, tracking=True, index=True)
    owner_id = fields.Many2one(
        "res.users",
        string="Accountable Owner",
        readonly=True,
        tracking=True,
        ondelete="restrict",
        help="Set from the initiative's primary X-Matrix owner relationship at activation.",
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("active", "Active"),
            ("completed", "Completed"),
            ("closed", "Closed"),
            ("archived", "Archived"),
        ],
        required=True,
        default="draft",
        index=True,
        tracking=True,
    )
    activated_by_id = fields.Many2one("res.users", readonly=True, copy=False, tracking=True)
    activated_at = fields.Datetime(readonly=True, copy=False, tracking=True)
    alignment_ids = fields.One2many(
        "strategic.xmatrix.alignment", "initiative_id", string="X-Matrix Alignments"
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("state", "draft") != "draft":
                raise ValidationError(
                    "Initiatives must be created as drafts and activated through the "
                    "validated lifecycle action."
                )
            if vals.get("owner_id"):
                raise ValidationError(
                    "The initiative owner must be assigned through its primary I>O alignment."
                )
        return super().create(vals_list)

    @api.constrains("plan_id", "date_start", "date_end")
    def _check_initiative_dates(self):
        for initiative in self:
            if (
                initiative.date_start
                and initiative.date_end
                and initiative.date_start > initiative.date_end
            ):
                raise ValidationError(
                    "Initiative start date must not be after its end date."
                )
            if initiative.plan_id and initiative.date_start and initiative.plan_id.date_start:
                if initiative.date_start < initiative.plan_id.date_start:
                    raise ValidationError(
                        "Initiative start date cannot be before the strategic plan."
                    )
            if initiative.plan_id and initiative.date_end and initiative.plan_id.date_end:
                if initiative.date_end > initiative.plan_id.date_end:
                    raise ValidationError(
                        "Initiative end date cannot be after the strategic plan."
                    )

    def write(self, vals):
        protected_fields = {"state", "activated_by_id", "activated_at"}
        if protected_fields.intersection(vals):
            raise ValidationError(
                "Initiative lifecycle fields can only be changed through lifecycle actions."
            )
        core_fields = {"name", "plan_id", "description", "date_start", "date_end", "owner_id"}
        if core_fields.intersection(vals):
            protected = self.filtered(lambda initiative: initiative.state != "draft")
            if protected:
                raise ValidationError(
                    "Active or finalized initiatives require a controlled revision for core changes."
                )
        return super().write(vals)

    def action_activate(self):
        self._require_head_office()
        Alignment = self.env["strategic.xmatrix.alignment"]
        for initiative in self:
            if initiative.state != "draft":
                raise ValidationError("Only draft initiatives can be activated.")

            annual_link = Alignment.search(
                [
                    ("link_type", "=", "I>A"),
                    ("state", "=", "active"),
                    ("initiative_id", "=", initiative.id),
                ],
                limit=1,
            )
            metric_link = Alignment.search(
                [
                    ("link_type", "=", "I>M"),
                    ("state", "=", "active"),
                    ("initiative_id", "=", initiative.id),
                ],
                limit=1,
            )
            owner_link = Alignment.search(
                [
                    ("link_type", "=", "I>O"),
                    ("state", "=", "active"),
                    ("initiative_id", "=", initiative.id),
                    ("relationship_type", "=", "primary"),
                ],
                limit=2,
            )

            missing = []
            if not annual_link:
                missing.append("an annual-objective alignment")
            if not metric_link:
                missing.append("a KPI/metric alignment")
            if not owner_link:
                missing.append("a primary accountable-owner alignment")
            if missing:
                raise ValidationError(
                    "Initiative cannot be activated without " + ", ".join(missing) + "."
                )
            if len(owner_link) != 1 or not owner_link.owner_id:
                raise ValidationError(
                    "An initiative must have exactly one primary accountable owner."
                )

            # The primary I>O relationship is the authoritative source of the
            # accountable owner; direct edits cannot bypass the X-Matrix link.
            super(StrategicInitiative, initiative).write(
                {
                    "owner_id": owner_link.owner_id.id,
                    "state": "active",
                    "activated_by_id": self.env.user.id,
                    "activated_at": fields.Datetime.now(),
                }
            )
        return True

    def _require_head_office(self):
        if not self.env.user.has_group(
            "strategic_management.group_strategic_head_office_manager"
        ):
            raise AccessError("Only Head Office Managers can activate initiatives.")


class StrategicXMatrixAlignment(models.Model):
    _name = "strategic.xmatrix.alignment"
    _description = "Strategic X-Matrix Alignment"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "link_type, relationship_type, id"

    name = fields.Char(compute="_compute_name", store=True)
    link_type = fields.Selection(
        [
            ("B>A", "Breakthrough Objective to Annual Objective"),
            ("I>A", "Initiative to Annual Objective"),
            ("I>M", "Initiative to KPI / Metric"),
            ("B>M", "Breakthrough Objective to KPI / Metric"),
            ("I>O", "Initiative to Owner"),
        ],
        required=True,
        index=True,
        tracking=True,
    )
    relationship_type = fields.Selection(
        [("primary", "Primary"), ("supporting", "Supporting")],
        required=True,
        default="primary",
        index=True,
        tracking=True,
    )
    breakthrough_objective_id = fields.Many2one(
        "strategic.objective", ondelete="restrict", index=True, tracking=True
    )
    annual_objective_id = fields.Many2one(
        "strategic.objective", ondelete="restrict", index=True, tracking=True
    )
    initiative_id = fields.Many2one(
        "strategic.initiative", ondelete="restrict", index=True, tracking=True
    )
    kpi_id = fields.Many2one(
        "strategic.kpi.definition",
        string="KPI / Metric",
        ondelete="restrict",
        index=True,
        tracking=True,
    )
    owner_id = fields.Many2one(
        "res.users", string="Accountable Owner", ondelete="restrict", index=True, tracking=True
    )
    effective_date = fields.Date(
        required=True, default=fields.Date.context_today, index=True, tracking=True
    )
    state = fields.Selection(
        [("active", "Active"), ("superseded", "Superseded")],
        required=True,
        default="active",
        readonly=True,
        copy=False,
        index=True,
        tracking=True,
    )
    revision_ids = fields.One2many(
        "strategic.xmatrix.alignment.history", "source_alignment_id",
        string="Revision History", readonly=True
    )
    change_reason = fields.Text(
        tracking=True,
        help="Optional explanation for creating or revising this alignment."
    )

    @api.depends(
        "link_type",
        "relationship_type",
        "breakthrough_objective_id.name",
        "annual_objective_id.name",
        "initiative_id.name",
        "kpi_id.name",
        "owner_id.name",
    )
    def _compute_name(self):
        for alignment in self:
            left = (
                alignment.breakthrough_objective_id.display_name
                or alignment.initiative_id.display_name
                or "Origin"
            )
            right = (
                alignment.annual_objective_id.display_name
                or alignment.kpi_id.display_name
                or alignment.owner_id.display_name
                or "Destination"
            )
            alignment.name = "%s: %s → %s" % (
                alignment.link_type or "X-Matrix",
                left,
                right,
            )

    @api.constrains(
        "link_type",
        "relationship_type",
        "breakthrough_objective_id",
        "annual_objective_id",
        "initiative_id",
        "kpi_id",
        "owner_id",
        "effective_date",
        "state",
    )
    def _check_endpoint_contract(self):
        endpoint_fields = {
            "breakthrough_objective_id",
            "annual_objective_id",
            "initiative_id",
            "kpi_id",
            "owner_id",
        }
        expected_by_type = {
            "B>A": {"breakthrough_objective_id", "annual_objective_id"},
            "I>A": {"initiative_id", "annual_objective_id"},
            "I>M": {"initiative_id", "kpi_id"},
            "B>M": {"breakthrough_objective_id", "kpi_id"},
            "I>O": {"initiative_id", "owner_id"},
        }

        for alignment in self:
            expected = expected_by_type.get(alignment.link_type)
            present = {
                field_name
                for field_name in endpoint_fields
                if alignment[field_name]
            }
            if expected is None or present != expected:
                raise ValidationError(
                    "X-Matrix link endpoints do not match the selected link type."
                )

            if alignment.effective_date is False:
                raise ValidationError("An X-Matrix alignment requires an effective date.")

            if alignment.link_type == "B>A":
                breakthrough = alignment.breakthrough_objective_id
                annual = alignment.annual_objective_id
                if (
                    breakthrough.scope != "organization"
                    or breakthrough.hoshin_level != "breakthrough"
                ):
                    raise ValidationError(
                        "The B endpoint of a B>A link must be a breakthrough objective."
                    )
                self._check_annual_objective(annual)
                if breakthrough.plan_id != annual.plan_id:
                    raise ValidationError(
                        "Breakthrough and annual objectives must belong to the same plan."
                    )

            elif alignment.link_type == "I>A":
                initiative = alignment.initiative_id
                annual = alignment.annual_objective_id
                self._check_annual_objective(annual)
                if initiative.plan_id != annual.plan_id:
                    raise ValidationError(
                        "Initiative and annual objective must belong to the same plan."
                    )

            elif alignment.link_type == "I>M":
                initiative = alignment.initiative_id
                if initiative.plan_id.company_id != alignment.kpi_id.company_id:
                    raise ValidationError(
                        "Initiative and KPI must belong to the same company."
                    )

            elif alignment.link_type == "B>M":
                breakthrough = alignment.breakthrough_objective_id
                if (
                    breakthrough.scope != "organization"
                    or breakthrough.hoshin_level != "breakthrough"
                ):
                    raise ValidationError(
                        "The B endpoint of a B>M link must be a breakthrough objective."
                    )
                if breakthrough.company_id != alignment.kpi_id.company_id:
                    raise ValidationError(
                        "Breakthrough objective and KPI must belong to the same company."
                    )

            elif alignment.link_type == "I>O":
                # An ownership relationship is a responsibility link only; it
                # intentionally does not grant record access to the owner.
                pass

            # Creating a new relationship to an active objective is valid. The
            # controlled-revision rule belongs in write(), where it protects
            # changes to an existing alignment. Applying that rule here would
            # incorrectly block a new B>A link to an active breakthrough objective.

            # A primary relationship is unique per origin and link category.
            if alignment.state == "active" and alignment.relationship_type == "primary":
                origin_field = (
                    "breakthrough_objective_id"
                    if alignment.link_type in {"B>A", "B>M"}
                    else "initiative_id"
                )
                origin = alignment[origin_field]
                duplicate_domain = [
                    ("id", "!=", alignment.id),
                    ("link_type", "=", alignment.link_type),
                    ("relationship_type", "=", "primary"),
                    ("state", "=", "active"),
                    (origin_field, "=", origin.id),
                ]
                replacing_id = self.env.context.get("replacing_alignment_id")
                if replacing_id:
                    duplicate_domain.append(("id", "!=", replacing_id))
                if self.search_count(duplicate_domain):
                    raise ValidationError(
                        "Only one primary destination is allowed for each X-Matrix "
                        "origin and link category; use a supporting link for additional destinations."
                    )

    @api.model
    def _check_annual_objective(self, objective):
        if (
            not objective
            or objective.scope != "organization"
            or objective.hoshin_level != "annual"
        ):
            raise ValidationError(
                "The A endpoint of an X-Matrix link must be an annual organization objective."
            )

    def write(self, vals):
        if "state" in vals and not self.env.context.get("allow_hoshin_revision"):
            raise ValidationError("Alignment state can only be changed through the controlled revision workflow.")
        core_fields = {
            "link_type",
            "relationship_type",
            "breakthrough_objective_id",
            "annual_objective_id",
            "initiative_id",
            "kpi_id",
            "owner_id",
            "effective_date",
        }
        if core_fields.intersection(vals):
            objective_endpoints = self.mapped("breakthrough_objective_id") | self.mapped("annual_objective_id")
            protected = objective_endpoints.filtered(
                lambda objective: objective.state
                in {"active", "completed", "closed", "archived"}
            )
            if protected or any(
                alignment.initiative_id and alignment.initiative_id.state != "draft"
                for alignment in self
            ):
                raise ValidationError(
                    "Active or finalized X-Matrix alignments require a controlled revision."
                )
        return super().write(vals)

    def action_revise(self, replacement_values, reason):
        """Replace an alignment without erasing the former approved relationship.

        The existing row becomes superseded. A new active row and an immutable
        history snapshot are created in the same transaction.
        """
        self.ensure_one()
        if not self.env.user.has_group(
            "strategic_management.group_strategic_head_office_manager"
        ):
            raise AccessError("Only Head Office Managers can revise X-Matrix alignments.")
        if self.state != "active":
            raise ValidationError("Only active alignments can be revised.")
        if not reason or not reason.strip():
            raise ValidationError("A reason is required when revising an X-Matrix alignment.")
        if not isinstance(replacement_values, dict) or not replacement_values:
            raise ValidationError("Provide replacement alignment values.")

        allowed_fields = {
            "link_type", "relationship_type", "breakthrough_objective_id",
            "annual_objective_id", "initiative_id", "kpi_id", "owner_id",
            "effective_date", "change_reason",
        }
        if set(replacement_values) - allowed_fields:
            raise ValidationError("Replacement values contain unsupported alignment fields.")

        old_snapshot = self._snapshot_values()
        # A revision changes only the supplied attributes; omitted endpoints and
        # relationship metadata carry forward from the previous alignment.
        new_values = {key: value for key, value in old_snapshot.items() if key != "state"}
        new_values.update(replacement_values)
        new_values.setdefault("effective_date", fields.Date.context_today(self))
        new_values["change_reason"] = reason.strip()
        replacing_context = {
            "allow_hoshin_revision": True,
            "replacing_alignment_id": self.id,
        }
        # The newly-created active row is checked against other active primary
        # links, while the record being replaced is excluded from that check.
        new_alignment = self.with_context(**replacing_context).create(new_values)
        # Bypass public write guard only after the replacement itself validates.
        super(StrategicXMatrixAlignment, self.with_context(allow_hoshin_revision=True)).write(
            {"state": "superseded"}
        )
        self.env["strategic.xmatrix.alignment.history"].with_context(
            allow_hoshin_history_create=True
        ).create(
            {
                "source_alignment_id": self.id,
                "replacement_alignment_id": new_alignment.id,
                "old_snapshot": json.dumps(old_snapshot, sort_keys=True),
                "new_snapshot": json.dumps(new_alignment._snapshot_values(), sort_keys=True),
                "reason": reason.strip(),
                "changed_by_id": self.env.user.id,
            }
        )
        return new_alignment

    def action_open_revision_wizard(self):
        """Open the controlled revision wizard for an active alignment."""
        self.ensure_one()
        if not self.env.user.has_group(
            "strategic_management.group_strategic_head_office_manager"
        ):
            raise AccessError("Only Head Office Managers can revise X-Matrix alignments.")
        if self.state != "active":
            raise ValidationError("Only active alignments can be revised.")
        # Provide explicit defaults in the returned action as well as through
        # default_get. This makes the wizard reliable across web-client action
        # contexts and prevents an incomplete replacement from being submitted.
        return {
            "type": "ir.actions.act_window",
            "name": "Revise X-Matrix Alignment",
            "res_model": "strategic.xmatrix.alignment.revision.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_alignment_id": self.id,
                "default_link_type": self.link_type,
                "default_relationship_type": self.relationship_type,
                "default_breakthrough_objective_id": self.breakthrough_objective_id.id or False,
                "default_annual_objective_id": self.annual_objective_id.id or False,
                "default_initiative_id": self.initiative_id.id or False,
                "default_kpi_id": self.kpi_id.id or False,
                "default_owner_id": self.owner_id.id or False,
                "default_effective_date": fields.Date.to_string(self.effective_date)
                if self.effective_date else False,
            },
        }

    def _snapshot_values(self):
        self.ensure_one()
        relational_fields = {
            "breakthrough_objective_id", "annual_objective_id", "initiative_id",
            "kpi_id", "owner_id",
        }
        values = {}
        for field_name in (
            "link_type", "relationship_type", "breakthrough_objective_id",
            "annual_objective_id", "initiative_id", "kpi_id", "owner_id",
            "effective_date", "change_reason", "state",
        ):
            value = self[field_name]
            if field_name in relational_fields:
                value = value.id or False
            elif field_name == "effective_date":
                value = fields.Date.to_string(value) if value else False
            values[field_name] = value
        return values

    def unlink(self):
        objective_endpoints = self.mapped("breakthrough_objective_id") | self.mapped("annual_objective_id")
        protected = objective_endpoints.filtered(
            lambda objective: objective.state
            in {"active", "completed", "closed", "archived"}
        )
        if protected or any(
            alignment.initiative_id and alignment.initiative_id.state != "draft"
            for alignment in self
        ):
            raise ValidationError(
                "Active or finalized X-Matrix alignments cannot be deleted; use a controlled revision."
            )
        return super().unlink()


class StrategicXMatrixAlignmentHistory(models.Model):
    _name = "strategic.xmatrix.alignment.history"
    _description = "Strategic X-Matrix Alignment Revision History"
    _order = "changed_at desc, id desc"

    source_alignment_id = fields.Many2one(
        "strategic.xmatrix.alignment", required=True, ondelete="restrict", index=True
    )
    replacement_alignment_id = fields.Many2one(
        "strategic.xmatrix.alignment", required=True, ondelete="restrict", index=True
    )
    old_snapshot = fields.Text(required=True, readonly=True)
    new_snapshot = fields.Text(required=True, readonly=True)
    reason = fields.Text(required=True, readonly=True)
    changed_by_id = fields.Many2one(
        "res.users", required=True, readonly=True, default=lambda self: self.env.user
    )
    changed_at = fields.Datetime(
        required=True, readonly=True, default=fields.Datetime.now, index=True
    )

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get("allow_hoshin_history_create"):
            raise AccessError("X-Matrix revision history can only be recorded by the controlled revision workflow.")
        return super().create(vals_list)

    def write(self, vals):
        raise ValidationError("X-Matrix revision history is immutable.")

    def unlink(self):
        raise ValidationError("X-Matrix revision history cannot be deleted.")


class StrategicXMatrixAlignmentRevisionWizard(models.TransientModel):
    """Transient form used to revise a relationship without deleting history."""

    _name = "strategic.xmatrix.alignment.revision.wizard"
    _description = "Revise X-Matrix Alignment"

    alignment_id = fields.Many2one(
        "strategic.xmatrix.alignment", required=True, readonly=True
    )
    link_type = fields.Selection(
        [
            ("B>A", "Breakthrough Objective to Annual Objective"),
            ("I>A", "Initiative to Annual Objective"),
            ("I>M", "Initiative to KPI / Metric"),
            ("B>M", "Breakthrough Objective to KPI / Metric"),
            ("I>O", "Initiative to Owner"),
        ], required=True, readonly=True
    )
    relationship_type = fields.Selection(
        [("primary", "Primary"), ("supporting", "Supporting")],
        required=True,
        default="primary",
    )
    breakthrough_objective_id = fields.Many2one("strategic.objective")
    annual_objective_id = fields.Many2one("strategic.objective")
    initiative_id = fields.Many2one("strategic.initiative")
    kpi_id = fields.Many2one("strategic.kpi.definition", string="KPI / Metric")
    owner_id = fields.Many2one("res.users", string="Accountable Owner")
    effective_date = fields.Date(required=True, default=fields.Date.context_today)
    reason = fields.Text(required=True, string="Reason for Revision")

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        alignment_id = self.env.context.get("default_alignment_id")
        if alignment_id:
            alignment = self.env["strategic.xmatrix.alignment"].browse(alignment_id).exists()
            if alignment:
                snapshot = alignment._snapshot_values()
                values.update({
                    "alignment_id": alignment.id,
                    "link_type": alignment.link_type,
                    "relationship_type": alignment.relationship_type,
                    "breakthrough_objective_id": snapshot["breakthrough_objective_id"],
                    "annual_objective_id": snapshot["annual_objective_id"],
                    "initiative_id": snapshot["initiative_id"],
                    "kpi_id": snapshot["kpi_id"],
                    "owner_id": snapshot["owner_id"],
                    "effective_date": snapshot["effective_date"],
                })
        return values

    def action_apply_revision(self):
        self.ensure_one()
        if not self.env.user.has_group(
            "strategic_management.group_strategic_head_office_manager"
        ):
            raise AccessError("Only Head Office Managers can revise X-Matrix alignments.")
        alignment = self.alignment_id.exists()
        if not alignment or alignment.state != "active":
            raise ValidationError("The source alignment is no longer active.")
        if not self.link_type or self.link_type != alignment.link_type:
            raise ValidationError(
                "The relationship type was not initialized correctly. Close this wizard, "
                "reopen Revise Alignment on a saved active record, and try again."
            )
        if not self.reason or not self.reason.strip():
            raise ValidationError("A reason is required when revising an X-Matrix alignment.")

        # Send every endpoint, including unused endpoints as False, so the new
        # record exactly matches its link type rather than retaining stale values.
        replacement_values = {
            "link_type": self.link_type,
            "relationship_type": self.relationship_type,
            "breakthrough_objective_id": self.breakthrough_objective_id.id or False,
            "annual_objective_id": self.annual_objective_id.id or False,
            "initiative_id": self.initiative_id.id or False,
            "kpi_id": self.kpi_id.id or False,
            "owner_id": self.owner_id.id or False,
            "effective_date": self.effective_date,
        }
        revised = alignment.action_revise(replacement_values, self.reason)
        return {
            "type": "ir.actions.act_window",
            "name": "Revised X-Matrix Alignment",
            "res_model": "strategic.xmatrix.alignment",
            "view_mode": "form",
            "res_id": revised.id,
            "target": "current",
        }
