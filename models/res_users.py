from odoo import fields, models


class ResUsers(models.Model):
    _inherit = "res.users"

    strategic_branch_warehouse_id = fields.Many2one(
        "stock.warehouse",
        string="Strategic Branch Warehouse",
        help="Warehouse used to scope branch-level strategic KPI access.",
    )
