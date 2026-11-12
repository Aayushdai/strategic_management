/** @odoo-module **/

// "← Back" in Odoo's own header bar (lists and forms) while inside the Strategic Management app, next to the
// breadcrumbs: the lists its cards and charts open, and the records opened from them. Other apps are left as they are.

import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { ControlPanel } from "@web/search/control_panel/control_panel";

const APP = "strategic_management.menu_strategic_management_root";

patch(ControlPanel.prototype, {
    setup() {
        super.setup(...arguments);
        this.smMenu = useService("menu");
    },

    get smShowBack() {
        if (this.uiService.isSmall || this.env.config.noBreadcrumbs) return false; // phones already get Odoo's back arrow
        return this.smMenu.getCurrentApp()?.xmlid === APP && (this.breadcrumbs || []).filter(Boolean).length > 1;
    },

    smBack() {
        this.env.config.historyBack();
    },
});
