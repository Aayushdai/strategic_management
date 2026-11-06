"""Workflow gaps from the role diagrams: branches vs the main warehouse, Branch Managers told about their objectives,
Head Office changing positions, and a Branch Manager's own Purchasing page without the Purchase app."""

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, tagged

HEAD = "strategic_management.group_strategic_head_office_manager"
MANAGER = "strategic_management.group_strategic_branch_manager"
EMPLOYEE = "strategic_management.group_strategic_employee"
ADMIN = "strategic_management.group_strategic_admin"


@tagged("post_install", "-at_install")
class TestWorkflowGaps(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ref = cls.env.ref
        # A company of its own, so its warehouses are exactly the ones made here: the first is its main warehouse.
        cls.company = cls.env["res.company"].create({"name": "WFG Café Co"})
        cls.env.user.company_ids |= cls.company
        cls.env = cls.env(context=dict(cls.env.context, allowed_company_ids=[cls.company.id]))
        Warehouse = cls.env["stock.warehouse"]
        cls.main = Warehouse.search([("company_id", "=", cls.company.id)], limit=1) \
            or Warehouse.create({"name": "WFG Central", "code": "WFGC", "company_id": cls.company.id})
        cls.branch1 = Warehouse.create({"name": "WFG Branch 1", "code": "WFG1", "company_id": cls.company.id})
        cls.branch2 = Warehouse.create({"name": "WFG Branch 2", "code": "WFG2", "company_id": cls.company.id})

        def user(login, group, branch=False, extra=()):
            account = cls.env["res.users"].with_context(no_reset_password=True).create({
                "name": login, "login": login, "email": "%s@example.com" % login, "company_id": cls.company.id, "company_ids": [(6, 0, cls.company.ids)],
                "group_ids": [(6, 0, [ref("base.group_user").id, ref(group).id, *[ref(x).id for x in extra]])],
                "strategic_branch_warehouse_id": branch.id if branch else False})
            cls.env["hr.employee"].create({"name": login, "user_id": account.id, "company_id": cls.company.id})
            return account

        cls.head = user("wfg_head", HEAD)
        cls.manager1 = user("wfg_manager1", MANAGER, cls.branch1)
        cls.manager2 = user("wfg_manager2", MANAGER, cls.branch2)
        cls.emp = user("wfg_emp", EMPLOYEE, cls.branch1)
        cls.admin = user("wfg_admin", ADMIN)
        cls.plan = cls.env["strategic.plan"].with_user(cls.head).with_company(cls.company).create({
            "name": "WFG Plan", "company_id": cls.company.id, "date_start": "2000-01-01", "date_end": "2099-12-31"})

    # ------------------------------------------------------------------ branches are not the main warehouse
    def test_branches_leave_out_the_main_warehouse_unless_it_is_staffed(self):
        Warehouse = self.env["stock.warehouse"]
        self.assertEqual(Warehouse._strategic_branches(self.company), self.branch1 | self.branch2)
        rows = self.env["strategic.admin.console"].with_user(self.admin).with_company(self.company).get_admin_branches()["branches"]
        self.assertNotIn(self.main.id, [row["id"] for row in rows], "the Admin's branch list leaves out the central warehouse")
        self.emp.sudo().strategic_branch_warehouse_id = self.main
        self.assertIn(self.main, Warehouse._strategic_branches(self.company), "someone works there as their branch")
        solo = self.env["res.company"].create({"name": "WFG Solo"})
        only = Warehouse.search([("company_id", "=", solo.id)]) or Warehouse.create({"name": "Solo", "code": "WFGS", "company_id": solo.id})
        self.assertEqual(Warehouse._strategic_branches(solo), only, "a single warehouse is the branch")

    # ------------------------------------------------------------------ Branch Managers are told
    def _objective(self, by, scope="branch", branch=None, name="WFG objective"):
        values = {"name": name, "description": "Why.", "plan_id": self.plan.id, "perspective": "internal_process", "scope": scope,
                  "deadline": "2099-06-30", "owner_id": by.id, "company_id": self.company.id}
        if scope == "branch":
            # A branch objective supports an active company objective.
            parent = self._objective(self.head, scope="organization", name=name + " (company)")
            parent.with_user(self.head).action_activate()
            values.update(warehouse_id=(branch or self.branch1).id, parent_objective_id=parent.id)
        return self.env["strategic.objective"].with_user(by).with_company(self.company).create(values)

    def test_head_office_objective_for_a_branch_gives_its_manager_a_todo(self):
        objective = self._objective(self.head)
        objective.with_user(self.head).action_activate()
        todo = objective.activity_ids.filtered(lambda activity: activity.user_id == self.manager1)
        self.assertTrue(todo, "the branch's manager gets a To-Do")
        self.assertIn("New objective for WFG Branch 1", todo.summary)
        self.assertFalse(objective.activity_ids.filtered(lambda activity: activity.user_id == self.manager2), "only that branch")
        organization = self._objective(self.head, scope="organization", name="WFG company goal")
        organization.with_user(self.head).action_activate()
        self.assertFalse(organization.activity_ids, "a company objective notifies nobody here")

    def test_approved_proposal_tells_its_proposer(self):
        proposal = self._objective(self.manager1, name="WFG proposal")
        proposal.with_user(self.manager1).action_submit()
        proposal.with_user(self.head).action_approve()
        messages = proposal.message_ids.filtered(lambda message: "approved by" in (message.body or "") and self.manager1.partner_id in message.partner_ids)
        self.assertTrue(messages, "the proposer is told, as a rejection already does")

    # ------------------------------------------------------------------ positions
    def test_head_office_changes_a_position(self):
        Task = self.env["strategic.branch.task"].with_company(self.company)
        result = Task.with_user(self.head).set_employee_position(self.emp.id, "Shift Supervisor")
        employee = self.env["hr.employee"].search([("user_id", "=", self.emp.id)])
        self.assertEqual((result["position"], employee.job_id.name), ("Shift Supervisor", "Shift Supervisor"))
        Task.with_user(self.head).set_employee_position(self.emp.id, "Shift Supervisor")
        self.assertEqual(self.env["hr.job"].search_count([("name", "=", "Shift Supervisor"), ("company_id", "=", self.company.id)]), 1, "no duplicate positions")
        for actor in (self.manager1, self.emp, self.admin):
            with self.assertRaises(AccessError, msg=actor.login):
                Task.with_user(actor).set_employee_position(self.emp.id, "Barista")
        with self.assertRaises(ValidationError):
            Task.with_user(self.head).set_employee_position(self.emp.id, "   ")

    # ------------------------------------------------------------------ Branch Manager's own Purchasing page
    def test_branch_manager_sees_own_branch_purchasing_without_the_purchase_app(self):
        self.assertFalse(self.manager1.has_group("purchase.group_purchase_user"))
        partner = self.env["res.partner"].create({"name": "WFG Supplier"})
        product = self.env["product.product"].create({"name": "WFG Beans", "type": "consu", "standard_price": 5.0})
        orders = {}
        for branch in (self.branch1, self.branch2):
            order = self.env["purchase.order"].with_company(self.company).create({
                "partner_id": partner.id, "company_id": self.company.id, "picking_type_id": branch.in_type_id.id,
                "order_line": [(0, 0, {"product_id": product.id, "product_qty": 3, "price_unit": 5.0})]})
            order.button_confirm()
            orders[branch] = order
        today = fields.Date.to_string(fields.Date.context_today(self.env.user))
        data = self.env["strategic.performance.dashboard"].with_user(self.manager1).with_company(self.company).get_purchasing_overview_data(today, today, self.branch2.id)
        self.assertTrue(data.get("available"), data.get("messages"))
        self.assertEqual(data["selected_warehouse_id"], self.branch1.id, "pinned to the manager's own branch, whatever the browser sends")
        self.assertEqual(data["branch_options"], [{"id": self.branch1.id, "name": self.branch1.display_name}])
        with self.assertRaises(AccessError, msg="still no direct access to purchase orders"):
            self.env["purchase.order"].with_user(self.manager1).browse(orders[self.branch1].id).read(["name"])
