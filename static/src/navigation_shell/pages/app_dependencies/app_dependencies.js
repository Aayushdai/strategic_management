/** @odoo-module **/

// App Dependencies.
// These functions become methods of StrategicNavigationShell (see navigation_shell.js); this page's
// template is app_dependencies.xml and its styles app_dependencies.scss, next to this file.

export const appDependenciesPage = {
    async loadAppDependencies({ silent = false } = {}) {
        if (!silent) {
            this.state.appDependenciesLoading = true;
            this.state.appDependenciesError = "";
        }
        try {
            this.state.appDependenciesData = await this.orm.silent.call(
                "strategic.performance.dashboard",
                "get_app_dependencies_data",
                []
            );
            this.state.appDependenciesError = "";
        } catch (error) {
            if (silent) {
                console.warn("App dependency status could not be refreshed", error);
                return;
            }
            this.state.appDependenciesError = error.message || "App dependency status could not be loaded.";
        } finally {
            if (!silent) {
                this.state.appDependenciesLoading = false;
            }
        }
    },

    toggleAppDependency(id) {
        this.state.expandedDependencyId = this.state.expandedDependencyId === id ? "" : id;
    },
};
