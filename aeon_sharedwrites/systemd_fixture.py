"""Disposable Linux user-manager integration; only temporary synthetic databases.

Run as root in VPS staging. No installed unit, live service, or canonical file
is touched. Unique transient path/service/timer units are stopped in finally.
"""
import concurrent.futures
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT.parent))
sys.path.insert(0,str(ROOT/'read_adapter'))
from aeon_sharedwrites import shared_writer as writer
import refresh_event

def append(path,event):
    with path.open('a') as stream:stream.write(json.dumps(event)+'\n')

def worker(directory):
    root=Path(directory)
    def publish(source,output,group):
        if (root/'fail-next').exists():
            (root/'fail-next').unlink()
            append(root/'runs',dict(kind='injected_failure',at=time.monotonic()))
            raise OSError('Synthetic refresh failure')
        result=refresh_event.publish(source,output,group)
        append(root/'runs',dict(kind='snapshot',count=result['accepted'],at=time.monotonic()))
        # Keep a known window after snapshot, before service completion.
        time.sleep(.4)
        append(root/'runs',dict(kind='completion',at=time.monotonic()))
        return result
    refresh_event.refresh_once(root/'source.sqlite',root/'projection.sqlite',root/'dirty',publisher=publish,failure_backoff=.1)

def integration():
    if sys.platform!='linux':raise SystemExit('Linux systemd required')
    os.environ['XDG_RUNTIME_DIR']='/run/user/'+str(os.getuid())
    suffix=uuid.uuid4().hex[:12]
    unit='aeon-synthetic-refresh-'+suffix
    recovery='aeon-synthetic-reconcile-'+suffix
    def manager(*args,check=True):
        return subprocess.run(['systemctl','--user',*args],check=check,capture_output=True,text=True)
    def run(*args):
        subprocess.run(['systemd-run','--user','--quiet',*args],check=True,capture_output=True,text=True)
    with tempfile.TemporaryDirectory(prefix='aeon-systemd-synthetic-') as directory:
        root=Path(directory);source=root/'source.sqlite';output=root/'projection.sqlite'
        db=sqlite3.connect(source,isolation_level=None);db.execute('PRAGMA journal_mode=WAL')
        for name in ['base_schema_fixture.sql','migration.sql']:db.executescript((ROOT/name).read_text())
        db.close();writer.REFRESH_MARKER=str(root/'dirty')
        expected=0
        def capture(i,signal=True):
            db=sqlite3.connect(source,isolation_level=None,timeout=.1)
            try:
                if signal:
                    return writer.capture(db,consumer_id='dot' if i%2 else 'hermes',request_id='r'+str(i),type='note',domain='work',source='chat:dot',content='Synthetic project note '+str(i),entry_kind='idea',attribution_basis='user_explicit')
                # Model the narrow crash window: committed source mutation, no hint.
                old=writer.REFRESH_MARKER;writer.REFRESH_MARKER=str(root/'missing'/'dirty')
                try:return capture(i)
                finally:writer.REFRESH_MARKER=old
            finally:db.close()
        def events():
            return [json.loads(line) for line in (root/'runs').read_text().splitlines()] if (root/'runs').exists() else []
        def count():
            if not output.exists():return -1
            db=sqlite3.connect('file:'+str(output)+'?mode=ro',uri=True)
            try:return db.execute('SELECT count(*) FROM approved_memories').fetchone()[0]
            finally:db.close()
        def wait(predicate,timeout=15):
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                if predicate():return
                time.sleep(.01)
            raise AssertionError('Synthetic systemd condition timed out')
        try:
            run('--unit='+unit,'--path-property=PathExists='+str(root/'dirty'),
                '--property=Type=oneshot','--property=StartLimitIntervalSec=0',sys.executable,str(Path(__file__).resolve()),'worker',directory)
            capture(0);expected=1
            wait(lambda:any(e['kind']=='snapshot' for e in events()))
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:list(pool.map(capture,range(1,13)))
            expected+=12
            wait(lambda:count()==expected)
            # Burst commits while previous service exits/rechecks PathExists.
            for start in [13,19,25]:
                with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:list(pool.map(capture,range(start,start+6)))
                expected+=6;wait(lambda:count()==expected)
            (root/'fail-next').touch();capture(31);expected+=1
            wait(lambda:any(e['kind']=='injected_failure' for e in events()))
            wait(lambda:count()==expected)
            wait(lambda:not (root/'dirty').exists() and manager('is-active',unit+'.service',check=False).stdout.strip() not in {'active','activating'})
            capture(32,signal=False);expected+=1
            assert not (root/'dirty').exists()
            run('--unit='+recovery,'--on-active=1s','--timer-property=AccuracySec=10ms',
                '--property=Type=oneshot',sys.executable,str(Path(__file__).resolve()),'worker',directory)
            wait(lambda:count()==expected)
            summary=dict(canonical_rows=expected,projection_rows=count(),snapshots=sum(e['kind']=='snapshot' for e in events()),injected_failures=sum(e['kind']=='injected_failure' for e in events()),lost_wakeups=0,transient_timer_repaired_missing_hint=True)
            print(json.dumps(summary))
        finally:
            manager('stop',unit+'.path',unit+'.service',recovery+'.timer',recovery+'.service',check=False)
            manager('reset-failed',unit+'.path',unit+'.service',recovery+'.timer',recovery+'.service',check=False)

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='worker':worker(sys.argv[2])
    else:integration()
