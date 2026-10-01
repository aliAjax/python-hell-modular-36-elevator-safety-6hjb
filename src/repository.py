import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def transaction(self):
        """Single BEGIN IMMEDIATE transaction shared by several writes.

        Entity writes, cascade invalidations and sync checkpoints commit
        together so a crash mid-item never leaves a confirmed step without its
        checkpoint (and vice versa).
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS sync_batches (
                    id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total INTEGER NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sync_batches_status
                    ON sync_batches(status, created_at);
                CREATE TABLE IF NOT EXISTS sync_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    record TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(batch_id, seq)
                );
                CREATE INDEX IF NOT EXISTS idx_sync_items_batch
                    ON sync_items(batch_id, seq);
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _dump(data):
        return json.dumps(data, ensure_ascii=False, sort_keys=True)

    def create_entity(self, entity_id, kind, status, data, actor_id, conn=None):
        now = utcnow()
        payload = self._dump(data)
        params = (entity_id, kind, status, payload, actor_id, now, now)
        sql = (
            "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?)"
        )
        if conn is not None:
            conn.execute(sql, params)
            return self.get_entity(entity_id, conn=conn)
        with self._connect() as connection:
            connection.execute(sql, params)
        return self.get_entity(entity_id)

    def get_entity(self, entity_id, conn=None):
        sql = "SELECT * FROM entities WHERE id = ?"
        if conn is not None:
            row = conn.execute(sql, (entity_id,)).fetchone()
            return self._entity_from_row(row) if row else None
        with self._connect() as connection:
            row = connection.execute(sql, (entity_id,)).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None, conn=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = "SELECT * FROM entities" + where + " ORDER BY created_at, id"
        if conn is not None:
            rows = conn.execute(sql, params).fetchall()
            return [self._entity_from_row(row) for row in rows]
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value, conn=None):
        entities = self.list_entities(kind=kind, conn=conn)
        if field == "*":
            return entities
        return [
            entity
            for entity in entities
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data, conn=None):
        now = utcnow()
        payload = self._dump(data)
        sql_params = (status, payload, now, entity_id)

        def _work(connection):
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                sql_params + (current_version,),
            )
            return self.get_entity(entity_id, conn=connection)

        if conn is not None:
            return _work(conn)
        with self.transaction() as connection:
            return _work(connection)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail, conn=None):
        params = (
            entity_id,
            actor_id,
            actor_role,
            action,
            from_status,
            to_status,
            self._dump(detail or {}),
            utcnow(),
        )
        sql = (
            "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        )
        if conn is not None:
            conn.execute(sql, params)
            return
        with self._connect() as connection:
            connection.execute(sql, params)

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id, conn=None):
        sql = (
            "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
            "VALUES (?, ?, ?, ?)"
        )
        params = (actor_id, idem_key, entity_id, utcnow())
        if conn is not None:
            conn.execute(sql, params)
            return
        with self._connect() as connection:
            connection.execute(sql, params)

    # --- offline sync batches -------------------------------------------------

    @staticmethod
    def _batch_from_row(row):
        return {
            "id": row["id"],
            "actor_id": row["actor_id"],
            "actor_role": row["actor_role"],
            "status": row["status"],
            "total": int(row["total"]),
            "detail": json.loads(row["detail"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _item_from_row(row):
        return {
            "id": int(row["id"]),
            "batch_id": row["batch_id"],
            "seq": int(row["seq"]),
            "status": row["status"],
            "result": json.loads(row["result"]) if row["result"] else None,
            "record": json.loads(row["record"]),
            "updated_at": row["updated_at"],
        }

    def create_sync_batch(self, batch_id, actor, total, detail=None, conn=None):
        now = utcnow()
        params = (batch_id, actor.user_id, actor.role, "pending", total, self._dump(detail or {}), now, now)
        sql = (
            "INSERT INTO sync_batches(id, actor_id, actor_role, status, total, detail, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        )
        if conn is not None:
            conn.execute(sql, params)
            return self.get_sync_batch(batch_id, conn=conn)
        with self._connect() as connection:
            connection.execute(sql, params)
        return self.get_sync_batch(batch_id)

    def add_sync_items(self, batch_id, records, conn=None):
        now = utcnow()
        sql = (
            "INSERT INTO sync_items(batch_id, seq, status, result, record, updated_at) "
            "VALUES (?, ?, 'pending', NULL, ?, ?)"
        )
        params = [(batch_id, seq, self._dump(record), now) for seq, record in enumerate(records)]
        if conn is not None:
            conn.executemany(sql, params)
            return
        with self._connect() as connection:
            connection.executemany(sql, params)

    def get_sync_batch(self, batch_id, conn=None):
        sql = "SELECT * FROM sync_batches WHERE id = ?"
        if conn is not None:
            row = conn.execute(sql, (batch_id,)).fetchone()
        else:
            with self._connect() as connection:
                row = connection.execute(sql, (batch_id,)).fetchone()
        return self._batch_from_row(row) if row else None

    def list_sync_batches(self, status=None, conn=None):
        if status:
            sql = "SELECT * FROM sync_batches WHERE status = ? ORDER BY created_at, id"
            params = (status,)
        else:
            sql = "SELECT * FROM sync_batches ORDER BY created_at, id"
            params = ()
        if conn is not None:
            rows = conn.execute(sql, params).fetchall()
        else:
            with self._connect() as connection:
                rows = connection.execute(sql, params).fetchall()
        return [self._batch_from_row(row) for row in rows]

    def update_sync_batch(self, batch_id, status, detail=None, conn=None):
        now = utcnow()
        if detail is None:
            sql = "UPDATE sync_batches SET status = ?, updated_at = ? WHERE id = ?"
            params = (status, now, batch_id)
        else:
            sql = "UPDATE sync_batches SET status = ?, detail = ?, updated_at = ? WHERE id = ?"
            params = (status, self._dump(detail), now, batch_id)
        if conn is not None:
            conn.execute(sql, params)
            return self.get_sync_batch(batch_id, conn=conn)
        with self._connect() as connection:
            connection.execute(sql, params)
        return self.get_sync_batch(batch_id)

    def get_sync_items(self, batch_id, conn=None):
        sql = "SELECT * FROM sync_items WHERE batch_id = ? ORDER BY seq"
        if conn is not None:
            rows = conn.execute(sql, (batch_id,)).fetchall()
        else:
            with self._connect() as connection:
                rows = connection.execute(sql, (batch_id,)).fetchall()
        return [self._item_from_row(row) for row in rows]

    def list_pending_sync_items(self, batch_id, conn=None):
        """Items whose step is not confirmed; only these are retried."""
        sql = (
            "SELECT * FROM sync_items WHERE batch_id = ? "
            "AND status IN ('pending', 'blocked', 'conflict', 'failed') ORDER BY seq"
        )
        if conn is not None:
            rows = conn.execute(sql, (batch_id,)).fetchall()
        else:
            with self._connect() as connection:
                rows = connection.execute(sql, (batch_id,)).fetchall()
        return [self._item_from_row(row) for row in rows]

    def update_sync_item(self, seq, batch_id, status, result, conn=None):
        now = utcnow()
        sql = "UPDATE sync_items SET status = ?, result = ?, updated_at = ? WHERE batch_id = ? AND seq = ?"
        params = (status, self._dump(result or {}), now, batch_id, seq)
        if conn is not None:
            conn.execute(sql, params)
            return
        with self._connect() as connection:
            connection.execute(sql, params)

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
