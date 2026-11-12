/** @odoo-module **/

// "← <previous screen>" for the module's own full-page screens (Business Goals, dashboards), which have no
// Odoo breadcrumb bar. Uses Odoo's own history, so it returns to whatever screen opened this one; hidden when this
// screen was opened first (nothing to go back to).

import { Component, proxy } from "@odoo/owl";

export class BackButton extends Component {
    static template = "strategic_management.BackButton";
    static props = {};

    setup() {
        // Subscribed like Odoo's control panel: the trail is completed after this screen's first render.
        this.breadcrumbs = this.env.config?.breadcrumbs ? proxy(this.env.config.breadcrumbs) : [];
    }

    get previous() {
        const crumbs = this.breadcrumbs.filter(Boolean);
        return crumbs.length > 1 ? crumbs.at(-2) : null;
    }

    back() {
        this.env.config.historyBack();
    }
}
