/** @odoo-module **/

// Settings (Head Office): the switches and numbers that change how the app behaves, and the company details printed
// reports show. These functions become methods of StrategicNavigationShell (see navigation_shell.js); template
// settings.xml. Server: models/settings.py (every change is written to the admin audit log).

export const settingsPage = {
    async loadSettings() {
        this.state.settingsError = "";
        try {
            const data = await this.orm.call("strategic.performance.dashboard", "get_strategic_settings", []);
            this.applySettings(data);
        } catch (error) {
            this.state.settingsError = error.data?.message || error.message || "Settings could not be loaded.";
        }
    },

    applySettings(data) {
        this.state.settings = { ...data, draft: { ...data.values }, profile: { ...data.company }, saving: false };
    },

    setSettingsTab(tab) {
        this.state.settingsTab = tab;
        if (tab === "settings" && !this.state.settings) this.loadSettings();
    },

    settingsDirty() {
        const s = this.state.settings;
        return Boolean(s) && Object.keys(s.values).some((key) => String(s.draft[key]) !== String(s.values[key]));
    },

    async saveSettings() {
        const s = this.state.settings;
        if (!s || s.saving) return;
        s.saving = true;
        try {
            this.applySettings(await this.orm.call("strategic.performance.dashboard", "save_strategic_settings", [s.draft]));
            this.notification.add("Settings saved.", { type: "success" });
            // The app's own switches (live refresh, proposals) take effect now.
            this.state.navContext = await this.orm.call("strategic.performance.dashboard", "get_navigation_context", []);
        } catch (error) {
            s.saving = false;
            this.notification.add(error.data?.message || error.message || "Settings could not be saved.", { type: "danger" });
        }
    },

    async saveCompanyProfile() {
        const s = this.state.settings;
        if (!s || s.saving) return;
        s.saving = true;
        try {
            this.applySettings(await this.orm.call("strategic.performance.dashboard", "save_company_profile", [s.profile]));
            this.notification.add("Company details saved.", { type: "success" });
        } catch (error) {
            s.saving = false;
            this.notification.add(error.data?.message || error.message || "Company details could not be saved.", { type: "danger" });
        }
    },

    onLogoPicked(event) {
        const file = event.target.files?.[0];
        if (!file) return;
        if (!file.type.startsWith("image/") || file.size > 2 * 1024 * 1024) {
            this.notification.add("Choose an image file of at most 2 MB.", { type: "warning" });
            return;
        }
        const reader = new FileReader();
        reader.onload = () => {
            this.state.settings.profile.logo = String(reader.result).split(",")[1];
            this.state.settings.profile.logoPreview = reader.result;
        };
        reader.readAsDataURL(file);
    },

    removeLogo() {
        Object.assign(this.state.settings.profile, { logo: false, logoPreview: false, has_logo: false });
    },

    nextRunLabel() {
        const value = this.state.settings?.next_run;
        if (!value) return "";
        const date = new Date(value.replace(" ", "T") + "Z");
        return date.toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
    },
};
