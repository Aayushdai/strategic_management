"""Read-only app dependency overview for Strategic Management."""
from odoo import api, models
from odoo.modules.module import get_module_icon

_GENERIC_ICON = "/base/static/description/icon.png"


# These are the Odoo modules directly used by Strategic Management's data sources.
# "required" means part of the module's core dependency chain; optional modules
# add data/features when installed. Installation state is always read from Odoo.
_APP_DEPENDENCIES = [
    {
        "id": "inventory", "name": "Inventory", "modules": [("stock", "Inventory")],
        "required": True, "icon": "inventory_2",
        "purpose": "Stock by branch, receipts and transfers, replenishment, scrap, tracked lots and physical counts.",
        "setup": "Low-stock alerts need reorder rules. Expiry needs tracked lots with expiration dates. Waste cost needs supported scrap categories and valuation. Count variance needs a recorded physical count.",
    },
    {
        "id": "purchasing", "name": "Purchase & Supplier Deliveries", "modules": [("purchase", "Purchase"), ("purchase_stock", "Purchase–Inventory integration")],
        "required": True, "icon": "shopping_cart",
        "purpose": "Purchase quantities, purchasing spend, incoming receipts and supplier on-time delivery measures.",
        "setup": "Delivery measures need confirmed purchase orders with promised dates and validated receipts. Draft receipts do not count as received stock.",
    },
    {
        "id": "pos", "name": "Point of Sale", "modules": [("point_of_sale", "Point of Sale"), ("pos_stock", "POS–Inventory integration")],
        "required": True, "icon": "credit_card",
        "purpose": "Completed café sales, order counts, sales by product and branch, and POS lines used for recipe estimates.",
        "setup": "Map POS registers to the correct branch warehouse and record completed orders in the reporting period.",
    },
    {
        "id": "recipes", "name": "Manufacturing & Recipes (BoMs)", "modules": [("mrp", "Manufacturing / Bills of Materials")],
        "required": True, "icon": "coffee",
        "purpose": "Recipe and ingredient mappings used to estimate ingredient consumption from completed POS sales.",
        "setup": "Maintain accurate BoMs, ingredient quantities and units. Recipe estimates are not proof of actual consumption; POS stock deduction may need the POS Recipe Stock Integration app below.",
    },
    {
        "id": "accounting", "name": "Accounting & Financial Results", "modules": [("account", "Invoicing / Accounting")],
        "required": True, "icon": "payments",
        "purpose": "Posted income and expense entries for supported financial KPIs such as net profit and margin.",
        "setup": "Profitability needs a configured chart of accounts and posted journal entries. Missing financial data must remain unavailable, not be treated as zero.",
    },
    {
        "id": "activities", "name": "Activities & Live Updates", "modules": [("mail", "Activities / Messaging"), ("bus", "Live update service")],
        "required": True, "icon": "notifications",
        "purpose": "Assigned follow-up activities, reminders and live refresh notifications after supported business changes.",
        "setup": "Follow-ups need an owner and due date. Live refresh also needs the Odoo bus service and browser connection.",
    },
    {
        "id": "employees", "name": "Employees", "modules": [("hr", "Employees")],
        "required": True, "icon": "group",
        "purpose": "Employee records, employee-to-user links, branch staffing and employee-scoped KPI context.",
        "setup": "Create employee records where needed, link them to the correct user, and assign each user to the correct branch.",
    },
    {
        "id": "attendance", "name": "Attendances", "modules": [("hr_attendance", "Attendances")],
        "required": True, "icon": "schedule",
        "purpose": "Clock-in and clock-out records for attendance exceptions and punctuality analysis.",
        "setup": "Requires Employees, work schedules and recorded attendance entries. No source data is not the same as zero exceptions.",
    },
    {
        "id": "planning", "name": "Planning & Shifts", "modules": [("planning", "Planning")],
        "required": False, "icon": "calendar_today",
        "purpose": "Planned shifts that add context to staffing and workforce coverage.",
        "setup": "Create shift plans and assign employees before relying on shift-based figures.",
    },
    {
        "id": "expiry", "name": "Expiration Tracking", "modules": [("product_expiry", "Product Expiration Dates")],
        "required": False, "icon": "event_busy",
        "purpose": "Lot expiration information used by the Expiring Stock panel.",
        "setup": "Enable lot tracking and expiration dates for the relevant products, then receive positive stock into a lot with an expiration date.",
    },
    {
        "id": "valuation", "name": "Inventory Valuation", "modules": [("stock_account", "Inventory Valuation / Accounting integration")],
        "required": False, "icon": "calculate",
        "purpose": "Stock valuation evidence used to calculate inventory and scrap cost where supported.",
        "setup": "Configure a valid costing and valuation method. Unvalued scrap must not be shown as a verified monetary cost.",
    },
    {
        "id": "pos_recipe_stock", "name": "POS Recipe Stock Deduction", "modules": [("pos_mrp", "POS–Manufacturing integration")],
        "required": False, "icon": "restaurant",
        "purpose": "Connects POS sales with kit-style recipes so ingredient stock can be deducted in the POS workflow.",
        "setup": "Requires POS, Manufacturing, supported Kit BoMs and matching recipes. Without it, recipe consumption may be an estimate without corresponding ingredient issue moves.",
    },
]


class StrategicPerformanceDashboardAppDependencies(models.AbstractModel):
    _inherit = "strategic.performance.dashboard"

    @api.model
    def get_app_dependencies_data(self):
        """Read module status and business purpose. This method never installs modules."""
        names = sorted({module for app in _APP_DEPENDENCIES for module, _label in app["modules"]})
        rows = self.env["ir.module.module"].sudo().search_read(
            [("name", "in", names)], ["name", "state"], limit=len(names) + 20,
        )
        states = {row["name"]: row.get("state") or "uninstalled" for row in rows}
        apps = []
        for definition in _APP_DEPENDENCIES:
            modules = []
            for technical_name, label in definition["modules"]:
                state = states.get(technical_name, "uninstalled")
                state_label = {
                    "installed": "Installed",
                    "to install": "Installation pending",
                    "to upgrade": "Upgrade pending",
                    "to remove": "Removal pending",
                }.get(state, "Not installed")
                modules.append({
                    "name": technical_name, "label": label, "state": state,
                    "state_label": state_label, "installed": state == "installed",
                })
            installed = all(module["installed"] for module in modules)
            apps.append({
                "id": definition["id"], "name": definition["name"], "icon": definition["icon"],
                # Official app icon of the first checked module that ships one; the UI falls back to "icon".
                "icon_url": next((url for url in (get_module_icon(name) for name, _label in definition["modules"]) if url != _GENERIC_ICON), False),
                "required": definition["required"],
                "requirement_label": "Core dependency" if definition["required"] else "Optional feature",
                "installed": installed, "status_label": "Installed" if installed else "Not installed",
                "purpose": definition["purpose"], "setup": definition["setup"], "modules": modules,
            })
        return {
            "apps": apps,
            "summary": {
                "installed_count": sum(1 for app in apps if app["installed"]),
                "not_installed_count": sum(1 for app in apps if not app["installed"]),
                "total_count": len(apps),
            },
            "read_only": True,
        }
