import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


CREW = Actor("crew-1", "maintenance")
DISP = Actor("disp-1", "dispatcher")
ADMIN = Actor("admin", "admin")
INSP = Actor("insp-1", "inspector")


class OfflineMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.repo = SQLiteRepository(self.db)
        self.service = DomainService(self.repo, RuleEngine())
        self.equipment = self.service.create(
            ADMIN,
            "equipment",
            {"asset_no": "E-1", "equipment_type": "elevator", "location": "B2", "inspection_interval_days": 365},
        )
        inspection = self.service.create(
            INSP,
            "inspection",
            {"equipment_id": self.equipment["id"], "scheduled_at": "2026-09-01T09:00:00Z", "cycle_days": 365},
        )
        self.service.transition(INSP, inspection["id"], "pass", {"findings": "ok"})

    def tearDown(self):
        self.tmp.cleanup()

    def _batch(self, records):
        return self.service.merge_offline(CREW, records)

    def test_duplicate_alarm_is_kept_once_even_when_cleared(self):
        records = [
            {"type": "alarm", "record_id": "a1", "asset_no": "E-1", "code": "TRAP", "occurred_at": "2026-10-01T10:00:00Z"},
            {"type": "alarm", "record_id": "a2", "asset_no": "E-1", "code": "TRAP", "occurred_at": "2026-10-01T10:00:00Z"},
        ]
        view = self._batch(records)
        statuses = [item["status"] for item in view["items"]]
        self.assertEqual(statuses, ["applied", "skipped"])
        alarms = self.service.list("alarm")
        self.assertEqual(len(alarms), 1)

        # A later backfill carrying the same (equipment, code) must not
        # re-attach an alarm that has already been resolved and closed.
        job = self.service.list("rescue_job")
        self.assertEqual(job, [])
        alarm = alarms[0]
        self.service.transition(DISP, alarm["id"], "dispatch", {"team": "Alpha"})
        self.service.create(DISP, "rescue_job", {"alarm_id": alarm["id"], "dedupe_key": "j1", "team": "Alpha"})
        self.service.transition(CREW, self.service.list("rescue_job")[0]["id"], "arrive", {})
        self.service.transition(CREW, self.service.list("rescue_job")[0]["id"], "complete", {"outcome": "freed"})
        self.service.transition(DISP, alarm["id"], "resolve", {"resolution": "safe"})
        self.service.transition(DISP, alarm["id"], "close", {})

        view2 = self._batch([
            {"type": "alarm", "record_id": "a3", "asset_no": "E-1", "code": "TRAP", "occurred_at": "2026-10-01T10:00:00Z"},
        ])
        self.assertEqual(view2["items"][0]["status"], "skipped")
        self.assertEqual(len(self.service.list("alarm")), 1)
        self.assertEqual(self.service.list("alarm")[0]["status"], "closed")

    def test_duplicate_rescue_job_is_kept_once_and_reports_team(self):
        self._batch([
            {"type": "alarm", "record_id": "a1", "asset_no": "E-1", "code": "TRAP", "occurred_at": "2026-10-01T10:00:00Z"},
        ])
        records = [
            {"type": "rescue_job", "record_id": "j1", "asset_no": "E-1", "code": "TRAP", "dedupe_key": "d1", "team": "Alpha", "phase": "on_site"},
            {"type": "rescue_job", "record_id": "j2", "asset_no": "E-1", "code": "TRAP", "dedupe_key": "d1", "team": "Alpha", "phase": "on_site"},
        ]
        view = self._batch(records)
        self.assertEqual([i["status"] for i in view["items"]], ["applied", "skipped"])
        jobs = self.service.list("rescue_job")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(view["items"][1]["result"]["related"]["rescue_team"], "Alpha")
        # Auto-dispatch advanced the alarm as part of the same merge.
        self.assertEqual(self.service.list("alarm")[0]["status"], "dispatched")

    def test_equipment_change_invalidates_open_permits_immediately(self):
        permit = self.service.create(
            INSP,
            "permit",
            {"equipment_id": self.equipment["id"], "purpose": "return_to_service", "requested_by": "ops"},
        )
        permit = self.service.transition(INSP, permit["id"], "request_review", {})
        permit = self.service.transition(INSP, permit["id"], "grant", {})
        self.assertEqual(permit["status"], "granted")

        view = self._batch([
            {"type": "equipment_status", "record_id": "e1", "asset_no": "E-1", "action": "suspend",
             "revision": self.equipment["version"], "reason": "offline stop"},
        ])
        self.assertEqual(view["items"][0]["status"], "applied")
        refreshed = self.service.get(permit["id"])
        self.assertEqual(refreshed["status"], "revoked")
        self.assertIn("suspended", refreshed["data"]["invalidated_reason"])

        # Revoking a 'blocked' permit works too (it was never sent to review).
        blocked = self.service.create(
            INSP,
            "permit",
            {"equipment_id": self.equipment["id"], "purpose": "special_inspection", "requested_by": "ops"},
        )
        self.service.transition(ADMIN, self.equipment["id"], "out_of_service", {"reason": "worse"})
        self.assertEqual(self.service.get(blocked["id"])["status"], "revoked")

    def test_permit_grant_is_blocked_while_rescue_runs_and_releases_after(self):
        view = self._batch([
            {"type": "alarm", "record_id": "a1", "asset_no": "E-1", "code": "TRAP", "occurred_at": "2026-10-01T10:00:00Z"},
            {"type": "rescue_job", "record_id": "j1", "asset_no": "E-1", "code": "TRAP", "dedupe_key": "d1", "team": "Alpha", "phase": "on_site"},
            {"type": "permit", "record_id": "p1", "asset_no": "E-1", "purpose": "return_to_service", "phase": "granted",
             "role": "inspector", "user_id": "insp-1"},
        ])
        permit_item = view["items"][2]
        self.assertEqual(permit_item["status"], "blocked")
        kinds = {b["type"] for b in permit_item["result"]["blockers"]}
        self.assertIn("rescue", kinds)

        # Finish the rescue and close the alarm, then re-drive the same batch.
        job = self.service.list("rescue_job")[0]
        self.service.transition(CREW, job["id"], "complete", {"outcome": "freed"})
        alarm = self.service.list("alarm")[0]
        self.service.transition(DISP, alarm["id"], "resolve", {"resolution": "safe"})
        self.service.transition(DISP, alarm["id"], "close", {})

        resumed = self.service.resume_batch(view["batch_id"])
        permit_item = next(i for i in resumed["items"] if i["type"] == "permit")
        self.assertEqual(permit_item["status"], "applied")
        self.assertEqual(len(self.service.list("permit")), 1)
        self.assertEqual(self.service.list("permit")[0]["status"], "granted")

    def test_stale_equipment_revision_conflicts_with_current_state(self):
        view = self._batch([
            {"type": "equipment_status", "record_id": "e1", "asset_no": "E-1", "action": "suspend",
             "revision": self.equipment["version"]},
        ])
        self.assertEqual(view["items"][0]["status"], "applied")
        view2 = self._batch([
            {"type": "equipment_status", "record_id": "e2", "asset_no": "E-1", "action": "out_of_service",
             "revision": self.equipment["version"]},
        ])
        item = view2["items"][0]
        self.assertEqual(item["status"], "conflict")
        self.assertEqual(item["result"]["current"]["status"], "suspended")
        self.assertGreater(item["result"]["current"]["version"], self.equipment["version"])

    def test_concurrent_dispatch_loser_sees_conflict_and_latest_team(self):
        alarm = self.service.create(
            DISP,
            "alarm",
            {"equipment_id": self.equipment["id"], "code": "TRAP", "occurred_at": "2026-10-01T10:00:00Z"},
        )
        self.service.create(DISP, "rescue_job", {"alarm_id": alarm["id"], "dedupe_key": "k-a", "team": "TeamA"})
        first = self.service.transition(DISP, alarm["id"], "dispatch", {"team": "TeamA"}, alarm["version"])
        self.assertEqual(first["status"], "dispatched")

        second_dispatcher = Actor("disp-2", "dispatcher")
        with self.assertRaises(ConflictError) as caught:
            self.service.transition(
                second_dispatcher, alarm["id"], "dispatch", {"team": "TeamB"}, alarm["version"]
            )
        exc = caught.exception
        self.assertEqual(exc.current["status"], "dispatched")
        self.assertEqual(exc.related["rescue_team"], "TeamA")
        self.assertEqual(exc.related["rescue_job"]["data"]["team"], "TeamA")

    def test_write_failure_keeps_confirmed_steps_and_retries_rest(self):
        records = [
            {"type": "alarm", "record_id": "a1", "asset_no": "E-1", "code": "C1", "occurred_at": "2026-10-01T10:00:00Z"},
            {"type": "alarm", "record_id": "a2", "asset_no": "E-1", "code": "C2", "occurred_at": "2026-10-01T10:01:00Z"},
            {"type": "alarm", "record_id": "a3", "asset_no": "E-1", "code": "C3", "occurred_at": "2026-10-01T10:02:00Z"},
        ]
        original = self.repo.update_sync_item
        calls = {"n": 0}

        def fail_second_item(seq, batch_id, status, result, conn=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated write failure")
            return original(seq, batch_id, status, result, conn=conn)

        self.repo.update_sync_item = fail_second_item
        with self.assertRaises(RuntimeError):
            self.service.merge_offline(CREW, records)
        self.repo.update_sync_item = original

        # The first item committed atomically (entity + audit + checkpoint);
        # the failing item rolled back entirely and never created an alarm.
        self.assertEqual(len(self.service.list("alarm")), 1)

        # Service restart on the same database: only unfinished items replay.
        restarted = DomainService(self.repo, RuleEngine())
        batches = self.repo.list_sync_batches()
        self.assertEqual(len(batches), 1)
        view = restarted.get_sync_batch(batches[0]["id"])
        statuses = [(item["record_id"], item["status"]) for item in view["items"]]
        self.assertEqual(statuses, [("a1", "applied"), ("a2", "applied"), ("a3", "applied")])
        self.assertEqual(len(restarted.list("alarm")), 3)

    def test_recovery_report_lists_inspection_remediation_and_rescue_blockers(self):
        # Equipment with no passed inspection at all.
        bare = self.service.create(
            ADMIN,
            "equipment",
            {"asset_no": "E-BARE", "equipment_type": "elevator", "location": "B3", "inspection_interval_days": 365},
        )
        self.service.create(
            INSP,
            "remediation",
            {"equipment_id": bare["id"], "issue": "brake wear", "owner": "Maint", "due_at": "2026-10-10"},
        )
        self.service.create(
            DISP,
            "alarm",
            {"equipment_id": bare["id"], "code": "TRAP", "occurred_at": "2026-10-01T10:00:00Z"},
        )
        alarm = [a for a in self.service.list("alarm") if a["data"]["equipment_id"] == bare["id"]][0]
        self.service.create(DISP, "rescue_job", {"alarm_id": alarm["id"], "dedupe_key": "k1", "team": "Alpha"})

        report = self.service.recovery_report(bare["id"])
        entry = next(e for e in report["equipment"] if e["equipment_id"] == bare["id"])
        types = [b["type"] for b in entry["blockers"]]
        self.assertIn("inspection", types)
        self.assertIn("remediation", types)
        self.assertIn("rescue", types)
        rescue = [b for b in entry["blockers"] if b["type"] == "rescue" and "team" in b][0]
        self.assertEqual(rescue["team"], "Alpha")

    def test_same_batch_submitted_twice_is_idempotent(self):
        records = [
            {"type": "alarm", "record_id": "a1", "asset_no": "E-1", "code": "C1", "occurred_at": "2026-10-01T10:00:00Z"},
        ]
        first = self.service.merge_offline(CREW, records)
        second = self.service.merge_offline(CREW, [dict(r) for r in records])
        self.assertEqual(first["batch_id"], second["batch_id"])
        self.assertEqual(len(self.service.list("alarm")), 1)


if __name__ == "__main__":
    unittest.main()
