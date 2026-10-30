from odoo import fields, models


class ResUsers(models.Model):
    _inherit = "res.users"

    strategic_branch_warehouse_id = fields.Many2one(
        "stock.warehouse",
        string="Strategic Branch Warehouse",
        help="Warehouse used to scope branch-level strategic KPI access.",
    )

    def _get_invalidation_fields(self):
        # Record rules read the user's branch, and Odoo caches evaluated rules per user: a branch change must clear them.
        return super()._get_invalidation_fields() | {"strategic_branch_warehouse_id"}
