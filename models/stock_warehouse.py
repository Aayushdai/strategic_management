from odoo import fields, models


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
