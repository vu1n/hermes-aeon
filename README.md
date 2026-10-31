# hermes-aeon

Connects [Hermes-Agent](https://github.com/NousResearch/hermes-agent) to
[Aeon Emerald](https://github.com/vu1n/aeon-emerald), the owner's shared memory. The old
libSQL brain this repository used to hold lives on the `legacy-sqlite-brain` branch and is retired.

Most of the integration is not code. Hermes reaches Emerald as an ordinary MCP server, so recall
(`search`, `context`, `fetch`, `timeline`) and saving (`capture`, `correct`) are Emerald's own tools.
This repository holds the two things MCP cannot do:

- **Session capture.** Hermes hands the full transcript only to a memory provider, at a real
  session end. This repository is that provider, `aeon-emerald`, and it does nothing else: each
  conversation is captured as a `transcript` under Hermes's own principal, dated when it ended, and
  Emerald extracts what matters.
  - Sources are `hermes-session:<id>:<segment>:<part>`. The segment is the session's latest
    compaction summary ("0" before any), so a compacted session's later transcript never replaces
    the full one. Hermes's compaction summaries themselves are left out: the user never said them.
    When Hermes folds a summary into a real message, that message keeps its own words.
  - A session left with `/resume` or `/branch` (which never reaches `on_session_end`) is saved from
    the last transcript Hermes passed to `sync_turn`, always under that transcript's own session. A
    rewind drops that snapshot, so turns the user undid are never stored; a session left right after
    a rewind, before another turn, is saved only if it is later ended. A branch saves its whole
    transcript, inherited turns included. Cron runs and subagents are skipped.
  - Every capture is written to `$HERMES_HOME/aeon-emerald/spool/` first and drained oldest first.
    Only Emerald refusing a capture's content (or an unreadable file) moves it to `spool/rejected/`.
    No reply from Emerald stops the drain; any other failure (a rotated key, FORBIDDEN, an Emerald
    error) leaves the capture for the next session end and moves on to the next one. When Hermes
    releases an agent without ending its session (a settings change), the unsaved transcript is
    spooled and sent at the next session end.
  - `$HERMES_HOME/aeon-emerald/acknowledged-sessions` lists the captures Emerald acknowledged; they
    are never sent again. Erasing an episode in Emerald also erases its replay record, so resending
    an unchanged part would store it again.
- **Cron jobs** in `jobs/`, plain MCP clients sharing `client.py`:
  - `oura_pull.py` (no-agent job): Oura daily summaries, one document per category and day
    (`oura:<category>:<day>`), health-sensitive, under a principal of its own. Days Emerald
    acknowledged are recorded in `$HERMES_HOME/aeon-emerald/acknowledged-oura` and not sent again.
  - `digest_inputs.py`: new FreshRSS items and the last day's Emerald timeline, printed for the
    digest job's prompt to summarize. FreshRSS is read only (the account is the owner's own). A
    crawl-time watermark in `$HERMES_HOME/aeon-emerald/freshrss-watermark`, passed back as `ot`,
    moves once per day, so a digest Hermes re-runs after a failed model call sees the same items.

## Install

```bash
git clone https://github.com/vu1n/hermes-aeon /opt/hermes-aeon
ln -s /opt/hermes-aeon ~/.hermes/plugins/aeon-emerald
hermes config set memory.provider aeon-emerald
hermes config set mcp_servers.aeon.url https://aeon.example.com/mcp
hermes config set mcp_servers.aeon.headers.Authorization 'Bearer ${AEON_EMERALD_KEY}'
```

Mint the keys in Emerald (`npm run admin -- key NAME`). Emerald requires read on whatever a
principal writes, so the Oura principal gets `read,capture --project health --source text
--sensitive health` and nothing else.

Hermes runs cron scripts only from `$HERMES_HOME/scripts/` and refuses symlinks out of it, so each
job is a two-line launcher there:

```python
import runpy
runpy.run_path("/opt/hermes-aeon/jobs/oura_pull.py", run_name="__main__")
```

## Settings

Hermes scrubs secrets from cron script environments, so the jobs read `$HERMES_HOME/.env` directly.

| Setting | Used by | |
| --- | --- | --- |
| `AEON_EMERALD_URL` | all | Emerald's `/mcp` endpoint |
| `AEON_EMERALD_KEY` | provider, digest | Hermes's principal (read, capture) |
| `AEON_EMERALD_OURA_KEY` | Oura | the Oura principal |
| `OURA_PAT` | Oura | Oura personal access token |
| `FRESHRSS_URL`, `FRESHRSS_USER` | digest | Google Reader API base and user |
| `FRESHRSS_PASSWORD_FILE`, `FRESHRSS_PASSWORD_VAR` | digest | where the FreshRSS deployment keeps the API password |

The FreshRSS API password is never copied to another file: the digest reads it at run time from
the FreshRSS deployment's own env file and keeps it in memory. That file is its only source.

## Tests

```bash
uv run --no-project --with pytest --with httpx -- python -m pytest -q
```

`HERMES_AGENT_PATH=/usr/local/lib/hermes-agent` runs the provider tests against the real Hermes
`MemoryProvider` instead of a stub.
