"""The Admin starts a KPI or OKR proposal; Head Office approves it; it lands in the department it is for."""

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged

from . import test_kpi_work_request as base  # a module import, so the loader does not run its tests twice

ADMIN, EMPLOYEE, HEAD, MANAGER = base.ADMIN, base.EMPLOYEE, base.HEAD, base.MANAGER


@tagged("post_install", "-at_install")
class TestAdminProposals(TransactionCase):
    KPI = base.TestKpiWorkRequest.KPI
    OKR = dict(base.TestKpiWorkRequest.OKR, key_results=[dict(line, source_type="manual", calculation_type="manual_value") for line in base.TestKpiWorkRequest.OKR["key_results"]])

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ref = cls.env.ref
        cls.company = cls.env.company
        cls.branch = cls.env["stock.warehouse"].create({"name": "AP Branch", "code": "APB1", "company_id": cls.company.id})

        def user(login, group, branch=False):
            account = cls.env["res.users"].with_context(no_reset_password=True).create({
                "name": login, "login": login, "company_id": cls.company.id, "company_ids": [(6, 0, cls.company.ids)],
                "group_ids": [(6, 0, [ref("base.group_user").id, ref(group).id])], "strategic_branch_warehouse_id": branch.id if branch else False})
            cls.env["hr.employee"].create({"name": login, "user_id": account.id, "company_id": cls.company.id})
            return account

        cls.head = user("ap_head", HEAD)
        cls.manager = user("ap_manager", MANAGER, cls.branch)
        cls.emp = user("ap_emp", EMPLOYEE, cls.branch)
        cls.admin = user("ap_admin", ADMIN)
        cls.Request = cls.env["strategic.kpi.work.request"]
        cls.KPI = dict(cls.KPI, unit=cls.company.currency_id.name)
        cls.department = {code: cls.env["strategic.department"].search([("code", "=", code)], limit=1) for code in ("sales", "inventory", "purchasing")}

    def _start(self, request_type="kpi", department="sales", **changes):
        values = dict({"name": "AP Evening sales", "description": "Track evening revenue.", "request_type": request_type,
                       "department_id": self.department[department].id, "scope": "all", "date_start": "2026-11-01", "date_end": "2026-11-30"}, **changes)
        row = self.Request.with_user(self.admin).admin_start_proposal(values)
        return row, self.Request.browse(row["id"])

    def test_admin_starts_writes_and_head_office_approves_a_kpi(self):
        row, request = self._start()
        self.assertEqual((row["state"], row["origin"], row["department"], row["can_edit_proposal"]), ("in_progress", "admin", "Sales", True))
        heads = {r["id"] for r in self.Request.with_user(self.head).get_work_requests()["requests"]}
        self.assertNotIn(request.id, heads, "Head Office does not see a proposal the Admin is still writing")
        request.with_user(self.admin).save_proposal(self.KPI, True)
        listed = {r["id"]: r for r in self.Request.with_user(self.head).get_work_requests()["requests"]}
        self.assertEqual((listed[request.id]["state"], listed[request.id]["origin"]), ("pending_review", "admin"))
        self.assertTrue(request.activity_ids.filtered(lambda activity: activity.user_id == self.head), "Head Office is asked to review")
        request.with_user(self.head).action_review("approve")
        kpi = request.proposal_ids.activated_kpi_id
        self.assertTrue(kpi, "approval puts it into effect")
        self.assertEqual(kpi.source_type, "pos", "a Sales KPI reads Sales data")
        self.assertEqual(request.history_ids.mapped("action")[:2], ["proposed", "submitted"])

    def test_a_kpi_must_use_its_departments_data(self):
        row, request = self._start(department="purchasing")
        with self.assertRaisesRegex(ValidationError, "Purchasing"):
            request.with_user(self.admin).save_proposal(self.KPI, True)
        self.assertEqual(request.state, "in_progress", "nothing was submitted")

    def test_an_approved_okr_lands_in_its_departments_area_owned_by_head_office(self):
        self.env["strategic.plan"].create({"name": "AP Plan", "company_id": self.company.id, "date_start": "2030-01-01", "date_end": "2032-12-31"})
        row, request = self._start(request_type="okr", department="inventory")
        request.with_user(self.admin).save_proposal(self.OKR, True)
        request.with_user(self.head).action_review("approve")
        objective = request.proposal_ids.activated_objective_id
        self.assertEqual((objective.state, objective.business_area, objective.owner_id), ("active", "inventory_waste", self.head))

    def test_only_the_admin_starts_proposals_and_input_is_checked(self):
        for actor in (self.head, self.manager, self.emp):
            with self.assertRaises(AccessError, msg=actor.login):
                self.Request.with_user(actor).admin_start_proposal({"name": "X", "description": "Y", "request_type": "kpi", "department_id": self.department["sales"].id})
        with self.assertRaises(ValidationError):
            self._start(department_id=False)
        with self.assertRaises(ValidationError):
            self._start(request_type="unsure")
        with self.assertRaises(ValidationError):
            self._start(name="  ")
        with self.assertRaises(ValidationError):
            self._start(scope="selected", warehouse_ids=[])

    def test_admin_withdraws_their_own_proposal_before_review(self):
        row, request = self._start()
        request.with_user(self.admin).action_withdraw_own_proposal()
        self.assertEqual(request.state, "withdrawn")
        row, request = self._start()
        request.with_user(self.admin).save_proposal(self.KPI, True)
        with self.assertRaises(UserError, msg="once sent, Head Office decides"):
            request.with_user(self.admin).action_withdraw_own_proposal()
