from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    strategic_recipe_required = fields.Boolean(
        "Needs a recipe",
        help="Menu item made from ingredients. 'Ensure key menu items have approved recipes' checks that every sale of it "
             "had an active recipe (Bill of Materials). Leave it off for items sold as they are, such as bottled drinks.",
    )
