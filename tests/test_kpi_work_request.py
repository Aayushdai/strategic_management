"""KPI/OKR work requests: Head Office only, assigned to Admins, separate from objective proposals."""

from datetime import timedelta

from odoo import fields
from odoo.addons.strategic_management.models.kpi_scheduler import reporting_periods
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged

ADMIN = "strategic_management.group_strategic_admin"
HEAD = "strategic_management.group_strategic_head_office_manager"
MANAGER = "strategic_management.group_strategic_branch_manager"
EMPLOYEE = "strategic_management.group_strategic_employee"


@tagged("post_install", "-at_install")
class TestKpiWorkRequest(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ref = cls.env.ref
        cls.company = cls.env.company
        cls.branch = cls.env["stock.warehouse"].create({"name": "WR Branch", "code": "WRB1", "company_id": cls.company.id})
        cls.other_company = cls.env["res.company"].create({"name": "WR Other"})
        cls.foreign_branch = cls.env["stock.warehouse"].with_company(cls.other_company).create({"name": "WR Foreign", "code": "WRF1", "company_id": cls.other_company.id})

        def user(login, group, branch=False):
            account = cls.env["res.users"].with_context(no_reset_password=True).create({
                "name": login, "login": login, "company_id": cls.company.id, "company_ids": [(6, 0, cls.company.ids)],
                "group_ids": [(6, 0, [ref("base.group_user").id, ref(group).id])], "strategic_branch_warehouse_id": branch.id if branch else False})
            cls.env["hr.employee"].create({"name": login, "user_id": account.id, "company_id": cls.company.id})
            return account

        cls.head = user("wr_head", HEAD)
        cls.head2 = user("wr_head2", HEAD)
        cls.manager = user("wr_manager", MANAGER, cls.branch)
        cls.emp = user("wr_emp", EMPLOYEE, cls.branch)
        cls.admin = user("wr_admin", ADMIN)
        cls.admin2 = user("wr_admin2", ADMIN)
        cls.Request = cls.env["strategic.kpi.work.request"]
        cls.KPI = dict(cls.KPI, unit=cls.company.currency_id.name)
        cls.values = {"name": "Evening Sales Performance", "description": "Compare revenue by branch between 5 PM and 9 PM.", "request_type": "kpi", "scope": "all", "admin_user_id": cls.admin.id,
                      "date_start": "2026-11-01", "date_end": "2026-11-30", "time_start": "17:00", "time_end": "21:00", "instructions": "Weekdays only."}

    def _save(self, submit=True, by=None, **changes):
        row = self.Request.with_user(by or self.head).save_work_request(dict(self.values, **changes), submit)
        return row, self.Request.browse(row["id"])

    def test_the_example_request_is_created_and_assigned(self):
        row, request = self._save()
        self.assertEqual((row["state"], row["status"]), ("assigned", "Assigned / Pending Admin"))
        self.assertEqual((row["requested_by"], row["admin"], row["type_label"], row["scope_label"]), (self.head.name, self.admin.name, "KPI", "All Branches"))
        self.assertEqual((row["time_start"], row["time_end"], row["date_start"]), ("17:00", "21:00", "2026-11-01"))
        self.assertEqual(row["instructions"], "Weekdays only.")
        self.assertTrue(row["created_at"])
        self.assertTrue(request.submitted_at and request.assigned_at)

    def test_status_path(self):
        row, request = self._save(submit=False, admin_user_id=False, description="")
        self.assertEqual(row["state"], "draft", "a draft may be incomplete")
        self.assertFalse(request.submitted_at)
        with self.assertRaises(ValidationError):
            self._save(id=request.id, description="")
        row, _request = self._save(id=request.id, admin_user_id=False)
        self.assertEqual(row["state"], "submitted", "sent without an Admin yet")
        row = request.with_user(self.head).action_assign_admin(self.admin.id)
        self.assertEqual((row["state"], row["admin"]), ("assigned", self.admin.name))
        row = request.with_user(self.head).action_assign_admin(self.admin2.id)
        self.assertEqual(row["admin"], self.admin2.name, "reassigned")
        with self.assertRaises(UserError):
            request.with_user(self.head).action_delete_draft()

    def test_head_office_edits_a_sent_request_and_it_stays_sent(self):
        row, request = self._save()
        assigned_at, submitted_at = request.assigned_at, request.submitted_at
        row = self._save(id=request.id, name="Evening Sales v2", description="New wording.", scope="selected", warehouse_ids=[self.branch.id], time_start="18:00", request_type="okr")[0]
        self.assertEqual((row["name"], row["description"], row["scope"], row["time_start"], row["request_type"], row["state"]), ("Evening Sales v2", "New wording.", "selected", "18:00", "okr", "assigned"))
        self.assertEqual((request.submitted_at, request.assigned_at), (submitted_at, assigned_at), "same Admin: the original dates stand")
        row = self._save(submit=False, id=request.id, name="Still sent", description="New wording.")[0]
        self.assertEqual((row["name"], row["state"]), ("Still sent", "assigned"), "an edit cannot turn it back into a draft")
        with self.assertRaises(ValidationError, msg="a sent request keeps its requirement"):
            self._save(submit=False, id=request.id, description=" ")
        self.assertEqual(request.description, "New wording.", "the failed edit changed nothing")
        row = self._save(id=request.id, admin_user_id=self.admin2.id)[0]
        self.assertEqual((row["admin"], row["state"]), (self.admin2.name, "assigned"), "reassigned from the form")
        self.assertGreaterEqual(request.assigned_at, assigned_at)
        row = self._save(id=request.id, admin_user_id=False)[0]
        self.assertEqual((row["state"], row["admin"]), ("submitted", False), "taking the Admin off goes back to Submitted")
        self.assertFalse(request.assigned_at)
        row = self._save(id=request.id, name="Submitted edit", admin_user_id=False)[0]
        self.assertEqual((row["name"], row["state"]), ("Submitted edit", "submitted"))

    def test_withdraw_from_submitted_and_assigned_and_it_is_final(self):
        for admin in (self.admin.id, False):
            _row, request = self._save(admin_user_id=admin)
            row = request.with_user(self.head).action_withdraw()
            self.assertEqual((row["state"], row["status"], row["withdrawn_by"]), ("withdrawn", "Withdrawn", self.head.name))
            self.assertEqual(row["withdrawn_at"], fields.Datetime.to_string(request.withdrawn_at))
            self.assertTrue(row["withdrawn_at"])
            self.assertEqual((row["can_edit"], row["can_assign"], row["can_withdraw"], row["can_delete"]), (False, False, False, False))
            self.assertTrue(request.exists(), "kept for its history")
            self.assertIn(request.id, [item["id"] for item in self.Request.with_user(self.head).get_work_requests()["requests"]])
            for call in (lambda: self._save(id=request.id, name="Back"), lambda: self._save(submit=False, id=request.id),
                         lambda: request.with_user(self.head).action_assign_admin(self.admin.id), lambda: request.with_user(self.head).action_withdraw(),
                         lambda: request.with_user(self.head).action_delete_draft()):
                with self.assertRaises(UserError):
                    call()
            self.assertEqual(request.state, "withdrawn")

    def test_a_draft_is_deleted_not_withdrawn(self):
        _row, request = self._save(submit=False)
        with self.assertRaises(UserError):
            request.with_user(self.head).action_withdraw()
        self.assertEqual(request.state, "draft")

    def test_another_head_office_manager_can_also_manage_it(self):
        _row, request = self._save()
        self.assertEqual(self._save(by=self.head2, id=request.id, name="Taken over")[0]["requested_by"], self.head.name, "who asked does not change")
        self.assertEqual(request.with_user(self.head2).action_withdraw()["withdrawn_by"], self.head2.name)

    def test_draft_can_be_edited_and_deleted_and_not_assigned(self):
        row, request = self._save(submit=False)
        self.assertEqual(self._save(submit=False, id=request.id, name="Renamed")[0]["name"], "Renamed")
        with self.assertRaises(UserError):
            request.with_user(self.head).action_assign_admin(self.admin.id)
        request.with_user(self.head).action_delete_draft()
        self.assertFalse(request.exists())

    def test_only_admins_can_be_assigned(self):
        for person in (self.head, self.manager, self.emp):
            with self.assertRaises(ValidationError, msg=person.login):
                self._save(admin_user_id=person.id)
        _row, request = self._save(admin_user_id=False)
        for person in (self.manager, self.emp):
            with self.assertRaises(ValidationError, msg=person.login):
                request.with_user(self.head).action_assign_admin(person.id)
        with self.assertRaises(ValidationError):
            request.with_user(self.head).action_assign_admin(False)
        with self.assertRaisesRegex(ValidationError, "does not have the Admin role"):
            request.sudo().admin_user_id = self.manager
        self.assertEqual({row["id"] for row in self.Request.with_user(self.head).get_work_requests()["admins"]} & {self.head.id, self.manager.id, self.emp.id}, set())

    def test_scope_and_period_are_checked(self):
        row, _request = self._save(scope="selected", warehouse_ids=[self.branch.id])
        self.assertEqual(row["branches"], [self.branch.display_name])
        self.assertEqual(self._save(scope="all", warehouse_ids=[self.branch.id])[0]["branches"], [], "All Branches ignores a stale selection")
        with self.assertRaises(ValidationError):
            self._save(scope="selected", warehouse_ids=[])
        with self.assertRaises(ValidationError, msg="another company's branch"):
            self._save(scope="selected", warehouse_ids=[self.foreign_branch.id])
        for change in ({"scope": "nowhere"}, {"request_type": "x"}, {"name": " "}, {"date_end": False}, {"date_start": "2026-12-01"}, {"date_start": "nope"},
                       {"time_end": ""}, {"time_end": "16:00"}, {"time_start": "25:00"}, {"time_start": "soon"}):
            with self.assertRaises(ValidationError, msg=str(change)):
                self._save(**change)
        self.assertFalse(self._save(date_start=False, date_end=False, time_start="", time_end="")[0]["time_start"], "period and interval are optional")
        self.assertEqual(self._save(time_start="00:00", time_end="09:30")[0]["time_start"], "00:00", "midnight is a real start time")

    def test_nobody_else_reaches_them(self):
        _row, request = self._save()
        for actor in (self.manager, self.emp):
            As = self.Request.with_user(actor)
            for call in (As.get_work_requests, lambda: As.save_work_request(self.values), lambda: request.with_user(actor).action_assign_admin(self.admin.id),
                         lambda: request.with_user(actor).action_delete_draft(), lambda: request.with_user(actor).action_withdraw(), lambda: As.save_work_request(dict(self.values, id=request.id, name="Forged")), lambda: As.search_count([]), lambda: request.with_user(actor).read(["name"]),
                         lambda: As.create({"name": "x", "requested_by_id": actor.id, "company_id": self.company.id})):
                with self.assertRaises(AccessError, msg=actor.login):
                    call()

    def test_head_office_cannot_bypass_the_workflow_directly(self):
        _row, request = self._save(submit=False)
        As = request.with_user(self.head)
        self.assertTrue(As.read(["name"]), "reads are allowed")
        for call in (lambda: As.write({"state": "assigned"}), lambda: As.unlink(), lambda: self.Request.with_user(self.head).create({"name": "x", "requested_by_id": self.head.id, "company_id": self.company.id})):
            with self.assertRaises(AccessError):
                call()

    def test_forged_values_are_ignored(self):
        row, request = self._save(requested_by_id=self.emp.id, state="assigned", company_id=self.other_company.id, submit=False)
        self.assertEqual((request.requested_by_id, request.state, request.company_id), (self.head, "draft", self.company))

    def test_another_head_office_manager_sees_the_list_and_other_company_is_refused(self):
        _row, request = self._save()
        self.assertIn(request.id, [row["id"] for row in self.Request.with_user(self.head2).get_work_requests()["requests"]])
        foreign = self.Request.sudo().create({"name": "Foreign", "requested_by_id": self.head.id, "company_id": self.other_company.id})
        self.assertNotIn(foreign.id, [row["id"] for row in self.Request.with_user(self.head).get_work_requests()["requests"]])
        with self.assertRaises(AccessError):
            foreign.with_user(self.head).action_delete_draft()

    def test_kept_apart_from_objectives_and_admin_stays_without_data(self):
        objectives = self.env["strategic.objective"].sudo().search_count([])
        kpis = self.env["strategic.kpi.definition"].sudo().search_count([])
        _row, _request = self._save()
        self.assertEqual((self.env["strategic.objective"].sudo().search_count([]), self.env["strategic.kpi.definition"].sudo().search_count([])), (objectives, kpis))
        data = self.env["strategic.performance.dashboard"].with_user(self.head).get_objective_requests()
        self.assertNotIn("Evening Sales Performance", str(data))
        with self.assertRaises(AccessError):
            self.env["strategic.performance.dashboard"].with_user(self.admin).get_business_overview_data()

    def test_admin_who_loses_the_role_is_flagged_not_hidden(self):
        row, request = self._save()
        self.assertFalse(row["admin_lost_role"])
        self.env["strategic.performance.dashboard"].with_user(self.head).set_user_role(self.admin.id, "employee", self.branch.id)
        row = next(item for item in self.Request.with_user(self.head).get_work_requests()["requests"] if item["id"] == request.id)
        self.assertTrue(row["admin_lost_role"])

    # ------------------------------------------------------------------ Phase 2: the assigned Admin
    KPI = {"proposal_type": "kpi", "kpi_name": "Evening Revenue", "kpi_description": "Revenue 5 PM to 9 PM.", "source_type": "pos", "calculation_type": "pos_net_sales",
           "measurement_method": "Net POS sales of orders paid between 17:00 and 21:00.", "unit": "USD", "direction": "higher", "frequency": "weekly", "level": "branch", "target_value": "100",
           "date_start": "2026-11-01", "date_end": "2026-11-30", "time_window_start": "17:00", "time_window_end": "21:00"}
    OKR = {"proposal_type": "okr", "objective_title": "Grow evenings", "objective_description": "More evening trade.", "perspective": "financial", "date_start": "2031-01-01", "date_end": "2031-03-31",
           "key_results": [{"name": "Evening revenue up", "measurement_method": "Net sales 17:00-21:00.", "unit": "%", "baseline": "0", "target": "10"}, {"name": "More orders", "measurement_method": "Order count.", "unit": "orders", "baseline": "", "target": "200"}]}

    def _assigned(self, **changes):
        return self._save(**changes)[1]

    def _started(self, **changes):
        request = self._assigned(**changes)
        request.with_user(self.admin).action_accept_start()
        return request

    def test_admin_sees_only_their_own_requests(self):
        mine, theirs = self._assigned(), self._assigned(admin_user_id=self.admin2.id)
        draft = self._save(submit=False)[1]
        withdrawn = self._assigned()
        withdrawn.with_user(self.head).action_withdraw()
        data = self.Request.with_user(self.admin).get_my_work_requests()
        self.assertEqual([row["id"] for row in data["requests"]], [mine.id])
        row = data["requests"][0]
        self.assertEqual((row["name"], row["description"], row["type_label"], row["scope_label"], row["time_start"], row["time_end"], row["date_start"], row["status"]),
                         (mine.name, mine.description, "KPI", "All Branches", "17:00", "21:00", "2026-11-01", "Assigned / Pending Admin"))
        self.assertTrue(row["created_at"])
        As = self.Request.with_user(self.admin)
        self.assertEqual(As.search([]).ids, [mine.id], "direct search agrees")
        for other in (theirs, draft, withdrawn):
            for call in (lambda: other.with_user(self.admin).read(["name"]), lambda: other.with_user(self.admin).action_accept_start(), lambda: other.with_user(self.admin).save_proposal(self.KPI)):
                with self.assertRaises(AccessError):
                    call()
        for actor in (self.head, self.manager, self.emp):
            with self.assertRaises(AccessError, msg=actor.login):
                self.Request.with_user(actor).get_my_work_requests()
            with self.assertRaises(AccessError, msg=actor.login):
                mine.with_user(actor).action_accept_start()
            with self.assertRaises(AccessError, msg=actor.login):
                mine.with_user(actor).save_proposal(self.KPI)

    def test_reassigned_request_leaves_the_first_admin(self):
        request = self._assigned()
        request.with_user(self.head).action_assign_admin(self.admin2.id)
        self.assertEqual(self.Request.with_user(self.admin).get_my_work_requests()["requests"], [])
        with self.assertRaises(AccessError):
            request.with_user(self.admin).action_accept_start()
        self.assertEqual(self.Request.with_user(self.admin2).browse(request.id).action_accept_start()["state"], "in_progress")

    def test_accept_locks_the_requirement_for_head_office(self):
        request = self._assigned()
        with self.assertRaises(UserError, msg="not before accepting"):
            request.with_user(self.admin).save_proposal(self.KPI)
        row = request.with_user(self.admin).action_accept_start()
        self.assertEqual((row["state"], row["status"]), ("in_progress", "In Progress"))
        self.assertTrue(request.started_at)
        with self.assertRaises(UserError):
            request.with_user(self.admin).action_accept_start()
        before = (request.name, request.description, request.admin_user_id)
        Head = request.with_user(self.head)
        for call in (lambda: self._save(id=request.id, name="Changed"), lambda: self._save(submit=False, id=request.id, name="Changed"), lambda: Head.action_assign_admin(self.admin2.id), Head.action_withdraw, Head.action_delete_draft):
            with self.assertRaises(UserError):
                call()
        self.assertEqual((request.name, request.description, request.admin_user_id, request.state), (*before, "in_progress"))
        listed = next(item for item in self.Request.with_user(self.head).get_work_requests()["requests"] if item["id"] == request.id)
        self.assertEqual((listed["status"], listed["can_edit"], listed["can_assign"], listed["can_withdraw"], listed["can_delete"]), ("In Progress", False, False, False, False))

    def test_kpi_proposal_draft_edit_submit_and_lock(self):
        request = self._started()
        row = request.with_user(self.admin).save_proposal({"proposal_type": "kpi", "kpi_name": "Partial"})
        self.assertEqual((row["state"], row["proposal"]["state"], row["proposal"]["kpi_name"]), ("in_progress", "draft", "Partial"), "a draft may be incomplete")
        with self.assertRaisesRegex(ValidationError, "Before submitting"):
            request.with_user(self.admin).save_proposal({"proposal_type": "kpi", "kpi_name": "Partial"}, True)
        self.assertEqual(request.state, "in_progress")
        row = request.with_user(self.admin).save_proposal(self.KPI)
        self.assertEqual((row["proposal"]["kpi_name"], row["proposal"]["target_value"]), ("Evening Revenue", 100.0), "edited")
        self.assertEqual(len(request.proposal_ids), 1, "edits the one proposal")
        row = request.with_user(self.admin).save_proposal(self.KPI, True)
        self.assertEqual((row["state"], row["status"], row["proposal"]["state"], row["can_edit_proposal"]), ("pending_review", "Pending Head Office Review", "submitted", False))
        self.assertTrue(request.proposal_ids.submitted_at)
        for submit in (False, True):
            with self.assertRaises(UserError):
                request.with_user(self.admin).save_proposal(dict(self.KPI, kpi_name="Changed"), submit)
        self.assertEqual(request.proposal_ids.kpi_name, "Evening Revenue")
        with self.assertRaises(AccessError, msg="no direct write"):
            request.proposal_ids.with_user(self.admin).write({"kpi_name": "Changed"})

    def test_kpi_source_and_calculation_must_agree(self):
        request = self._started()
        with self.assertRaisesRegex(ValidationError, "does not use the chosen data source"):
            request.with_user(self.admin).save_proposal(dict(self.KPI, source_type="inventory"), True)
        for bad in ({"source_type": "nope"}, {"frequency": "hourly"}, {"target_value": "lots"}, {"target_value": "inf"}, {"proposal_type": "report"}):
            with self.assertRaises(ValidationError, msg=str(bad)):
                request.with_user(self.admin).save_proposal(dict(self.KPI, **bad))

    def test_okr_proposal_with_key_results(self):
        request = self._started(request_type="okr")
        for broken in ({"key_results": []}, {"objective_title": ""}, {"date_end": "2026-01-01"}, {"key_results": [{"name": "No method"}]}):
            with self.assertRaisesRegex(ValidationError, "Before submitting", msg=str(broken)):
                request.with_user(self.admin).save_proposal(dict(self.OKR, **broken), True)
        request.with_user(self.admin).save_proposal(dict(self.OKR, key_results=self.OKR["key_results"][:1]))
        row = request.with_user(self.admin).save_proposal(self.OKR, True)
        lines = row["proposal"]["key_results"]
        self.assertEqual([line["name"] for line in lines], ["Evening revenue up", "More orders"], "replaced, not added to")
        self.assertEqual((lines[0]["baseline"], lines[0]["target"], lines[1]["baseline"], lines[1]["target"]), (0.0, 10.0, "", 200.0))
        self.assertEqual(row["state"], "pending_review")

    def test_type_is_held_to_the_request_unless_not_sure(self):
        kpi = self._started()
        with self.assertRaisesRegex(ValidationError, "requested the type KPI"):
            kpi.with_user(self.admin).save_proposal(self.OKR)
        okr = self._started(request_type="okr")
        with self.assertRaisesRegex(ValidationError, "requested the type OKR"):
            okr.with_user(self.admin).save_proposal(self.KPI)
        unsure = self._started(request_type="unsure")
        unsure.with_user(self.admin).save_proposal(self.KPI)
        row = unsure.with_user(self.admin).save_proposal(self.OKR)
        self.assertEqual((row["proposal"]["proposal_type"], row["proposal"]["kpi_name"]), ("okr", ""), "switching type leaves nothing behind")

    def test_a_proposal_creates_nothing_active(self):
        models = ("strategic.kpi.definition", "strategic.kpi.definition.version", "strategic.kpi.target", "strategic.kpi.evaluation", "strategic.objective", "strategic.key.result")
        before = [self.env[model].sudo().with_context(active_test=False).search_count([]) for model in models]
        request = self._started()
        request.with_user(self.admin).save_proposal(self.KPI, True)
        okr = self._started(request_type="okr")
        okr.with_user(self.admin).save_proposal(self.OKR, True)
        self.assertEqual([self.env[model].sudo().with_context(active_test=False).search_count([]) for model in models], before)

    def test_admin_still_reaches_no_business_data(self):
        request = self._started()
        request.with_user(self.admin).save_proposal(self.KPI, True)
        Dashboard = self.env["strategic.performance.dashboard"].with_user(self.admin)
        for call in (Dashboard.get_business_overview_data, Dashboard.get_purchasing_overview_data, Dashboard.get_inventory_waste_data, Dashboard.get_head_office_data, Dashboard.get_branch_data, lambda: Dashboard.get_department_operations("sales")):
            with self.assertRaises(AccessError):
                call()
        for model in ("pos.order", "sale.order", "stock.quant", "stock.picking", "purchase.order", "account.move", "stock.warehouse", "strategic.kpi.evaluation", "strategic.kpi.definition", "strategic.objective", "strategic.key.result"):
            try:
                count = self.env[model].with_user(self.admin).search_count([])
            except AccessError:
                continue
            self.assertEqual(count, 0, model)
        data = self.Request.with_user(self.admin).get_my_work_requests()
        self.assertFalse({"net_sales", "revenue", "figures", "results"} & set(data))

    def test_proposals_are_private_to_the_assigned_admin(self):
        request = self._started()
        request.with_user(self.admin).save_proposal(self.OKR | {"proposal_type": "kpi", **self.KPI})
        proposal = request.proposal_ids
        self.assertEqual(self.env["strategic.kpi.work.proposal"].with_user(self.admin2).search_count([("id", "=", proposal.id)]), 0)
        self.assertEqual(self.env["strategic.kpi.work.proposal.key.result"].with_user(self.admin2).search_count([]), 0)
        for actor in (self.admin2, self.manager, self.emp):
            with self.assertRaises(AccessError, msg=actor.login):
                proposal.with_user(actor).read(["kpi_name"])
        with self.assertRaises(AccessError):
            self.env["strategic.kpi.work.proposal"].with_user(self.admin).create({"request_id": request.id, "author_id": self.admin.id, "proposal_type": "kpi"})
        self.assertEqual(proposal.with_user(self.head).kpi_name, "Evening Revenue", "Head Office can read it for the next phase")

    # ------------------------------------------------------------------ Phase 3: review
    def _submitted(self, **changes):
        request = self._started(**changes)
        request.with_user(self.admin).save_proposal(self.KPI, True)
        return request

    def test_changes_are_requested_read_edited_and_resubmitted(self):
        request = self._submitted()
        Head, Admin = request.with_user(self.head), request.with_user(self.admin)
        with self.assertRaises(UserError, msg="locked while waiting"):
            Admin.save_proposal(dict(self.KPI, kpi_name="Sneaky"))
        with self.assertRaises(ValidationError, msg="a comment is required"):
            Head.action_review("changes", " ")
        row = Head.action_review("changes", "Exclude staff meals.")
        self.assertEqual((row["state"], row["status"], row["can_review"]), ("changes_requested", "Changes Requested", False))
        admin_row = self.Request.with_user(self.admin).get_my_work_requests()["requests"][0]
        self.assertTrue(admin_row["can_edit_proposal"])
        self.assertIn("Exclude staff meals.", [item["comment"] for item in admin_row["history"]])
        Admin.save_proposal(dict(self.KPI, measurement_method="Without staff meals."))
        self.assertEqual(Head.env["strategic.kpi.work.request"].browse(request.id).state, "changes_requested", "a saved edit is not a resubmission")
        Admin.save_proposal(dict(self.KPI, measurement_method="Without staff meals."), True)
        self.assertEqual((request.state, request.proposal_ids.revision), ("pending_review", 2))
        row = Head.action_review("approve")
        actions = [item["action"] for item in row["history"]]
        self.assertEqual(actions, ["assigned", "accepted", "submitted", "changes_requested", "submitted", "approved"])
        snapshots = [item["snapshot"]["measurement_method"] for item in row["history"] if item["action"] == "submitted"]
        self.assertEqual(snapshots, [self.KPI["measurement_method"], "Without staff meals."], "each submission is kept as sent")

    def test_reject_needs_a_reason_and_creates_nothing(self):
        request = self._submitted()
        before = self.env["strategic.kpi.definition"].sudo().search_count([])
        with self.assertRaises(ValidationError):
            request.with_user(self.head).action_review("reject")
        row = request.with_user(self.head).action_review("reject", "Not needed this year.")
        self.assertEqual((row["state"], row["activated"]), ("rejected", False))
        self.assertEqual(self.env["strategic.kpi.definition"].sudo().search_count([]), before)
        with self.assertRaises(UserError):
            request.with_user(self.admin).save_proposal(self.KPI)
        with self.assertRaises(UserError):
            request.with_user(self.head).action_review("approve")

    def test_only_head_office_reviews_and_only_when_waiting(self):
        request = self._started()
        with self.assertRaises(UserError):
            request.with_user(self.head).action_review("approve")
        request.with_user(self.admin).save_proposal(self.KPI, True)
        for actor in (self.admin, self.admin2, self.manager, self.emp):
            with self.assertRaises(AccessError, msg=actor.login):
                request.with_user(actor).action_review("approve")
        with self.assertRaises(ValidationError):
            request.with_user(self.head).action_review("publish")
        self.assertEqual(request.state, "pending_review")

    def test_approving_a_kpi_activates_it_once_with_everything_transferred(self):
        request = self._submitted(scope="selected", warehouse_ids=[self.branch.id])
        row = request.with_user(self.head).action_review("approve")
        proposal = request.proposal_ids
        kpi = proposal.activated_kpi_id
        self.assertEqual(row["activated"]["kpi"]["code"], "WR-%s" % request.id)
        self.assertEqual((kpi.name, kpi.source_type, kpi.calculation_type, kpi.unit, kpi.reporting_frequency), ("Evening Revenue", "pos", "pos_net_sales", self.company.currency_id.name, "weekly"))
        self.assertEqual((kpi.has_time_window, kpi.time_window_start, kpi.time_window_end), (True, 17.0, 21.0))
        version = kpi._get_definition_version_for_period("2026-11-01", "2026-11-30")
        self.assertEqual((version.has_time_window, version.time_window_start, version.time_window_end, version.state), (True, 17.0, 21.0, "approved"))
        self.assertIn(self.KPI["measurement_method"], kpi.description)
        schedule = kpi.reporting_schedule_ids
        self.assertEqual((schedule.scope, schedule.warehouse_id, schedule.target_value), ("branch", self.branch, 100.0), "the selected branch only")
        with self.assertRaises(UserError):
            request.with_user(self.head).action_review("approve")
        self.assertEqual(self.env["strategic.kpi.definition"].sudo().search_count([("code", "=", "WR-%s" % request.id)]), 1)
        with self.assertRaises(UserError):
            proposal._activate()

    def test_all_branches_gets_one_schedule_per_branch_and_organization_gets_one(self):
        request = self._submitted()
        request.with_user(self.head).action_review("approve")
        # "All Branches" means the branches: the company's main (central) warehouse is not one unless someone works there.
        branches = self.env["stock.warehouse"]._strategic_branches(self.company)
        self.assertEqual(set(request.proposal_ids.activated_kpi_id.reporting_schedule_ids.warehouse_id.ids), set(branches.ids))
        warehouses = self.env["stock.warehouse"].search([("company_id", "=", self.company.id)])
        if len(warehouses) > 1 and not self.env["res.users"].with_context(active_test=False).search_count([("strategic_branch_warehouse_id", "=", warehouses[:1].id)]):
            self.assertNotIn(warehouses[:1], request.proposal_ids.activated_kpi_id.reporting_schedule_ids.warehouse_id, "no schedule for the central warehouse")
        other = self._started()
        other.with_user(self.admin).save_proposal(dict(self.KPI, level="organization"), True)
        other.with_user(self.head).action_review("approve")
        schedule = other.proposal_ids.activated_kpi_id.reporting_schedule_ids
        self.assertEqual((len(schedule), schedule.scope, schedule.warehouse_id.id), (1, "organization", False))

    def test_proposal_is_checked_against_what_the_engine_can_run(self):
        request = self._started()
        Admin = request.with_user(self.admin)
        for bad, message in (({"target_value": "0"}, "target above zero"), ({"target_value": ""}, "target above zero"), ({"direction": "range"}, "not a range"), ({"unit": "cups"}, "currency"),
                             ({"time_window_start": "", "time_window_end": ""}, "time interval"), ({"time_window_start": "21:00", "time_window_end": "17:00"}, "start before"),
                             ({"date_end": False}, "measurement period"), ({"calculation_type": "pos_product_net_sales"}, "cannot be proposed"),
                             ({"source_type": "accounting", "calculation_type": "accounting_net_profit"}, "applies only to POS")):
            with self.assertRaisesRegex(ValidationError, message, msg=str(bad)):
                Admin.save_proposal(dict(self.KPI, **bad), True)
        selected = self._started(scope="selected", warehouse_ids=[self.branch.id])
        with self.assertRaisesRegex(ValidationError, "each branch"):
            selected.with_user(self.admin).save_proposal(dict(self.KPI, level="organization"), True)
        names = {row["id"] for row in self.Request.with_user(self.admin).get_my_work_requests()["calculations"]}
        self.assertIn("pos_net_sales", names)
        self.assertFalse(names & {"pos_product_net_sales", "pos_sold_quantity", "purchase_unit_price_change_pct"}, "those need a product an Admin cannot pick")

    def test_approving_an_okr_creates_the_objective_and_key_results(self):
        plan = self.env["strategic.plan"].create({"name": "WR Plan", "company_id": self.company.id, "date_start": "2030-01-01", "date_end": "2032-12-31"})
        request = self._started(request_type="okr")
        request.with_user(self.admin).save_proposal(self.OKR, True)
        self.assertFalse(request.proposal_ids.activated_objective_id, "nothing before approval")
        row = request.with_user(self.head).action_review("approve")
        objective = request.proposal_ids.activated_objective_id
        self.assertEqual((objective.state, objective.plan_id, objective.owner_id, objective.perspective, str(objective.deadline)), ("active", plan, self.head, "financial", "2031-03-31"))
        results = objective.okr_objective_ids.key_result_ids
        self.assertEqual(results.mapped("name"), ["Evening revenue up", "More orders"])
        first, second = results
        self.assertEqual((first.target_id.target_value, first.target_id.state, first.target_id.target_basis, first.target_id.baseline_id.baseline_value, first.target_id.baseline_id.state), (10.0, "approved", "baseline_relative", 0.0, "approved"))
        self.assertEqual((second.target_id.target_value, second.target_id.target_basis, second.primary_kpi_id.direction), (200.0, "absolute", "higher"))
        self.assertIn("Order count.", second.primary_kpi_id.description)
        self.assertEqual(row["activated"]["objective"]["key_results"], ["Evening revenue up", "More orders"])
        self.assertEqual(self.env["strategic.kpi.definition"].sudo().search_count([("code", "=like", "WR-%s-KR%%" % request.id)]), 2)

    def test_okr_approval_without_a_plan_changes_nothing(self):
        request = self._started(request_type="okr")
        request.with_user(self.admin).save_proposal(dict(self.OKR, date_start="2090-01-01", date_end="2090-02-01"), True)
        with self.assertRaises(UserError), self.env.cr.savepoint():
            request.with_user(self.head).action_review("approve")
        self.assertEqual(request.state, "pending_review")
        self.assertFalse(request.proposal_ids.activated_objective_id)

    # ------------------------------------------------------------------ recovering abandoned requests
    def test_lost_role_can_be_recovered_with_a_reason_keeping_the_draft(self):
        request = self._started()
        request.with_user(self.admin).save_proposal({"proposal_type": "kpi", "kpi_name": "Half done"})
        with self.assertRaises(UserError, msg="not while the Admin still has the role"):
            request.with_user(self.head).action_recover_abandoned(self.admin2.id, "Why")
        self.env["strategic.performance.dashboard"].with_user(self.head).set_user_role(self.admin.id, "employee", self.branch.id)
        row = next(item for item in self.Request.with_user(self.head).get_work_requests()["requests"] if item["id"] == request.id)
        self.assertEqual((row["admin_lost_role"], row["can_recover"], row["can_edit"]), (True, True, False), "the requirement stays locked")
        with self.assertRaises(ValidationError):
            request.with_user(self.head).action_recover_abandoned(self.admin2.id, " ")
        with self.assertRaises(ValidationError):
            request.with_user(self.head).action_recover_abandoned(self.manager.id, "Why")
        for actor in (self.admin2, self.manager):
            with self.assertRaises(AccessError):
                request.with_user(actor).action_recover_abandoned(self.admin2.id, "Why")
        row = request.with_user(self.head).action_recover_abandoned(self.admin2.id, "Left the role.")
        self.assertEqual((row["admin"], row["state"]), (self.admin2.name, "in_progress"))
        recovered = [item for item in row["history"] if item["action"] == "recovered"][0]
        self.assertIn("Left the role.", recovered["comment"])
        self.assertIn(self.admin.name, recovered["comment"], "who had it is on record")
        mine = self.Request.with_user(self.admin2).get_my_work_requests()["requests"][0]
        self.assertEqual(mine["proposal"]["kpi_name"], "Half done", "the draft is not discarded")
        request.with_user(self.admin2).save_proposal(self.KPI, True)
        self.assertEqual(request.state, "pending_review")

    # ------------------------------------------------------------------ notifications
    def _activities(self, request, user):
        return request.sudo().activity_ids.filtered(lambda activity: activity.user_id == user)

    def test_each_step_tells_the_right_person(self):
        request = self._assigned()
        self.assertTrue(self._activities(request, self.admin), "assignment reaches the Admin")
        request.with_user(self.admin).action_accept_start()
        self.assertFalse(request.sudo().activity_ids, "accepted: nothing outstanding")
        request.with_user(self.admin).save_proposal(self.KPI, True)
        self.assertTrue(self._activities(request, self.head), "submission reaches Head Office")
        self.assertFalse(self._activities(request, self.admin))
        request.with_user(self.head).action_review("changes", "Fix it.")
        self.assertTrue(self._activities(request, self.admin), "a change request reaches the Admin")
        self.assertFalse(self._activities(request, self.head))
        request.with_user(self.admin).save_proposal(self.KPI, True)
        request.with_user(self.head).action_review("approve")
        self.assertTrue(self._activities(request, self.admin), "the outcome reaches the Admin")
        self.assertIn("approved", self._activities(request, self.admin).summary.lower())
        rejected = self._submitted()
        rejected.with_user(self.head).action_review("reject", "No.")
        self.assertIn("rejected", self._activities(rejected, self.admin).summary.lower())
        reassigned = self._assigned()
        reassigned.with_user(self.head).action_assign_admin(self.admin2.id)
        self.assertTrue(self._activities(reassigned, self.admin2))
        self.assertFalse(self._activities(reassigned, self.admin), "the first Admin's to-do is closed")


@tagged("post_install", "-at_install")
class TestKpiTimeWindowAndFrequency(TransactionCase):
    """The KPI engine itself: real POS records, the time window in the company's time zone, one result per period."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().tz = "Asia/Kathmandu"  # UTC+5:45
        cls.branch = cls.env["stock.warehouse"].create({"name": "TW Branch 1", "code": "TWB1", "company_id": cls.company.id})
        cls.branch2 = cls.env["stock.warehouse"].create({"name": "TW Branch 2", "code": "TWB2", "company_id": cls.company.id})
        cls.pos = {branch: cls.env["pos.config"].create({"name": "TW %s" % branch.code, "picking_type_id": branch.pos_type_id.id}) for branch in (cls.branch, cls.branch2)}
        cls.product = cls.env["product.product"].create({"name": "TW Latte", "available_in_pos": True, "list_price": 10.0})
        cls.currency = cls.company.currency_id.name

    @classmethod
    def _order(cls, branch, utc, amount, state="paid", refund=False):
        config = cls.pos[branch]
        session = cls.env["pos.session"].search([("config_id", "=", config.id), ("state", "!=", "closed")], limit=1) or cls.env["pos.session"].create({"config_id": config.id, "user_id": cls.env.uid})
        return cls.env["pos.order"].create({"session_id": session.id, "company_id": cls.company.id, "state": state, "date_order": utc, "amount_tax": round(amount * 0.13, 2), "amount_total": amount + round(amount * 0.13, 2),
                                            "amount_paid": amount + round(amount * 0.13, 2), "amount_return": 0.0, "is_refund": refund,
                                            "lines": [(0, 0, {"product_id": cls.product.id, "qty": -1 if amount < 0 else 1, "price_unit": abs(amount), "price_subtotal": amount, "price_subtotal_incl": amount * 1.13, "full_product_name": "TW Latte"})]})

    def _kpi(self, code="TW-KPI", calculation="pos_net_sales", window=(17.0, 21.0), frequency="daily", unit=None):
        return self.env["strategic.kpi.definition"].create({"name": code, "code": code, "company_id": self.company.id, "unit": unit or self.currency, "direction": "higher", "source_type": "pos", "calculation_type": calculation,
                                                            "reporting_frequency": frequency, "has_time_window": bool(window), "time_window_start": window[0] if window else 0.0, "time_window_end": window[1] if window else 0.0})

    def _evaluate(self, kpi, start, end, branch=None, value=100.0):
        from odoo.addons.strategic_management.services.kpi_sources import collect_pos_result
        target = self.env["strategic.kpi.target"].create({"kpi_id": kpi.id, "date_start": start, "date_end": end, "scope": "branch" if branch else "organization", "warehouse_id": branch.id if branch else False, "target_value": value, "state": "approved"})
        version = kpi._get_definition_version_for_period(start, end)
        return self.env["strategic.kpi.evaluation"].sudo()._create_from_source_adapter(target, version, collect_pos_result(target, version))

    def test_window_boundaries_use_the_company_time_zone(self):
        day = "2001-02-01"
        # Kathmandu is UTC+5:45: 17:00 local is 11:15 UTC, 21:00 local is 15:15 UTC.
        self._order(self.branch, "%s 11:14:59" % day, 1.0)   # 16:59:59 out
        self._order(self.branch, "%s 11:15:00" % day, 10.0)  # 17:00:00 in
        self._order(self.branch, "%s 15:14:59" % day, 100.0) # 20:59:59 in
        self._order(self.branch, "%s 15:15:00" % day, 1000.0)# 21:00:00 out
        self._order(self.branch, "%s 18:00:00" % day, 5000.0)# 23:45 same local day, out
        evaluation = self._evaluate(self._kpi(), day, day, self.branch)
        self.assertEqual((evaluation.status, evaluation.actual_value), ("valid", 110.0))
        self.assertIn("17:00 to 21:00", evaluation.reason or evaluation.source_note or "")

    def test_the_utc_date_is_not_the_business_date(self):
        # 2001-02-02 20:00 UTC is 01:45 on the 3rd in Kathmandu; 2001-02-03 12:00 UTC is 17:45 on the 3rd.
        self._order(self.branch, "2001-02-03 12:00:00", 7.0)
        kpi = self._kpi(window=(0.0, 6.0))
        self._order(self.branch, "2001-02-02 20:00:00", 3.0)
        self.assertEqual(self._evaluate(kpi, "2001-02-03", "2001-02-03", self.branch).actual_value, 3.0)
        self.assertEqual(self._evaluate(kpi, "2001-02-02", "2001-02-02", self.branch).status, "not_evaluated", "nothing local on the 2nd")

    def test_another_time_zone_moves_the_window(self):
        self.company.partner_id.sudo().tz = "America/New_York"  # UTC-5 in February
        self._order(self.branch, "2001-02-05 22:30:00", 20.0)  # 17:30 New York: in
        self._order(self.branch, "2001-02-05 11:30:00", 40.0)  # 17:15 Kathmandu but 06:30 New York: out
        self.assertEqual(self._evaluate(self._kpi(), "2001-02-05", "2001-02-05", self.branch).actual_value, 20.0)

    def test_the_window_is_configurable_not_the_example(self):
        self._order(self.branch, "2001-02-06 03:00:00", 9.0)   # 08:45 local
        self._order(self.branch, "2001-02-06 12:00:00", 50.0)  # 17:45 local
        kpi = self._kpi("TW-MORNING", window=(8.0, 9.0))
        self.assertEqual(self._evaluate(kpi, "2001-02-06", "2001-02-06", self.branch).actual_value, 9.0)
        whole_day = self._kpi("TW-ALLDAY", window=None)
        evaluation = self._evaluate(whole_day, "2001-02-06", "2001-02-06", self.branch)
        self.assertEqual(evaluation.status, "valid", "no window: the existing calculation is untouched")
        self.assertAlmostEqual(evaluation.actual_value, 59.0, 2)

    def test_scope_refunds_drafts_and_average(self):
        self._order(self.branch, "2001-02-07 12:00:00", 30.0)
        self._order(self.branch, "2001-02-07 12:30:00", 50.0)
        self._order(self.branch, "2001-02-07 13:00:00", -5.0, refund=True)
        self._order(self.branch, "2001-02-07 13:30:00", 999.0, state="draft")
        self._order(self.branch2, "2001-02-07 12:00:00", 200.0)
        self._order(self.branch2, "2001-02-07 03:00:00", 777.0)  # morning, out of the window
        kpi = self._kpi()
        self.assertEqual(self._evaluate(kpi, "2001-02-07", "2001-02-07", self.branch).actual_value, 75.0, "refund subtracted, draft ignored")
        self.assertEqual(self._evaluate(kpi, "2001-02-07", "2001-02-07", self.branch2).actual_value, 200.0, "each branch its own")
        self.assertEqual(self._evaluate(kpi, "2001-02-07", "2001-02-07").actual_value, 275.0, "all branches together")
        average = self._kpi("TW-AVG", "pos_average_order_value")
        self.assertEqual(self._evaluate(average, "2001-02-07", "2001-02-07", self.branch).actual_value, 37.5, "75 over 2 customer orders; a refund is not an order")

    def test_no_sales_in_the_window_is_not_zero(self):
        self._order(self.branch, "2001-02-08 03:00:00", 30.0)
        evaluation = self._evaluate(self._kpi(), "2001-02-08", "2001-02-08", self.branch)
        self.assertEqual((evaluation.status, evaluation.has_actual_value), ("not_evaluated", False))

    def test_window_only_for_supported_calculations_and_within_a_day(self):
        with self.assertRaises(ValidationError):
            self._kpi("TW-BAD", "pos_sales_growth_pct", unit="%")
        with self.assertRaises(ValidationError):
            self._kpi("TW-BAD2", window=(21.0, 17.0))
        kpi = self._kpi("TW-LOCK")
        with self.assertRaises(ValidationError, msg="part of the versioned contract"):
            kpi.write({"time_window_start": 1.0})

    def test_reporting_periods_follow_the_calendar_and_clip(self):
        d = fields.Date.to_date
        self.assertEqual(len(reporting_periods(d("2026-11-01"), d("2026-11-30"), "daily")), 30)
        self.assertEqual(reporting_periods(d("2026-11-04"), d("2026-11-18"), "weekly"), [(d("2026-11-04"), d("2026-11-08")), (d("2026-11-09"), d("2026-11-15")), (d("2026-11-16"), d("2026-11-18"))])
        self.assertEqual(reporting_periods(d("2026-01-15"), d("2026-03-10"), "monthly"), [(d("2026-01-15"), d("2026-01-31")), (d("2026-02-01"), d("2026-02-28")), (d("2026-03-01"), d("2026-03-10"))])
        self.assertEqual([p[1] for p in reporting_periods(d("2026-02-01"), d("2026-08-01"), "quarterly")], [d("2026-03-31"), d("2026-06-30"), d("2026-08-01")])
        self.assertEqual([p[1] for p in reporting_periods(d("2025-06-01"), d("2026-02-01"), "yearly")], [d("2025-12-31"), d("2026-02-01")])

    def _schedule(self, kpi, start, end, branch=None):
        return self.env["strategic.kpi.reporting.schedule"].create({"kpi_id": kpi.id, "date_start": start, "date_end": end, "target_value": 50.0, "scope": "branch" if branch else "organization", "warehouse_id": branch.id if branch else False})

    def test_the_daily_job_produces_one_result_per_period_of_the_frequency(self):
        self._order(self.branch, "2001-03-05 12:00:00", 40.0)   # Monday 5th, in the window
        self._order(self.branch, "2001-03-06 12:00:00", 60.0)   # Tuesday 6th
        self._order(self.branch, "2001-03-12 12:00:00", 70.0)   # next Monday
        daily, weekly = self._kpi("TW-DAILY", frequency="daily"), self._kpi("TW-WEEKLY", frequency="weekly")
        daily_schedule, weekly_schedule = self._schedule(daily, "2001-03-05", "2001-03-07", self.branch), self._schedule(weekly, "2001-03-05", "2001-03-18", self.branch)
        Target = self.env["strategic.kpi.target"].sudo()
        Target._cron_process_scheduled_kpis()
        self.assertEqual(sorted(daily_schedule.target_ids.mapped("date_start")), [fields.Date.to_date("2001-03-05"), fields.Date.to_date("2001-03-06"), fields.Date.to_date("2001-03-07")])
        self.assertEqual(len(weekly_schedule.target_ids), 2)
        evaluations = self.env["strategic.kpi.evaluation"].sudo().search([("kpi_id", "=", daily.id)], order="date_start")
        self.assertEqual([(str(row.date_start), row.status, row.actual_value) for row in evaluations], [("2001-03-05", "valid", 40.0), ("2001-03-06", "valid", 60.0), ("2001-03-07", "not_evaluated", 0.0)])
        self.assertEqual(evaluations[0].attainment, 80.0, "scored against the target for each period")
        weekly_rows = self.env["strategic.kpi.evaluation"].sudo().search([("kpi_id", "=", weekly.id)], order="date_start")
        self.assertEqual([row.actual_value for row in weekly_rows], [100.0, 70.0])
        # Idempotent: a second run adds nothing.
        Target._cron_process_scheduled_kpis()
        self.assertEqual(len(daily_schedule.target_ids), 3)
        self.assertEqual(self.env["strategic.kpi.evaluation"].sudo().search_count([("kpi_id", "=", daily.id)]), 3)

    def test_a_period_without_sales_asks_nobody_to_review_but_a_real_problem_does(self):
        self.env["res.users"].with_context(no_reset_password=True).create({"name": "tw_reviewer", "login": "tw_reviewer", "company_id": self.company.id, "company_ids": [(6, 0, self.company.ids)],
                                                                           "group_ids": [(6, 0, [self.env.ref("base.group_user").id, self.env.ref("strategic_management.group_strategic_head_office_manager").id])]})
        quiet = self._kpi("TW-QUIET")
        self._schedule(quiet, "2001-05-07", "2001-05-07", self.branch)
        no_pos = self.env["stock.warehouse"].create({"name": "TW No POS", "code": "TWNP", "company_id": self.company.id})
        broken = self._kpi("TW-BROKEN")
        self._schedule(broken, "2001-05-07", "2001-05-07", no_pos)
        self.env["strategic.kpi.target"].sudo()._cron_process_scheduled_kpis()
        activities = lambda kpi: self.env["mail.activity"].sudo().search_count([("res_model", "=", "strategic.kpi.target"), ("res_id", "in", kpi.reporting_schedule_ids.target_ids.ids)])
        self.assertEqual(self.env["strategic.kpi.evaluation"].sudo().search([("kpi_id", "=", quiet.id)]).status, "not_evaluated")
        self.assertEqual(activities(quiet), 0, "no sales is not an exception")
        self.assertEqual(self.env["strategic.kpi.evaluation"].sudo().search([("kpi_id", "=", broken.id)]).status, "data_quality_issue")
        self.assertGreater(activities(broken), 0, "a branch with no POS is")

    def test_an_open_period_is_not_measured_yet(self):
        kpi = self._kpi("TW-OPEN", frequency="daily")
        today = fields.Date.context_today(self.env["strategic.kpi.target"].with_context(tz="Asia/Kathmandu"))
        schedule = self._schedule(kpi, today - timedelta(days=1), today + timedelta(days=3), self.branch)
        schedule._generate_due_targets()
        self.assertEqual(schedule.target_ids.mapped("date_start"), [today - timedelta(days=1)], "yesterday only; today is still open")
        schedule._generate_due_targets()
        self.assertEqual(len(schedule.target_ids), 1)

    def test_admin_gets_no_result_by_any_route(self):
        admin = self.env["res.users"].with_context(no_reset_password=True).create({"name": "tw_admin", "login": "tw_admin", "company_id": self.company.id, "company_ids": [(6, 0, self.company.ids)],
                                                                                   "group_ids": [(6, 0, [self.env.ref("base.group_user").id, self.env.ref("strategic_management.group_strategic_admin").id])]})
        head = self.env["res.users"].with_context(no_reset_password=True).create({"name": "tw_head", "login": "tw_head", "company_id": self.company.id, "company_ids": [(6, 0, self.company.ids)],
                                                                                  "group_ids": [(6, 0, [self.env.ref("base.group_user").id, self.env.ref("strategic_management.group_strategic_head_office_manager").id])]})
        Request = self.env["strategic.kpi.work.request"]
        values = {"name": "Evening", "description": "Evening sales.", "request_type": "kpi", "scope": "all", "admin_user_id": admin.id}
        request = Request.browse(Request.with_user(head).save_work_request(values, True)["id"])
        request.with_user(admin).action_accept_start()
        request.with_user(admin).save_proposal({"proposal_type": "kpi", "kpi_name": "Evening", "kpi_description": "d", "source_type": "pos", "calculation_type": "pos_net_sales", "measurement_method": "m", "unit": self.currency,
                                                "direction": "higher", "frequency": "daily", "level": "organization", "target_value": "50", "date_start": "2001-04-02", "date_end": "2001-04-02", "time_window_start": "17:00", "time_window_end": "21:00"}, True)
        self._order(self.branch, "2001-04-02 12:00:00", 123.45)
        request.with_user(head).action_review("approve")
        self.env["strategic.kpi.target"].sudo()._cron_process_scheduled_kpis()
        kpi = request.proposal_ids.activated_kpi_id
        evaluation = self.env["strategic.kpi.evaluation"].sudo().search([("kpi_id", "=", kpi.id)])
        self.assertEqual(evaluation.actual_value, 123.45, "the system did calculate it")
        self.assertEqual(request.with_user(head)._row()["activated"]["kpi"]["results"][0]["actual"], 123.45, "Head Office sees it")
        admin_data = Request.with_user(admin).get_my_work_requests()
        self.assertNotIn("123.45", str(admin_data), "nothing in the Admin's payload")
        self.assertNotIn("activated", admin_data["requests"][0])
        for model in ("strategic.kpi.evaluation", "strategic.kpi.definition", "strategic.kpi.target", "strategic.kpi.reporting.schedule", "pos.order"):
            try:
                count = self.env[model].with_user(admin).search_count([])
            except AccessError:
                continue
            self.assertEqual(count, 0, model)
        with self.assertRaises(AccessError):
            evaluation.with_user(admin).read(["actual_value"])
        with self.assertRaises(AccessError):
            kpi.reporting_schedule_ids.target_ids.with_user(admin).action_evaluate_source()
        with self.assertRaises(AccessError):
            request.with_user(admin)._row() and request.with_user(admin).action_review("approve")
