import hashlib
import threading
from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    Actor,
    ConflictError,
    DomainError,
    NotFoundError,
    PermissionDenied,
    Role,
    ValidationError,
)
from .rules import RuleEngine, permit_blockers

PERMIT_OPEN_STATUSES = ("blocked", "pending_review", "granted")


def _natural_id(prefix, *parts):
    digest = hashlib.sha256("\0".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:24]
    return prefix + "-" + digest


def _summary(entity):
    if not entity:
        return None
    return {
        "id": entity["id"],
        "kind": entity["kind"],
        "status": entity["status"],
        "version": entity["version"],
        "data": entity["data"],
    }


class DomainService:
    def __init__(self, repository, rules=None, resume_on_start=True):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        # Offline batch processing is serialized so concurrent submissions
        # observe each other's confirmed steps instead of racing the DB.
        self._sync_lock = threading.RLock()
        if resume_on_start:
            # "服务重启接着处理": continue any unfinished batch from its
            # durable checkpoints as soon as the service comes back.
            self.resume_pending_batches()

    def _lookup(self, kind, field, value, conn=None):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value, conn=conn)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    # ------------------------------------------------------------------ create

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        with self._sync_lock, self.repository.transaction() as conn:
            self.rules.validate_create(actor, kind, payload, lambda k, f, v: self._lookup(k, f, v, conn))
            entity_id = str(payload.pop("id", "") or uuid4())
            if self.repository.get_entity(entity_id, conn=conn):
                raise ConflictError("entity already exists: " + entity_id)
            status = self.rules.initial_status(kind, payload)
            entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id, conn=conn)
            self.audit.record(entity_id, actor, "create", None, status, {"kind": kind}, conn=conn)
            if idempotency_key:
                self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id, conn=conn)
        return entity

    # -------------------------------------------------------------- transition

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        with self._sync_lock, self.repository.transaction() as conn:
            return self._do_transition(actor, entity_id, action, dict(data or {}), expected_version, conn)

    def _do_transition(self, actor, entity_id, action, data, expected_version, conn):
        entity = self.repository.get_entity(entity_id, conn=conn)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        expected = int(expected_version) if expected_version is not None else entity["version"]
        # Check the revision before the state machine so a stale dispatcher
        # loses with a version conflict (plus the winning state) rather than a
        # bare invalid-transition error.
        if entity["version"] != expected:
            exc = ConflictError(
                "version conflict: expected %s, found %s" % (expected, entity["version"]),
                current=_summary(entity),
            )
            self._enrich_conflict(exc, kind, action, entity, conn)
            raise exc
        try:
            next_status, patch = self.rules.validate_transition(
                actor,
                entity,
                action,
                dict(data or {}),
                lambda k, f, v: self._lookup(k, f, v, conn),
            )
        except ConflictError as exc:
            self._enrich_conflict(exc, kind, action, entity, conn)
            raise
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged, conn=conn)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
            conn=conn,
        )
        if kind == "equipment":
            self._cascade_equipment(actor, updated, conn)
        return updated

    def _enrich_conflict(self, exc, kind, action, entity, conn):
        """Attach the winning/conflicting state so late callers can refresh."""
        if kind == "alarm" and action == "dispatch" and entity is not None:
            jobs = [
                j for j in self.repository.list_entities(kind="rescue_job", conn=conn)
                if j["data"].get("alarm_id") == entity["id"]
                and j["status"] not in ("completed", "aborted")
            ]
            if jobs:
                latest = jobs[-1]
                exc.related = exc.related or {
                    "rescue_team": latest["data"].get("team"),
                    "rescue_job": _summary(latest),
                }
            exc.current = exc.current or _summary(entity)
        if kind == "rescue_job" and entity is not None:
            exc.current = exc.current or _summary(entity)
            alarm = self.repository.get_entity(entity["data"].get("alarm_id"), conn=conn)
            exc.related = exc.related or {"alarm": _summary(alarm)}

    def _cascade_equipment(self, actor, equipment, conn):
        """Invalidate unfinished permits the moment equipment status changes.

        Suspension / out-of-service revokes every non-terminal permit; return
        to service closes out a granted permit as completed. This runs inside
        the equipment update's transaction, so the two effects never diverge.
        """
        permits = [
            p for p in self.repository.list_entities(kind="permit", conn=conn)
            if p["data"].get("equipment_id") == equipment["id"]
        ]
        if equipment["status"] in ("suspended", "out_of_service"):
            reason = "equipment status changed to %s" % equipment["status"]
            for permit in permits:
                if permit["status"] in PERMIT_OPEN_STATUSES:
                    data = dict(permit["data"])
                    data["invalidated_reason"] = reason
                    updated = self.repository.update_entity(
                        permit["id"], permit["version"], "revoked", data, conn=conn
                    )
                    self.audit.record(
                        permit["id"], actor, "revoke", permit["status"], "revoked",
                        {"auto": True, "reason": reason, "equipment_id": equipment["id"]},
                        conn=conn,
                    )
        elif equipment["status"] == "in_service":
            for permit in permits:
                if permit["status"] == "granted":
                    data = dict(permit["data"])
                    data["completed_reason"] = "equipment returned to service"
                    self.repository.update_entity(
                        permit["id"], permit["version"], "completed", data, conn=conn
                    )
                    self.audit.record(
                        permit["id"], actor, "complete", "granted", "completed",
                        {"auto": True, "equipment_id": equipment["id"]},
                        conn=conn,
                    )

    # ------------------------------------------------------- offline merging

    def merge_offline(self, actor, records, batch_id=None):
        """Backfill records captured while the garage network was down.

        Records merge at the *current* revision: natural keys dedupe alarms and
        rescue jobs (a cleared alarm is never re-attached), equipment updates
        carry their base revision for optimistic concurrency, and each
        confirmed step is checkpointed so retries only repeat unfinished work.
        """
        batch_id = batch_id or self._batch_id(actor, records)
        with self._sync_lock:
            existing = self.repository.get_sync_batch(batch_id)
            if existing:
                return self._batch_view(existing)
            self._validate_records_shape(records)
            with self.repository.transaction() as conn:
                self.repository.create_sync_batch(batch_id, actor, len(records), conn=conn)
                self.repository.add_sync_items(batch_id, records, conn=conn)
            return self._process_batch(batch_id, actor.user_id)

    def _validate_records_shape(self, records):
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            if not raw.get("type"):
                raise ValidationError("each offline record requires a type")
            if not raw.get("record_id"):
                raise ValidationError("each offline record requires a record_id")

    def _batch_id(self, actor, records):
        joined = hashlib.sha256()
        joined.update(actor.user_id.encode("utf-8"))
        joined.update(b"\0")
        joined.update(_canonical_json(records).encode("utf-8"))
        return "batch-" + joined.hexdigest()[:24]

    def resume_batch(self, batch_id):
        """Manually re-drive one batch.

        Applied/skipped steps are kept; blocked and conflict items are retried
        once the rescue has finished or the dispatcher refreshes revisions.
        """
        with self._sync_lock:
            batch = self.repository.get_sync_batch(batch_id)
            if not batch:
                raise NotFoundError("sync batch not found: " + batch_id)
            if batch["status"] == "completed":
                self.repository.update_sync_batch(batch_id, "pending")
            return self._process_batch(batch_id, batch["actor_id"], include_resolved=True)

    def resume_pending_batches(self):
        """Continue every unfinished batch after a process restart.

        Only items still 'pending' or marked 'failed' by a write error replay;
        applied/skipped steps are confirmed and never repeated.
        """
        with self._sync_lock:
            views = []
            for batch in self.repository.list_sync_batches(status="pending"):
                items = self.repository.get_sync_items(batch["id"])
                if any(item["status"] in ("pending", "failed") for item in items):
                    views.append(self._process_batch(batch["id"], batch["actor_id"]))
                else:
                    # Every step reached a confirmed conclusion (applied,
                    # skipped, blocked, conflict or invalid); nothing is left
                    # for an automatic replay.
                    self.repository.update_sync_batch(batch["id"], "completed")
                    views.append(self._batch_view(self.repository.get_sync_batch(batch["id"])))
            return views

    def _process_batch(self, batch_id, actor_id, include_resolved=False):
        batch = self.repository.get_sync_batch(batch_id)
        if batch["status"] != "pending":
            return self._batch_view(batch)
        actor = _BatchActor(actor_id, batch["actor_role"])
        # Restart replay only covers unconfirmed steps (pending/failed).
        # Explicit resume also retries blocked/conflict conclusions.
        replay = ("pending", "failed") if not include_resolved else (
            "pending", "failed", "blocked", "conflict"
        )
        for item in self.repository.list_pending_sync_items(batch_id):
            if item["status"] not in replay:
                continue
            with self.repository.transaction() as conn:
                outcome = self._apply_record(actor, item["record"], conn)
                self.repository.update_sync_item(
                    item["seq"], batch_id, outcome["status"], outcome, conn=conn
                )
            # Crash after commit: the checkpoint is durable, so a restart
            # resumes with the next item instead of replaying this one.
        items = self.repository.get_sync_items(batch_id)
        unfinished = [item for item in items if item["status"] in ("pending", "failed")]
        next_status = "pending" if unfinished else "completed"
        self.repository.update_sync_batch(batch_id, next_status)
        return self._batch_view(self.repository.get_sync_batch(batch_id))

    def _apply_record(self, actor, record, conn):
        # Each garage log may have been entered by a different role; the
        # record's declared role must still be one the system recognizes.
        record_actor = actor
        declared_role = record.get("role")
        if declared_role:
            if declared_role not in {item.value for item in Role}:
                return {"status": "invalid", "error": "unknown role: " + str(declared_role)}
            record_actor = Actor(record.get("user_id") or actor.user_id, declared_role)
        record_type = record["type"]
        try:
            if record_type == "equipment_status":
                return self._merge_equipment_status(record_actor, record, conn)
            if record_type == "alarm":
                return self._merge_alarm(record_actor, record, conn)
            if record_type == "rescue_job":
                return self._merge_rescue(record_actor, record, conn)
            if record_type == "permit":
                return self._merge_permit(record_actor, record, conn)
            return {"status": "invalid", "error": "unknown record type: " + str(record_type)}
        except (ValidationError, PermissionDenied) as exc:
            return {"status": "invalid", "error": str(exc), "error_type": type(exc).__name__}
        except ConflictError as exc:
            result = {
                "status": "conflict",
                "error": str(exc),
                "error_type": "ConflictError",
            }
            if exc.blockers:
                result["status"] = "blocked"
                result["blockers"] = exc.blockers
            if exc.current:
                result["current"] = exc.current
            if exc.related:
                result["related"] = exc.related
            return result
        except NotFoundError as exc:
            return {"status": "invalid", "error": str(exc), "error_type": "NotFoundError"}
        except DomainError as exc:
            return {"status": "failed", "error": str(exc), "error_type": type(exc).__name__}

    # ----- merge handlers ---------------------------------------------------

    def _resolve_equipment(self, ref, conn):
        equipment = None
        if ref:
            equipment = self.repository.get_entity(str(ref), conn=conn)
            if not equipment:
                hits = self.repository.find_entities("equipment", "asset_no", ref, conn=conn)
                equipment = hits[0] if hits else None
        if not equipment:
            raise ValidationError("record references unknown equipment: " + str(ref))
        return equipment

    def _merge_equipment_status(self, actor, record, conn):
        equipment = self._resolve_equipment(record.get("equipment_id") or record.get("asset_no"), conn)
        action = record.get("action", "suspend")
        if action not in ("suspend", "out_of_service", "return_to_service"):
            raise ValidationError("invalid equipment action: " + str(action))
        base_revision = record.get("revision")
        data = {"reason": record.get("reason", "offline merge")}
        try:
            self._do_transition(actor, equipment["id"], action, data, base_revision, conn)
        except ConflictError as exc:
            exc.current = exc.current or _summary(self.repository.get_entity(equipment["id"], conn=conn))
            raise
        return {
            "status": "applied",
            "entity_id": equipment["id"],
            "action": action,
        }

    def _merge_alarm(self, actor, record, conn):
        equipment = self._resolve_equipment(record.get("equipment_id") or record.get("asset_no"), conn)
        code = record.get("code")
        if not code:
            raise ValidationError("alarm record requires a code")
        natural = _natural_id("alarm", equipment["id"], code, record.get("occurred_at"))
        # Dedupe across the *whole* history: if this (equipment, code) alarm
        # already exists it is never re-attached, even when already resolved.
        history = [
            a for a in self.repository.list_entities(kind="alarm", conn=conn)
            if a["data"].get("equipment_id") == equipment["id"] and a["data"].get("code") == code
        ]
        if history:
            existing = history[-1]
            return {
                "status": "skipped",
                "reason": "duplicate alarm already on record",
                "entity_id": existing["id"],
                "current": _summary(existing),
            }
        payload = {
            "id": natural,
            "equipment_id": equipment["id"],
            "code": code,
            "occurred_at": record.get("occurred_at"),
            "source": "offline",
            "source_record_id": record.get("record_id"),
        }
        self.rules.validate_create(
            actor, "alarm", payload, lambda k, f, v: self._lookup(k, f, v, conn)
        )
        entity = self.repository.create_entity(
            natural, "alarm", self.rules.initial_status("alarm"), payload, actor.user_id, conn=conn
        )
        self.audit.record(natural, actor, "merge_create", None, entity["status"],
                         {"kind": "alarm", "record_id": record.get("record_id")}, conn=conn)
        return {"status": "applied", "entity_id": natural, "action": "create"}

    def _merge_rescue(self, actor, record, conn):
        alarm_ref = record.get("alarm_id")
        alarm = None
        if alarm_ref:
            alarm = self.repository.get_entity(str(alarm_ref), conn=conn)
        if not alarm:
            equipment_ref = record.get("equipment_id") or record.get("asset_no")
            if equipment_ref and record.get("code"):
                equipment = self._resolve_equipment(equipment_ref, conn)
                alarms = [
                    a for a in self.repository.list_entities(kind="alarm", conn=conn)
                    if a["data"].get("equipment_id") == equipment["id"]
                    and a["data"].get("code") == record.get("code")
                ]
                alarm = alarms[-1] if alarms else None
        if not alarm:
            raise ValidationError("rescue record references unknown alarm")
        dedupe_key = record.get("dedupe_key") or record.get("record_id")
        team = record.get("team")
        natural = _natural_id("rescue", alarm["id"], dedupe_key)
        existing_job = self.repository.get_entity(natural, conn=conn)
        if not existing_job:
            other_jobs = [
                j for j in self.repository.list_entities(kind="rescue_job", conn=conn)
                if j["data"].get("dedupe_key") == dedupe_key
            ]
            existing_job = other_jobs[0] if other_jobs else None
        if existing_job:
            # A finished or in-flight rescue for this dispatch is never
            # duplicated; the merge returns the latest team for the screen.
            return {
                "status": "skipped",
                "reason": "rescue job already exists for dedupe key",
                "entity_id": existing_job["id"],
                "current": _summary(existing_job),
                "related": {"rescue_team": existing_job["data"].get("team")},
            }
        payload = {
            "id": natural,
            "alarm_id": alarm["id"],
            "dedupe_key": dedupe_key,
            "team": team,
            "source": "offline",
            "source_record_id": record.get("record_id"),
        }
        self.rules.validate_create(
            actor, "rescue_job", payload, lambda k, f, v: self._lookup(k, f, v, conn)
        )
        # The garage logged the rescue arrival locally; if the alarm was never
        # dispatched server-side, advance it first within the same revision.
        if alarm["status"] == "received":
            alarm = self._do_transition(
                SYSTEM_ACTOR, alarm["id"], "dispatch", {"team": team, "source": "offline"}, alarm["version"], conn
            )
        job = self.repository.create_entity(
            natural, "rescue_job", self.rules.initial_status("rescue_job"), payload, actor.user_id, conn=conn
        )
        self.audit.record(natural, actor, "merge_create", None, job["status"],
                         {"kind": "rescue_job", "record_id": record.get("record_id")}, conn=conn)
        entity_id = natural
        phase = record.get("phase", "dispatched")
        if phase in ("on_site", "arrived", "arrive"):
            job = self._do_transition(actor, entity_id, "arrive", {"arrived_at": record.get("arrived_at")}, job["version"], conn)
        elif phase in ("completed", "complete"):
            job = self._do_transition(actor, entity_id, "arrive", {"arrived_at": record.get("arrived_at")}, job["version"], conn)
            job = self._do_transition(
                actor, entity_id, "complete",
                {"outcome": record.get("outcome", "offline recorded completion"),
                 "completed_at": record.get("completed_at")},
                job["version"], conn,
            )
        return {
            "status": "applied",
            "entity_id": entity_id,
            "action": "create",
            "rescue_team": team,
            "final_status": job["status"],
        }

    def _merge_permit(self, actor, record, conn):
        equipment = self._resolve_equipment(record.get("equipment_id") or record.get("asset_no"), conn)
        purpose = record.get("purpose", "return_to_service")
        natural = _natural_id("permit", equipment["id"], purpose, record.get("record_id"))
        existing = self.repository.get_entity(natural, conn=conn)
        if existing:
            # Replay of a blocked permit (after the rescue closes, etc.) keeps
            # advancing the same permit instead of duplicating it. A permit in
            # a terminal state is simply confirmed again.
            if existing["status"] in ("revoked", "completed", "expired"):
                return {
                    "status": "skipped",
                    "reason": "permit already terminal",
                    "entity_id": existing["id"],
                    "current": _summary(existing),
                }
            permit = existing
            result = {"status": "applied", "entity_id": natural, "action": "resume"}
        else:
            payload = {
                "id": natural,
                "equipment_id": equipment["id"],
                "purpose": purpose,
                "requested_by": record.get("requested_by", actor.user_id),
                "source": "offline",
                "source_record_id": record.get("record_id"),
            }
            self.rules.validate_create(
                actor, "permit", payload, lambda k, f, v: self._lookup(k, f, v, conn)
            )
            permit = self.repository.create_entity(
                natural, "permit", self.rules.initial_status("permit"), payload, actor.user_id, conn=conn
            )
            self.audit.record(natural, actor, "merge_create", None, permit["status"],
                             {"kind": "permit", "record_id": record.get("record_id")}, conn=conn)
            result = {"status": "applied", "entity_id": natural, "action": "create"}
        phase = record.get("phase", "requested")
        if phase in ("request_review", "review", "grant", "granted") and permit["status"] == "blocked":
            permit = self._do_transition(actor, natural, "request_review", {}, permit["version"], conn)
        if phase in ("grant", "granted") and permit["status"] == "pending_review":
            try:
                permit = self._do_transition(actor, natural, "grant", {}, permit["version"], conn)
            except ConflictError as exc:
                # "恢复许可也会在救援没结束时放行" — keep the permit but record
                # every blocker (inspection / remediation / rescue) instead of
                # granting. The step stays on the server and is retriable.
                current = self.repository.get_entity(natural, conn=conn)
                result["status"] = "blocked"
                result["error"] = str(exc)
                result["blockers"] = exc.blockers or permit_blockers(equipment, lambda k, f, v: self._lookup(k, f, v, conn))
                result["entity_id"] = natural
                result["current"] = _summary(current)
                return result
        result["final_status"] = permit["status"]
        return result

    def _batch_view(self, batch):
        items = self.repository.get_sync_items(batch["id"])
        counts = {"applied": 0, "skipped": 0, "blocked": 0, "conflict": 0, "invalid": 0, "failed": 0, "pending": 0}
        for item in items:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        unfinished = sum(counts[s] for s in ("pending", "failed"))
        status = "pending" if unfinished else batch["status"]
        return {
            "batch_id": batch["id"],
            "status": status,
            "total": len(items),
            "counts": counts,
            "items": [
                {
                    "seq": item["seq"],
                    "record_id": item["record"].get("record_id"),
                    "type": item["record"].get("type"),
                    "status": item["status"],
                    "result": item["result"],
                }
                for item in items
            ],
        }

    def get_sync_batch(self, batch_id):
        batch = self.repository.get_sync_batch(batch_id)
        if not batch:
            raise NotFoundError("sync batch not found: " + batch_id)
        return self._batch_view(batch)

    # ------------------------------------------------------------ recovery

    def recovery_report(self, equipment_id=None):
        """Blocker view shown when operations resume.

        Aggregates unfinished sync batches plus the inspection, remediation
        and rescue reasons each equipment cannot return to service.
        """
        batches = self.repository.list_sync_batches(status="pending")
        report = {
            "pending_batches": [self._batch_view(batch) for batch in batches],
            "equipment": [],
        }
        equipments = self.repository.list_entities(kind="equipment")
        for equipment in equipments:
            if equipment_id and equipment["id"] != equipment_id:
                continue
            blockers = permit_blockers(equipment, self._lookup)
            open_permits = [
                p for p in self.repository.list_entities(kind="permit")
                if p["data"].get("equipment_id") == equipment["id"]
                and p["status"] in PERMIT_OPEN_STATUSES
            ]
            if blockers or equipment["status"] != "in_service" or open_permits:
                report["equipment"].append({
                    "equipment_id": equipment["id"],
                    "asset_no": equipment["data"].get("asset_no"),
                    "status": equipment["status"],
                    "blocked": bool(blockers),
                    "blockers": blockers,
                    "open_permits": [_summary(p) for p in open_permits],
                })
        return report

    # ------------------------------------------------------------- reads

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)


class _BatchActor:
    """Actor identity reconstructed from a durable sync batch."""

    def __init__(self, user_id, role):
        self.user_id = user_id
        self.role = role


# Automatic reconciliation steps during a merge are attributed to the sync
# pipeline rather than the field crew, who cannot dispatch on paper.
SYSTEM_ACTOR = _BatchActor("sync-pipeline", "dispatcher")


def _canonical_json(value):
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
