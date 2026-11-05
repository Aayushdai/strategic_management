/** @odoo-module **/

// Employee Management: employees, staffing requests, performance, department assignment.
// These functions become methods of StrategicNavigationShell (see navigation_shell.js); this page's
// template is employees.xml and its styles employees.scss, next to this file.

import { user } from "@web/core/user";
import { TASK_MODEL } from "../../constants";

export const employeesPage = {
    // ------------------------------------------------------------ Employee Management: read-only workload view
    async loadEmployeeManagement({ silent = false } = {}) {
        if (!(this.isBranchManager || this.isHeadOffice) || this.branchError) return;
        if (!silent) this.state.employeesLoading = true;
        try {
            this.state.employees = await this.orm.silent.call(TASK_MODEL, "get_employee_management", []);
            this.state.employeesError = "";
        } catch (error) {
            if (!silent) {
                this.state.employees = null;
                this.state.employeesError = error.data?.message || error.message || "Employees could not be loaded.";
            }
        } finally {
            this.state.employeesLoading = false;
        }
    },

    /** Filtered on the page: the server has already limited these rows to what this user may see. */
    matchesDepartment(person) {
        const selected = this.state.employeeDepartment;
        return !selected || (person.department_ids || []).includes(Number(selected));
    },

    get filteredEmployees() {
        return (this.state.employees?.employees || []).filter((person) => this.matchesDepartment(person));
    },

    get filteredWithoutLogin() {
        return (this.state.employees?.without_login || []).filter((person) => this.matchesDepartment(person));
    },

    onEmployeeDepartment(event) {
        this.state.employeeDepartment = event.target.value;
        this.setPage("employee_management", 1);
    },

    async openEmployee(employee, { silent = false } = {}) {
        this.loadEmployeePerformance(employee.id);
        if (!silent) {
            this.state.employeeDetail = { employee, current: null, history: null };
            this.state.employeeDetailError = "";
            this.state.employeeDetailLoading = true;
        }
        try {
            this.state.employeeDetail = await this.orm.silent.call(TASK_MODEL, "get_employee_tasks", [employee.id]);
        } catch (error) {
            if (!silent) this.state.employeeDetailError = error.data?.message || error.message || "This employee's tasks could not be loaded.";
        } finally {
            this.state.employeeDetailLoading = false;
        }
    },

    closeEmployee() {
        this.state.employeeDetail = null;
        this.state.employeeDetailError = "";
        return this.loadEmployeeManagement({ silent: true });
    },

    refreshEmployeeManagement() {
        const detail = this.state.employeeDetail;
        return detail && detail.current ? this.openEmployee(detail.employee, { silent: true }) : this.loadEmployeeManagement({ silent: true });
    },

    /** Task Management stays the place to act on tasks: open it narrowed to this employee. */
    async viewEmployeeTasks(employee) {
        this.state.activeSection = "task_management";
        this.state.taskEmployee = { id: employee.id, name: employee.name };
        this.state.taskFilter = "all";
        this.state.pages.task_management = 0;
        await this.loadTaskManagement();
    },

    // ------------------------------------------------------------ staffing requests and performance
    async loadStaffing() {
        try {
            const filters = this.state.staffingFilters;
            this.state.staffing = await this.orm.silent.call("strategic.staffing.request", "get_staffing_requests", [{
                warehouse_id: filters.warehouse_id ? Number(filters.warehouse_id) : false, position: filters.position, state: filters.state }]);
            this.state.staffingError = "";
        } catch (error) {
            this.state.staffing = null;
            this.state.staffingError = error.data?.message || error.message || "Staffing requests could not be loaded.";
        }
    },

    onStaffingFilter(field, event) {
        this.state.staffingFilters[field] = event.target.value;
        return this.loadStaffing();
    },

    async openStaffingForm() {
        if (!this.state.staffing) await this.loadStaffing();
        this.state.staffingForm = { department_id: "", position: "", quantity: 1, reason: "", expected_date: "", comments: "" };
    },

    /** Run one server step, then refresh; nothing changes on screen before the server confirms. Returns whether it worked. */
    async staffingCall(method, args, done) {
        if (this.state.staffingBusy) return false;
        this.state.staffingBusy = true;
        try {
            const result = await this.orm.call("strategic.staffing.request", method, args);
            if (done) done(result);
            await this.loadStaffing();
            if (this.state.activeSection === "employee_management") await this.loadEmployeeManagement({ silent: true });
            if (this.state.activeSection === "overview") await this.loadPendingStaffing();
            return true;
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "This could not be saved.", { type: "danger" });
            return false;
        } finally {
            this.state.staffingBusy = false;
        }
    },

    async saveStaffingForm(submit) {
        if (await this.staffingCall("save_staffing_request", [{ ...this.state.staffingForm }, submit])) {
            this.state.staffingForm = null;
            this.notification.add(submit ? "Request sent to Head Office." : "Draft saved.", { type: "success" });
        }
    },

    submitStaffing(row) {
        return this.staffingCall("action_submit", [[row.id]]);
    },

    approveStaffing(row) {
        return this.staffingCall("action_decide", [[row.id], "approve"]);
    },

    async confirmReject() {
        const decision = this.state.staffingDecision;
        if (await this.staffingCall("action_decide", [[decision.row.id], "reject", decision.reason])) this.state.staffingDecision = null;
    },

    /** From a request's card (row) or from Add Employee (choose the request). The server re-checks all of it. */
    async openRegister(row = null) {
        const options = await this.orm.silent.call("strategic.staffing.request", "get_staffing_requests", [{ state: "approved" }]);
        this.state.staffingRegister = { options, request_id: "", warehouse_id: "", name: "", work_email: "", work_phone: "", job: "", department_id: "", parent_id: "" };
        if (row) this.selectRegisterRequest(String(row.id));
    },

    get registerRequests() {
        const form = this.state.staffingRegister;
        return form.options.requests.filter((request) => request.remaining && (!form.warehouse_id || String(request.branch_id) === form.warehouse_id));
    },

    selectRegisterRequest(requestId) {
        const form = this.state.staffingRegister;
        const request = form.options.requests.find((row) => String(row.id) === requestId);
        Object.assign(form, { request_id: requestId, warehouse_id: request ? String(request.branch_id) : form.warehouse_id, job: request ? request.position : "", department_id: request ? String(request.department_id) : "" });
    },

    selectRegisterBranch(branchId) {
        const form = this.state.staffingRegister;
        form.warehouse_id = branchId;
        if (!this.registerRequests.some((row) => String(row.id) === form.request_id)) this.selectRegisterRequest("");
    },

    async saveRegister() {
        const form = this.state.staffingRegister;
        const values = { name: form.name, work_email: form.work_email, work_phone: form.work_phone, job: form.job, department_id: form.department_id ? Number(form.department_id) : false,
                         warehouse_id: Number(form.warehouse_id), parent_id: form.parent_id ? Number(form.parent_id) : false };
        if (await this.staffingCall("register_employee", [[Number(form.request_id)], values])) {
            this.state.staffingRegister = null;
            this.notification.add("Employee registered.", { type: "success" });
        }
    },

    createLogin(employee, resend = false) {
        return this.staffingCall(resend ? "resend_employee_invitation" : "create_employee_login", [employee.id], (result) => {
            this.notification.add(result.invitation === "sent" ? `Invitation handed to the mail server for ${result.login}.` : `Login ready, but the invitation was not sent: ${result.error}`,
                                  { type: result.invitation === "sent" ? "success" : "warning", sticky: result.invitation !== "sent" });
        });
    },

    async loadEmployeePerformance(userId) {
        try {
            this.state.employeePerformance = await this.orm.silent.call("strategic.performance.dashboard", "get_employee_performance",
                [userId, this.state.dateStart || false, this.state.dateEnd || false]);
            this.state.employeePerformanceError = "";
        } catch (error) {
            this.state.employeePerformance = null;
            this.state.employeePerformanceError = error.data?.message || error.message || "Performance could not be loaded.";
        }
    },

    get assessmentCriteria() {
        return this.state.employeePerformance?.criteria || [];
    },

    openAssessment() {
        this.state.assessmentForm = { work_quality: "", teamwork: "", communication: "", responsibility: "", problem_solving: "", customer_service: "", notes: "" };
    },

    async saveAssessment() {
        try {
            this.state.employeePerformance = await this.orm.call("strategic.performance.dashboard", "save_employee_assessment",
                [this.state.employeePerformance.employee.id, { ...this.state.assessmentForm }, this.state.dateStart || false, this.state.dateEnd || false]);
            this.state.assessmentForm = null;
            this.notification.add("Assessment saved.", { type: "success" });
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "The assessment could not be saved.", { type: "danger" });
        }
    },

    async loadPerformanceOverview() {
        try {
            this.state.performanceOverview = await this.orm.silent.call("strategic.performance.dashboard", "get_performance_overview", [this.state.dateStart || false, this.state.dateEnd || false]);
            this.state.performanceOverviewError = "";
        } catch (error) {
            this.state.performanceOverview = null;
            this.state.performanceOverviewError = error.data?.message || error.message || "Performance could not be loaded.";
        }
    },

    formatRate(value) {
        return value === false || value === null || value === undefined ? "No completed tasks" : `${value}%`;
    },

    // ------------------------------------------------------------ department assignment
    async loadDepartmentAssignments() {
        try {
            this.state.departmentAssignments = await this.orm.silent.call("strategic.performance.dashboard", "get_department_assignments", []);
            this.state.departmentAssignmentsError = "";
        } catch (error) {
            this.state.departmentAssignments = null;
            this.state.departmentAssignmentsError = error.data?.message || error.message || "Department assignments could not be loaded.";
        }
    },

    get departmentChoices() {
        return (this.state.activeSection === "employee_management" ? this.state.employees : this.state.departmentAssignments)?.departments || [];
    },

    get departmentRows() {
        const search = this.state.departmentSearch.trim().toLowerCase();
        const rows = this.state.departmentAssignments?.employees || [];
        return search ? rows.filter((row) => row.name.toLowerCase().includes(search) || (row.branch || "").toLowerCase().includes(search)) : rows;
    },

    openDepartmentForm(row) {
        this.state.departmentForm = { row, departmentIds: [...row.department_ids] };
    },

    closeDepartmentForm() {
        if (!this.state.departmentSaving) this.state.departmentForm = null;
    },

    toggleDepartment(id, event) {
        const form = this.state.departmentForm;
        form.departmentIds = event.target.checked ? [...form.departmentIds, id] : form.departmentIds.filter((value) => value !== id);
    },

    async saveDepartments() {
        const form = this.state.departmentForm;
        if (!form || this.state.departmentSaving) return;
        this.state.departmentSaving = true;
        try {
            await this.orm.call("strategic.performance.dashboard", "set_employee_departments", [form.row.id, form.departmentIds]);
            this.notification.add(`Departments saved for ${form.row.name}.`, { type: "success" });
            this.state.departmentForm = null;
            await (this.state.activeSection === "employee_management" ? this.loadEmployeeManagement({ silent: true }) : this.loadDepartmentAssignments());
        } catch (error) {
            this.notification.add(error.data?.message || error.message || "The departments could not be saved.", { type: "danger" });
        } finally {
            this.state.departmentSaving = false;
        }
    },
};
