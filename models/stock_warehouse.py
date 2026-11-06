from odoo import api, fields, models


class StockWarehouse(models.Model):
    # Branch-level issues (e.g. understaffing) get follow-up activities and a
    # discussion history on the branch itself.
    _name = "stock.warehouse"
    _inherit = ["stock.warehouse", "mail.thread", "mail.activity.mixin"]

    # Odoo Community has no shift-planning app, so the staffing requirement
    # used by the People dashboard is configured per branch. Zero means
    # "not configured" and is never treated as a requirement of nobody.
    strategic_planned_headcount = fields.Integer(
        string="Planned Staff",
        help="Number of staff this branch should have. Leave at 0 when not configured.",
    )

    @api.model
    def _strategic_branches(self, company):
        """The company's branches: its warehouses except the main one, unless someone works at the main one as their branch.

        The main warehouse is Odoo's own: the company's first warehouse by sequence, the default for users and sales
        orders (res.users._get_default_warehouse_id). It is the central store, not a branch; a company with a single
        warehouse has that one as its branch. Decided by these relationships, never by a warehouse's name.
        """
        warehouses = self.sudo().search([("company_id", "=", company.id)])
        if len(warehouses) < 2:
            return warehouses
        main = warehouses[:1]
        staffed = self.env["res.users"].sudo().with_context(active_test=False).search_count([("strategic_branch_warehouse_id", "=", main.id)])
        return warehouses if staffed else warehouses - main
