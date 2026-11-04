"""Staffing requests, employee registration, logins and task-based performance."""

from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged

RESET = "odoo.addons.auth_signup.models.res_users.ResUsers._action_reset_password"


@tagged("post_install", "-at_install")
class TestStaffing(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        ref = cls.env.ref
        Warehouse = cls.env["stock.warehouse"]
        cls.branch1 = Warehouse.create({"name": "STF Branch 1", "code": "SFB1", "company_id": cls.company.id})
        cls.branch2 = Warehouse.create({"name": "STF Branch 2", "code": "SFB2", "company_id": cls.company.id})

        def user(login, group, branch=False):
            account = cls.env["res.users"].with_context(no_reset_password=True).create({
                "name": login, "login": login, "email": "%s@example.com" % login, "company_id": cls.company.id, "company_ids": [(6, 0, cls.company.ids)],
                "group_ids": [(6, 0, [ref("base.group_user").id, ref(group).id])], "strategic_branch_warehouse_id": branch.id if branch else False})
            cls.env["hr.employee"].sudo().create({"name": login, "user_id": account.id, "company_id": cls.company.id})
            return account

        cls.head = user("stf_head", "strategic_management.group_strategic_head_office_manager")
        cls.manager1 = user("stf_manager1", "strategic_management.group_strategic_branch_manager", cls.branch1)
        cls.manager2 = user("stf_manager2", "strategic_management.group_strategic_branch_manager", cls.branch2)
        cls.emp1 = user("stf_emp1", "strategic_management.group_strategic_employee", cls.branch1)
        cls.emp2 = user("stf_emp2", "strategic_management.group_strategic_employee", cls.branch2)
        cls.Request = cls.env["strategic.staffing.request"]
        cls.Task = cls.env["strategic.branch.task"]
        cls.Dashboard = cls.env["strategic.performance.dashboard"]
        cls.today = fields.Date.today()
        cls.department = cls.env["hr.department"].create({"name": "STF Bar", "company_id": cls.company.id})
        cls.values = {"department_id": cls.department.id, "position": "STF Barista", "quantity": 2, "reason": "New evening shift.", "expected_date": str(cls.today + timedelta(days=14)), "comments": "Weekends"}

    def _submitted(self, manager=None):
        row = self.Request.with_user(manager or self.manager1).save_staffing_request(self.values, submit=True)
        return self.Request.browse(row["id"])

    def _approved(self):
        request = self._submitted()
        request.with_user(self.head).action_decide("approve")
        return request

    def _register(self, request, email="stf.new@example.com", name="STF New"):
        row = request.with_user(self.head).register_employee({"name": name, "work_email": email, "work_phone": "555", "job": "STF Barista"})
        return self.env["hr.employee"].sudo().browse(row["new_employee_id"])

    def _login(self, employee):
        with patch(RESET) as send:
            result = self.Request.with_user(self.head).create_employee_login(employee.id)
        self.assertTrue(send.called)
        return result, employee.user_id

    # ------------------------------------------------------------------ requests
    def test_manager_request_is_tied_to_their_branch(self):
        forged = dict(self.values, warehouse_id=self.branch2.id, requested_by_id=self.emp1.id, state="approved")
        row = self.Request.with_user(self.manager1).save_staffing_request(forged)
        request = self.Request.browse(row["id"])
        self.assertEqual((request.warehouse_id, request.requested_by_id, request.state), (self.branch1, self.manager1, "draft"))
        request.with_user(self.manager1).action_submit()
        self.assertEqual(request.state, "submitted")
        self.assertTrue(request.submitted_at)

    def test_invalid_requests_are_refused(self):
        for change in ({"position": " "}, {"reason": ""}, {"quantity": 0}, {"quantity": "x"}, {"expected_date": False}, {"department_id": False}, {"department_id": 0}):
            with self.assertRaises(ValidationError, msg=str(change)):
                self.Request.with_user(self.manager1).save_staffing_request(dict(self.values, **change))

    def test_branch_isolation_of_requests(self):
        mine, theirs = self._submitted(), self._submitted(self.manager2)
        ids = [row["id"] for row in self.Request.with_user(self.manager1).get_staffing_requests({"warehouse_id": self.branch2.id})["requests"]]
        self.assertIn(mine.id, ids)
        self.assertNotIn(theirs.id, ids)
        with self.assertRaises(AccessError):
            theirs.with_user(self.manager1).read(["reason"])
        with self.assertRaises(AccessError):
            self.Request.with_user(self.manager1).save_staffing_request(dict(self.values, id=theirs.id))
        self.assertEqual(self.Request.with_user(self.manager1).search_count([("id", "=", theirs.id)]), 0)

    def test_head_office_sees_filters_and_decides(self):
        first, second = self._submitted(), self._submitted(self.manager2)
        Head = self.Request.with_user(self.head)
        ids = [row["id"] for row in Head.get_staffing_requests()["requests"]]
        self.assertLessEqual({first.id, second.id}, set(ids))
        self.assertEqual({row["id"] for row in Head.get_staffing_requests({"warehouse_id": self.branch2.id, "position": "stf bar", "state": "submitted"})["requests"]}, {second.id})
        first.with_user(self.head).action_decide("approve")
        self.assertEqual((first.state, first.decided_by_id), ("approved", self.head))
        with self.assertRaises(ValidationError):
            second.with_user(self.head).action_decide("reject")
        second.with_user(self.head).action_decide("reject", "No budget.")
        self.assertEqual((second.state, second.rejection_reason), ("rejected", "No budget."))
        with self.assertRaises(UserError):
            second.with_user(self.head).action_decide("approve")
        self.assertTrue(first.message_ids.filtered(lambda m: "Approved" in (m.body or "")), "the history is kept")

    def test_managers_and_employees_cannot_decide_or_write(self):
        request = self._submitted()
        for actor in (self.manager1, self.manager2, self.emp1):
            with self.assertRaises(AccessError, msg=actor.login):
                request.with_user(actor).action_decide("approve")
            with self.assertRaises(AccessError, msg=actor.login):
                request.with_user(actor).write({"state": "approved"})
            with self.assertRaises(AccessError, msg=actor.login):
                self.Request.with_user(actor).create(dict(self.values, warehouse_id=self.branch1.id, requested_by_id=actor.id, company_id=self.company.id))
        self.assertEqual(request.state, "submitted")
        with self.assertRaises(AccessError):
            self.Request.with_user(self.emp1).get_staffing_requests()
        with self.assertRaises(AccessError):
            self.Request.with_user(self.head).save_staffing_request(self.values)

    # ------------------------------------------------------------------ registration
    def test_registration_needs_approval_and_stops_at_the_quantity(self):
        request = self._submitted()
        with self.assertRaises(UserError):
            self._register(request)
        request.with_user(self.head).action_decide("approve")
        self.assertFalse(request.employee_ids, "approval alone creates nobody")
        first = self._register(request, "stf.one@example.com", "STF One")
        second = self._register(request, "stf.two@example.com", "STF Two")
        self.assertEqual(request.employee_ids, first | second)
        with self.assertRaises(UserError):
            self._register(request, "stf.three@example.com", "STF Three")
        self.assertEqual(request._row()["remaining"], 0)

    def test_filling_a_request_closes_its_open_to_dos(self):
        request = self._approved()
        self.assertTrue(request.activity_ids, "the manager is told it was approved")
        self._register(request, "stf.one@example.com", "STF One")
        self.assertTrue(request.activity_ids, "one place is still open")
        self._register(request, "stf.two@example.com", "STF Two")
        self.assertFalse(request.activity_ids, "filled: nothing left to do")

    def test_registered_employee_is_a_real_hr_employee_without_a_login(self):
        users = self.env["res.users"].with_context(active_test=False).search_count([])
        employee = self._register(self._approved())
        self.assertEqual(employee._name, "hr.employee")
        self.assertEqual((employee.work_email, employee.work_phone, employee.job_id.name), ("stf.new@example.com", "555", "STF Barista"))
        self.assertEqual(employee._strategic_branch(), self.branch1)
        self.assertEqual(employee.parent_id.user_id, self.manager1)
        self.assertFalse(employee.user_id)
        self.assertEqual(users, self.env["res.users"].with_context(active_test=False).search_count([]))

    def test_only_head_office_registers_and_duplicates_are_refused(self):
        request = self._approved()
        for actor in (self.manager1, self.emp1):
            with self.assertRaises(AccessError):
                request.with_user(actor).register_employee({"name": "X", "work_email": "x@example.com"})
        self._register(request)
        for email in ("stf.new@example.com", "STF.NEW@example.com", "stf_emp1@example.com", "stf_emp1", "not an email"):
            with self.assertRaises(ValidationError, msg=email):
                self._register(request, email if "@" in email or " " in email else email, "Dup")

    def test_employee_without_login_shows_only_in_their_branch(self):
        employee = self._register(self._approved())
        mine = self.Task.with_user(self.manager1).get_employee_management()
        other = self.Task.with_user(self.manager2).get_employee_management()
        self.assertIn(employee.id, [row["id"] for row in mine["without_login"]])
        self.assertNotIn(employee.id, [row["id"] for row in other["without_login"]])

    # ------------------------------------------------------------------ logins
    def test_login_has_only_the_employee_role_and_is_linked(self):
        employee = self._register(self._approved())
        result, user = self._login(employee)
        self.assertEqual((user.login, user.strategic_branch_warehouse_id, result["invitation"]), ("stf.new@example.com", self.branch1, "sent"))
        self.assertEqual(employee._strategic_branch(), self.branch1)
        self.assertTrue(user.has_group("strategic_management.group_strategic_employee"))
        for xmlid in ("strategic_management.group_strategic_branch_manager", "base.group_erp_manager", "base.group_system", "stock.group_stock_user", "hr.group_hr_user"):
            self.assertFalse(user.has_group(xmlid), xmlid)
        self.assertFalse(user.password, "no password is set by the system")
        with self.assertRaises(UserError):
            self.Request.with_user(self.head).create_employee_login(employee.id)

    def test_new_login_can_receive_tasks_only_in_its_branch(self):
        _result, user = self._login(self._register(self._approved()))
        form = self.Task.with_user(self.manager1)
        rows = {row["id"] for row in form.get_employee_management()["employees"]}
        self.assertIn(user.id, rows)
        self.assertNotIn(user.id, {row["id"] for row in self.Task.with_user(self.manager2).get_employee_management()["employees"]})
        task = form.create_branch_task({"name": "T", "description": "D", "assigned_user_id": user.id, "date_deadline": str(self.today)})
        self.assertEqual(self.Task.browse(task["id"]).warehouse_id, self.branch1)
        with self.assertRaises(AccessError):
            self.Task.with_user(self.manager2).create_branch_task({"name": "T", "description": "D", "assigned_user_id": user.id, "date_deadline": str(self.today)})
        self.assertEqual(self.Dashboard.with_user(user).get_employee_context()["branch"]["id"], self.branch1.id)

    def test_failed_invitation_keeps_a_consistent_record_and_can_be_resent(self):
        employee = self._register(self._approved())
        with patch(RESET, side_effect=UserError("No mail server")):
            result = self.Request.with_user(self.head).create_employee_login(employee.id)
        self.assertEqual((result["invitation"], result["error"]), ("failed", "No mail server"))
        self.assertTrue(employee.user_id, "the account and the link stand")
        with patch(RESET):
            result = self.Request.with_user(self.head).resend_employee_invitation(employee.id)
        self.assertEqual((result["invitation"], result["error"]), ("sent", False))
        self.assertEqual(self.env["res.users"].search_count([("login", "=", employee.work_email)]), 1)

    def test_only_head_office_creates_logins(self):
        employee = self._register(self._approved())
        for actor in (self.manager1, self.emp1):
            with self.assertRaises(AccessError):
                self.Request.with_user(actor).create_employee_login(employee.id)
            with self.assertRaises(AccessError):
                self.Request.with_user(actor).resend_employee_invitation(employee.id)

    # ------------------------------------------------------------------ performance
    def _task(self, days, priority="0", assignee=None, manager=None):
        row = self.Task.with_user(manager or self.manager1).create_branch_task({
            "name": "P", "description": "D", "assigned_user_id": (assignee or self.emp1).id, "date_deadline": str(self.today + timedelta(days=days)), "priority": priority})
        return self.Task.browse(row["id"])

    def _submit(self, task, user=None):
        task.with_user(user or self.emp1).action_start()
        task.with_user(user or self.emp1).action_submit("Done.")

    def _figures(self, actor=None, person=None, days=(-40, 40)):
        return self.Dashboard.with_user(actor or self.manager1).get_employee_performance(
            (person or self.emp1).id, str(self.today + timedelta(days=days[0])), str(self.today + timedelta(days=days[1])))["figures"]

    def test_figures_come_from_real_tasks(self):
        on_time = self._task(2, "1")
        self._submit(on_time)
        on_time.with_user(self.manager1).action_accept()
        late = self._task(-1)
        self._submit(late)
        late.with_user(self.manager1).action_accept()
        waiting = self._task(3)
        self._submit(waiting)
        overdue = self._task(-2)
        returned = self._task(5)
        self._submit(returned)
        returned.with_user(self.manager1).action_request_changes("Redo.")
        cancelled = self._task(1)
        cancelled.with_user(self.manager1).action_cancel()
        figures = self._figures()
        self.assertEqual((figures["assigned"], figures["completed"], figures["on_time"], figures["late"]), (5, 2, 1, 1))
        self.assertEqual((figures["overdue"], figures["awaiting_review"], figures["returned"], figures["cancelled"]), (1, 1, 1, 1))
        self.assertEqual(figures["on_time_rate"], 50.0)
        self.assertEqual((figures["high_priority"], figures["high_priority_completed"]), (1, 1))
        self.assertEqual(overdue.state, "assigned")

    def test_awaiting_review_is_not_completed_and_no_rate_is_not_zero(self):
        self._submit(self._task(2))
        figures = self._figures()
        self.assertEqual((figures["completed"], figures["awaiting_review"]), (0, 1))
        self.assertIs(figures["on_time_rate"], False)

    def test_a_deadline_change_moves_the_task_once(self):
        task = self._task(2)
        task.with_user(self.manager1).action_extend_deadline(str(self.today + timedelta(days=100)), "New supplier date.")
        self.assertEqual(self._figures()["assigned"], 0, "no longer due in this period")
        self.assertEqual(self._figures(days=(90, 110))["assigned"], 1)

    def test_objective_contribution_is_traced_without_business_results(self):
        plan = self.env["strategic.plan"].with_user(self.head).create({"name": "P", "company_id": self.company.id, "date_start": "2000-01-01", "date_end": "2099-12-31"})
        objective = self.env["strategic.objective"].with_user(self.head).create({
            "name": "STF Goal", "plan_id": plan.id, "perspective": "internal_process", "scope": "organization", "deadline": "2099-12-31", "owner_id": self.head.id})
        objective.with_user(self.head).action_activate()
        task = self._task(2)
        task.sudo().objective_id = objective
        self._submit(task)
        task.with_user(self.manager1).action_accept()
        self.assertEqual([(row["name"], row["tasks"], row["completed"]) for row in self._figures()["objectives"]], [("STF Goal", 1, 1)])
        self.assertFalse({"sales", "net_sales", "profit"} & set(self._figures()))

    def test_performance_visibility(self):
        self._task(2)
        self._task(2, assignee=self.emp2, manager=self.manager2)
        self.assertEqual(self._figures()["assigned"], 1)
        self.assertEqual(self._figures(self.head)["assigned"], 1)
        for actor, person in ((self.manager1, self.emp2), (self.manager2, self.emp1), (self.emp1, self.emp2), (self.emp2, self.emp1)):
            with self.assertRaises(AccessError):
                self._figures(actor, person)
        self.assertEqual(self.Dashboard.with_user(self.emp1).get_my_performance(str(self.today), str(self.today + timedelta(days=9)))["figures"]["assigned"], 1)
        overview = self.Dashboard.with_user(self.head).get_performance_overview(str(self.today), str(self.today + timedelta(days=9)))
        self.assertLessEqual({self.branch1.display_name, self.branch2.display_name}, {row["branch"] for row in overview["branches"]})
        with self.assertRaises(AccessError):
            self.Dashboard.with_user(self.manager1).get_performance_overview()
        with self.assertRaises(AccessError):
            self.Dashboard.with_user(self.emp1).get_performance_overview()

    def test_on_time_is_kept_at_submission_and_extensions_are_audited(self):
        task = self._task(-1)
        as_manager = task.with_user(self.manager1)
        for bad in ((str(self.today + timedelta(days=5)), " "), ("", "Reason"), ("not a date", "Reason"), (str(self.today - timedelta(days=9)), "Earlier")):
            with self.assertRaises(ValidationError, msg=str(bad)):
                as_manager.action_extend_deadline(*bad)
        with self.assertRaises(ValidationError, msg="a plain edit is refused"):
            as_manager.write({"date_deadline": str(self.today + timedelta(days=5))})
        for actor in (self.emp1, self.manager2):
            with self.assertRaises(AccessError, msg=actor.login):
                task.with_user(actor).action_extend_deadline(str(self.today + timedelta(days=5)), "Mine")
        self._submit(task)
        self.assertEqual((task.submission_deadline, task.submitted_on_time), (self.today - timedelta(days=1), False))
        with self.assertRaises(ValidationError, msg="frozen during review"):
            as_manager.action_extend_deadline(str(self.today + timedelta(days=5)), "Too late")
        as_manager.action_request_changes("Redo.")
        # New work after changes were requested: an approved extension applies to the next submission, visibly.
        new_date = self.today + timedelta(days=5)
        as_manager.action_extend_deadline(str(new_date), "Waiting for parts.")
        self.assertEqual(task.date_deadline, new_date)
        extension = task.extension_ids
        self.assertEqual((extension.previous_deadline, extension.new_deadline, extension.reason, extension.approved_by_id), (self.today - timedelta(days=1), new_date, "Waiting for parts.", self.manager1))
        with self.assertRaises(AccessError, msg="the audit row is append-only"):
            extension.with_user(self.manager1).write({"reason": "Other"})
        self._submit(task)
        as_manager.action_accept()
        self.assertTrue(task.submitted_on_time)
        figures = self._figures()
        self.assertEqual((figures["on_time"], figures["late"], figures["extended"], figures["returned"]), (1, 0, 1, 1))
        with self.assertRaises(ValidationError, msg="a completed task's deadline is closed"):
            as_manager.action_extend_deadline(str(new_date + timedelta(days=5)), "Later")
        row = task.with_user(self.emp1)._row()
        self.assertEqual((row["submission_deadline"], row["extensions"][0]["approved_by"]), (str(new_date), self.manager1.name))

    def test_on_time_uses_the_company_day_not_the_browser_context(self):
        task = self._task(0)
        self.company.partner_id.tz = "Asia/Kathmandu"
        with patch("odoo.fields.Datetime.now", return_value=fields.Datetime.to_datetime(str(self.today)) + timedelta(hours=20)):  # 01:45 next day in Kathmandu
            as_emp = task.with_user(self.emp1).with_context(tz="Pacific/Honolulu")
            as_emp.action_start()
            as_emp.action_submit("Done.")
        self.assertFalse(task.submitted_on_time)

    def test_returned_counts_only_real_returns(self):
        task = self._task(2)
        task.with_user(self.emp1).action_start()
        task.with_user(self.emp1).action_submit("Changes requested: my own wording")
        self.assertEqual(self._figures()["returned"], 0)

    # ------------------------------------------------------------------ blockers
    def test_employee_reports_blocker_to_their_manager(self):
        task = self._task(2)
        as_emp = task.with_user(self.emp1)
        for bad in (("", "Help"), ("Problem", " ")):
            with self.assertRaises(ValidationError):
                as_emp.action_report_blocker(*bad)
        for actor in (self.manager1, self.emp2):
            with self.assertRaises(AccessError, msg=actor.login):
                task.with_user(actor).action_report_blocker("P", "H")
        as_emp.action_report_blocker("Machine is broken.", "Call the technician.")
        with self.assertRaises(UserError):
            as_emp.action_report_blocker("Again", "Again")
        self.assertEqual(task.date_deadline, self.today + timedelta(days=2), "a blocker moves nothing by itself")
        data = self.Task.with_user(self.manager1).get_branch_task_summary()
        self.assertEqual(data["blocked"], 1)
        self.assertEqual(data["attention"][0]["open_blocker"]["problem"], "Machine is broken.")
        self.assertEqual(self.Task.with_user(self.manager2).get_branch_task_summary()["blocked"], 0, "other branches never see it")
        self.assertEqual(self.env["strategic.task.blocker"].with_user(self.manager2).search_count([("task_id", "=", task.id)]), 0)
        self.assertEqual(self.env["strategic.task.blocker"].with_user(self.emp2).search_count([("task_id", "=", task.id)]), 0)
        self.assertEqual(self._figures()["blockers"], 1)
        for actor in (self.emp1, self.manager2):
            with self.assertRaises(AccessError, msg=actor.login):
                task.with_user(actor).action_resolve_blocker("Done")
        with self.assertRaises(ValidationError):
            task.with_user(self.manager1).action_resolve_blocker(" ")
        task.with_user(self.manager1).action_resolve_blocker("Technician came.")
        blocker = task.blocker_ids
        self.assertEqual((blocker.state, blocker.resolved_by_id, blocker.resolution), ("resolved", self.manager1, "Technician came."))
        self.assertEqual(self._figures()["blockers"], 1, "the history stays counted")
        self.assertFalse(as_emp._row()["open_blocker"])
        as_emp.action_report_blocker("New problem.", "Help.")
        with self.assertRaises(AccessError, msg="append-only for employees"):
            task.blocker_ids[0].with_user(self.emp1).write({"state": "resolved"})

    # ------------------------------------------------------------------ fulfilment and duplicates
    def test_partial_fulfilment_is_reported(self):
        request = self._approved()
        self.assertEqual(request._row()["fulfilment"], "Not started")
        self._register(request, "stf.one@example.com", "STF One")
        self.assertEqual((request._row()["fulfilment"], request._row()["remaining"]), ("Partly filled", 1))
        employee = self._register(request, "stf.two@example.com", "STF Two")
        self.assertEqual(request._row()["fulfilment"], "Filled")
        employee.sudo().active = False
        self.assertEqual(request._row()["registered"], 2, "someone who left needs a new approval, not a free slot")
        with self.assertRaises(UserError):
            self._register(request, "stf.three@example.com", "STF Three")

    def test_email_check_is_exact_and_the_database_backs_it_up(self):
        request = self._approved()
        self._register(request, "stf_x@example.com", "STF X")
        self._register(request, "stfyx@example.com", "STF Underscore Is Not A Wildcard")
        with self.assertRaises(Exception), self.env.cr.savepoint():
            self.env["hr.employee"].sudo().create({"name": "Race", "work_email": "STF_X@example.com", "strategic_staffing_request_id": request.id, "company_id": self.company.id})
            self.env.flush_all()

    # ------------------------------------------------------------------ Employee Management page
    def test_head_office_page_covers_every_branch_and_manager_only_theirs(self):
        Department = self.env["strategic.department"]
        sales, inventory = Department.search([("code", "=", "sales")]), Department.search([("code", "=", "inventory")])
        Manager = self.Dashboard.with_user(self.manager1)
        Manager.set_employee_departments(self.emp1.id, [sales.id, inventory.id])
        self.assertEqual(self.emp1.strategic_department_ids, sales | inventory, "several departments, saved")
        self.assertEqual({row["code"] for row in self.Dashboard.with_user(self.emp1).get_employee_context()["departments"]}, {"sales", "inventory"})
        with self.assertRaises(AccessError, msg="the employee reads only what was assigned"):
            self.Dashboard.with_user(self.emp1).get_department_operations("purchasing")
        for actor, target in ((self.manager1, self.emp2), (self.manager2, self.emp1), (self.emp1, self.emp1), (self.manager1, self.manager1)):
            with self.assertRaises(AccessError, msg="%s -> %s" % (actor.login, target.login)):
                self.Dashboard.with_user(actor).set_employee_departments(target.id, [sales.id])
        mine = self.Task.with_user(self.manager1).get_employee_management()
        rows = {row["id"]: row for row in mine["employees"]}
        self.assertEqual((set(rows[self.emp1.id]["department_ids"]), rows[self.emp1.id]["can_assign_departments"]), (set((sales | inventory).ids), True))
        self.assertNotIn(self.emp2.id, rows)
        head = {row["id"]: row for row in self.Task.with_user(self.head).get_employee_management()["employees"]}
        self.assertLessEqual({self.emp1.id, self.emp2.id, self.manager1.id}, set(head))
        self.assertEqual(head[self.emp2.id]["branch"], self.branch2.display_name)
        self.assertEqual(self.Task.with_user(self.head).get_employee_tasks(self.emp2.id)["employee"]["id"], self.emp2.id)
        with self.assertRaises(AccessError):
            self.Task.with_user(self.manager1).get_employee_tasks(self.emp2.id)

    def test_registration_keeps_department_branch_and_manager(self):
        request = self._approved()
        other = self.env["hr.department"].create({"name": "STF Kitchen", "company_id": self.company.id})
        Head = request.with_user(self.head)
        with self.assertRaises(ValidationError):
            Head.register_employee({"name": "X", "work_email": "x1@example.com", "warehouse_id": self.branch2.id})
        with self.assertRaises(ValidationError):
            Head.register_employee({"name": "X", "work_email": "x1@example.com", "parent_id": 999999})
        head_employee = self.env["hr.employee"].search([("user_id", "=", self.head.id)])
        row = Head.register_employee({"name": "STF Y", "work_email": "stfy@example.com", "warehouse_id": self.branch1.id, "department_id": other.id, "parent_id": head_employee.id})
        employee = self.env["hr.employee"].sudo().browse(row["new_employee_id"])
        self.assertEqual((employee.department_id, employee.parent_id, employee._strategic_branch()), (other, head_employee, self.branch1))
        employee = self._register(request, "stf.z@example.com", "STF Z")
        self.assertEqual(employee.department_id, self.department, "the request's department is the default")
        self.assertEqual(employee.parent_id.user_id, self.manager1, "the branch's manager is the default")
        self.assertIn(employee.id, [row["id"] for row in self.Task.with_user(self.manager1).get_employee_management()["without_login"]])

    # ------------------------------------------------------------------ employee workspace and My Performance
    def test_employee_workspace_comes_from_their_own_tasks(self):
        late, returned, today_task, soon, far = self._task(-2), self._task(5), self._task(0), self._task(3, "1"), self._task(30)
        other = self._task(0, assignee=self.emp2, manager=self.manager2)
        waiting = self._task(1)
        self._submit(waiting)
        self._submit(returned)
        returned.with_user(self.manager1).action_request_changes("Use the new checklist.")
        far.with_user(self.emp1).action_report_blocker("No gloves.", "Please order them.")
        self.company.partner_id.tz = "UTC"
        data = self.Task.with_user(self.emp1).get_employee_workspace()
        self.assertEqual((data["counts"]["overdue"], data["counts"]["review"], data["counts"]["correction"], data["counts"]["open"]), (1, 1, 1, 6))
        self.assertEqual(data["counts"]["due_today"], 1)
        self.assertEqual([row["id"] for row in data["tasks"]][:3], [late.id, returned.id, today_task.id], "late, then corrections, then today")
        self.assertNotIn(other.id, [row["id"] for row in data["tasks"]])
        self.assertIn(soon.id, [row["id"] for row in data["upcoming"]])
        self.assertNotIn(far.id, [row["id"] for row in data["upcoming"]])
        self.assertEqual((data["instructions"][0]["note"], data["instructions"][0]["by"]), ("Use the new checklist.", self.manager1.name))
        self.assertEqual((data["blockers"][0]["problem"], data["blockers"][0]["status"]), ("No gloves.", "Open"))
        self.assertTrue(data["activity"])
        for actor in (self.manager1, self.head):
            with self.assertRaises(AccessError):
                self.Task.with_user(actor).get_employee_workspace()

    def test_my_performance_is_operational_and_only_mine(self):
        done = self._task(1)
        self._submit(done)
        done.with_user(self.manager1).action_request_changes("Redo the count.")
        done.with_user(self.emp1).action_start()
        done.with_user(self.emp1).action_submit("Done again.")
        done.with_user(self.manager1).action_accept()
        self._task(1, assignee=self.emp2, manager=self.manager2)
        data = self.Dashboard.with_user(self.emp1).get_my_performance(str(self.today - timedelta(days=5)), str(self.today + timedelta(days=5)))
        self.assertEqual((data["figures"]["assigned"], data["figures"]["completed"], data["figures"]["returned"]), (1, 1, 1))
        self.assertEqual(sum(row["completed"] for row in data["history"]), 1)
        self.assertEqual(data["feedback"][0]["note"], "Redo the count.")
        self.assertEqual(data["individual_kpis"], [])
        self.assertFalse({"score", "overall", "assessments"} & set(data), "no invented score, no private assessments")

    # ------------------------------------------------------------------ directory
    def test_standard_directory_is_limited_to_the_branch(self):
        for model in ("hr.employee", "hr.employee.public"):
            seen = self.env[model].with_user(self.emp1).search([]).mapped("name")
            self.assertIn("stf_emp1", seen)
            self.assertIn("stf_manager1", seen)
            self.assertNotIn("stf_emp2", seen, model)
            self.assertNotIn("stf_manager2", seen, model)
            self.assertNotIn("stf_emp1", self.env[model].with_user(self.manager2).search([]).mapped("name"), model)
            self.assertIn("stf_emp2", self.env[model].with_user(self.head).search([]).mapped("name"), model)
        self.emp1.sudo().strategic_branch_warehouse_id = False
        self.assertEqual(self.env["hr.employee.public"].with_user(self.emp1).search([]).mapped("name"), ["stf_emp1"], "no branch: only themselves")

    def _assess(self, actor=None, person=None, period=("2026-01-01", "2026-01-31")):
        ratings = dict.fromkeys(("work_quality", "teamwork", "communication", "responsibility", "problem_solving", "customer_service"), 4)
        return self.Dashboard.with_user(actor or self.manager1).save_employee_assessment((person or self.emp1).id, dict(ratings, notes="Solid."), *period)

    def test_assessment_needs_comments_a_real_period_and_defines_the_scale(self):
        ratings = dict.fromkeys(("work_quality", "teamwork", "communication", "responsibility", "problem_solving", "customer_service"), 3)
        Manager = self.Dashboard.with_user(self.manager1)
        with self.assertRaises(ValidationError):
            Manager.save_employee_assessment(self.emp1.id, dict(ratings, notes="  "), "2026-01-01", "2026-01-31")
        future = str(self.today + timedelta(days=10))
        with self.assertRaises(ValidationError):
            Manager.save_employee_assessment(self.emp1.id, dict(ratings, notes="x"), future, future)
        row = self._assess()["assessments"][0]
        self.assertEqual((row["assessor"], row["notes"], row["start"], row["end"]), (self.manager1.name, "Solid.", "2026-01-01", "2026-01-31"))
        data = Manager.get_employee_performance(self.emp1.id)
        self.assertEqual([level[0] for level in data["scale"]], [1, 2, 3, 4, 5])
        self.assertTrue(all(level[1] and level[2] for level in data["scale"]))
        self.assertEqual(len(data["criteria"]), 6)
        record = self.env["strategic.employee.assessment"].sudo().search([("user_id", "=", self.emp1.id)])
        with self.assertRaises(Exception), self.env.cr.savepoint():
            record.work_quality = 9
            self.env.flush_all()

    def test_assessments_are_for_the_manager_and_hidden_from_employees(self):
        self.assertEqual(self._assess()["assessments"][0]["work_quality"], 4)
        with self.assertRaises(UserError):
            self._assess()
        with self.assertRaises(AccessError):
            self._assess(self.manager2)
        with self.assertRaises(AccessError):
            self._assess(self.head)
        with self.assertRaises(AccessError):
            self._assess(self.emp2, self.emp1)
        with self.assertRaises(ValidationError):
            self.Dashboard.with_user(self.manager1).save_employee_assessment(self.emp1.id, {"work_quality": 9}, "2026-02-01", "2026-02-28")
        Assessment = self.env["strategic.employee.assessment"]
        with self.assertRaises(AccessError):
            Assessment.with_user(self.emp1).search_read([], ["notes"], limit=1)
        self.assertNotIn("assessments", self.Dashboard.with_user(self.emp1).get_my_performance())
        self.assertEqual(Assessment.with_user(self.manager2).search_count([("user_id", "=", self.emp1.id)]), 0)
        self.assertEqual(Assessment.with_user(self.head).search_count([("user_id", "=", self.emp1.id)]), 1)
