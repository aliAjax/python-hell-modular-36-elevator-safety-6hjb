import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.service = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.dispatcher = Actor("disp", "dispatcher")

    def tearDown(self):
        self.tmp.cleanup()

    def equipment(self, asset_no="E-1"):
        return self.service.create(
            self.admin, "equipment",
            {"asset_no": asset_no, "equipment_type": "elevator", "location": "A", "inspection_interval_days": 365},
        )

    def pass_inspection(self, equipment):
        inspection = self.service.create(
            self.admin, "inspection",
            {"equipment_id": equipment["id"], "scheduled_at": "2026-09-27T09:00:00Z", "cycle_days": 365},
        )
        return self.service.transition(self.admin, inspection["id"], "pass", {"findings": "ok"})

    def close_remediation(self, equipment):
        remediation = self.service.create(
            self.admin, "remediation",
            {"equipment_id": equipment["id"], "issue": "wear", "owner": "M", "due_at": "2026-10-01"},
        )
        self.service.transition(self.admin, remediation["id"], "submit_evidence", {"evidence": "IMG"})
        self.service.transition(self.admin, remediation["id"], "verify", {})
        return self.service.transition(self.admin, remediation["id"], "close", {})

    def complete_rescue(self, alarm, job):
        self.service.transition(self.admin, job["id"], "arrive", {})
        self.service.transition(self.admin, job["id"], "complete", {"outcome": "freed"})
        self.service.transition(self.admin, alarm["id"], "resolve", {"resolution": "ok"})
        return self.service.transition(self.admin, alarm["id"], "close", {})

    def test_merge_creates_domain_entities(self):
        result = self.service.merge_offline(self.admin, [
            {"source_id": "tab1", "record_id": "e1", "kind": "equipment",
             "data": {"asset_no": "E-1", "equipment_type": "elevator", "location": "B2", "inspection_interval_days": 365}},
            {"source_id": "tab1", "record_id": "a1", "kind": "alarm",
             "data": {"equipment_id": "E-1", "code": "DOOR-JAM", "occurred_at": "2026-09-27T10:00:00Z"}},
            {"source_id": "tab1", "record_id": "j1", "kind": "rescue_job",
             "data": {"alarm_ref": {"equipment_asset_no": "E-1", "code": "DOOR-JAM"}, "dedupe_key": "job-1", "team": "Alpha"}},
        ])
        self.assertEqual(result["status"], "completed")
        by_kind = {}
        for entity in result["items"]:
            by_kind.setdefault(entity["kind"], []).append(entity)
        self.assertEqual(len(by_kind["equipment"]), 1)
        self.assertEqual(len(by_kind["alarm"]), 1)
        self.assertEqual(len(by_kind["rescue_job"]), 1)
        equipment = by_kind["equipment"][0]
        alarm = by_kind["alarm"][0]
        job = by_kind["rescue_job"][0]
        self.assertEqual(alarm["data"]["equipment_id"], equipment["id"])
        self.assertEqual(job["data"]["alarm_id"], alarm["id"])
        # merging a rescue job dispatches its alarm so state stays consistent
        self.assertEqual(self.service.get(alarm["id"])["status"], "dispatched")

    def test_duplicate_alarm_keeps_one(self):
        self.equipment()
        payload = {"equipment_id": "E-1", "code": "DOOR-JAM", "occurred_at": "2026-09-27T10:00:00Z"}
        first = self.service.merge_offline(self.admin, [
            {"source_id": "tab1", "record_id": "a1", "kind": "alarm", "data": payload},
        ])
        second = self.service.merge_offline(self.admin, [
            {"source_id": "tab2", "record_id": "a2", "kind": "alarm",
             "data": dict(payload, occurred_at="2026-09-28T09:00:00Z")},
        ])
        alarms = self.service.list("alarm")
        self.assertEqual(len(alarms), 1)
        self.assertEqual(alarms[0]["id"], first["items"][0]["id"])
        self.assertEqual(second["items"][0]["id"], first["items"][0]["id"])

    def test_resolved_alarm_not_reattached(self):
        self.equipment()
        result = self.service.merge_offline(self.admin, [
            {"source_id": "tab1", "record_id": "a1", "kind": "alarm",
             "data": {"equipment_id": "E-1", "code": "DOOR-JAM", "occurred_at": "2026-09-27T10:00:00Z"}},
            {"source_id": "tab1", "record_id": "j1", "kind": "rescue_job",
             "data": {"alarm_ref": {"equipment_asset_no": "E-1", "code": "DOOR-JAM"}, "dedupe_key": "job-1", "team": "Alpha"}},
        ])
        alarm = next(e for e in result["items"] if e["kind"] == "alarm")
        job = next(e for e in result["items"] if e["kind"] == "rescue_job")
        self.complete_rescue(alarm, job)
        self.assertEqual(self.service.get(alarm["id"])["status"], "closed")
        # a new field record for the same code must not re-open / re-attach it
        self.service.merge_offline(self.admin, [
            {"source_id": "tab3", "record_id": "a3", "kind": "alarm",
             "data": {"equipment_id": "E-1", "code": "DOOR-JAM", "occurred_at": "2026-09-29T09:00:00Z"}},
        ])
        alarms = self.service.list("alarm")
        self.assertEqual(len(alarms), 1)
        self.assertEqual(alarms[0]["status"], "closed")

    def test_suspend_invalidates_unfinished_permits(self):
        equipment = self.equipment()
        self.pass_inspection(equipment)
        self.close_remediation(equipment)
        permit = self.service.create(
            self.admin, "permit",
            {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"},
        )
        self.service.transition(self.admin, permit["id"], "request_review", {})
        self.service.transition(self.admin, permit["id"], "grant", {})
        self.assertEqual(self.service.get(permit["id"])["status"], "granted")
        self.service.transition(self.admin, equipment["id"], "suspend", {})
        self.assertEqual(self.service.get(permit["id"])["status"], "revoked")
        # a permit still in review must also be invalidated
        pending = self.service.create(
            self.admin, "permit",
            {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"},
        )
        self.service.transition(self.admin, pending["id"], "request_review", {})
        self.service.transition(self.admin, equipment["id"], "out_of_service", {})
        self.assertEqual(self.service.get(pending["id"])["status"], "revoked")

    def test_permit_grant_blocked_by_unfinished_rescue(self):
        equipment = self.equipment()
        self.pass_inspection(equipment)
        alarm = self.service.create(
            self.admin, "alarm",
            {"equipment_id": equipment["id"], "code": "TRAP", "occurred_at": "2026-09-27T11:00:00Z"},
        )
        self.service.transition(self.admin, alarm["id"], "dispatch", {"team": "Bravo"})
        job = self.service.create(
            self.admin, "rescue_job",
            {"alarm_id": alarm["id"], "dedupe_key": "job-1", "team": "Bravo"},
        )
        self.service.transition(self.admin, job["id"], "arrive", {})
        permit = self.service.create(
            self.admin, "permit",
            {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"},
        )
        self.service.transition(self.admin, permit["id"], "request_review", {})
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.admin, permit["id"], "grant", {})
        blockers = ctx.exception.details.get("blockers", [])
        self.assertTrue(any("rescue" in b for b in blockers))

    def test_concurrent_dispatch_shows_conflicting_task_and_team(self):
        equipment = self.equipment()
        alarm = self.service.create(
            self.admin, "alarm",
            {"equipment_id": equipment["id"], "code": "TRAP", "occurred_at": "2026-09-27T12:00:00Z"},
        )
        self.service.transition(self.admin, alarm["id"], "dispatch", {"team": "Alpha"})
        self.service.create(
            self.admin, "rescue_job",
            {"alarm_id": alarm["id"], "dedupe_key": "job-1", "team": "Alpha"},
        )
        # second dispatcher creates a duplicate rescue job
        with self.assertRaises(ConflictError) as ctx:
            self.service.create(
                self.dispatcher, "rescue_job",
                {"alarm_id": alarm["id"], "dedupe_key": "job-1", "team": "Bravo"},
            )
        conflicting = ctx.exception.details.get("conflicting", [])
        self.assertEqual(len(conflicting), 1)
        self.assertEqual(conflicting[0]["data"]["team"], "Alpha")
        # second dispatcher dispatches the already-dispatched alarm
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.dispatcher, alarm["id"], "dispatch", {"team": "Bravo"})
        jobs = ctx.exception.details.get("rescue_jobs", [])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["data"]["team"], "Alpha")

    def test_merge_resumable_keeps_confirmed_steps(self):
        self.equipment()
        result = self.service.merge_offline(self.admin, [
            {"source_id": "tab1", "record_id": "m1", "kind": "equipment",
             "data": {"asset_no": "E-2", "equipment_type": "elevator", "location": "B", "inspection_interval_days": 365}},
            {"source_id": "tab1", "record_id": "m2", "kind": "alarm",
             "data": {"equipment_id": "E-2", "occurred_at": "2026-09-27T10:00:00Z"}},  # missing code
            {"source_id": "tab1", "record_id": "m3", "kind": "alarm",
             "data": {"equipment_id": "E-2", "code": "DOOR-JAM", "occurred_at": "2026-09-27T10:00:00Z"}},
        ])
        self.assertEqual(result["status"], "partial")
        self.assertEqual([f["record_id"] for f in result["failed"]], ["m2"])
        # confirmed records are durable
        self.assertTrue(any(e["data"]["asset_no"] == "E-2" for e in self.service.list("equipment")))
        self.assertTrue(any(e["data"]["code"] == "DOOR-JAM" for e in self.service.list("alarm")))
        # retry the same batch: only the unfinished record is retried
        retry = self.service.merge_offline(self.admin, [
            {"source_id": "tab1", "record_id": "m1", "kind": "equipment",
             "data": {"asset_no": "E-2", "equipment_type": "elevator", "location": "B", "inspection_interval_days": 365}},
            {"source_id": "tab1", "record_id": "m2", "kind": "alarm",
             "data": {"equipment_id": "E-2", "occurred_at": "2026-09-27T10:00:00Z"}},
            {"source_id": "tab1", "record_id": "m3", "kind": "alarm",
             "data": {"equipment_id": "E-2", "code": "DOOR-JAM", "occurred_at": "2026-09-27T10:00:00Z"}},
        ])
        self.assertEqual(retry["status"], "partial")
        self.assertEqual(len(retry["failed"]), 1)
        # fix the failed record and retry only it
        fixed = self.service.merge_offline(self.admin, [
            {"source_id": "tab1", "record_id": "m2", "kind": "alarm",
             "data": {"equipment_id": "E-2", "code": "FIRE", "occurred_at": "2026-09-27T10:00:00Z"}},
        ])
        self.assertEqual(fixed["status"], "completed")
        codes = sorted(
            e["data"]["code"] for e in self.service.list("alarm")
            if e["data"]["equipment_id"] == next(
                x["id"] for x in self.service.list("equipment") if x["data"]["asset_no"] == "E-2"
            )
        )
        self.assertEqual(codes, ["DOOR-JAM", "FIRE"])

    def test_merge_resumes_after_restart(self):
        # a partial batch left over from a previous process
        self.service.merge_offline(self.admin, [
            {"source_id": "tabA", "record_id": "r1", "kind": "equipment",
             "data": {"asset_no": "R-1", "equipment_type": "elevator", "location": "B", "inspection_interval_days": 365}},
            {"source_id": "tabA", "record_id": "r2", "kind": "alarm",
             "data": {"equipment_id": "R-1", "occurred_at": "2026-09-27T10:00:00Z"}},  # missing code
        ])
        self.assertTrue(any(b["status"] == "partial" for b in self.service.merge_batches()))
        # simulate a restart: a brand new service on the same database
        restarted = DomainService(SQLiteRepository(self.db_path), RuleEngine())
        result = restarted.merge_offline(self.admin, [
            {"source_id": "tabA", "record_id": "r2", "kind": "alarm",
             "data": {"equipment_id": "R-1", "code": "DOOR-JAM", "occurred_at": "2026-09-27T10:00:00Z"}},
        ])
        self.assertEqual(result["status"], "completed")
        self.assertTrue(any(e["data"]["asset_no"] == "R-1" for e in restarted.list("equipment")))
        self.assertTrue(any(e["data"]["code"] == "DOOR-JAM" for e in restarted.list("alarm")))

    def test_equipment_downtime_suspends_via_merge(self):
        # equipment + passed inspection + granted permit, all from the field
        self.service.merge_offline(self.admin, [
            {"source_id": "tab1", "record_id": "e1", "kind": "equipment",
             "data": {"asset_no": "E-1", "equipment_type": "elevator", "location": "B2", "inspection_interval_days": 365}},
            {"source_id": "tab1", "record_id": "i1", "kind": "inspection",
             "data": {"equipment_id": "E-1", "scheduled_at": "2026-09-27T09:00:00Z", "cycle_days": 365}},
        ])
        equipment = next(e for e in self.service.list("equipment") if e["data"]["asset_no"] == "E-1")
        inspection = next(i for i in self.service.list("inspection") if i["data"]["equipment_id"] == equipment["id"])
        self.service.transition(self.admin, inspection["id"], "pass", {"findings": "ok"})
        self.service.merge_offline(self.admin, [
            {"source_id": "tab1", "record_id": "p1", "kind": "permit",
             "data": {"equipment_id": "E-1", "purpose": "return_to_service", "requested_by": "ops", "dedupe_key": "p1"}},
        ])
        permit = next(p for p in self.service.list("permit") if p["data"]["equipment_id"] == equipment["id"])
        self.service.transition(self.admin, permit["id"], "request_review", {})
        self.service.transition(self.admin, permit["id"], "grant", {})
        self.assertEqual(self.service.get(permit["id"])["status"], "granted")
        # downtime record arrives from the field -> equipment suspended, permit revoked
        self.service.merge_offline(self.admin, [
            {"source_id": "tab1", "record_id": "e1-down", "kind": "equipment", "op": "suspend",
             "data": {"asset_no": "E-1"}},
        ])
        self.assertEqual(self.service.get(equipment["id"])["status"], "suspended")
        self.assertEqual(self.service.get(permit["id"])["status"], "revoked")

    def test_permit_grant_through_merge_waits_for_rescue(self):
        self.service.merge_offline(self.admin, [
            {"source_id": "tab2", "record_id": "e2", "kind": "equipment",
             "data": {"asset_no": "E-2", "equipment_type": "elevator", "location": "B1", "inspection_interval_days": 365}},
            {"source_id": "tab2", "record_id": "i2", "kind": "inspection",
             "data": {"equipment_id": "E-2", "scheduled_at": "2026-09-27T09:00:00Z", "cycle_days": 365}},
            {"source_id": "tab2", "record_id": "a2", "kind": "alarm",
             "data": {"equipment_id": "E-2", "code": "TRAP", "occurred_at": "2026-09-27T10:00:00Z"}},
            {"source_id": "tab2", "record_id": "j2", "kind": "rescue_job",
             "data": {"alarm_ref": {"equipment_asset_no": "E-2", "code": "TRAP"}, "dedupe_key": "j2", "team": "Bravo"}},
            {"source_id": "tab2", "record_id": "p2", "kind": "permit",
             "data": {"equipment_id": "E-2", "purpose": "return_to_service", "requested_by": "ops", "dedupe_key": "p2"}},
        ])
        equipment = next(e for e in self.service.list("equipment") if e["data"]["asset_no"] == "E-2")
        inspection = next(i for i in self.service.list("inspection") if i["data"]["equipment_id"] == equipment["id"])
        self.service.transition(self.admin, inspection["id"], "pass", {"findings": "ok"})
        permit = next(p for p in self.service.list("permit") if p["data"]["equipment_id"] == equipment["id"])
        self.service.transition(self.admin, permit["id"], "request_review", {})
        # grant arrives while rescue is still open -> record fails, batch partial
        blocked = self.service.merge_offline(self.admin, [
            {"source_id": "tab2", "record_id": "p2-grant", "kind": "permit", "op": "grant",
             "data": {"dedupe_key": "p2"}},
        ])
        self.assertEqual(blocked["status"], "partial")
        self.assertEqual(len(blocked["failed"]), 1)
        self.assertEqual(self.service.get(permit["id"])["status"], "pending_review")
        # rescue completes in the field
        self.service.merge_offline(self.admin, [
            {"source_id": "tab2", "record_id": "j2-arr", "kind": "rescue_job", "op": "arrive",
             "data": {"dedupe_key": "j2"}},
            {"source_id": "tab2", "record_id": "j2-done", "kind": "rescue_job", "op": "complete",
             "data": {"dedupe_key": "j2", "outcome": "freed"}},
            {"source_id": "tab2", "record_id": "a2-res", "kind": "alarm", "op": "resolve",
             "data": {"code": "TRAP", "equipment_id": "E-2", "resolution": "ok"}},
            {"source_id": "tab2", "record_id": "a2-close", "kind": "alarm", "op": "close",
             "data": {"code": "TRAP", "equipment_id": "E-2"}},
        ])
        # retry the grant -> now it succeeds
        granted = self.service.merge_offline(self.admin, [
            {"source_id": "tab2", "record_id": "p2-grant2", "kind": "permit", "op": "grant",
             "data": {"dedupe_key": "p2"}},
        ])
        self.assertEqual(granted["status"], "completed")
        self.assertEqual(self.service.get(permit["id"])["status"], "granted")

    def test_merge_applies_rescue_arrival(self):
        self.equipment()
        result = self.service.merge_offline(self.admin, [
            {"source_id": "tab1", "record_id": "a1", "kind": "alarm",
             "data": {"equipment_id": "E-1", "code": "DOOR-JAM", "occurred_at": "2026-09-27T10:00:00Z"}},
            {"source_id": "tab1", "record_id": "j1", "kind": "rescue_job",
             "data": {"alarm_ref": {"equipment_asset_no": "E-1", "code": "DOOR-JAM"}, "dedupe_key": "job-1", "team": "Alpha"}},
        ])
        job = next(e for e in result["items"] if e["kind"] == "rescue_job")
        self.assertEqual(job["status"], "dispatched")
        # field record for the rescue arrival (a distinct field event)
        arrival = self.service.merge_offline(self.admin, [
            {"source_id": "tab1", "record_id": "j1-arrive", "kind": "rescue_job", "op": "arrive",
             "data": {"dedupe_key": "job-1"}},
        ])
        self.assertEqual(arrival["status"], "completed")
        self.assertEqual(self.service.get(job["id"])["status"], "on_site")

    def test_return_to_service_reports_all_blockers(self):
        equipment = self.equipment()
        self.service.transition(self.admin, equipment["id"], "suspend", {})
        # no passed inspection, open remediation, active rescue
        self.service.create(
            self.admin, "remediation",
            {"equipment_id": equipment["id"], "issue": "fix", "owner": "M", "due_at": "2026-10-01"},
        )
        alarm = self.service.create(
            self.admin, "alarm",
            {"equipment_id": equipment["id"], "code": "TRAP", "occurred_at": "2026-09-27T12:00:00Z"},
        )
        self.service.transition(self.admin, alarm["id"], "dispatch", {"team": "Alpha"})
        job = self.service.create(
            self.admin, "rescue_job",
            {"alarm_id": alarm["id"], "dedupe_key": "job-1", "team": "Alpha"},
        )
        self.service.transition(self.admin, job["id"], "arrive", {})
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.admin, equipment["id"], "return_to_service", {})
        blockers = ctx.exception.details.get("blockers", [])
        self.assertTrue(any("inspection" in b or "检验" in b for b in blockers))
        self.assertTrue(any("remediation" in b or "整改" in b for b in blockers))
        self.assertTrue(any("rescue" in b or "救援" in b for b in blockers))


if __name__ == "__main__":
    unittest.main()
