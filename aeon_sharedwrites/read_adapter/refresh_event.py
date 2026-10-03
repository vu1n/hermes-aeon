#!/usr/bin/env python3
"""Consume a coalesced dirty hint, then publish the usual filtered snapshot."""
import argparse
import os
import time
from pathlib import Path
if __package__:
    from .publish import publish
else:
    from publish import publish

def refresh_once(source, output, marker, reader_group=None, coalesce=.2, publisher=publish, failure_backoff=0):
    # Clearing BEFORE the snapshot prevents dropping a concurrent later commit.
    # PathExists retriggers after service completion if another marker appears.
    marker=Path(marker)
    time.sleep(coalesce)
    try:
        try:marker.unlink()
        except FileNotFoundError:pass
        return publisher(source,output,reader_group)
    except Exception:
        try:
            fd=os.open(marker,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o660);os.close(fd)
        except OSError:pass
        # PathExists can retrigger even on failure. Bound that loop independently
        # of systemd's RestartSec; unit activation behavior must not spin on errors.
        time.sleep(failure_backoff)
        raise

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--marker',required=True)
    parser.add_argument('--reader-group')
    args=parser.parse_args()
    refresh_once(args.source,args.output,args.marker,args.reader_group,failure_backoff=5)

if __name__=='__main__':main()
