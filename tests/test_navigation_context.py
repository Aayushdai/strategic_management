"""The app's navigation knows each user's role even when a Branch Manager's branch is missing (Branch Manager Phase 1)."""

from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestNavigationContext(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.other_company = cls.env["res.company"].create({"name": "Nav Other Company"})
        cls.branch = cls.env["stock.warehouse"].create({"name": "Nav Branch", "code": "NAVB", "company_id": cls.company.id})
        cls.foreign = cls.env["stock.warehouse"].create({"name": "Nav Foreign", "code": "NAVF", "company_id": cls.other_company.id})
        ref = cls.env.ref

        def user(login, group, warehouse=False):
            return cls.env["res.users"].with_context(no_reset_password=True).create({
                "name": login, "login": login, "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
                "group_ids": [(6, 0, [ref("base.group_user").id, ref(group).id])],
                "strategic_branch_warehouse_id": warehouse.id if warehouse else False})

        cls.head = user("nav_head", "strategic_management.group_strategic_head_office_manager")
        cls.manager = user("nav_manager", "strategic_management.group_strategic_branch_manager", cls.branch)
        cls.unassigned = user("nav_unassigned", "strategic_management.group_strategic_branch_manager")
        cls.misassigned = user("nav_misassigned", "strategic_management.group_strategic_branch_manager", cls.foreign)
        cls.employee = user("nav_employee", "strategic_management.group_strategic_employee", cls.branch)
        cls.Dashboard = cls.env["strategic.performance.dashboard"]

    def _context(self, user):
        return self.Dashboard.with_user(user).get_navigation_context()

    def test_roles_and_branch_assignment(self):
        switches = {"can_propose": True, "live_refresh": True}  # Settings switches the app follows; no business data
        self.assertEqual(self._context(self.head), {"role": "head_office", "branch": False, "branch_error": False, **switches})
        self.assertEqual(self._context(self.manager),
                         {"role": "branch_manager", "branch": {"id": self.branch.id, "name": self.branch.display_name}, "branch_error": False, **switches})
        self.assertEqual(self._context(self.employee), {"role": "employee", "branch": False, "branch_error": False, **switches},
                         "an employee's branch field is not exposed as manager context")

    def test_missing_or_foreign_branch_keeps_the_role_and_reports_the_existing_error(self):
        for user in (self.unassigned, self.misassigned):
            context = self._context(user)
            self.assertEqual(context["role"], "branch_manager", "still recognised as a Branch Manager")
            self.assertFalse(context["branch"], "no branch is given out")
            self.assertEqual(context["branch_error"], "Assign this Branch Manager to a warehouse in the active company.")

    def test_data_methods_still_refuse_without_a_valid_branch(self):
        for user in (self.unassigned, self.misassigned):
            with self.assertRaisesRegex(AccessError, "Assign this Branch Manager"):
                self.Dashboard.with_user(user).get_business_overview_data(False, False, False)
            with self.assertRaises(AccessError):
                self.Dashboard.with_user(user).get_branches_overview_data(False, False, False)

    def test_branch_managers_cannot_pick_another_branch(self):
        data = self.Dashboard.with_user(self.manager).get_business_overview_data(False, False, self.foreign.id)
        self.assertEqual(data["selected_warehouse_id"], self.branch.id)
        self.assertEqual([option["id"] for option in data["branch_options"]], [self.branch.id])
