"""Common transactional writer for Hermes and a local write broker.

No connection creation, schema mutation, external requests or provider imports.
Install this exact module once in Hermes' store directory and load that same
file from the broker. Connections must be exclusive to one operation/worker.
"""
import hashlib
import json
import math
import os
import random
import re
import time
import uuid

DOMAINS = {"inbox","work","side_projects","learning","health","life_admin","people_comms"}
TYPES = {"note","link","email","thread","task","health_log","file","calendar_event","contact","other"}
KINDS = {"idea","decision","preference","project_context","imported_record"}
BASES = {"user_explicit","assistant_inferred","user_corrected","source_import"}
REFRESH_MARKER = '/var/lib/aeon-events/dirty'

def notify_refresh():
    """Post-commit hint only; failure cannot turn a committed write into failure.

    One persistent marker coalesces commits. The root refresh worker consumes it
    before taking its snapshot; concurrent later commits create another marker.
    Periodic reconciliation recovers crashes between commit and notification.
    """
    try:
        fd=os.open(REFRESH_MARKER,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o660)
        os.close(fd)
    except OSError:
        pass

class WriteError(Exception):
    code = "write_failed"

class Conflict(WriteError):
    code = "revision_conflict"
    def __init__(self, revision=None):
        self.current_revision=revision

class IdempotencyConflict(WriteError):
    code = "request_id_conflict"

class NotFound(WriteError):
    code = "not_found_or_ineligible"

class Busy(WriteError):
    code = "write_busy"

class Invalid(WriteError):
    code = "invalid_write"


def text(value, maximum, optional=True):
    if value is None and optional:return None
    if not isinstance(value,str) or len(value)>maximum:raise Invalid()
    return value

def identity(value):
    if not isinstance(value,str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}",value):raise Invalid()
    return value

def number(value, optional=True):
    if value is None and optional:return None
    if type(value) is not int or value<0:raise Invalid()
    return value

def provenance(consumer_id,request_id,entry_kind,attribution_basis,conversation_ref):
    identity(consumer_id);identity(request_id)
    if entry_kind not in KINDS or attribution_basis not in BASES:raise Invalid()
    return {"consumer_id":consumer_id,"request_id":request_id,"entry_kind":entry_kind,
            "attribution_basis":attribution_basis,"conversation_ref":text(conversation_ref,128)}

def _digest(value):
    try:data=json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False)
    except (ValueError,TypeError):raise Invalid()
    if len(data)>1_000_000:raise Invalid()
    return hashlib.sha256(data.encode()).hexdigest()

def _busy(exc):
    return getattr(exc,"sqlite_errorcode",None) in {5,6} or any(s in str(exc).lower() for s in ["database is locked","database is busy","database table is locked"])

def _transaction(db,operation,payload,meta,action,checkpoint=None,retries=3):
    # Bound even legacy sqlite3 connections created with a multi-second timeout.
    old_timeout=db.execute('PRAGMA busy_timeout').fetchone()[0]
    db.execute('PRAGMA busy_timeout=100')
    try:
        result = _transaction_bounded(db,operation,payload,meta,action,checkpoint,retries)
        notify_refresh()
        return result
    finally:
        try:db.execute('PRAGMA busy_timeout='+str(int(old_timeout)))
        except Exception:pass

def _transaction_bounded(db,operation,payload,meta,action,checkpoint=None,retries=3):
    if type(retries) is not int or not 0<=retries<=5:raise Invalid()
    digest=_digest({"operation":operation,"payload":payload,"provenance":meta})
    # Do not roll back an unrelated caller transaction. BEGIN refusal leaves it intact.
    for attempt in range(retries+1):
        started=False
        try:
            db.execute("BEGIN IMMEDIATE");started=True
            previous=db.execute("SELECT operation,request_sha256,result_json FROM memory_write_requests WHERE consumer_id=? AND request_id=?",(meta['consumer_id'],meta['request_id'])).fetchone()
            if previous:
                if previous[0]!=operation or previous[1]!=digest:raise IdempotencyConflict()
                result=json.loads(previous[2]);db.commit();return result
            now=int(time.time()*1000)
            def point(name):
                if checkpoint is not None:checkpoint(name)
            result=action(now,point)
            point('after_mutation')
            db.execute("INSERT INTO memory_write_requests VALUES(?,?,?,?,?,?)",(meta['consumer_id'],meta['request_id'],operation,digest,json.dumps(result,sort_keys=True),now))
            point('after_idempotency')
            db.commit();started=False
            point('after_commit')
            return result
        except Exception as exc:
            if started:
                try:db.execute("ROLLBACK")
                except Exception:pass
            if not _busy(exc):raise
            if attempt==retries:raise Busy() from None
            time.sleep(min(.02*(2**attempt)+random.uniform(0,.01),.15))

def _audit(db,mid,revision,operation,meta,now,point):
    db.execute("INSERT INTO memory_write_audit VALUES(?,?,?,?,?,?,?,?,?)",(uuid.uuid4().hex,mid,revision,meta['consumer_id'],operation,meta['entry_kind'],meta['attribution_basis'],meta['conversation_ref'],now))
    point('after_audit')

def _embedding(db,mid,embedding,model,now,replace=False):
    if not embedding or not getattr(db,'has_vector',False):return
    if replace:db.execute("DELETE FROM memory_embeddings WHERE memory_id=?",(mid,))
    literal='['+','.join(f'{v:.6f}' for v in embedding)+']'
    db.execute("INSERT INTO memory_embeddings(memory_id,embedding,model,embedded_at) VALUES(?,vector32(?),?,?)",(mid,literal,model or 'unknown',now))

def capture(db,*,consumer_id,request_id,type,domain,title=None,summary=None,content=None,url=None,
            tags=None,entities=None,project_id=None,source=None,event_start=None,event_end=None,
            dedup_key=None,quality_score=None,captured_at=None,embedding=None,embedding_model=None,
            entry_kind='imported_record',attribution_basis='source_import',conversation_ref=None,
            checkpoint=None,retries=3):
    meta=provenance(consumer_id,request_id,entry_kind,attribution_basis,conversation_ref)
    if domain not in DOMAINS or type not in TYPES:raise Invalid()
    for v,n in [(title,300),(summary,2000),(content,100000),(url,2048),(source,128),(project_id,128),(dedup_key,512),(embedding_model,128)]:text(v,n)
    for v in [event_start,event_end,captured_at]:number(v)
    if quality_score is not None and (type_of(quality_score) not in (int,float) or not math.isfinite(quality_score)):raise Invalid()
    tags=[] if tags is None else tags;entities={} if entities is None else entities
    if not isinstance(tags,list) or len(tags)>100 or any(not isinstance(t,str) or len(t)>128 for t in tags) or not isinstance(entities,dict):raise Invalid()
    if embedding is not None and (not isinstance(embedding,list) or len(embedding)>4096 or any(type_of(v) not in (int,float) or not math.isfinite(v) for v in embedding)):raise Invalid()
    payload={"type":type,"domain":domain,"title":title,"summary":summary,"content":content,"url":url,"tags":tags,"entities":entities,"project_id":project_id,"source":source,"event_start":event_start,"event_end":event_end,"dedup_key":dedup_key,"quality_score":quality_score,"captured_at":captured_at,"embedding":embedding,"embedding_model":embedding_model}
    def action(now,point):
        if dedup_key is not None:
            old=db.execute("SELECT id,current_revision FROM memory_items WHERE dedup_key=?",(dedup_key,)).fetchone()
            if old:return {"memory_id":old[0],"revision":old[1],"created":False,"deduplicated":True}
        mid=uuid.uuid4().hex;ts=now if captured_at is None else captured_at
        db.execute("INSERT INTO memory_items(id,user_id,type,domain,status,title,summary,content,url,entities,tags,summary_bullets,project_id,quality_score,source,captured_at,event_start,event_end,dedup_key,current_revision) VALUES(?,'local',?,?,'active',?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",(mid,type,domain,title,summary,content,url,json.dumps(entities),json.dumps(tags),'[]',project_id,quality_score,source,ts,event_start,event_end,dedup_key))
        point('after_item')
        db.execute("INSERT INTO memory_revisions VALUES(?,1,?,?,?,?)",(mid,content,summary,source,ts));point('after_revision')
        db.execute("INSERT INTO memory_fts(memory_id,title,summary,content) VALUES(?,?,?,?)",(mid,title or '',summary or '',content or ''));point('after_fts')
        _embedding(db,mid,embedding,embedding_model,now);point('after_embedding')
        _audit(db,mid,1,'capture',meta,now,point)
        return {"memory_id":mid,"revision":1,"created":True,"deduplicated":False}
    return _transaction(db,'capture',payload,meta,action,checkpoint,retries)

# The capture signature retains Hermes' keyword `type`; avoid shadowing built-in validation.
type_of=type

def update(db,*,consumer_id,request_id,memory_id,expected_revision,content,summary=None,
           source=None,embedding=None,embedding_model=None,entry_kind='imported_record',
           attribution_basis='source_import',conversation_ref=None,checkpoint=None,retries=3,
           eligible_sources=None,eligible_domains=None):
    meta=provenance(consumer_id,request_id,entry_kind,attribution_basis,conversation_ref)
    if not isinstance(memory_id,str) or not re.fullmatch(r'[a-f0-9]{32}',memory_id):raise Invalid()
    if type(expected_revision) is not int or expected_revision<1:raise Invalid()
    text(content,100000);text(summary,2000);text(source,128);text(embedding_model,128)
    if embedding is not None and (not isinstance(embedding,list) or len(embedding)>4096 or any(type(v) not in (int,float) or not math.isfinite(v) for v in embedding)):raise Invalid()
    payload={"id":memory_id,"expected_revision":expected_revision,"content":content,"summary":summary,"source":source,"embedding":embedding,"embedding_model":embedding_model,"eligible_sources":sorted(eligible_sources) if eligible_sources is not None else None,"eligible_domains":sorted(eligible_domains) if eligible_domains is not None else None}
    def action(now,point):
        row=db.execute("SELECT current_revision,title,status,source,domain FROM memory_items WHERE id=?",(memory_id,)).fetchone()
        if not row or row[2]!='active' or (eligible_sources is not None and row[3] not in eligible_sources) or (eligible_domains is not None and row[4] not in eligible_domains):raise NotFound()
        if row[0]!=expected_revision:raise Conflict(row[0])
        rev=row[0]+1
        db.execute("INSERT INTO memory_revisions VALUES(?,?,?,?,?,?)",(memory_id,rev,content,summary,source,now));point('after_revision')
        db.execute("UPDATE memory_items SET content=?,summary=?,current_revision=? WHERE id=? AND current_revision=?",(content,summary,rev,memory_id,expected_revision));point('after_item')
        db.execute("DELETE FROM memory_fts WHERE memory_id=?",(memory_id,))
        db.execute("INSERT INTO memory_fts(memory_id,title,summary,content) VALUES(?,?,?,?)",(memory_id,row[1] or '',summary or '',content or ''));point('after_fts')
        _embedding(db,memory_id,embedding,embedding_model,now,True);point('after_embedding')
        _audit(db,memory_id,rev,'update',meta,now,point)
        return {"memory_id":memory_id,"revision":rev,"updated":True}
    return _transaction(db,'update',payload,meta,action,checkpoint,retries)
