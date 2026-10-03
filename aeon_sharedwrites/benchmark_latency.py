"""Rebuild benchmark: read-only source, private temporary filtered output only."""
import argparse
import json
import os
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT.parent));sys.path.insert(0,str(ROOT/'read_adapter'))
from aeon_sharedwrites import broker,mcp_adapter,shared_writer as writer
from publish import publish

def benchmark(source,trials=3):
    with tempfile.TemporaryDirectory(prefix='aeon-private-latency-') as directory:
        root=Path(directory);elapsed=[]
        for index in range(trials):
            start=time.perf_counter();result=publish(source,root/'filtered.sqlite');elapsed.append(time.perf_counter()-start)
        synthetic=root/'synthetic.sqlite'
        db=sqlite3.connect(synthetic,isolation_level=None)
        for name in ['base_schema_fixture.sql','migration.sql']:db.executescript((ROOT/name).read_text())
        writer.REFRESH_MARKER=str(root/'dirty')
        mid=writer.capture(db,consumer_id='dot',request_id='synthetic-r1',type='note',domain='work',source='chat:dot',content='Synthetic project note',entry_kind='idea',attribution_basis='user_explicit')['memory_id']
        db.close();publish(synthetic,root/'synthetic-projection.sqlite')
        server=broker.Server(str(root/'synthetic.sock'),synthetic,{os.getuid():'dot'})
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        store=mcp_adapter.Store(root/'synthetic-projection.sqlite',str(root/'synthetic.sock'));reads=[]
        try:
            for _ in range(100):
                start=time.perf_counter();item=store.call('aeon_get',{'id':mid})['item'];reads.append(time.perf_counter()-start)
                assert item['current_revision']==1
        finally:store.db.close();server.shutdown();server.server_close();worker.join()
        print(json.dumps(dict(rebuild_seconds=elapsed,rebuild_median_seconds=statistics.median(elapsed),examined_rows=result['examined_in_scope'],accepted_rows=result['accepted'],canonical_source_untouched=True,synthetic_socket_reads=100,synthetic_read_median_ms=statistics.median(reads)*1000,synthetic_read_p95_ms=sorted(reads)[94]*1000)))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source',required=True)
    args=parser.parse_args();benchmark(args.source)
