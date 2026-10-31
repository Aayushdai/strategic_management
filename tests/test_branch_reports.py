"""Branch Manager Phase 7: branch reports, their Head Office review, notifications and the blocker list."""

from datetime import datetime, timedelta

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, tagged

PERIOD = ("2001-04-02", "2001-04-08")


def at(day, hour=10):
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=hour)


@tagged("post_install", "-at_install")
class TestBranchReports(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        ref = cls.env.ref
        Warehouse = cls.env["stock.warehouse"]
        cls.branch1 = Warehouse.create({"name": "REP Branch 1", "code": "RPB1", "company_id": cls.company.id})
        cls.branch2 = Warehouse.create({"name": "REP Branch 2", "code": "RPB2", "company_id": cls.company.id})
        cls.pos1 = cls.env["pos.config"].create({"name": "REP POS 1", "picking_type_id": cls.branch1.pos_type_id.id})
        apps = [ref("stock.group_stock_user").id, ref("purchase.group_purchase_user").id, ref("point_of_sale.group_pos_user").id]

        def user(login, groups, warehouse=False):
            return cls.env["res.users"].with_context(no_reset_password=True).create({
                "name": login, "login": login, "email": "%s@example.com" % login, "tz": "UTC",
                "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
                "group_ids": [(6, 0, [ref("base.group_user").id] + groups)],
                "strategic_branch_warehouse_id": warehouse.id if warehouse else False})

        manager = ref("strategic_management.group_strategic_branch_manager").id
        cls.head = user("rep_head", apps + [ref("strategic_management.group_strategic_head_office_manager").id])
        cls.manager1 = user("rep_manager1", apps + [manager], cls.branch1)
        cls.manager2 = user("rep_manager2", apps + [manager], cls.branch2)
        cls.unassigned = user("rep_unassigned", apps + [manager])
        cls.employee = user("rep_employee", [ref("strategic_management.group_strategic_employee").id], cls.branch1)
        cls.latte = cls.env["product.template"].create({"name": "REP Latte", "type": "consu", "available_in_pos": True,
                                                        "list_price": 4.0}).product_variant_id
        cls.Report = cls.env["strategic.branch.report"]
        cls.Dashboard = cls.env["strategic.performance.dashboard"]

    def _sell(self, total, day):
        session = self.env["pos.session"].search([("config_id", "=", self.pos1.id), ("state", "!=", "closed")], limit=1) \
            or self.env["pos.session"].create({"config_id": self.pos1.id, "user_id": self.env.uid})
        return self.env["pos.order"].create({
            "session_id": session.id, "company_id": self.company.id, "state": "paid", "date_order": at(day),
            "amount_tax": 0.0, "amount_total": total, "amount_paid": total, "amount_return": 0.0,
            "lines": [(0, 0, {"product_id": self.latte.id, "qty": 1, "price_unit": total, "price_subtotal": total,
                              "price_subtotal_incl": total, "full_product_name": self.latte.name})]})

    def _draft(self, user=None):
        data = self.Report.with_user(user or self.manager1).create_branch_report(*PERIOD)
        return self.Report.browse(data["id"]), data

    def _todos(self, report, prefix):
        return report.activity_ids.filtered(lambda activity: activity.summary.startswith(prefix))

    # ------------------------------------------------------------------ 1-5: prepare, snapshot, stability
    def test_manager_prepares_and_submits_a_report_whose_figures_stay_as_submitted(self):
        self._sell(20.0, "2001-04-03")
        report, data = self._draft(self.manager1)
        self.assertEqual((report.warehouse_id, report.state, str(report.date_start), str(report.date_end)), (self.branch1, "draft", *PERIOD))
        self.assertTrue(data["preview"] and data["can_edit"], "a draft shows today's figures as a preview")
        ops = self.Dashboard.with_user(self.manager1).get_branch_operations(*PERIOD)
        self.assertEqual(data["snapshot"]["sales"]["net_sales"], ops["sales"]["net_sales"], "the same Phase 6 figures")
        report.with_user(self.manager1).action_submit("Quiet week; the espresso machine was out for a day.")
        snapshot = report.snapshot
        self.assertEqual((report.state, report.submitted_by_id, report.submission_count), ("submitted", self.manager1, 1))
        self.assertEqual((snapshot["sales"]["net_sales"], snapshot["sales"]["orders"]), (20.0, 1))
        self.assertEqual(snapshot["period"], {"start": PERIOD[0], "end": PERIOD[1]})
        # Period figures vs. figures as of the moment they were taken are kept apart.
        self.assertIn("awaiting_count", snapshot["purchasing"])
        self.assertIn("confirmed_value", snapshot["purchasing"])
        self.assertIn("low_stock_count", snapshot["inventory"])
        self.assertTrue(snapshot["taken_at"])
        self.assertEqual(set(snapshot["tasks"]) >= {"open", "overdue", "review"}, True)
        # New sales after submission do not change what was reported.
        self._sell(50.0, "2001-04-04")
        self.assertEqual(self.Dashboard.with_user(self.manager1).get_branch_operations(*PERIOD)["sales"]["net_sales"], 70.0)
        report.invalidate_recordset()
        saved = self.Report.with_user(self.head).get_branch_report(report.id)
        self.assertEqual((saved["snapshot"]["sales"]["net_sales"], saved["preview"]), (20.0, False))
        self.assertEqual(saved["comment"], "Quiet week; the espresso machine was out for a day.")
        # The backend form shows real zeros as zeros; only missing figures say "Not available".
        self.assertEqual(snapshot["tasks"]["open"], 0)
        self.assertIn("<td>Open</td><td>0</td>", report.summary_html)

    def test_snapshot_and_workflow_fields_cannot_be_written_directly(self):
        report, _data = self._draft()
        for vals in ({"snapshot": {"sales": {"net_sales": 1e6}}}, {"state": "reviewed"}, {"warehouse_id": self.branch2.id},
                     {"date_end": "2001-12-31"}, {"submission_count": 7}):
            with self.assertRaises(ValidationError, msg=vals):
                report.with_user(self.manager1).write(vals)
        with self.assertRaises(ValidationError):
            self.Report.with_user(self.manager1).create({"date_start": PERIOD[0], "date_end": PERIOD[1], "warehouse_id": self.branch2.id})
        report.with_user(self.manager1).action_submit()
        with self.assertRaises(ValidationError):
            report.with_user(self.manager1).write({"comment": "changed after submission"})

    # ------------------------------------------------------------------ 6, 11, 12, 14, 15, 18: review and notifications
    def test_review_cycle_notifies_the_right_people_once(self):
        report, _data = self._draft()
        report.with_user(self.manager1).action_submit("First version.")
        review_todos = self._todos(report, "Review branch report: ")
        self.assertIn(self.head, review_todos.user_id, "Head Office gets the review To-Do")
        self.assertNotIn(self.manager1, review_todos.user_id)
        self.assertNotIn(self.manager2, review_todos.user_id, "no To-Do for other branches' managers")
        self.assertEqual(len(review_todos), len(set(review_todos.user_id.ids)), "one To-Do per recipient")
        self.assertEqual({(a.res_model, a.res_id) for a in review_todos}, {("strategic.branch.report", report.id)}, "linked to the report")

        with self.assertRaises(ValidationError):
            report.with_user(self.head).action_request_changes(" ")
        report.with_user(self.head).action_request_changes("Add why waste doubled.")
        self.assertEqual((report.state, report.review_note), ("changes_requested", "Add why waste doubled."))
        self.assertFalse(self._todos(report, "Review branch report: "), "Head Office's To-Dos close with the decision")
        self.assertEqual(self._todos(report, "Revise branch report: ").user_id, self.manager1, "only the submitting manager is asked")
        mine = self.Report.with_user(self.manager1).get_branch_report(report.id)
        self.assertEqual((mine["review_note"], mine["can_edit"]), ("Add why waste doubled.", True))

        report.with_user(self.manager1).write({"comment": "Second version: the fridge failed on Tuesday."})
        report.with_user(self.manager1).action_submit()
        self.assertEqual((report.state, report.submission_count), ("submitted", 2))
        self.assertFalse(self._todos(report, "Revise branch report: "), "resubmitting completes the manager's revise To-Do")
        notes = report.message_ids.filtered(lambda m: "submission" in (m.body or "").lower())
        self.assertTrue(any("Updated submission 2 (replaces submission 1)" in m.body for m in notes), "an update is told apart from the original")
        self.assertTrue(any("Submission 1" in m.body for m in notes), "the original submission stays in the history")
        self.assertEqual(len(self._todos(report, "Review branch report: ").filtered(lambda a: a.user_id == self.head)), 1)

        before = len(self.env["mail.notification"].search([("res_partner_id", "=", self.manager1.partner_id.id)]))
        report.with_user(self.head).action_mark_reviewed("Thanks, clear.")
        self.assertEqual((report.state, report.reviewed_by_id), ("reviewed", self.head))
        after = self.env["mail.notification"].search([("res_partner_id", "=", self.manager1.partner_id.id)])
        self.assertEqual(len(after) - before, 1, "the manager is told once that the report was reviewed")
        self.assertFalse(self._todos(report, "Review branch report: "))
        with self.assertRaises(ValidationError):
            report.with_user(self.head).action_mark_reviewed()

    # ------------------------------------------------------------------ 7-10: isolation and roles
    def test_isolation_and_roles(self):
        report, _data = self._draft(self.manager1)
        report.with_user(self.manager1).action_submit()
        with self.assertRaises(AccessError):
            report.with_user(self.manager2).read(["name"])
        with self.assertRaises(AccessError):
            self.Report.with_user(self.manager2).get_branch_report(report.id)
        self.assertEqual(self.Report.with_user(self.manager2).get_branch_reports()["reports"], [])
        for call in (lambda: report.with_user(self.manager1).action_mark_reviewed(),
                     lambda: report.with_user(self.manager1).action_request_changes("x")):
            with self.assertRaises(AccessError):
                call()
        with self.assertRaises(AccessError):
            self._draft(self.unassigned)
        with self.assertRaises(AccessError):
            self._draft(self.employee)
        with self.assertRaises(AccessError):
            self.Report.with_user(self.employee).get_branch_reports()
        with self.assertRaises(AccessError):
            self._draft(self.head)
        listed = {row["id"] for row in self.Report.with_user(self.head).get_branch_reports()["reports"]}
        self.assertIn(report.id, listed, "Head Office sees every branch's reports")
        self.assertTrue(self.Report.with_user(self.head).get_branch_report(report.id)["can_review"])

    def test_review_changes_nothing_else(self):
        plan = self.env["strategic.plan"].with_user(self.head).create({"name": "REP Plan", "company_id": self.company.id,
                                                                        "date_start": "2000-01-01", "date_end": "2099-12-31"})
        goal = self.env["strategic.objective"].with_user(self.head).create({"name": "REP Grow", "plan_id": plan.id, "perspective": "financial",
                                                                             "scope": "organization", "deadline": "2099-12-31", "owner_id": self.head.id})
        goal.action_activate()
        report, _data = self._draft()
        report.with_user(self.manager1).action_submit()
        titles = [item["title"] for item in report.snapshot["objectives"]["items"]]
        self.assertIn("REP Grow", titles, "applicable company-wide objectives are in the report")
        before = (goal.state, goal.scoring_status, goal.score)
        report.with_user(self.head).action_mark_reviewed()
        goal.invalidate_recordset()
        self.assertEqual((goal.state, goal.scoring_status, goal.score), before)

    # ------------------------------------------------------------------ 16: task submissions reach the reviewer
    def test_task_submission_notifies_its_reviewer(self):
        Task = self.env["strategic.branch.task"]
        row = Task.with_user(self.manager1).create_branch_task({"name": "REP count", "description": "Count the milk.",
                                                                "assigned_user_id": self.employee.id,
                                                                "date_deadline": str(fields.Date.today() + timedelta(days=2))})
        task = Task.browse(row["id"])
        task.with_user(self.employee).action_start()
        task.with_user(self.employee).action_submit("Counted 12 L.")
        message = task.message_ids.filtered(lambda m: "Submitted for review" in (m.body or ""))
        self.assertEqual(len(message), 1, "the existing note, not a second message")
        self.assertEqual(message.notified_partner_ids, self.manager1.partner_id, "addressed to the manager who assigned it")
        self.assertEqual(task.state, "submitted")

    # ------------------------------------------------------------------ 17: blockers keep working and Head Office sees them
    def test_open_blockers_list_existing_blocker_todos(self):
        plan = self.env["strategic.plan"].with_user(self.head).create({"name": "REP Plan B", "company_id": self.company.id,
                                                                        "date_start": "2000-01-01", "date_end": "2099-12-31"})
        goal = self.env["strategic.objective"].with_user(self.head).create({"name": "REP Cut waste", "plan_id": plan.id,
                                                                             "perspective": "internal_process", "scope": "organization",
                                                                             "deadline": "2099-12-31", "owner_id": self.head.id})
        goal.action_activate()
        result = self.Dashboard.with_user(self.manager1).report_objective_blocker(goal.id, "Fridge broken", "It failed on Monday.", "Approve a repair.")
        self.assertTrue(result["activities"], "the existing blocker To-Do is still created")
        rows = [row for row in self.Dashboard.with_user(self.head).get_open_blockers() if row["objective_id"] == goal.id]
        self.assertEqual(len(rows), 1, "one row per blocker, however many Head Office users have its To-Do")
        self.assertEqual((rows[0]["problem"], rows[0]["branch"], rows[0]["reported_by"]), ("Fridge broken", self.branch1.display_name, self.manager1.name))
        self.assertEqual(goal.state, "active", "a blocker is an escalation, not a failure")
        with self.assertRaises(AccessError):
            self.Dashboard.with_user(self.manager1).get_open_blockers()

    # ------------------------------------------------------------------ Phase 8: transfers and several reviewers
    def _new_request(self):
        """What the next web request sees: no access results or records cached in this transaction."""
        self.env.transaction.invalidate_access_cache()
        self.env.invalidate_all()

    def test_branch_change_moves_access_and_the_follow_up_to_the_new_manager(self):
        report, _data = self._draft(self.manager1)
        report.with_user(self.manager1).action_submit("v1")
        task = self.env["strategic.branch.task"].browse(self.env["strategic.branch.task"].with_user(self.manager1).create_branch_task({
            "name": "REP shelf", "description": "Tidy it.", "assigned_user_id": self.employee.id,
            "date_deadline": str(fields.Date.today() + timedelta(days=2))})["id"])
        self.assertTrue(self.Report.with_user(self.manager1).search_count([("id", "=", report.id)]))  # rules evaluated (and cached) for manager1
        # manager1 moves to branch 2; manager2 takes over branch 1.
        self.manager1.write({"strategic_branch_warehouse_id": self.branch2.id})
        self.manager2.write({"strategic_branch_warehouse_id": self.branch1.id})
        self._new_request()
        self.assertFalse(self.Report.with_user(self.manager1).search_count([("id", "=", report.id)]), "the old branch's report is no longer visible")
        with self.assertRaises(AccessError):
            report.with_user(self.manager1).read(["name"])
        with self.assertRaises(AccessError):
            task.with_user(self.manager1).read(["name"])
        self.assertIn(report.id, [row["id"] for row in self.Report.with_user(self.manager2).get_branch_reports()["reports"]])
        self.assertEqual((report.warehouse_id, report.snapshot["branch"]["id"], report.submitted_by_id), (self.branch1, self.branch1.id, self.manager1),
                         "the report keeps its branch, figures and submitter")
        with self.assertRaises(ValidationError):
            report.with_user(self.manager2).write({"warehouse_id": self.branch2.id})
        report.with_user(self.head).action_request_changes("Add the waste figure.")
        self.assertEqual(self._todos(report, "Revise branch report: ").user_id, self.manager2, "the follow-up goes to who can act on it now")
        report.with_user(self.manager2).action_submit("v2")
        report.with_user(self.head).action_mark_reviewed()
        reviewed = report.message_ids.filtered(lambda m: "Report reviewed" in (m.body or ""))
        self.assertEqual(reviewed.notified_partner_ids, self.manager2.partner_id)

    def test_every_head_office_reviewer_gets_one_todo_and_one_decision_closes_them(self):
        head2 = self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "rep_head2", "login": "rep_head2", "email": "rep_head2@example.com", "company_id": self.company.id,
            "company_ids": [(6, 0, [self.company.id])],
            "group_ids": [(6, 0, [self.env.ref("base.group_user").id, self.env.ref("strategic_management.group_strategic_head_office_manager").id])]})
        report, _data = self._draft()
        report.with_user(self.manager1).action_submit()
        todos = self._todos(report, "Review branch report: ")
        self.assertTrue({self.head, head2} <= set(todos.user_id), "each Head Office reviewer is asked")
        self.assertEqual(len(todos), len(set(todos.user_id.ids)), "once each")
        with self.assertRaises(ValidationError):
            report.with_user(self.manager1).action_submit()
        self.assertEqual(self._todos(report, "Review branch report: "), todos, "a repeated submit adds nothing")
        report.with_user(head2).action_request_changes("More detail on waste.")
        self.assertFalse(self._todos(report, "Review branch report: "), "one decision closes every reviewer's To-Do")
        report.with_user(self.manager1).action_submit()
        again = self._todos(report, "Review branch report: ")
        self.assertEqual(len(again.filtered(lambda a: a.user_id == head2)), 1)
        before = self.env["mail.notification"].search_count([("res_partner_id", "=", self.manager1.partner_id.id)])
        report.with_user(self.head).action_mark_reviewed()
        self.assertFalse(self._todos(report, "Review branch report: "))
        self.assertEqual(self.env["mail.notification"].search_count([("res_partner_id", "=", self.manager1.partner_id.id)]) - before, 1)
        # A To-Do on the record does not make its holder a reviewer, nor give anyone else access.
        with self.assertRaises(AccessError):
            report.with_user(self.manager2).read(["name"])

