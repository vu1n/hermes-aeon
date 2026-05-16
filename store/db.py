"""libSQL connection + schema bootstrap. Falls back to sqlite3 when libsql isn't available (vector queries become no-ops)."""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    import libsql_experimental as libsql  # type: ignore
    HAS_LIBSQL = True
except ImportError:
    libsql = None  # type: ignore
    HAS_LIBSQL = False


class AeonDB:
    """Thin connection wrapper. Uses libSQL when available, sqlite3 otherwise."""

    def __init__(
        self,
        db_path: str,
        turso_url: Optional[str] = None,
        turso_token: Optional[str] = None,
    ):
        self.db_path = db_path
        self._turso_url = turso_url or os.getenv("HERMES_AEON_TURSO_URL") or None
        self._turso_token = turso_token or os.getenv("HERMES_AEON_TURSO_TOKEN") or None
        self._conn: Any = None
        self.has_vector: bool = False

    def connect(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        if HAS_LIBSQL:
            if self._turso_url and self._turso_token:
                self._conn = libsql.connect(
                    self.db_path,
                    sync_url=self._turso_url,
                    auth_token=self._turso_token,
                )
                try:
                    self._conn.sync()
                except Exception as e:
                    logger.warning("turso sync on connect failed: %s", e)
            else:
                self._conn = libsql.connect(self.db_path)
        else:
            logger.warning(
                "libsql-experimental not installed — vector search disabled, FTS5-only mode"
            )
            self._conn = sqlite3.connect(self.db_path)
            self._conn.execute("PRAGMA journal_mode=WAL")

    def bootstrap_schema(self) -> None:
        schema_path = Path(__file__).parent / "schema.sql"
        sql = schema_path.read_text(encoding="utf-8")
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        for stmt in statements:
            try:
                self._conn.execute(stmt)
            except Exception as e:
                # F32_BLOB is libSQL-only; embeddings table fails silently on stock sqlite.
                if "F32_BLOB" in stmt or "vector" in stmt.lower():
                    logger.debug("vector schema skipped: %s", e)
                else:
                    logger.warning("schema stmt failed: %s -- %s", e, stmt[:80])
        self._conn.commit()
        self.has_vector = self._probe_vector()

    def _probe_vector(self) -> bool:
        try:
            self._conn.execute("SELECT vector_distance_cos(vector32('[0.1,0.2]'), vector32('[0.1,0.2]'))").fetchone()
            return True
        except Exception:
            return False

    def execute(self, sql: str, params: tuple = ()) -> Any:
        return self._conn.execute(sql, params)

    def executemany(self, sql: str, seq: list) -> Any:
        return self._conn.executemany(sql, seq)

    def commit(self) -> None:
        self._conn.commit()

    def sync(self) -> None:
        if HAS_LIBSQL and self._turso_url and self._conn is not None:
            try:
                self._conn.sync()
            except Exception as e:
                logger.warning("turso sync failed: %s", e)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


def emb_to_libsql_literal(embedding: list[float]) -> str:
    """Format an embedding as the JSON-style string libSQL's vector32() expects."""
    return "[" + ",".join(f"{v:.6f}" for v in embedding) + "]"
