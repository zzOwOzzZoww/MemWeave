"""Write-time, scoped assertion slots and explicit version replacement.

The extractor intentionally recognizes only single, unconditional assignments.
It does not infer truth, semantic equivalence, or recency from model prose.
"""
import hashlib
import re
import unicodedata
from decimal import Decimal

VERSION = 'assertion-slots-v1'

def fingerprint(row):
    return hashlib.sha256('\x1f'.join(str(row[k]) for k in
        ('title','content','scope','project_key')).encode()).hexdigest()

def extract(content, kind):
    if kind not in {'decision','preference'} or len(content) > 1000:
        return None
    text = unicodedata.normalize('NFKC', content).strip()
    # Conditional, speculative, quoted, comparative and scheduled statements
    # require explicit review; never flatten their conditions into one value.
    if re.search(r'[?？“”"`]|如果|假如|(?:^|[，。；])若|可能|假设|示例|旧|原来|以前|改为|调整为|替代|废止|不适用|明天|下周|\d{4}年|\d{4}-\d|\b(?:if|unless|maybe|old|previous|tomorrow|example)\b', text, re.I):
        return None
    sentences = [s.strip() for s in re.split('[。；;\n]',text) if s.strip()]
    assignments=[]
    for sentence in sentences:
        sentence=re.sub(r'^(?:请记住(?:这个项目决策)?|请保存(?:这个项目决策)?|记住|please remember)\s*[:：,，]?\s*', '', sentence, flags=re.I)
        sentence=re.sub(r'^(?:今后|以后|从现在起|from now on)\s*', '', sentence, flags=re.I)
        match=re.fullmatch(r'(.{2,80}?)(?:正式定为|正式设置为|设置为|定为|默认为|为|是|=|\s+is\s+)(?:创建后|after creation\s+)?\s*(\d+(?:\.\d+)?\s*(?:小时|分钟|秒|天|次|MB|GB|KB|hours?|minutes?|days?)|浅色|深色|简体中文|英文|light|dark)\s*',sentence,re.I)
        if match:
            topic=re.sub(r'正式|当前|默认|\bcurrent\b|\bdefault\b|[\s的:：]', '',match[1],flags=re.I).casefold()
            if len(topic)<2:continue
            value=match[2].replace(' ','').casefold()
            number=re.fullmatch(r'(\d+(?:\.\d+)?)(小时|分钟|秒|天|次|mb|gb|kb|hours?|minutes?|days?)',value)
            if number:
                amount=Decimal(number[1]); unit=number[2]
                factor={'小时':3600,'hour':3600,'hours':3600,'分钟':60,'minute':60,'minutes':60,
                        '秒':1,'天':86400,'day':86400,'days':86400}.get(unit)
                if factor:value=f'{(amount*factor).normalize():f}s'
                else:value=f'{amount.normalize():f}{unit}'
            assignments.append((topic,value))
    # Unparsed or compound text stays unstructured.
    if len(assignments)!=1:return None
    if len(sentences)>1:return None
    return assignments[0]

def initialize(db):
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    columns={r['name'] for r in db.execute('PRAGMA table_info(knowledge_records)')}
    definitions={'claim_topic':"TEXT NOT NULL DEFAULT ''",'claim_value':"TEXT NOT NULL DEFAULT ''",
                 'claim_fingerprint':"TEXT NOT NULL DEFAULT ''",'claim_conflicted':'INTEGER NOT NULL DEFAULT 0',
                 'superseded_by':'TEXT','valid_from':'TEXT','valid_until':'TEXT'}
    for name,definition in definitions.items():
        if name not in columns:db.execute(f'ALTER TABLE knowledge_records ADD COLUMN {name} {definition}')
    db.execute('CREATE INDEX IF NOT EXISTS idx_claim_slot ON knowledge_records(scope,project_key,claim_topic,status)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_claim_user ON knowledge_records(scope,claim_topic,status)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_superseded_by ON knowledge_records(superseded_by)')
    db.execute('CREATE TABLE IF NOT EXISTS knowledge_migrations (version TEXT PRIMARY KEY)')
    if not db.execute('SELECT 1 FROM knowledge_migrations WHERE version=?',(VERSION,)).fetchone():
        for row in db.execute('SELECT * FROM knowledge_records').fetchall():
            index_record(db,row)
        db.execute('INSERT INTO knowledge_migrations VALUES (?)',(VERSION,))

def slot_where(row):
    # User-wide assertions are shared across source projects; project assertions
    # never silently override a global preference or another project's decision.
    return ("scope='user' AND claim_topic=?",(row['claim_topic'],)) if row['scope']=='user' else (
        "scope='project' AND project_key=? AND claim_topic=?",(row['project_key'],row['claim_topic']))

def peers(db,row):
    if not row['claim_topic']:return []
    where,params=slot_where(row)
    return db.execute(f'''SELECT * FROM knowledge_records WHERE {where} AND id<>?
        AND (status IN ('active','stale') OR (status='archived' AND verified_count>0))
        AND superseded_by IS NULL AND claim_value<>?''',
        (*params,row['id'],row['claim_value'])).fetchall()

def refresh(db,row):
    if not row['claim_topic']:return
    where,params=slot_where(row)
    active=db.execute(f"SELECT DISTINCT claim_value FROM knowledge_records WHERE {where} AND status IN ('active','stale') AND superseded_by IS NULL",params).fetchall()
    values={r[0] for r in active}
    retired_values={r[0] for r in db.execute(f"SELECT DISTINCT claim_value FROM knowledge_records WHERE {where} AND status='archived' AND verified_count>0 AND superseded_by IS NULL",params)}
    members=db.execute(f'SELECT id,claim_value,superseded_by,status FROM knowledge_records WHERE {where}',params).fetchall()
    db.executemany('UPDATE knowledge_records SET claim_conflicted=? WHERE id=?',
        [(int(not m['superseded_by'] and bool((values if m['status'] in {'active','stale'} else values|retired_values)-{m['claim_value']})),m['id']) for m in members])

def index_record(db,row):
    assertion=extract(row['content'],row['knowledge_type'])
    topic,value=assertion or ('','')
    db.execute('UPDATE knowledge_records SET claim_topic=?,claim_value=?,claim_fingerprint=? WHERE id=?',
               (topic,value,fingerprint(row) if assertion else '',row['id']))
    fresh=db.execute('SELECT * FROM knowledge_records WHERE id=?',(row['id'],)).fetchone()
    refresh(db,fresh)

def blocked(row, *, historical=False):
    if 'superseded_by' not in row.keys():return None
    if row['superseded_by'] and not historical:return 'superseded_version'
    if row['claim_topic'] and row['claim_fingerprint'] != fingerprint(row):return 'assertion_source_changed'
    if row['claim_conflicted'] and not historical:return 'unresolved_value_conflict'
    return None

def details(db,row):
    current=peers(db,row)
    previous=db.execute('SELECT id,title,content,status,source_agent FROM knowledge_records WHERE superseded_by=?',(row['id'],)).fetchall()
    successor=db.execute('SELECT id,title,status,source_agent FROM knowledge_records WHERE id=?',(row['superseded_by'],)).fetchone() if row['superseded_by'] else None
    return {'topic':row['claim_topic'],'value':row['claim_value'],
            'conflicts':[{k:r[k] for k in ('id','title','content','source_agent','status','claim_value')} for r in current],
            'supersedes':[dict(r) for r in previous],'successor':dict(successor) if successor else None,
            'successor_removed':bool(row['superseded_by'] and not successor)}
