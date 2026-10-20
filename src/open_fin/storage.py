"""Owner-local SQLite storage and atomic snapshot ingestion."""

import hashlib
import json
import os
import re
import sqlite3
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet

from open_fin.models import ConnectorError, CreateConnection, Credentials, Snapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
 id TEXT PRIMARY KEY, bank TEXT NOT NULL, label TEXT NOT NULL, mode TEXT NOT NULL,
 credentials BLOB NOT NULL, status TEXT NOT NULL DEFAULT 'created',
 created_at TEXT NOT NULL, last_sync_at TEXT, last_attempt_at TEXT, last_error_code TEXT
);
CREATE TABLE IF NOT EXISTS syncs (
 id TEXT PRIMARY KEY, connection_id TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
 status TEXT NOT NULL, requested_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
 error_code TEXT, error_message TEXT, coverage TEXT, warnings TEXT NOT NULL DEFAULT '[]'
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_sync ON syncs(connection_id)
 WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS products (
 id TEXT PRIMARY KEY, connection_id TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, label TEXT NOT NULL, last4 TEXT NOT NULL, currency TEXT NOT NULL,
 balance TEXT, last_sync_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS movements (
 id TEXT PRIMARY KEY, connection_id TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
 product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
 date TEXT NOT NULL, description TEXT NOT NULL, amount TEXT NOT NULL,
 currency TEXT NOT NULL, source TEXT NOT NULL, installments TEXT, last_sync_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS movement_lookup ON movements(connection_id,product_id,date);
PRAGMA user_version = 1;
"""


def now() -> str:
    return datetime.now(UTC).isoformat()


def identifier(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:32]
    return f"{prefix}_{digest}"


class StoreError(Exception):
    def __init__(self, code: str, retry_after: int | None = None):
        self.code = code
        self.retry_after = retry_after


class Store:
    def __init__(self, path: str, encryption_key: str):
        self.path = path
        self.vault = Fernet(encryption_key.encode())
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
        os.chmod(path, 0o600)

    @contextmanager
    def db(self, immediate: bool = False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if immediate:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def public_connection(row) -> dict:
        return {key: row[key] for key in row.keys() if key != "credentials"}

    @staticmethod
    def public_sync(row) -> dict:
        result = dict(row)
        result["warnings"] = json.loads(result["warnings"])
        return result

    def create_connection(self, payload: CreateConnection, mode: str = "live") -> dict:
        connection_id = "con_" + uuid.uuid4().hex
        credential_data = {
            "rut": payload.credentials.rut.get_secret_value(),
            "password": payload.credentials.password.get_secret_value(),
        }
        ciphertext = self.vault.encrypt(json.dumps(credential_data).encode())
        with self.db() as db:
            db.execute(
                "INSERT INTO connections(id,bank,label,mode,credentials,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (connection_id, payload.bank, payload.label, mode, ciphertext, now()),
            )
        return self.connection(connection_id)

    def connection(self, connection_id: str) -> dict:
        with self.db() as db:
            row = db.execute("SELECT * FROM connections WHERE id=?", (connection_id,)).fetchone()
        if row is None:
            raise StoreError("not_found")
        return self.public_connection(row)

    def connections(self) -> list[dict]:
        with self.db() as db:
            rows = db.execute("SELECT * FROM connections ORDER BY created_at,id").fetchall()
        return [self.public_connection(row) for row in rows]

    def credentials(self, connection_id: str) -> Credentials:
        with self.db() as db:
            row = db.execute(
                "SELECT credentials FROM connections WHERE id=?", (connection_id,)
            ).fetchone()
        if row is None:
            raise StoreError("not_found")
        return Credentials.model_validate_json(self.vault.decrypt(row["credentials"]))

    def delete_connection(self, connection_id: str) -> None:
        with self.db(immediate=True) as db:
            if not db.execute("SELECT id FROM connections WHERE id=?", (connection_id,)).fetchone():
                raise StoreError("not_found")
            if db.execute(
                "SELECT id FROM syncs WHERE connection_id=? AND status='running'", (connection_id,)
            ).fetchone():
                raise StoreError("sync_in_progress")
            db.execute("DELETE FROM connections WHERE id=?", (connection_id,))

    def enqueue(self, connection_id: str, cooldown_seconds: int) -> dict:
        requested = now()
        with self.db(immediate=True) as db:
            row = db.execute("SELECT * FROM connections WHERE id=?", (connection_id,)).fetchone()
            if row is None:
                raise StoreError("not_found")
            if db.execute(
                "SELECT id FROM syncs WHERE connection_id=? AND status IN ('queued','running')",
                (connection_id,),
            ).fetchone():
                raise StoreError("sync_in_progress")
            if row["last_attempt_at"]:
                elapsed = (
                    datetime.fromisoformat(requested)
                    - datetime.fromisoformat(row["last_attempt_at"])
                ).total_seconds()
                if elapsed < cooldown_seconds:
                    raise StoreError("cooldown", max(1, int(cooldown_seconds - elapsed) + 1))
            sync_id = "syn_" + uuid.uuid4().hex
            db.execute(
                "INSERT INTO syncs(id,connection_id,status,requested_at) VALUES (?,?,'queued',?)",
                (sync_id, connection_id, requested),
            )
            db.execute(
                "UPDATE connections SET last_attempt_at=? WHERE id=?", (requested, connection_id)
            )
        return self.sync(sync_id)

    def sync(self, sync_id: str) -> dict:
        with self.db() as db:
            row = db.execute("SELECT * FROM syncs WHERE id=?", (sync_id,)).fetchone()
        if row is None:
            raise StoreError("not_found")
        return self.public_sync(row)

    def recover_interrupted(self) -> None:
        """Caller holds the process-wide worker file lock; no browser can still be running."""
        with self.db(immediate=True) as db:
            db.execute(
                "UPDATE connections SET status='error',last_error_code='worker_interrupted' "
                "WHERE id IN (SELECT connection_id FROM syncs WHERE status='running')"
            )
            db.execute(
                "UPDATE syncs SET status='failed',finished_at=?,error_code='worker_interrupted',"
                "error_message='Worker interrupted. Request a new synchronization.' "
                "WHERE status='running'",
                (now(),),
            )

    def claim(self) -> dict | None:
        with self.db(immediate=True) as db:
            row = db.execute(
                "SELECT * FROM syncs WHERE status='queued' ORDER BY requested_at,id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE syncs SET status='running',started_at=? WHERE id=?", (now(), row["id"])
            )
            db.execute(
                "UPDATE connections SET last_attempt_at=? WHERE id=?", (now(), row["connection_id"])
            )
        return self.sync(row["id"])

    def fail(self, sync_id: str, code: str, message: str) -> None:
        with self.db(immediate=True) as db:
            db.execute(
                "UPDATE connections SET status='error',last_error_code=? "
                "WHERE id=(SELECT connection_id FROM syncs WHERE id=?)",
                (code, sync_id),
            )
            db.execute(
                "UPDATE syncs SET status='failed',finished_at=?,error_code=?,error_message=? "
                "WHERE id=?",
                (now(), code, message, sync_id),
            )

    def ingest(self, sync_id: str, snapshot: Snapshot) -> None:
        job = self.sync(sync_id)
        connection_id = job["connection_id"]
        products = {product.external_id: product for product in snapshot.products}
        if len(products) != len(snapshot.products):
            raise ConnectorError("invalid_snapshot")
        for movement in snapshot.movements:
            product = products.get(movement.product_external_id)
            if (
                product is None
                or product.currency != movement.currency
                or (product.kind == "account") != (movement.source == "account")
            ):
                raise ConnectorError("invalid_snapshot")
        timestamp = now()
        occurrences: Counter = Counter()
        with self.db(immediate=True) as db:
            for product in products.values():
                product_id = identifier(product.kind, connection_id, product.external_id)
                label = re.sub(r"\d{5,}", lambda m: "****" + m[0][-4:], product.label)
                db.execute(
                    "INSERT INTO products VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "label=excluded.label,balance=excluded.balance,last_sync_at=excluded.last_sync_at",
                    (
                        product_id,
                        connection_id,
                        product.kind,
                        label,
                        product.last4,
                        product.currency,
                        str(product.balance) if product.balance is not None else None,
                        timestamp,
                    ),
                )
            for movement in snapshot.movements:
                product = products[movement.product_external_id]
                product_id = identifier(product.kind, connection_id, product.external_id)
                if movement.bank_movement_id:
                    key = (product_id, movement.source, movement.bank_movement_id)
                else:
                    key = (
                        product_id,
                        movement.source,
                        movement.date.isoformat(),
                        movement.description,
                        str(movement.amount.normalize()),
                        movement.currency,
                        movement.installments or "",
                    )
                occurrences[key] += 1
                occurrence = "1" if movement.bank_movement_id else str(occurrences[key])
                movement_id = identifier("mov", *key, occurrence)
                db.execute(
                    "INSERT INTO movements VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) "
                    "DO UPDATE SET description=excluded.description,amount=excluded.amount,"
                    "date=excluded.date,installments=excluded.installments,"
                    "last_sync_at=excluded.last_sync_at",
                    (
                        movement_id,
                        connection_id,
                        product_id,
                        movement.date.isoformat(),
                        movement.description,
                        str(movement.amount),
                        movement.currency,
                        movement.source,
                        movement.installments,
                        timestamp,
                    ),
                )
            db.execute(
                "UPDATE connections SET status='connected',last_sync_at=?,last_error_code=NULL "
                "WHERE id=?",
                (timestamp, connection_id),
            )
            db.execute(
                "UPDATE syncs SET status='succeeded',finished_at=?,coverage=?,warnings=? "
                "WHERE id=?",
                (timestamp, snapshot.coverage, json.dumps(snapshot.warnings), sync_id),
            )

    def query(
        self,
        resource: str,
        *,
        connection_id: str | None = None,
        product_id: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        currency: str | None = None,
        source: str | None = None,
        limit: int = 100,
        offset: int = 0,
        mode: str | None = None,
    ) -> dict:
        if connection_id:
            connection = self.connection(connection_id)
            if mode and connection["mode"] != mode:
                raise StoreError("mode_mismatch")
        clauses, values = [], []
        if mode:
            clauses.append("connection_id IN (SELECT id FROM connections WHERE mode=?)")
            values.append(mode)
        if resource in {"accounts", "cards"}:
            table = "products"
            clauses.append("kind=?")
            values.append("account" if resource == "accounts" else "card")
        elif resource == "movements":
            table = "movements"
        else:
            raise ValueError("Unsupported resource")
        if product_id:
            with self.db() as db:
                product = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
            if product is None or (connection_id and product["connection_id"] != connection_id):
                raise StoreError("not_found")
            if mode and self.connection(product["connection_id"])["mode"] != mode:
                raise StoreError("mode_mismatch")
        for column, value in [
            ("connection_id", connection_id),
            ("product_id", product_id),
            ("currency", currency),
            ("source", source),
        ]:
            if value is not None:
                clauses.append(column + "=?")
                values.append(value)
        for operator, value in [(">=", from_date), ("<=", to_date)]:
            if value is not None:
                clauses.append("date" + operator + "?")
                values.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        order = "date DESC,id" if resource == "movements" else "id"
        with self.db() as db:
            total = db.execute(f"SELECT count(*) FROM {table}{where}", values).fetchone()[0]
            rows = db.execute(
                f"SELECT * FROM {table}{where} ORDER BY {order} LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
