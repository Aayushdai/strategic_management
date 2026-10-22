"""People dashboard: real staffing, attendance and task data with role scoping."""

from datetime import datetime, timedelta

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestPeopleDashboard(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.sudo().write({"tz": "UTC"})
        ref = cls.env.ref
        Warehouse = cls.env["stock.warehouse"]
        cls.WarehouseA = Warehouse.create({"name": "People Branch A", "code": "PPA", "company_id": cls.company.id})
        cls.WarehouseB = Warehouse.create({"name": "People Branch B", "code": "PPB", "company_id": cls.company.id})
        cls.Head = cls._user("people_head", ref("strategic_management.group_strategic_head_office_manager"))
        cls.ManagerA = cls._user("people_manager_a", ref("strategic_management.group_strategic_branch_manager"), cls.WarehouseA)
        cls.StaffA = cls._user("people_staff_a", ref("strategic_management.group_strategic_employee"), cls.WarehouseA)
        cls.StaffB = cls._user("people_staff_b", ref("strategic_management.group_strategic_employee"), cls.WarehouseB)
        cls.today = fields.Date.context_today(cls.env.user)

    @classmethod
    def _user(cls, login, group, warehouse=False):
        return cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": login.replace("_", " ").title(), "login": login, "email": "%s@example.com" % login,
            "company_id": cls.company.id, "company_ids": [(6, 0, [cls.company.id])],
            "group_ids": [(6, 0, [cls.env.ref("base.group_user").id, group.id])],
            "strategic_branch_warehouse_id": warehouse.id if warehouse else False,
        })

    def _people(self, user, warehouse=False):
        return self.env["strategic.performance.dashboard"].with_user(user).get_people_overview_data(
            fields.Date.to_string(self.today - timedelta(days=13)), fields.Date.to_string(self.today),
            warehouse.id if warehouse else False,
        )

    def _staffing(self, data, warehouse):
        return next(row for row in data["staffing"] if row["warehouse_id"] == warehouse.id)

    def _activity(self, user, deadline, summary):
        return self.env["mail.activity"].create({
            "res_model_id": self.env["ir.model"]._get_id("res.partner"), "res_id": user.partner_id.id,
            "activity_type_id": self.env.ref("mail.mail_activity_data_todo").id,
            "summary": summary, "user_id": user.id, "date_deadline": deadline,
        })

    def test_roles_limit_people_data(self):
        self._activity(self.StaffB, self.today - timedelta(days=2), "Branch B only follow-up")
        head = self._people(self.Head)
        self.assertEqual(head["role"], "head_office")
        self.assertTrue({self.WarehouseA.id, self.WarehouseB.id} <= {row["warehouse_id"] for row in head["staffing"]})

        manager = self._people(self.ManagerA, self.WarehouseB)
        self.assertEqual(manager["role"], "branch_manager")
        self.assertEqual([row["warehouse_id"] for row in manager["staffing"]], [self.WarehouseA.id])
        self.assertNotIn("Branch B only follow-up", [row["task"] for row in manager["actions"]])
        self.assertFalse([row for row in manager["attention"] if row["branch"] == self.WarehouseB.display_name])

        employee = self._people(self.StaffA)
        self.assertEqual(employee["role"], "employee")
        self.assertFalse(employee["staffing"])
        self.assertFalse(employee["branch_options"])
        self.assertEqual({row["assigned"] for row in employee["actions"]} - {self.StaffA.name}, set())

        with self.assertRaises(AccessError):
            self.env["strategic.performance.dashboard"].with_user(self.Head).get_people_overview_data(False, False, 999999999)

    def test_coverage_requires_configured_planned_staff(self):
        row = self._staffing(self._people(self.Head, self.WarehouseA), self.WarehouseA)
        self.assertEqual(row["status"], "not_configured")
        self.assertIs(row["gap"], False)

        self.WarehouseA.strategic_planned_headcount = 6
        data = self._people(self.Head, self.WarehouseA)
        row = self._staffing(data, self.WarehouseA)
        self.assertEqual(row["status"], "understaffed")
        self.assertEqual(row["gap"], 6 - row["actual"])
        self.assertEqual(data["summary"]["branches_needing_coverage"], 1)
        item = next(item for item in data["attention"] if item["kind"] == "understaffed")
        self.assertIn("Manager A", item["responsible"])
        self.assertTrue(any("short of its planned 6" in text for text in data["insights"]))

    def test_overdue_staff_tasks_are_counted_and_attributed(self):
        self._activity(self.StaffA, self.today - timedelta(days=10), "Complete hygiene training")
        self._activity(self.StaffA, self.today + timedelta(days=3), "Prepare rota")
        data = self._people(self.ManagerA)
        self.assertEqual(data["summary"]["overdue_tasks"], 1)
        statuses = {row["task"]: row["status"] for row in data["actions"]}
        self.assertEqual(statuses["Complete hygiene training"], "Overdue")
        self.assertEqual(statuses["Prepare rota"], "Planned")
        item = next(item for item in data["attention"] if item["kind"] == "overdue")
        self.assertEqual(item["severity"], "critical", "More than 7 days overdue is critical.")
        self.assertEqual(item["responsible"], self.StaffA.name)

    def test_missing_attendance_app_is_reported_not_fabricated(self):
        if "hr.attendance" in self.env.registry.models:
            self.skipTest("Attendances app installed.")
        data = self._people(self.Head)
        self.assertIs(data["summary"]["attendance_exceptions"], False)
        self.assertFalse(data["attendance_exceptions"])
        self.assertTrue(any("Attendance: Not Configured" in message for message in data["messages"]))

    def test_late_arrivals_and_absences_follow_working_schedule(self):
        if "hr.attendance" not in self.env.registry.models:
            self.skipTest("Attendances app not installed.")
        calendar = self.env["resource.calendar"].create({
            "name": "People test 8-17", "company_id": self.company.id,
            "attendance_ids": [(0, 0, {"dayofweek": str(day), "hour_from": hour_from, "hour_to": hour_to, "day_period": period})
                               for day in range(7) for hour_from, hour_to, period in ((8, 12, "morning"), (13, 17, "afternoon"))],
        })
        employee = self.env["hr.employee"].create({
            "name": "Late Larry", "user_id": self.StaffA.id, "company_id": self.company.id, "resource_calendar_id": calendar.id,
        })
        employee.resource_id.tz = "UTC"
        self.env.cr.execute("UPDATE hr_employee SET create_date = %s WHERE id = %s", (datetime.now() - timedelta(days=30), employee.id))
        employee.invalidate_recordset(["create_date"])
        days = [self.today - timedelta(days=offset) for offset in (5, 4, 3, 2, 1)]
        Attendance = self.env["hr.attendance"]
        for day, minutes_late in zip(days, (25, 40, 3, 30)):  # last day: no check-in at all
            check_in = datetime.combine(day, datetime.min.time()) + timedelta(hours=8, minutes=minutes_late)
            Attendance.create({"employee_id": employee.id, "check_in": check_in, "check_out": check_in + timedelta(hours=8)})

        data = self._people(self.ManagerA, self.WarehouseA)
        row = next(row for row in data["attendance_exceptions"] if row["employee"] == "Late Larry")
        self.assertEqual(row["late_dates"], [fields.Date.to_string(day) for day in (days[0], days[1], days[3])],
                         "3 minutes late is within the grace period.")
        self.assertIn(fields.Date.to_string(days[4]), row["absent_dates"])
        kinds = {item["kind"]: item for item in data["attention"] if "Late Larry" in item["title"]}
        self.assertEqual(kinds["late"]["severity"], "critical")
        self.assertIn("absence", kinds)
        self.assertIn("Manager A", kinds["late"]["responsible"])
        self.assertGreaterEqual(data["summary"]["attendance_exceptions"], 4)
        self.assertEqual(sum(day["late"] for day in data["attendance_by_weekday"]), 3)

        # Branch B's manager scope must not include Branch A's employee.
        self.assertFalse(self._people(self.Head, self.WarehouseB)["attendance_exceptions"])

    def test_people_changes_queue_live_refresh(self):
        def queued_areas():
            payload = self.env.cr.precommit.data.get("strategic_management.data_changed")
            return payload["areas"] if payload else []

        self.env.cr.precommit.data.pop("strategic_management.data_changed", None)
        self.WarehouseA.strategic_planned_headcount = 4
        self.assertIn("people", queued_areas())

        self.env.cr.precommit.data.pop("strategic_management.data_changed", None)
        self.StaffB.strategic_branch_warehouse_id = self.WarehouseA
        self.assertIn("people", queued_areas())

        if "hr.attendance" in self.env.registry.models:
            employee = self.env["hr.employee"].create({"name": "Live Lisa", "company_id": self.company.id})
            self.env.cr.precommit.data.pop("strategic_management.data_changed", None)
            self.env["hr.attendance"].create({"employee_id": employee.id, "check_in": fields.Datetime.now() - timedelta(hours=1)})
            self.assertIn("people", queued_areas(), "Optional Attendances model must be hooked at registry load.")
