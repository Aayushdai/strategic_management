"""KPI/OKR work requests: what Head Office needs measured, assigned to an Admin.

Kept apart from Branch Manager objective proposals, and from KPI and objective records: a request is a requirement in
words, not yet a measure, so it must not appear in any KPI or objective report. Only Head Office reads these records and
every change goes through the checked methods below.

Head Office keeps full control until the assigned Admin accepts. From then on the requirement is locked, and later changes
go through review comments. The Admin writes a proposal: how something should be measured, kept apart from KPI and Key
Result records so nothing is active before Head Office approves it. Approval creates the real records through the existing
KPI, target, baseline, objective and Key Result rules. An Admin never sees a result: rows for them carry no evaluation.
"""

import json
import math

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .kpi import (
    REPORTING_FREQUENCIES,
    TIME_WINDOW_CALCULATIONS,
    _ACCOUNTING_CALCULATIONS,
    _DAYS_CALCULATIONS,
    _MONETARY_CALCULATIONS,
    _PERCENT_CALCULATIONS,
    _SOURCE_CALCULATIONS,
    _validate_calculation_contract,
)
from .kpi_source_adapter import _SOURCE_ADAPTERS

_ADMIN = "strategic_management.group_strategic_admin"
_OPEN_STATES = ("draft", "submitted", "assigned")
_ADMIN_STATES = ("assigned", "in_progress", "changes_requested", "pending_review", "approved", "rejected")
_EDITABLE_STATES = ("in_progress", "changes_requested")
# An Admin's own proposal names the department it is for; its KPIs must read that department's data,
# and an approved OKR lands in that department's business area.
_DEPARTMENT_SOURCES = {"sales": ("pos", "accounting"), "inventory": ("inventory",), "purchasing": ("purchase",)}
_DEPARTMENT_AREA = {"sales": "sales_finance", "inventory": "inventory_waste", "purchasing": "purchasing"}
# A request whose work an Admin holds: if they lose the role, Head Office recovers it.
_HELD_STATES = ("in_progress", "changes_requested", "pending_review")
# Calculations an Admin can propose: the engine can run them and they need no product record, which Admin cannot read.
_NEEDS_PRODUCT = {"purchase_unit_price_change_pct", "inventory_waste_recording_pct"}
_ALL_PRODUCTS = (_PERCENT_CALCULATIONS | _DAYS_CALCULATIONS | _ACCOUNTING_CALCULATIONS | _MONETARY_CALCULATIONS | {"supplier_on_time_delivery_pct"}) - _NEEDS_PRODUCT


def _proposable(calculation):
    return calculation == "manual_value" or (calculation in _ALL_PRODUCTS and calculation in _SOURCE_ADAPTERS.get(_SOURCE_CALCULATIONS.get(calculation), ()))


def _to_hours(value):
    """'17:30' to 17.5; False when empty. Anything else is refused."""
    if not value:
        return False
    try:
        hours, minutes = str(value).split(":")[:2]
        hours, minutes = int(hours), int(minutes)
    except ValueError:
        raise ValidationError("Enter the time interval as a time of day.") from None
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise ValidationError("Enter the time interval as a time of day.")
    return hours + minutes / 60


def _to_clock(hours):
    return "%02d:%02d" % (int(hours), round((hours - int(hours)) * 60))


class StrategicKpiWorkRequest(models.Model):
    _name = "strategic.kpi.work.request"
    _description = "KPI/OKR Work Request"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    name = fields.Char("Title", required=True, tracking=True)
    description = fields.Text("Business Requirement")
    request_type = fields.Selection([("kpi", "KPI"), ("okr", "OKR"), ("unsure", "Not Sure")], "Type", required=True, default="unsure", tracking=True)
    state = fields.Selection([("draft", "Draft"), ("submitted", "Submitted"), ("assigned", "Assigned / Pending Admin"),
                              ("in_progress", "In Progress"), ("changes_requested", "Changes Requested"), ("pending_review", "Pending Head Office Review"),
                              ("approved", "Approved"), ("rejected", "Rejected"), ("withdrawn", "Withdrawn")],
                             default="draft", required=True, readonly=True, index=True, tracking=True)
    requested_by_id = fields.Many2one("res.users", "Requested By", required=True, readonly=True)
    origin = fields.Selection([("head_office", "Head Office request"), ("admin", "Admin proposal")], required=True, default="head_office", readonly=True, index=True)
    department_id = fields.Many2one("strategic.department", "Department", readonly=True,
                                    help="Admin proposals: the department the KPI or OKR is for. Its KPIs must use that department's data.")
    admin_user_id = fields.Many2one("res.users", "Assigned Admin", index=True, tracking=True)
    scope = fields.Selection([("all", "All Branches"), ("selected", "Selected Branches")], required=True, default="all", tracking=True)
    warehouse_ids = fields.Many2many("stock.warehouse", string="Branches")
    date_start = fields.Date("Period Start")
    date_end = fields.Date("Period End")
    time_start = fields.Float("From Time")
    time_end = fields.Float("To Time")
    has_interval = fields.Boolean()
    instructions = fields.Text("Additional Instructions")
    company_id = fields.Many2one("res.company", required=True, readonly=True, index=True)
    submitted_at = fields.Datetime(readonly=True)
    assigned_at = fields.Datetime(readonly=True)
    withdrawn_by_id = fields.Many2one("res.users", "Withdrawn By", readonly=True)
    withdrawn_at = fields.Datetime(readonly=True)
    started_at = fields.Datetime("Work Started", readonly=True)
    proposal_ids = fields.One2many("strategic.kpi.work.proposal", "request_id", "Proposal")
    history_ids = fields.One2many("strategic.kpi.work.history", "request_id", "History")

    @api.constrains("admin_user_id")
    def _check_admin(self):
        for request in self.filtered("admin_user_id"):
            if not request.admin_user_id.has_group(_ADMIN):
                raise ValidationError("%s does not have the Admin role." % request.admin_user_id.name)

    @api.constrains("date_start", "date_end", "time_start", "time_end", "has_interval")
    def _check_period(self):
        for request in self:
            if bool(request.date_start) != bool(request.date_end):
                raise ValidationError("Enter both the start and the end of the measurement period, or neither.")
            if request.date_start and request.date_start > request.date_end:
                raise ValidationError("The measurement period must start before it ends.")
            # shortcut: an interval crossing midnight is refused, split it into two requests until one is needed.
            if request.has_interval and request.time_start >= request.time_end:
                raise ValidationError("The time interval must start before it ends, within one day.")

    # ------------------------------------------------------------------ checks
    @api.model
    def _require_head_office(self):
        if self.env["strategic.performance.dashboard"]._strategic_role() != "head_office":
            raise AccessError("KPI/OKR work requests are managed by Head Office.")

    @api.model
    def _assignable_admins(self):
        return self.env["res.users"].sudo().search([
            ("active", "=", True), ("share", "=", False), ("company_ids", "in", self.env.company.id), ("all_group_ids", "in", self.env.ref(_ADMIN).id)], order="name")

    def _admin_lost_role(self):
        self.ensure_one()
        admin = self.sudo().admin_user_id
        return bool(admin) and (not admin.active or not admin.has_group(_ADMIN))

    def _log(self, action, comment=False, snapshot=False):
        self.env["strategic.kpi.work.history"].sudo().create({
            "request_id": self.id, "action": action, "comment": comment or False, "user_id": self.env.user.id, "admin_user_id": self.sudo().admin_user_id.id or False,
            "revision": self.sudo().proposal_ids[:1].revision or 0, "snapshot": json.dumps(snapshot) if snapshot else False})

    def _notify(self, summary, note, recipients=None):
        """Close what this request was waiting for, then ask the next person, through the module's existing activities."""
        request = self.sudo()
        request.activity_ids.action_feedback(feedback=summary)
        request.message_post(body=note, subject=summary, subtype_xmlid="mail.mt_note")
        if recipients is not False:
            kwargs = {"recipients": recipients} if recipients else {"scope": "organization"}
            self.env["strategic.kpi.target"].sudo()._create_deduplicated_activity(request, "%s: %s" % (summary, request.name), note, company=request.company_id, **kwargs)

    def _announce_assignment(self, comment=False, action="assigned"):
        request = self.sudo()
        request._log(action, comment)
        request._notify("KPI/OKR request assigned to you", request.description or request.name, recipients=request.admin_user_id)

    def _is_open(self):
        """Whether Head Office may still change this request directly."""
        self.ensure_one()
        return self.state in _OPEN_STATES

    def _require_open(self):
        if not self._is_open():
            raise UserError("This request can no longer be changed directly: it was withdrawn, or its Admin has started work.")

    def _own(self):
        """This company's request as sudo; every change below has already passed the Head Office check."""
        self.ensure_one()
        request = self.sudo().exists()
        if not request or request.company_id != self.env.company:
            raise AccessError("This request is not in the active company.")
        return request

    @api.model
    def _admin_from(self, admin_id):
        if not admin_id:
            return self.env["res.users"]
        admin = self._assignable_admins().filtered(lambda user: user.id == int(admin_id))
        if not admin:
            raise ValidationError("Choose a user who has the Admin role.")
        return admin

    # ------------------------------------------------------------------ data for the page
    def _row(self):
        self.ensure_one()
        request = self.sudo()
        admin = request.admin_user_id
        return {
            "id": request.id, "name": request.name, "description": request.description or "", "request_type": request.request_type,
            "type_label": dict(self._fields["request_type"].selection)[request.request_type],
            "state": request.state, "status": dict(self._fields["state"].selection)[request.state],
            "requested_by": request.requested_by_id.name, "admin_user_id": admin.id, "admin": admin.name or False,
            "origin": request.origin, "department": request.department_id.name or False,
            "admin_lost_role": request._admin_lost_role(),
            "scope": request.scope, "scope_label": dict(self._fields["scope"].selection)[request.scope],
            "warehouse_ids": request.warehouse_ids.ids, "branches": request.warehouse_ids.mapped("display_name"),
            "date_start": fields.Date.to_string(request.date_start) if request.date_start else "", "date_end": fields.Date.to_string(request.date_end) if request.date_end else "",
            "time_start": _to_clock(request.time_start) if request.has_interval else "", "time_end": _to_clock(request.time_end) if request.has_interval else "",
            "instructions": request.instructions or "", "created_at": fields.Datetime.to_string(request.create_date),
            "withdrawn_by": request.withdrawn_by_id.name or False, "withdrawn_at": fields.Datetime.to_string(request.withdrawn_at) if request.withdrawn_at else False,
            "can_edit": request._is_open(), "can_assign": request.state in ("submitted", "assigned"), "can_withdraw": request.state in ("submitted", "assigned"),
            "can_delete": request.state == "draft",
            "can_review": request.state == "pending_review", "can_recover": request.state in _HELD_STATES and request._admin_lost_role(),
            "has_details": request.state not in ("draft", "submitted"),
            # What the Admin is still drafting stays with them; Head Office reads what was submitted.
            "proposal": request.proposal_ids[:1]._row() if request.state in ("pending_review", "approved", "rejected") and request.proposal_ids else False,
            "history": request.history_ids._rows(), "activated": request._activated_rows(),
        }

    @api.model
    def get_work_requests(self):
        self._require_head_office()
        requests = self.sudo().search([("company_id", "=", self.env.company.id)])
        requests = requests.filtered(lambda request: request.origin != "admin" or "submitted" in request.history_ids.mapped("action"))
        return {"requests": [request._row() for request in requests],
                "admins": [{"id": user.id, "name": user.name} for user in self._assignable_admins()],
                "branches": [{"id": branch.id, "name": branch.display_name} for branch in self.env["stock.warehouse"]._strategic_branches(self.env.company).sorted("name")],
                "types": [{"id": key, "name": label} for key, label in self._fields["request_type"].selection]}

    # ------------------------------------------------------------------ Head Office actions
    @api.model
    def save_work_request(self, values, submit=False):
        """Create or update a draft and optionally send it. Who asked, the company and the status never come from the browser."""
        self._require_head_office()
        name = (values.get("name") or "").strip()
        if not name:
            raise ValidationError("Enter the request title.")
        request_type = values.get("request_type") or "unsure"
        scope = values.get("scope") or "all"
        if request_type not in dict(self._fields["request_type"].selection) or scope not in dict(self._fields["scope"].selection):
            raise ValidationError("Choose a valid type and scope.")
        branches = self.env["stock.warehouse"]
        if scope == "selected":
            branches = self.env["stock.warehouse"]._strategic_branches(self.env.company).filtered(lambda branch: branch.id in [int(value) for value in values.get("warehouse_ids") or []])
            if len(branches) != len(set(values.get("warehouse_ids") or [])):
                raise ValidationError("Choose branches of this company.")
        admin = self._admin_from(values.get("admin_user_id"))
        try:
            date_start, date_end = fields.Date.to_date(values.get("date_start") or False), fields.Date.to_date(values.get("date_end") or False)
        except (TypeError, ValueError):
            raise ValidationError("Enter a valid measurement period.") from None
        time_start, time_end = _to_hours(values.get("time_start")), _to_hours(values.get("time_end"))
        if (time_start is False) != (time_end is False):
            raise ValidationError("Enter both the start and the end of the time interval, or neither.")
        description = (values.get("description") or "").strip()
        vals = {"name": name, "description": description or False, "request_type": request_type, "scope": scope, "warehouse_ids": [(6, 0, branches.ids)], "admin_user_id": admin.id or False,
                "date_start": date_start or False, "date_end": date_end or False, "has_interval": time_start is not False, "time_start": time_start or 0.0, "time_end": time_end or 0.0,
                "instructions": (values.get("instructions") or "").strip() or False}
        request = self.browse(int(values["id"]))._own() if values.get("id") else self.sudo()
        if request:
            request._require_open()
        # A request already sent stays sent: editing it can only keep or change its assignment, never make it a draft again.
        submit = submit or (bool(request) and request.state != "draft")
        if submit:
            if not description:
                raise ValidationError("Enter the business requirement before submitting.")
            if scope == "selected" and not branches:
                raise ValidationError("Choose at least one branch.")
        previous_admin = request.admin_user_id if request else admin.browse()
        was_assigned = bool(request) and request.state == "assigned"
        if request:
            request.write(vals)
        else:
            request = self.sudo().create(dict(vals, requested_by_id=self.env.user.id, company_id=self.env.company.id))
        if submit:
            now = fields.Datetime.now()
            status = {"state": "assigned" if admin else "submitted", "submitted_at": request.submitted_at or now}
            if admin != previous_admin or not request.assigned_at:
                status["assigned_at"] = now if admin else False
            request.write(status)
            if admin and (admin != previous_admin or not was_assigned):
                request._announce_assignment()
        return request._row()

    def action_assign_admin(self, admin_id):
        """Assign or reassign the Admin of a submitted request."""
        self._require_head_office()
        request = self._own()
        request._require_open()
        if request.state == "draft":
            raise UserError("Submit the request before assigning it.")
        admin = self._admin_from(admin_id)
        if not admin:
            raise ValidationError("Choose a user who has the Admin role.")
        request.write({"admin_user_id": admin.id, "state": "assigned", "assigned_at": fields.Datetime.now()})
        request._announce_assignment()
        return request._row()

    def action_recover_abandoned(self, admin_id, reason):
        """The assigned Admin lost the role after accepting. The new Admin carries on from any draft, which is kept."""
        self._require_head_office()
        request = self._own()
        reason = (reason or "").strip()
        if request.state not in _HELD_STATES or not request._admin_lost_role():
            raise UserError("Only a request whose Admin no longer has the role can be recovered.")
        if not reason:
            raise ValidationError("Give the reason for reassigning this request.")
        admin = self._admin_from(admin_id)
        if not admin:
            raise ValidationError("Choose a user who has the Admin role.")
        previous = request.admin_user_id.name
        request.write({"admin_user_id": admin.id, "assigned_at": fields.Datetime.now()})
        request._announce_assignment("Reassigned from %s: %s" % (previous, reason), action="recovered")
        return request._row()

    # ------------------------------------------------------------------ Head Office review
    def action_review(self, decision, comment=False):
        """Approve (activating the real records), request changes, or reject. A request is decided once per submission."""
        self._require_head_office()
        request = self._own()
        comment = (comment or "").strip()
        if decision not in ("approve", "changes", "reject"):
            raise ValidationError("Choose Approve, Request Changes or Reject.")
        if request.state != "pending_review":
            raise UserError("Only a proposal waiting for review can be decided.")
        if decision != "approve" and not comment:
            raise ValidationError("Explain what must change." if decision == "changes" else "Give the reason for rejecting this proposal.")
        state, proposal_state, action, summary = {
            "approve": ("approved", "approved", "approved", "KPI/OKR proposal approved"),
            "changes": ("changes_requested", "changes_requested", "changes_requested", "Changes requested on your KPI/OKR proposal"),
            "reject": ("rejected", "rejected", "rejected", "KPI/OKR proposal rejected"),
        }[decision]
        # Written and flushed first: a parallel decision fails on this row and retries, then finds it already decided.
        request.write({"state": state})
        request.flush_recordset()
        proposal = request.proposal_ids[:1]
        proposal.write({"state": proposal_state})
        request._log(action, comment)
        if decision == "approve":
            proposal._activate()
        # An approved or rejected proposal leaves nothing to do, but the Admin still gets the outcome as an activity to acknowledge.
        request._notify(summary, comment or summary, recipients=request.admin_user_id)
        return request._row()

    def _activated_rows(self):
        """For Head Office only: what approval created and its latest results. Never added to an Admin's row."""
        self.ensure_one()
        proposal = self.sudo().proposal_ids[:1]
        if not proposal or proposal.state != "approved":
            return False
        kpi = proposal.activated_kpi_id
        result = {"kpi": False, "objective": False}
        if kpi:
            evaluations = self.env["strategic.kpi.evaluation"].sudo().search([("kpi_id", "=", kpi.id), ("superseded_by_id", "=", False)], order="date_start desc, id desc", limit=30)
            status_labels = dict(evaluations._fields["status"].selection)
            result["kpi"] = {"id": kpi.id, "code": kpi.code, "name": kpi.name, "frequency": dict(REPORTING_FREQUENCIES).get(kpi.reporting_frequency, ""),
                             "results": [{"id": row.id, "start": fields.Date.to_string(row.date_start), "end": fields.Date.to_string(row.date_end), "branch": row.warehouse_id.display_name or "All Branches",
                                          "actual": row.actual_value if row.has_actual_value else False, "target": row.target_value, "unit": row.unit or "", "attainment": row.attainment if row.status == "valid" else False,
                                          "status": status_labels.get(row.status, row.status), "reason": row.reason or ""} for row in evaluations]}
        if proposal.activated_objective_id:
            objective = proposal.activated_objective_id
            result["objective"] = {"id": objective.id, "name": objective.name, "state": dict(objective._fields["state"].selection).get(objective.state),
                                   "key_results": objective.okr_objective_ids.key_result_ids.mapped("name")}
        return result

    def action_withdraw(self):
        """Take back a submitted or assigned request. It stays listed for its history."""
        self._require_head_office()
        request = self._own()
        if request.state not in ("submitted", "assigned"):
            raise UserError("Only a submitted or assigned request can be withdrawn; a draft can simply be deleted.")
        request.write({"state": "withdrawn", "withdrawn_by_id": self.env.user.id, "withdrawn_at": fields.Datetime.now()})
        request._log("withdrawn")
        request._notify("KPI/OKR request withdrawn", "Head Office withdrew this request.", recipients=False)
        return request._row()

    # ------------------------------------------------------------------ the assigned Admin
    @api.model
    def _require_admin(self):
        if self.env["strategic.performance.dashboard"]._strategic_role_name() != "admin":
            raise AccessError("This workspace is for the Admin role.")

    def _assigned_to_me(self):
        """This request as sudo, only for the Admin it is assigned to and only while it is with them."""
        self._require_admin()
        request = self._own()
        if request.admin_user_id != self.env.user or request.state not in _ADMIN_STATES:
            raise AccessError("This request is not assigned to you.")
        return request

    def _admin_row(self):
        self.ensure_one()
        request = self.sudo()
        proposal = request.proposal_ids[:1]
        return {
            "id": request.id, "name": request.name, "description": request.description or "", "request_type": request.request_type,
            "type_label": dict(self._fields["request_type"].selection)[request.request_type], "state": request.state,
            "status": dict(self._fields["state"].selection)[request.state], "requested_by": request.requested_by_id.name,
            "scope": request.scope, "scope_label": dict(self._fields["scope"].selection)[request.scope], "branches": request.warehouse_ids.sudo().mapped("display_name"),
            "date_start": fields.Date.to_string(request.date_start) if request.date_start else "", "date_end": fields.Date.to_string(request.date_end) if request.date_end else "",
            "time_start": _to_clock(request.time_start) if request.has_interval else "", "time_end": _to_clock(request.time_end) if request.has_interval else "",
            "instructions": request.instructions or "", "created_at": fields.Datetime.to_string(request.create_date),
            "can_accept": request.state == "assigned", "can_edit_proposal": request.state in _EDITABLE_STATES,
            "origin": request.origin, "department": request.department_id.name or False,
            "can_withdraw_own": request.origin == "admin" and request.state in _EDITABLE_STATES,
            "proposal": proposal._row() if proposal else False,
            "history": request.history_ids._rows(),
        }

    @api.model
    def get_my_work_requests(self):
        """The signed-in Admin's own requests, and the choices a proposal offers. No business figures."""
        self._require_admin()
        requests = self.sudo().search([("admin_user_id", "=", self.env.user.id), ("company_id", "=", self.env.company.id), ("state", "in", _ADMIN_STATES)])
        definition = self.env["strategic.kpi.definition"]._fields
        return {
            "requests": [request._admin_row() for request in requests],
            "sources": [{"id": key, "name": label} for key, label in definition["source_type"].selection],
            "calculations": [{"id": key, "name": label, "source": _SOURCE_CALCULATIONS.get(key, "manual"), "window": key in TIME_WINDOW_CALCULATIONS}
                             for key, label in definition["calculation_type"].selection if _proposable(key)],
            "directions": [{"id": key, "name": label} for key, label in definition["direction"].selection],
            "frequencies": [{"id": key, "name": label} for key, label in REPORTING_FREQUENCIES],
            "perspectives": [{"id": key, "name": label} for key, label in self.env["strategic.objective"]._fields["perspective"].selection],
            "departments": [{"id": d.id, "code": d.code, "name": d.name} for d in self.env["strategic.department"].sudo().search([("code", "in", list(_DEPARTMENT_SOURCES))], order="sequence, id")],
            "branches": [{"id": b.id, "name": b.display_name} for b in self.env["stock.warehouse"]._strategic_branches(self.env.company).sorted("name")],
        }

    @api.model
    def admin_start_proposal(self, values):
        """The Admin proposes a KPI or OKR without a Head Office request. It follows the same path from here: the Admin
        writes the proposal, Head Office reviews it, and only an approval creates anything."""
        self._require_admin()
        name = (values.get("name") or "").strip()
        description = (values.get("description") or "").strip()
        if not name or not description:
            raise ValidationError("Give the proposal a title and explain what it should measure.")
        request_type = values.get("request_type")
        if request_type not in ("kpi", "okr"):
            raise ValidationError("Choose KPI or OKR.")
        department = self.env["strategic.department"].sudo().browse(int(values.get("department_id") or 0)).exists()
        if not department or department.code not in _DEPARTMENT_SOURCES:
            raise ValidationError("Choose the department this is for: Sales, Inventory or Purchasing.")
        scope = values.get("scope") or "all"
        if scope not in ("all", "selected"):
            raise ValidationError("Choose all branches or selected branches.")
        branches = self.env["stock.warehouse"]
        if scope == "selected":
            wanted = {int(value) for value in values.get("warehouse_ids") or []}
            branches = self.env["stock.warehouse"]._strategic_branches(self.env.company).filtered(lambda branch: branch.id in wanted)
            if not wanted or len(branches) != len(wanted):
                raise ValidationError("Choose at least one branch of this company.")
        try:
            date_start, date_end = fields.Date.to_date(values.get("date_start") or False), fields.Date.to_date(values.get("date_end") or False)
        except ValueError:
            raise ValidationError("Enter valid dates.")
        now = fields.Datetime.now()
        request = self.sudo().create({
            "name": name, "description": description, "request_type": request_type, "origin": "admin", "department_id": department.id,
            "scope": scope, "warehouse_ids": [(6, 0, branches.ids)], "date_start": date_start, "date_end": date_end,
            "requested_by_id": self.env.user.id, "admin_user_id": self.env.user.id, "company_id": self.env.company.id,
            "state": "in_progress", "submitted_at": now, "assigned_at": now, "started_at": now})
        request._log("proposed")
        return request._admin_row()

    def action_withdraw_own_proposal(self):
        """The Admin drops their own proposal before Head Office has decided it."""
        request = self._assigned_to_me()
        if request.origin != "admin" or request.state not in _EDITABLE_STATES:
            raise UserError("Only your own proposal, before Head Office reviews it, can be withdrawn here.")
        request.write({"state": "withdrawn", "withdrawn_by_id": self.env.user.id, "withdrawn_at": fields.Datetime.now()})
        request._log("withdrawn")
        request.sudo().activity_ids.action_feedback(feedback="Withdrawn by the Admin.")
        return True

    def action_accept_start(self):
        """From here the requirement is locked: Head Office can no longer edit, reassign or withdraw it."""
        request = self._assigned_to_me()
        if request.state != "assigned":
            raise UserError("Work on this request has already started.")
        request.write({"state": "in_progress", "started_at": fields.Datetime.now()})
        request._log("accepted")
        request._notify("KPI/OKR request accepted", "%s started work." % self.env.user.name, recipients=False)
        return request._admin_row()

    def save_proposal(self, values, submit=False):
        """Save the Admin's one proposal for this request; submitting locks it for Head Office's review."""
        request = self._assigned_to_me()
        if request.state not in _EDITABLE_STATES:
            raise UserError("Accept the request first." if request.state == "assigned" else "This proposal can no longer be edited.")
        Proposal = self.env["strategic.kpi.work.proposal"].sudo()
        proposal_type = values.get("proposal_type")
        if proposal_type not in ("kpi", "okr"):
            raise ValidationError("Choose whether this is a KPI or an OKR proposal.")
        if request.request_type != "unsure" and proposal_type != request.request_type:
            raise ValidationError("Head Office requested the type %s, so the proposal must match it." % dict(self._fields["request_type"].selection)[request.request_type])
        vals = Proposal._vals_from(values, proposal_type)
        proposal = request.proposal_ids[:1]
        if proposal:
            proposal.write(vals)
        else:
            proposal = Proposal.create(dict(vals, request_id=request.id, author_id=self.env.user.id))
        if submit:
            proposal._check_complete()
            proposal._check_department()
            proposal.write({"state": "submitted", "submitted_at": fields.Datetime.now(), "revision": proposal.revision + 1})
            request.write({"state": "pending_review"})
            request._log("submitted", snapshot=proposal._row())
            request._notify("KPI/OKR proposal to review", "%s submitted a proposal." % self.env.user.name)
        return request._admin_row()

    def action_delete_draft(self):
        self._require_head_office()
        request = self._own()
        if request.state != "draft":
            raise UserError("Only a draft can be deleted; a submitted request is kept for its history.")
        request.unlink()
        return True


def _number(value, label):
    """A typed number, or False when left empty (0 is a real value)."""
    if value in (None, "", False):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError("%s must be a number." % label) from None
    if not math.isfinite(number):
        raise ValidationError("%s must be a number." % label)
    return number


class StrategicKpiWorkProposal(models.Model):
    """What an Admin proposes. Deliberately not a KPI or Objective record: those are live as soon as they exist."""
    _name = "strategic.kpi.work.proposal"
    _description = "KPI/OKR Proposal"
    _order = "id desc"

    request_id = fields.Many2one("strategic.kpi.work.request", required=True, ondelete="cascade", index=True, readonly=True)
    author_id = fields.Many2one("res.users", "Prepared By", required=True, readonly=True)
    proposal_type = fields.Selection([("kpi", "KPI"), ("okr", "OKR")], "Proposal Type", required=True)
    state = fields.Selection([("draft", "Draft"), ("submitted", "Submitted"), ("changes_requested", "Changes Requested"), ("approved", "Approved"), ("rejected", "Rejected")],
                             default="draft", required=True, readonly=True)
    submitted_at = fields.Datetime(readonly=True)
    revision = fields.Integer("Submissions", default=0, readonly=True)
    activated_kpi_id = fields.Many2one("strategic.kpi.definition", "Activated KPI", readonly=True, copy=False, ondelete="restrict")
    activated_objective_id = fields.Many2one("strategic.objective", "Activated Objective", readonly=True, copy=False, ondelete="restrict")
    # KPI: the same choices the KPI system offers, so an approved proposal maps straight onto a definition.
    kpi_name = fields.Char("KPI Name")
    kpi_description = fields.Text("Description")
    source_type = fields.Selection(selection=lambda self: self.env["strategic.kpi.definition"]._fields["source_type"].selection, string="Data Source")
    calculation_type = fields.Selection(selection=lambda self: self.env["strategic.kpi.definition"]._fields["calculation_type"].selection, string="Calculation")
    measurement_method = fields.Text("Measurement Method / Formula")
    unit = fields.Char()
    direction = fields.Selection(selection=lambda self: self.env["strategic.kpi.definition"]._fields["direction"].selection)
    frequency = fields.Selection(REPORTING_FREQUENCIES, "Reporting Frequency")
    has_time_window = fields.Boolean()
    time_window_start = fields.Float("Window From")
    time_window_end = fields.Float("Window To")
    level = fields.Selection([("organization", "Organization"), ("branch", "Each Branch")], "Measured At")
    has_target = fields.Boolean()
    target_value = fields.Float()
    # OKR
    objective_title = fields.Char("Objective")
    objective_description = fields.Text("Objective Description")
    perspective = fields.Selection(selection=lambda self: self.env["strategic.objective"]._fields["perspective"].selection)
    date_start = fields.Date("Measurement Start")
    date_end = fields.Date("Measurement End")
    key_result_ids = fields.One2many("strategic.kpi.work.proposal.key.result", "proposal_id", "Key Results")

    _one_per_request = models.Constraint("UNIQUE(request_id)", "A request has one proposal.")

    @api.model
    def _vals_from(self, values, proposal_type):
        """Only the fields of the chosen type; a type switched while drafting leaves nothing behind."""
        text = lambda key: (values.get(key) or "").strip() or False
        choice = lambda key: self._choice(key, values.get(key))
        blank_kpi = {"kpi_name": False, "kpi_description": False, "source_type": False, "calculation_type": False, "measurement_method": False, "unit": False,
                     "direction": False, "frequency": False, "level": False, "has_target": False, "target_value": 0.0,
                     "has_time_window": False, "time_window_start": 0.0, "time_window_end": 0.0}
        blank_okr = {"objective_title": False, "objective_description": False, "perspective": False, "key_result_ids": [(5,)]}
        try:
            start, end = fields.Date.to_date(values.get("date_start") or False), fields.Date.to_date(values.get("date_end") or False)
        except (TypeError, ValueError):
            raise ValidationError("Enter a valid measurement period.") from None
        if proposal_type == "kpi":
            target = _number(values.get("target_value"), "The target")
            window_start, window_end = _to_hours(values.get("time_window_start")), _to_hours(values.get("time_window_end"))
            if (window_start is False) != (window_end is False):
                raise ValidationError("Enter both the start and the end of the time window, or neither.")
            return dict(blank_okr, proposal_type="kpi", date_start=start or False, date_end=end or False,
                        has_time_window=window_start is not False, time_window_start=window_start or 0.0, time_window_end=window_end or 0.0, kpi_name=text("kpi_name"), kpi_description=text("kpi_description"), source_type=choice("source_type"), calculation_type=choice("calculation_type"),
                        measurement_method=text("measurement_method"), unit=text("unit"), direction=choice("direction"), frequency=choice("frequency"), level=choice("level"),
                        has_target=target is not False, target_value=target or 0.0)
        lines = []
        for index, row in enumerate(values.get("key_results") or [], 1):
            baseline, target = _number(row.get("baseline"), "Key Result %s baseline" % index), _number(row.get("target"), "Key Result %s target" % index)
            lines.append((0, 0, {"sequence": index, "source_type": self._choice("source_type", row.get("source_type")),
                                 "calculation_type": self._choice("calculation_type", row.get("calculation_type")), "name": (row.get("name") or "").strip() or False, "measurement_method": (row.get("measurement_method") or "").strip() or False, "unit": (row.get("unit") or "").strip() or False,
                                 "has_baseline": baseline is not False, "baseline_value": baseline or 0.0, "has_target": target is not False, "target_value": target or 0.0}))
        return dict(blank_kpi, proposal_type="okr", objective_title=text("objective_title"), objective_description=text("objective_description"), perspective=choice("perspective"),
                    date_start=start or False, date_end=end or False, key_result_ids=[(5,)] + lines)

    @api.model
    def _choice(self, key, value):
        if not value:
            return False
        if value not in dict(self._fields[key]._description_selection(self.env)):
            raise ValidationError("Choose a valid %s." % self._fields[key].string.lower())
        return value

    def _check_complete(self):
        self.ensure_one()
        if self.proposal_type == "kpi":
            required = (("kpi_name", "the KPI name"), ("kpi_description", "the description"), ("source_type", "the data source"), ("calculation_type", "the calculation"),
                        ("measurement_method", "the measurement method"), ("unit", "the unit"), ("direction", "whether higher or lower is better"), ("frequency", "the reporting frequency"), ("level", "where it is measured"))
            missing = [label for key, label in required if not self[key]]
            if missing:
                raise ValidationError("Before submitting, enter %s." % ", ".join(missing))
            self._check_measurable(self.source_type, self.calculation_type, self.unit)
            if self.direction == "range":
                raise ValidationError("Before submitting, choose higher or lower is better: a proposal has one target, not a range.")
            if not self.has_target or self.target_value <= 0:
                raise ValidationError("Before submitting, enter a target above zero: it applies to each reporting period, and a result cannot be scored without one.")
            self._check_period_complete()
            if self.has_time_window:
                if self.calculation_type not in TIME_WINDOW_CALCULATIONS:
                    raise ValidationError("A time window applies only to POS net sales and POS average order value.")
                if self.time_window_start >= self.time_window_end:
                    raise ValidationError("The time window must start before it ends, within one day.")
            elif self.request_id.has_interval:
                raise ValidationError("Head Office asked for a time interval, so set the time window.")
            if self.request_id.scope == "selected" and self.level != "branch":
                raise ValidationError("Head Office selected branches, so this must be measured at each branch.")
            return
        if not self.objective_title or not self.objective_description or not self.perspective:
            raise ValidationError("Before submitting, enter the objective title, description and perspective.")
        self._check_period_complete()
        if not self.key_result_ids:
            raise ValidationError("Before submitting, add at least one Key Result.")
        for line in self.key_result_ids:
            if not line.name or not line.measurement_method or not line.unit:
                raise ValidationError("Before submitting, give every Key Result a name, a measurement method and a unit.")
            if not line.has_target or line.target_value <= 0:
                raise ValidationError("Before submitting, give every Key Result a target above zero.")
            self._check_measurable(line.source_type or "manual", line.calculation_type or "manual_value", line.unit)

    def _check_period_complete(self):
        if not self.date_start or not self.date_end or self.date_start > self.date_end:
            raise ValidationError("Before submitting, enter a measurement period that starts before it ends.")

    def _check_measurable(self, source, calculation, unit):
        """The rules the KPI itself would enforce, checked now rather than failing at approval."""
        if not _proposable(calculation):
            raise ValidationError("This calculation cannot be proposed here.")
        if _SOURCE_CALCULATIONS.get(calculation, "manual") != source and not (calculation == "manual_value" and source == "other"):
            raise ValidationError("This calculation does not use the chosen data source.")
        _validate_calculation_contract(self.env(su=True), source, calculation, False, unit, self.request_id.company_id.id)

    # ------------------------------------------------------------------ activation
    def _create_kpi(self, code, name, description, source, calculation, unit, direction, **extra):
        return self.env["strategic.kpi.definition"].sudo().create(dict(
            {"name": name, "code": code, "company_id": self.request_id.company_id.id, "description": description, "source_type": source, "calculation_type": calculation, "unit": unit, "direction": direction},
            **extra))

    def _activate(self):
        """Only reached from an approved review, once. The KPI code and the flushed request row stop a second run."""
        self.ensure_one()
        if self.activated_kpi_id or self.activated_objective_id:
            raise UserError("This proposal has already been activated.")
        return self._activate_kpi() if self.proposal_type == "kpi" else self._activate_okr()

    def _activate_kpi(self):
        request = self.request_id
        kpi = self._create_kpi("WR-%s" % request.id, self.kpi_name, "%s\n\nHow it is measured: %s" % (self.kpi_description, self.measurement_method), self.source_type, self.calculation_type,
                               self.unit, self.direction, reporting_frequency=self.frequency, has_time_window=self.has_time_window, time_window_start=self.time_window_start, time_window_end=self.time_window_end)
        values = {"kpi_id": kpi.id, "date_start": self.date_start, "date_end": self.date_end, "target_value": self.target_value}
        if self.level == "organization":
            schedules = [dict(values, scope="organization", warehouse_id=False)]
        else:
            branches = request.warehouse_ids if request.scope == "selected" else self.env["stock.warehouse"]._strategic_branches(request.company_id)
            schedules = [dict(values, scope="branch", warehouse_id=branch.id) for branch in branches]
        self.env["strategic.kpi.reporting.schedule"].sudo().create(schedules)
        self.write({"activated_kpi_id": kpi.id})
        return kpi

    def _check_department(self):
        """An Admin proposal's KPIs read the data of the department it is for (manual values are allowed)."""
        self.ensure_one()
        department = self.request_id.department_id
        if self.request_id.origin != "admin" or not department:
            return
        allowed = _DEPARTMENT_SOURCES.get(department.code, ())
        sources = [self.source_type] if self.proposal_type == "kpi" else self.key_result_ids.mapped("source_type")
        wrong = [source for source in sources if source and source not in ("manual", "other") and source not in allowed]
        if wrong:
            raise ValidationError("This proposal is for %s, so its KPIs must use %s data." % (department.name, " or ".join(
                dict(self.env["strategic.kpi.definition"]._fields["source_type"].selection)[source] for source in allowed)))

    def _activate_okr(self):
        """A real objective with its Key Results, through the same steps goal set-up uses. Key Result KPIs are per Key Result."""
        request = self.request_id
        user_env = self.env(su=False)
        plan = user_env["strategic.goal.setup"]._resolve_plan(self.date_end)
        # An Admin proposal is owned by the Head Office manager who approves it, and lands in its department's area.
        owner = request.requested_by_id if request.origin != "admin" else self.env.user
        values = {"name": self.objective_title, "description": self.objective_description, "plan_id": plan.id, "perspective": self.perspective,
                  "scope": "organization", "deadline": self.date_end, "owner_id": owner.id}
        if request.department_id.code in _DEPARTMENT_AREA:
            values["business_area"] = _DEPARTMENT_AREA[request.department_id.code]
        objective = user_env["strategic.objective"].create(values)
        okr = user_env["strategic.okr.objective"].create({"name": objective.name, "strategic_objective_id": objective.id, "owner_id": objective.owner_id.id, "date_start": self.date_start, "date_end": self.date_end})
        for index, line in enumerate(self.key_result_ids, 1):
            direction = "lower" if line.has_baseline and line.target_value < line.baseline_value else "higher"
            kpi = self._create_kpi("WR-%s-KR%s" % (request.id, index), line.name, "How it is measured: %s" % line.measurement_method, line.source_type or "manual", line.calculation_type or "manual_value", line.unit, direction)
            target_values = {"kpi_id": kpi.id, "date_start": self.date_start, "date_end": self.date_end, "scope": "organization", "target_basis": "absolute", "target_value": line.target_value}
            if line.has_baseline:
                baseline = user_env["strategic.kpi.baseline"].create({
                    "kpi_id": kpi.id, "scope": "organization", "baseline_value": line.baseline_value, "source_period_start": self.date_start - relativedelta(days=1), "source_period_end": self.date_start - relativedelta(days=1),
                    "effective_from": self.date_start, "source_note": "Stated in the approved proposal of request %s." % request.name, "change_reason": "Approved KPI/OKR proposal."})
                baseline.action_approve()
                target_values.update(target_basis="baseline_relative", baseline_id=baseline.id)
            target = user_env["strategic.kpi.target"].create(target_values)
            target.action_approve()
            user_env["strategic.key.result"].create({"name": line.name, "okr_objective_id": okr.id, "required": True, "primary_kpi_id": kpi.id, "target_id": target.id, "sequence": index})
        objective.action_activate()
        self.write({"activated_objective_id": objective.id})
        return objective

    def _row(self):
        self.ensure_one()
        return {
            "id": self.id, "proposal_type": self.proposal_type, "state": self.state, "submitted_at": fields.Datetime.to_string(self.submitted_at) if self.submitted_at else False,
            "kpi_name": self.kpi_name or "", "kpi_description": self.kpi_description or "", "source_type": self.source_type or "", "calculation_type": self.calculation_type or "",
            "measurement_method": self.measurement_method or "", "unit": self.unit or "", "direction": self.direction or "", "frequency": self.frequency or "", "level": self.level or "",
            "target_value": self.target_value if self.has_target else "", "revision": self.revision,
            "time_window_start": _to_clock(self.time_window_start) if self.has_time_window else "", "time_window_end": _to_clock(self.time_window_end) if self.has_time_window else "",
            "labels": {"source": dict(self._fields["source_type"]._description_selection(self.env)).get(self.source_type, ""), "calculation": dict(self._fields["calculation_type"]._description_selection(self.env)).get(self.calculation_type, ""),
                       "direction": dict(self._fields["direction"]._description_selection(self.env)).get(self.direction, ""), "frequency": dict(REPORTING_FREQUENCIES).get(self.frequency, ""),
                       "level": dict(self._fields["level"].selection).get(self.level, ""), "perspective": dict(self._fields["perspective"]._description_selection(self.env)).get(self.perspective, "")},
            "objective_title": self.objective_title or "", "objective_description": self.objective_description or "", "perspective": self.perspective or "",
            "date_start": fields.Date.to_string(self.date_start) if self.date_start else "", "date_end": fields.Date.to_string(self.date_end) if self.date_end else "",
            "key_results": [{"source_type": line.source_type or "", "calculation_type": line.calculation_type or "", "name": line.name or "", "measurement_method": line.measurement_method or "", "unit": line.unit or "", "baseline": line.baseline_value if line.has_baseline else "",
                             "target": line.target_value if line.has_target else ""} for line in self.key_result_ids],
        }


class StrategicKpiWorkProposalKeyResult(models.Model):
    _name = "strategic.kpi.work.proposal.key.result"
    _description = "KPI/OKR Proposal Key Result"
    _order = "sequence, id"

    proposal_id = fields.Many2one("strategic.kpi.work.proposal", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(default=10)
    name = fields.Char("Key Result")
    measurement_method = fields.Text()
    # Empty means reported by hand; otherwise the KPI system calculates it.
    source_type = fields.Selection(selection=lambda self: self.env["strategic.kpi.definition"]._fields["source_type"].selection, string="Data Source")
    calculation_type = fields.Selection(selection=lambda self: self.env["strategic.kpi.definition"]._fields["calculation_type"].selection, string="Calculation")
    unit = fields.Char()
    has_baseline = fields.Boolean()
    baseline_value = fields.Float("Baseline")
    has_target = fields.Boolean()
    target_value = fields.Float("Target")


class StrategicKpiWorkHistory(models.Model):
    """What happened to a request, kept for good: assignments, each submission as it was sent, and every review comment."""
    _name = "strategic.kpi.work.history"
    _description = "KPI/OKR Request History"
    _order = "id"

    request_id = fields.Many2one("strategic.kpi.work.request", required=True, ondelete="cascade", index=True, readonly=True)
    action = fields.Selection([("proposed", "Proposed by Admin"), ("assigned", "Assigned"), ("recovered", "Reassigned"), ("accepted", "Accepted"), ("submitted", "Proposal submitted"), ("changes_requested", "Changes requested"),
                               ("approved", "Approved"), ("rejected", "Rejected"), ("withdrawn", "Withdrawn")], required=True, readonly=True)
    comment = fields.Text(readonly=True)
    user_id = fields.Many2one("res.users", "By", required=True, readonly=True)
    admin_user_id = fields.Many2one("res.users", "Assigned Admin", readonly=True)
    revision = fields.Integer(readonly=True)
    snapshot = fields.Text("Proposal as Submitted", readonly=True)

    def _rows(self):
        labels = dict(self._fields["action"].selection)
        return [{"id": row.id, "action": row.action, "label": labels[row.action], "comment": row.comment or "", "by": row.user_id.name, "admin": row.admin_user_id.name or "", "revision": row.revision,
                 "at": fields.Datetime.to_string(row.create_date), "snapshot": json.loads(row.snapshot) if row.snapshot else False} for row in self.sudo()]
