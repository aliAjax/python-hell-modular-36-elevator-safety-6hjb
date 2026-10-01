from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, InvalidTransition, NotFoundError, PermissionDenied, ValidationError
from .rules import RuleEngine


# Dependency order so references (equipment -> alarm -> rescue job -> permit)
# resolve even when the field records arrive out of order.
KIND_ORDER = {
    "equipment": 0,
    "inspection": 1,
    "maintenance": 2,
    "alarm": 3,
    "remediation": 4,
    "rescue_job": 5,
    "permit": 6,
}

TERMINAL_PERMIT_STATUSES = ("revoked", "expired")

# Exceptions that mean "this record cannot be applied right now" but should
# not abort the rest of the batch.
RECOVERABLE = (ValidationError, PermissionDenied, ConflictError, InvalidTransition)


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        # Cross-object effect: taking equipment out of service immediately
        # invalidates every unfinished recovery permit for that equipment.
        if entity["kind"] == "equipment" and next_status in ("suspended", "out_of_service"):
            self._invalidate_permits(entity["id"], actor, "equipment " + next_status)
        return updated

    def _invalidate_permits(self, equipment_id, actor, reason):
        for permit in self.repository.list_entities("permit"):
            if permit["data"].get("equipment_id") != equipment_id:
                continue
            if permit["status"] in TERMINAL_PERMIT_STATUSES:
                continue
            # Re-read to get the current version; a concurrent transaction may
            # have already moved this permit to a terminal state.
            current = self.repository.get_entity(permit["id"])
            if not current or current["status"] in TERMINAL_PERMIT_STATUSES:
                continue
            merged = dict(current["data"])
            merged["revoke_reason"] = reason
            try:
                self.repository.update_entity(current["id"], current["version"], "revoked", merged)
            except ConflictError:
                continue
            self.audit.record(
                current["id"], actor, "revoke", current["status"], "revoked",
                {"reason": reason, "system": True},
            )

    # ------------------------------------------------------------------
    # Offline merge
    # ------------------------------------------------------------------

    def merge_offline(self, actor, records):
        """Merge field records captured offline into live domain entities.

        Records are persisted in a durable batch before processing, so a
        write failure or service restart leaves confirmed steps intact and
        only unfinished records are retried. Duplicate records (same stable
        identity) collapse onto a single entity; resolved alarms are never
        re-attached to their equipment.
        """
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        # Resume batches left incomplete by a previous crash/restart first.
        self.resume_incomplete_batches()

        normalized = []
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            source_id = str(raw.get("source_id", "")).strip()
            record_id = str(raw.get("record_id", "")).strip()
            if not source_id or not record_id:
                raise ValidationError("source_id and record_id are required")
            kind = self.rules.normalize_kind(str(raw.get("kind", "")).strip())
            if kind not in self.rules.INITIAL_STATUS:
                raise ValidationError("unknown kind: " + str(raw.get("kind")))
            op = str(raw.get("op", "create")).strip() or "create"
            data = raw.get("data")
            if data is None:
                data = {
                    k: v for k, v in raw.items()
                    if k not in ("source_id", "record_id", "kind", "op")
                }
            if not isinstance(data, dict):
                raise ValidationError("offline record data must be an object")
            normalized.append({
                "source_id": source_id,
                "record_id": record_id,
                "kind": kind,
                "op": op,
                "data": data,
            })

        batch_id = "merge-" + uuid4().hex
        self.repository.create_merge_batch(
            batch_id, "offline", actor.user_id, actor.role, len(normalized)
        )
        ordered = sorted(normalized, key=lambda rec: KIND_ORDER.get(rec["kind"], 99))
        for seq, rec in enumerate(ordered):
            self.repository.add_merge_record(
                batch_id, seq, rec["source_id"], rec["record_id"],
                rec["kind"], rec["op"], rec["data"],
            )
        return self._run_batch(actor, batch_id)

    def resume_incomplete_batches(self):
        """Resume batches that were interrupted by a failure or restart."""
        results = []
        for batch in self.repository.list_merge_batches():
            if batch["status"] not in ("processing", "partial"):
                continue
            actor = Actor(batch["actor_id"], batch["actor_role"])
            results.append(self._run_batch(actor, batch["id"]))
        return results

    def _run_batch(self, actor, batch_id):
        records = self.repository.list_merge_records(batch_id)
        items = []
        failed = []
        for rec in records:
            if rec["status"] == "done" and rec["entity_id"]:
                entity = self.repository.get_entity(rec["entity_id"])
                if entity:
                    items.append(entity)
                continue
            try:
                status, entity_id, error = self._process_record(actor, rec)
            except Exception as exc:  # never abort the whole batch
                status, entity_id, error = "failed", rec.get("entity_id"), str(exc)
            self.repository.update_merge_record(batch_id, rec["seq"], status, entity_id, error)
            if status == "done" and entity_id:
                entity = self.repository.get_entity(entity_id)
                if entity:
                    items.append(entity)
            elif status == "failed":
                failed.append({
                    "seq": rec["seq"],
                    "record_id": rec["record_id"],
                    "error": error,
                })
        fresh = self.repository.list_merge_records(batch_id)
        done = sum(1 for r in fresh if r["status"] == "done")
        batch_status = "completed" if done == len(fresh) else "partial"
        self.repository.touch_merge_batch(batch_id, batch_status, done)
        return {
            "batch_id": batch_id,
            "status": batch_status,
            "total": len(fresh),
            "done": done,
            "items": items,
            "failed": failed,
        }

    def _process_record(self, actor, rec):
        # Idempotency: already confirmed under the same stable identity.
        prior = self.repository.find_merge_record(rec["source_id"], rec["record_id"])
        if prior and prior["status"] == "done" and prior["entity_id"]:
            entity = self.repository.get_entity(prior["entity_id"])
            if entity:
                return "done", prior["entity_id"], None
        kind = rec["kind"]
        op = rec["op"]
        data = dict(rec.get("data") or {})
        if op == "create":
            return self._merge_create(actor, kind, data)
        return self._merge_transition(actor, kind, op, data)

    def _merge_create(self, actor, kind, data):
        data = self._resolve_refs(kind, data)
        existing = self._find_existing(kind, data)
        if existing:
            # Duplicate record: keep the single existing entity. This is what
            # stops resolved alarms from being re-attached to equipment.
            return "done", existing["id"], None
        # A rescue job implies its alarm was dispatched; dispatch it first so
        # the alarm/job state stays consistent when merging offline records.
        if kind == "rescue_job" and data.get("alarm_id"):
            alarm = self.repository.get_entity(data["alarm_id"])
            if alarm and alarm["status"] == "received":
                self.transition(
                    actor, alarm["id"], "dispatch",
                    {"team": data.get("team")}, expected_version=None,
                )
        payload = self.rules.validate_create(actor, kind, data, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            return "done", entity_id, None
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "merge_offline", None, status, {"kind": kind})
        return "done", entity_id, None

    def _merge_transition(self, actor, kind, op, data):
        entity = self._find_target(kind, data)
        if not entity:
            return "failed", None, "target %s not found for %s" % (kind, op)
        desired = self.rules.next_status(kind, op)
        if desired and entity["status"] == desired:
            return "done", entity["id"], None  # already applied (idempotent)
        try:
            updated = self.transition(actor, entity["id"], op, data, expected_version=None)
        except RECOVERABLE as exc:
            return "failed", entity["id"], str(exc)
        return "done", updated["id"], None

    # ------------------------------------------------------------------
    # Reference resolution and natural-key dedupe
    # ------------------------------------------------------------------

    def _resolve_equipment(self, data):
        ref = data.get("equipment_id")
        if not ref:
            return None
        found = self.repository.find_entities("equipment", "id", ref)
        if found:
            return found[0]
        found = self.repository.find_entities("equipment", "asset_no", ref)
        if found:
            return found[0]
        return None

    def _resolve_alarm(self, data, alarm_ref=None):
        aid = data.get("alarm_id")
        if aid:
            found = self.repository.find_entities("alarm", "id", aid)
            if found:
                return found[0]
        ref = alarm_ref or {}
        code = ref.get("code") or data.get("code")
        equip_ref = (
            ref.get("equipment_asset_no") or ref.get("equipment_id") or data.get("equipment_id")
        )
        if code and equip_ref:
            equip = self._resolve_equipment({"equipment_id": equip_ref})
            if equip:
                for alarm in self.repository.list_entities("alarm"):
                    if (
                        alarm["data"].get("equipment_id") == equip["id"]
                        and alarm["data"].get("code") == code
                    ):
                        return alarm
        return None

    def _resolve_refs(self, kind, data):
        equip_ref = data.pop("equipment_ref", None)
        if equip_ref and not data.get("equipment_id"):
            if isinstance(equip_ref, dict):
                data["equipment_id"] = equip_ref.get("asset_no") or equip_ref.get("equipment_id")
            else:
                data["equipment_id"] = equip_ref
        if kind in ("alarm", "inspection", "maintenance", "remediation", "permit") and data.get("equipment_id"):
            equip = self._resolve_equipment(data)
            if equip:
                data["equipment_id"] = equip["id"]
        if kind == "rescue_job":
            alarm_ref = data.pop("alarm_ref", None)
            if alarm_ref and not data.get("alarm_id"):
                if isinstance(alarm_ref, dict):
                    data["alarm_id"] = alarm_ref.get("alarm_id") or alarm_ref.get("code")
            if data.get("alarm_id"):
                alarm = self._resolve_alarm(data, alarm_ref)
                if alarm:
                    data["alarm_id"] = alarm["id"]
        return data

    def _find_existing(self, kind, data):
        if kind == "equipment":
            asset_no = data.get("asset_no")
            if asset_no:
                found = self.repository.find_entities("equipment", "asset_no", asset_no)
                if found:
                    return found[0]
        elif kind == "alarm":
            equip = self._resolve_equipment(data)
            code = data.get("code")
            if equip and code:
                for alarm in self.repository.list_entities("alarm"):
                    if (
                        alarm["data"].get("equipment_id") == equip["id"]
                        and alarm["data"].get("code") == code
                    ):
                        return alarm
        elif kind == "rescue_job":
            key = data.get("dedupe_key")
            if key:
                for job in self.repository.list_entities("rescue_job"):
                    if job["data"].get("dedupe_key") == key:
                        return job
        elif kind == "permit":
            key = data.get("dedupe_key")
            if key:
                for permit in self.repository.list_entities("permit"):
                    if permit["data"].get("dedupe_key") == key:
                        return permit
        elif kind == "remediation":
            equip = self._resolve_equipment(data)
            issue = data.get("issue")
            if equip and issue:
                for item in self.repository.list_entities("remediation"):
                    if (
                        item["data"].get("equipment_id") == equip["id"]
                        and item["data"].get("issue") == issue
                    ):
                        return item
        elif kind == "inspection":
            equip = self._resolve_equipment(data)
            scheduled = data.get("scheduled_at")
            if equip and scheduled:
                for item in self.repository.list_entities("inspection"):
                    if (
                        item["data"].get("equipment_id") == equip["id"]
                        and item["data"].get("scheduled_at") == scheduled
                    ):
                        return item
        elif kind == "maintenance":
            equip = self._resolve_equipment(data)
            planned = data.get("planned_at")
            work_type = data.get("work_type")
            if equip and planned:
                for item in self.repository.list_entities("maintenance"):
                    if (
                        item["data"].get("equipment_id") == equip["id"]
                        and item["data"].get("planned_at") == planned
                        and item["data"].get("work_type") == work_type
                    ):
                        return item
        return None

    def _find_target(self, kind, data):
        if kind == "equipment":
            if data.get("asset_no"):
                found = self.repository.find_entities("equipment", "asset_no", data["asset_no"])
                if found:
                    return found[0]
            return self._resolve_equipment(data)
        if kind == "alarm":
            equip = self._resolve_equipment(data)
            code = data.get("code")
            if equip and code:
                for alarm in self.repository.list_entities("alarm"):
                    if (
                        alarm["data"].get("equipment_id") == equip["id"]
                        and alarm["data"].get("code") == code
                    ):
                        return alarm
            return None
        if kind in ("rescue_job", "permit"):
            key = data.get("dedupe_key")
            if key:
                for entity in self.repository.list_entities(kind):
                    if entity["data"].get("dedupe_key") == key:
                        return entity
            return None
        if data.get("id"):
            return self.repository.get_entity(data["id"])
        return None

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

    def merge_batches(self):
        return self.repository.list_merge_batches()

    def merge_batch(self, batch_id):
        batch = self.repository.get_merge_batch(batch_id)
        if not batch:
            raise NotFoundError("merge batch not found: " + batch_id)
        return batch

    def resume_merge_batch(self, actor, batch_id):
        batch = self.repository.get_merge_batch(batch_id)
        if not batch:
            raise NotFoundError("merge batch not found: " + batch_id)
        return self._run_batch(actor, batch_id)
