# Synthetic verification

Python 3.10 or newer:

```sh
python3 -m unittest discover -s aeon_sharedwrites -v
python3 -m unittest discover -s aeon_readonly -v
python3 -m pytest -q
```

Storage/filter/socket tests use temporary synthetic data. Linux SO_PEERCRED
tests skip on other operating systems. The production-query tests execute the
actual repository definitions against a temporary database, including the
installed SQLite/libSQL backend. Optional vector atomicity uses a local stub.
GitHub CI runs Linux Python/backend combinations without credentials or live
data. Tone tests run standalone; full provider lifecycle tests require the
Hermes host runtime and otherwise skip explicitly. Set PYTHONPATH to that
runtime when running the provider tests. Embedding uses the `none` provider.

The [general foundation](general-brain-foundation.md) fixtures explicitly apply
`brain_service/migration.sql` after the existing shared-writer migration. Its
tests cover two agents, immutable imports, complete snapshots, transactional
rollback, CAS and replay, topic/context filters, general index watermarks,
expiry, stale-index revocation and synthetic restricted-content canaries.
Provider tests exercise the same policy across recall and digest, including
missing-schema fail-closed behavior. No production migration or real records
are needed. The new gateway is a library boundary, not a deployed service.
Security regressions verify bounded shared-lineage work, depth-sensitive caching,
fresh revocation checks, no partial output on budget exhaustion, write rollback,
decoded nested-string screening and preservation of the envelope size bound.

For systemd path/service/timer behavior, run `systemd_fixture.py` in an isolated
Linux user manager. It creates uniquely named transient units and temporary
synthetic stores, injects a failure, checks concurrent/completion-window writes
and missing-hint recovery, and stops/resets its units in finally. It does not
install units or touch live services. This is opt-in, not a CI dependency.

```sh
python3 aeon_sharedwrites/systemd_fixture.py
```

For performance, after approving read access to the chosen source:

```sh
python3 aeon_sharedwrites/benchmark_latency.py --source /path/to/approved/aeon.db
```

The benchmark opens the source read-only, publishes only the filtered scope to
a private temporary directory, prints aggregate timings/counts and deletes the
outputs. It also measures synthetic Unix-socket owned-note reads. It makes no
provider/API calls. Measurements are not latency guarantees.

These tests do not certify an installed gateway, transport, mount namespace,
every concurrency interleaving or external replication. Perform the deployment
smoke checks before admitting real records.
