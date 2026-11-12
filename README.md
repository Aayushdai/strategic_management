# strategic_management

Strategic performance management for a multi-branch café business on Odoo 20: business goals, objectives, OKRs and
KPIs, plus the Strategic Management screen Head Office, Branch Managers and Employees work in.

## Where to find things

### The Strategic Management screen (`static/src/navigation_shell/`)

One Odoo client action with a sidebar. Each sidebar page has its own folder holding its three files:

```
navigation_shell/
  navigation_shell.js     sidebar, page switching, period and branch filters, live refresh, helpers several pages share
  navigation_shell.xml    the page frame and the switch that shows one page
  navigation_shell.scss   shared look, plus a few page rules whose order against shared rules matters
  constants.js            fixed lists: sidebar layout, page links, colours, page size
  pages/<page>/<page>.js    that page's functions (loading its data, its buttons)
  pages/<page>/<page>.xml   that page's template
  pages/<page>/<page>.scss  that page's styles
```

| Page in the app | Folder | Server data (method, file in `models/`) |
| --- | --- | --- |
| Inbox (Head Office) | `pages/inbox` | `get_objective_requests`, `decide_objective_request` (objective_requests.py); `get_staffing_requests` (staffing.py) |
| Sales | `pages/sales` | `get_business_overview_data` (business_overview.py), loaded by the shell's `loadOverview` |
| Purchasing | `pages/purchasing` | `get_purchasing_overview_data` (business_overview_purchasing.py) |
| Inventory | `pages/inventory` | `get_inventory_waste_data` (business_overview_inventory.py) |
| Branches, Branch Performance | `pages/branches` | `get_branches_overview_data` (branch_overview.py); branch operations panel (branch_operations.py) |
| People | `pages/people` | `get_people_overview_data`, `get_role_assignments`, `set_user_role` (people_overview.py) |
| Employee Management and its tabs | `pages/employees` | staffing.py, department_access.py |
| My Tasks, Task Management, Tasks & Issues | `pages/tasks` | branch_task.py, overview_extras.py |
| Reports, Settings | `pages/reports` | branch_report.py |
| App Dependencies | `pages/app_dependencies` | app_dependencies.py |
| Branch Manager dashboard | `pages/branch_manager` | branch_objectives.py, branch_overview.py |
| Employee dashboard, My Performance, department pages | `pages/employee_home` | department_access.py, staffing.py |

How a page fits together: `navigation_shell.js` imports every page's functions and adds them to the one screen
component, so inside any page file `this` is the whole screen (its `state`, `orm`, and every other page's functions).
A function name defined twice stops the screen with an error naming it, so nothing is silently replaced.

### Other screens

- `static/src/business_goals/`: Business Goals (goal list, setup drawer). Server: goal_setup.py, goal_setup_monitoring.py,
  goal_setup_payload.py, goal_templates.py.
- `static/src/kpi_dashboards/`: the original KPI dashboards behind the hidden Strategy & KPIs menu. Server: dashboard.py.

### Server side

- `models/`: one file per feature; the first line of each file says what it covers. Several files extend the same
  model (`_inherit`); for example every page's data lives on `strategic.performance.dashboard`.
- `models/goal_setup.py` (setup and approval workflow), `goal_setup_monitoring.py` (goals that monitor without a target),
  `goal_setup_payload.py` (what the Business Goals screen reads).
- `services/kpi_sources/`: how each KPI value is measured from POS, purchasing, inventory and accounting records.
- `security/`: roles (`strategic_management_security.xml`) and access rights (`ir.access.csv`).
- `views/`: Odoo forms, lists, menus and the client actions that open the screens above.
- `tests/`: automated tests; `tests/features/` describes the expected behaviour in plain language.

## Tracing a problem

1. Find the page's folder in the table above and open its `.xml` to find the button or figure.
2. The `t-on-click` or value names a function: search for it in the page's `.js` (or `navigation_shell.js` if shared).
3. That function calls the server with `this.orm.call("<model>", "<method>", ...)`: search `def <method>` in `models/`.
