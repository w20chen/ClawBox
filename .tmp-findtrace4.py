import os,json
n=0
for root,dirs,files in os.walk('/home/weitianc'):
  for fn in files:
    if not fn.endswith('.jsonl') or '15five' not in os.path.join(root,fn): continue
    p=os.path.join(root,fn)
    try:
      with open(p) as f: rows=[json.loads(x) for x in f]
    except Exception: continue
    if not rows or rows[0].get('schema_version')!=6: continue
    starts=[r for r in rows if r.get('kind')=='llm' and r.get('record_type')=='span_start']
    ends=[r for r in rows if r.get('kind')=='llm' and r.get('record_type')=='span_end']
    good=bool(starts) and all(isinstance((r.get('input') or {}).get('messages'),list) and (r.get('input') or {}).get('messages') for r in starts) and all((r.get('output') or {}).get('content') for r in ends)
    if good:
      print('GOOD',p,os.path.getsize(p),len(starts)); raise SystemExit
    n+=1
print('scanned',n)
