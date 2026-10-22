"""Notify open Strategic Management dashboards when their source data changes.

The notification only names the changed areas ("pos", "purchase", "inventory",
"strategic", "people"); it carries no business figures. Each dashboard re-fetches its
data through the normal RPC so the requesting user's access rights, record
rules and branch restrictions are applied as usual.
"""

from odoo import api, models


DATA_CHANGED = "strategic_management.data_changed"
_PAYLOAD_KEY = "strategic_management.data_changed"
_ACTIVITY_MODELS = {"strategic.objective", "strategic.kpi.target", "stock.picking"}
# Optional apps this module does not depend on; hooked at registry load when installed.
_OPTIONAL_MODEL_AREAS = {"hr.employee": "people", "hr.attendance": "people", "hr.leave": "people"}


def notify_data_changed(env, area):
    """Queue at most one bus notification per transaction for strategic managers.

    ``bus.bus._sendone`` serializes the payload in its pre-commit hook, so areas
    added later in the same transaction are still included, and a rolled-back
    transaction sends nothing. Branch Managers and Head Office inherit the
    Strategic Employee group, so one group channel reaches every dashboard role;
    each client re-fetches only what its own access rights allow.
    """
    payload = env.cr.precommit.data.get(_PAYLOAD_KEY)
    if payload is not None:
        if area not in payload["areas"]:
            payload["areas"].append(area)
        return
    payload = env.cr.precommit.data[_PAYLOAD_KEY] = {"areas": [area]}
    group = env.ref("strategic_management.group_strategic_employee", raise_if_not_found=False)
    if not group or "bus.bus" not in env.registry.models:
        return
    # sudo: bus.bus rows are technical and the payload carries no business data.
    group.sudo()._bus_send(DATA_CHANGED, payload)


class LiveRefreshMixin(models.AbstractModel):
    """Notify on create/write/unlink; ``_live_refresh_area`` returns the area or False."""

    _name = "strategic.live.refresh.mixin"
    _description = "Strategic dashboard live refresh"

    def _live_refresh_area(self, vals=None):
        return False

    def _register_hook(self):
        result = super()._register_hook()
        if self._name == "strategic.live.refresh.mixin":
            for model_name, area in _OPTIONAL_MODEL_AREAS.items():
                if model_name in self.env.registry.models:
                    _patch_live_refresh(self.env.registry[model_name], area)
        return result

    def _live_refresh_notify(self, vals=None):
        area = self._live_refresh_area(vals)
        if area:
            notify_data_changed(self.env, area)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._live_refresh_notify()
        return records

    def write(self, vals):
        result = super().write(vals)
        self._live_refresh_notify(vals)
        return result

    def unlink(self):
        area = self._live_refresh_area()
        result = super().unlink()
        if area:
            notify_data_changed(self.env, area)
        return result


def _patch_live_refresh(model_class, area):
    """Wrap create/write/unlink of an optional model, as base_automation does."""
    if model_class.__dict__.get("_strategic_live_refresh_patched"):
        return

    @api.model_create_multi
    def create(self, vals_list, **kwargs):
        records = create.origin(self, vals_list, **kwargs)
        notify_data_changed(self.env, area)
        return records

    def write(self, vals, **kwargs):
        result = write.origin(self, vals, **kwargs)
        notify_data_changed(self.env, area)
        return result

    def unlink(self, **kwargs):
        result = unlink.origin(self, **kwargs)
        notify_data_changed(self.env, area)
        return result

    for name, method in (("create", create), ("write", write), ("unlink", unlink)):
        method.origin = getattr(model_class, name)
        setattr(model_class, name, method)
    model_class._strategic_live_refresh_patched = True


class PurchaseOrder(models.Model):
    _name = "purchase.order"
    _inherit = ["purchase.order", "strategic.live.refresh.mixin"]

    def _live_refresh_area(self, vals=None):
        return "purchase"


class PurchaseOrderLine(models.Model):
    _name = "purchase.order.line"
    _inherit = ["purchase.order.line", "strategic.live.refresh.mixin"]

    def _live_refresh_area(self, vals=None):
        return "purchase"


class PosOrder(models.Model):
    _name = "pos.order"
    _inherit = ["pos.order", "strategic.live.refresh.mixin"]

    def _live_refresh_area(self, vals=None):
        return "pos"


class StockMove(models.Model):
    _name = "stock.move"
    _inherit = ["stock.move", "strategic.live.refresh.mixin"]

    def _live_refresh_area(self, vals=None):
        # Receipt progress drives purchasing figures; any completed or cancelled
        # move changes stock, waste or variance figures.
        if vals is not None and "state" not in vals and "date" not in vals:
            return "purchase" if self.filtered("purchase_line_id") else False
        return "purchase" if self.filtered("purchase_line_id") else "inventory"


class StockQuant(models.Model):
    _name = "stock.quant"
    _inherit = ["stock.quant", "strategic.live.refresh.mixin"]

    def _live_refresh_area(self, vals=None):
        # Counted quantities drive the stock-variance figures.
        if vals is not None and not {"inventory_quantity", "inventory_quantity_set"} & set(vals):
            return False
        return "inventory"


class StockWarehouseOrderpoint(models.Model):
    _name = "stock.warehouse.orderpoint"
    _inherit = ["stock.warehouse.orderpoint", "strategic.live.refresh.mixin"]

    def _live_refresh_area(self, vals=None):
        # Replenishment rules define low-stock status.
        return "inventory"


class StockLot(models.Model):
    _name = "stock.lot"
    _inherit = ["stock.lot", "strategic.live.refresh.mixin"]

    def _live_refresh_area(self, vals=None):
        # Lot expiry dates drive the expiring-stock figures.
        return "inventory"


class AccountMove(models.Model):
    _name = "account.move"
    _inherit = ["account.move", "strategic.live.refresh.mixin"]

    def _live_refresh_area(self, vals=None):
        # Posting or resetting entries changes Net Profit and expenses.
        return "accounting" if vals is not None and "state" in vals else False


class MrpBom(models.Model):
    _name = "mrp.bom"
    _inherit = ["mrp.bom", "strategic.live.refresh.mixin"]

    def _live_refresh_area(self, vals=None):
        # Recipes drive the estimated POS ingredient consumption.
        return "inventory"


class MrpBomLine(models.Model):
    _name = "mrp.bom.line"
    _inherit = ["mrp.bom.line", "strategic.live.refresh.mixin"]

    def _live_refresh_area(self, vals=None):
        return "inventory"


class StockPicking(models.Model):
    _name = "stock.picking"
    _inherit = ["stock.picking", "strategic.live.refresh.mixin"]

    def _live_refresh_area(self, vals=None):
        # Rescheduling or completing a supplier receipt changes delivery figures.
        if self.filtered(lambda picking: picking.picking_type_code == "incoming"):
            return "purchase"
        return False


class StrategicKpiEvaluation(models.Model):
    _name = "strategic.kpi.evaluation"
    _inherit = ["strategic.kpi.evaluation", "strategic.live.refresh.mixin"]

    def _live_refresh_area(self, vals=None):
        return "strategic"


class StrategicKpiTarget(models.Model):
    _name = "strategic.kpi.target"
    _inherit = ["strategic.kpi.target", "strategic.live.refresh.mixin"]

    def _live_refresh_area(self, vals=None):
        return "strategic"


class StrategicObjective(models.Model):
    _name = "strategic.objective"
    _inherit = ["strategic.objective", "strategic.live.refresh.mixin"]

    def _live_refresh_area(self, vals=None):
        return "strategic"


class MailActivity(models.Model):
    _name = "mail.activity"
    _inherit = ["mail.activity", "strategic.live.refresh.mixin"]

    def _live_refresh_area(self, vals=None):
        # Follow-up actions are attention-item action statuses and People staff tasks.
        return "strategic" if set(self.mapped("res_model")) & _ACTIVITY_MODELS else "people"


class ResUsers(models.Model):
    _name = "res.users"
    _inherit = ["res.users", "strategic.live.refresh.mixin"]

    def _live_refresh_area(self, vals=None):
        # Branch assignment and role changes alter People staffing figures.
        if vals is not None and not {"strategic_branch_warehouse_id", "active", "group_ids", "name"} & set(vals):
            return False
        return "people"


class StockWarehouse(models.Model):
    _name = "stock.warehouse"
    _inherit = ["stock.warehouse", "strategic.live.refresh.mixin"]

    def _live_refresh_area(self, vals=None):
        return "people" if vals is None or "strategic_planned_headcount" in vals or "name" in vals else False


class ResourceCalendarLeaves(models.Model):
    _name = "resource.calendar.leaves"
    _inherit = ["resource.calendar.leaves", "strategic.live.refresh.mixin"]

    def _live_refresh_area(self, vals=None):
        # Holidays and time off change which days count as absences.
        return "people"
