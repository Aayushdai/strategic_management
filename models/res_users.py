from odoo import api, fields, models
from odoo.exceptions import ValidationError

_NOTHING = [("id", "=", False)]
_POS_PATH = {
    "pos.config": "picking_type_id.warehouse_id",
    "pos.session": "config_id.picking_type_id.warehouse_id",
    "pos.order": "config_id.picking_type_id.warehouse_id",
    "pos.order.line": "order_id.config_id.picking_type_id.warehouse_id",
    "pos.payment": "pos_order_id.config_id.picking_type_id.warehouse_id",
    "report.pos.order": "config_id.picking_type_id.warehouse_id",
    "stock.picking.batch": "picking_type_id.warehouse_id",
}


class ResUsers(models.Model):
    _inherit = "res.users"

    strategic_branch_warehouse_id = fields.Many2one(
        "stock.warehouse",
        string="Strategic Branch Warehouse",
        help="Warehouse used to scope branch-level strategic KPI access.",
    )
    # Admin-only as a field: Head Office changes it through set_employee_departments, which validates role, company and branch.
    strategic_department_ids = fields.Many2many(
        "strategic.department",
        "strategic_department_res_users_rel",
        "user_id",
        "department_id",
        string="Strategic Departments",
        groups="base.group_erp_manager",
        help="Operational departments this Employee belongs to. Grants no management rights.",
    )

    @api.constrains("strategic_department_ids")
    def _check_strategic_departments(self):
        for user in self.sudo().filtered("strategic_department_ids"):
            if not user.has_group("strategic_management.group_strategic_employee") \
                    or user.has_group("strategic_management.group_strategic_branch_manager"):
                raise ValidationError("Departments can only be assigned to Employees.")
            branch = user.strategic_branch_warehouse_id
            if not branch or branch.company_id not in user.company_ids:
                raise ValidationError("Departments require a branch in one of the employee's companies.")

    def _get_invalidation_fields(self):
        # Record rules read the user's branch and departments, and Odoo caches evaluated rules per user: a change must clear them.
        return super()._get_invalidation_fields() | {"strategic_branch_warehouse_id", "strategic_department_ids"}

    # ------------------------------------------------------------------ direct-access restrictions (Phase 9C)
    def _strategic_access_branch(self):
        """The user's valid branch for direct model access, or an empty recordset (same checks as the dashboards)."""
        user = self.sudo()
        branch = user.strategic_branch_warehouse_id
        if not branch or branch.company_id not in user.company_ids:
            return branch.browse()
        if self.has_group("strategic_management.group_strategic_branch_manager"):
            return branch
        linked = self.env["hr.employee"].sudo().search_count([("user_id", "=", user.id), ("company_id", "=", branch.company_id.id)], limit=1)
        return branch if linked else branch.browse()

    def _strategic_stock_domain(self, model_name, branch, operation_types=None):
        """Records of a stock model that belong to the branch, or, with operation_types, to those operation types only."""
        if model_name == "stock.quant":
            return [("location_id", "child_of", branch.view_location_id.id)]
        if model_name == "report.stock.quantity":
            return [("warehouse_id", "=", branch.id)]
        if model_name == "stock.picking":
            return [("picking_type_id", "in", operation_types)] if operation_types else [("picking_type_id.warehouse_id", "=", branch.id)]
        if operation_types:
            return [("picking_id.picking_type_id", "in", operation_types)]
        root = branch.view_location_id.id
        return ["|", ("location_id", "child_of", root), ("location_dest_id", "child_of", root)]

    def _strategic_pos_operation_types(self, branch):
        """The operation types POS sales and refunds of this branch use: what a cashier's sale creates, never receipts."""
        configs = self.env["pos.config"].sudo().search([("picking_type_id.warehouse_id", "=", branch.id)])
        types = configs.picking_type_id | branch.pos_type_id
        return (types | types.return_picking_type_id).ids

    def _strategic_access_domain(self, model_name, operation):
        """Global ir.access restriction for Branch Managers and Strategic Employees; [] (no effect) for everybody else.

        Global entries AND onto every other rule, so no extra Odoo group can widen them. They run on direct access only:
        sudo reads such as the department dashboards are unaffected. Odoo itself creates a cashier's POS pickings and
        closes sessions as that cashier, so a POS User keeps their branch's POS records and POS operation types, while
        receipts and transfers need Inventory / User (or, for a manager, Purchase / User).
        """
        if not self.has_group("strategic_management.group_strategic_employee") \
                or self.has_group("strategic_management.group_strategic_head_office_manager"):
            return []
        manager = self.has_group("strategic_management.group_strategic_branch_manager")
        branch = self._strategic_access_branch()
        pos_warehouse = "config_id.picking_type_id.warehouse_id"
        if model_name == "account.move":
            # Only POS-linked entries belong to a branch; other entries keep whatever the user's groups allow.
            return ["|", ("pos_order_ids", "=", False), ("pos_order_ids.%s" % pos_warehouse, "=", branch.id or False)]
        if model_name in ("hr.employee", "hr.employee.public"):
            # Branch staff see their own branch's colleagues; Odoo's standard directory would show every branch's contacts.
            own = [("user_id", "=", self.id)]
            return ["|", *own, ("user_id.strategic_branch_warehouse_id", "=", branch.id)] if branch else own
        if not branch:
            return _NOTHING
        departments = self.sudo().strategic_department_ids.mapped("code")
        if model_name == "product.supplierinfo":
            return [] if manager or "purchasing" in departments else _NOTHING
        if model_name in _POS_PATH:
            return [(_POS_PATH[model_name], "=", branch.id)]
        if model_name == "account.bank.statement":
            return ["|", ("line_ids", "=", False), ("line_ids.pos_session_id.%s" % pos_warehouse, "=", branch.id)]
        pos_types = self._strategic_pos_operation_types(branch) if self.has_group("point_of_sale.group_pos_user") else []
        pos_only = self._strategic_stock_domain(model_name, branch, pos_types) if pos_types and model_name in ("stock.picking", "stock.move", "stock.move.line") else _NOTHING
        if operation == "write":
            if self.has_group("stock.group_stock_user") or (manager and self.has_group("purchase.group_purchase_user")):
                return self._strategic_stock_domain(model_name, branch)
            return pos_only
        return self._strategic_stock_domain(model_name, branch) if manager or "inventory" in departments else pos_only

