#!/usr/bin/env python3
"""Automatic source-category projection; no source writes or human-review claim."""
import argparse
import fcntl
import grp
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from adapter import DOMAINS, FIELDS, SCOPE, SOURCES, Store

SOURCE_FIELDS = FIELDS[:-2]

def publish(source, output, reader_group=None):
    source, output = Path(source).resolve(strict=True), Path(output).resolve()
    if source == output or str(output) in {str(source)+"-wal", str(source)+"-shm"}:
        raise ValueError("Output must be separate from source")
    reader_gid = None
    if reader_group is not None:
        if os.geteuid() != 0: raise ValueError("Reader-group grant requires root")
        reader_gid = grp.getgrnam(reader_group).gr_gid
    output.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(output.parent / ".publish.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    src = dst = None
    temp = None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        src = sqlite3.connect(source.as_uri()+"?mode=ro", uri=True, timeout=1)
        src.row_factory = sqlite3.Row
        src.execute("PRAGMA query_only=ON")
        src.execute("PRAGMA trusted_schema=OFF")
        deadline = time.monotonic() + 30
        src.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        src.execute("BEGIN")
        now = int(time.time()*1000)
        fd, temp = tempfile.mkstemp(prefix=".aeon-projection-", dir=output.parent)
        os.close(fd)
        dst = sqlite3.connect(temp)
        dst.executescript(Path(__file__).with_name("projection_schema.sql").read_text())
        dst.execute("INSERT OR REPLACE INTO projection_metadata VALUES('published_at_ms',?)", (str(now),))
        dst.execute("INSERT INTO projection_metadata VALUES('approval_basis','source-category scope; automatic screening; not individual human review')")
        sources, domains = sorted(SOURCES), sorted(DOMAINS)
        where = ("source IN ("+",".join("?" for _ in sources)+") AND domain IN ("+
                 ",".join("?" for _ in domains)+") AND status='active' AND type IN ('link','note')")
        rows = src.execute("SELECT "+",".join(SOURCE_FIELDS)+" FROM memory_items WHERE "+where+" ORDER BY id", sources+domains)
        accepted = excluded = examined = content_bytes = 0
        for raw in rows:
            examined += 1
            if examined > 100_000 or time.monotonic() > deadline: raise ValueError("Snapshot budget exceeded")
            row = dict(raw)
            row.update(screened_at=now, scope_version=SCOPE)
            if Store.safe(row, full=True) is None:
                excluded += 1; continue
            content_bytes += sum(len((row[k] or "").encode()) for k in ("title","summary","content","url"))
            if content_bytes > 100_000_000: raise ValueError("Byte budget exceeded")
            dst.execute("INSERT INTO approved_memories("+",".join(FIELDS)+") VALUES("+",".join("?" for _ in FIELDS)+")", tuple(row[k] for k in FIELDS))
            accepted += 1
        dst.commit(); dst.close(); dst = None
        src.close(); src = None
        if reader_gid is None: os.chmod(temp, 0o600)
        else: os.chown(temp, 0, reader_gid); os.chmod(temp, 0o640)
        with open(temp, "rb") as f: os.fsync(f.fileno())
        os.replace(temp, output); temp = None
        dir_fd = os.open(output.parent, os.O_RDONLY)
        try: os.fsync(dir_fd)
        finally: os.close(dir_fd)
        return {"accepted": accepted, "excluded_by_screening": excluded, "examined_in_scope": examined, "published_at_ms": now}
    finally:
        if src is not None: src.close()
        if dst is not None: dst.close()
        if temp is not None and os.path.exists(temp): os.unlink(temp)
        os.close(lock_fd)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--reader-group", help="Explicit approved read-only group grant; root only")
    args = p.parse_args()
    try: result = publish(args.source, args.output, args.reader_group)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        raise SystemExit("Projection refresh failed; previous projection retained; source unchanged")
    print(json.dumps(result))

if __name__ == "__main__": main()
