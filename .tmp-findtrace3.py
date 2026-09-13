import glob,json,os
n=0
for p in glob.glob('/home/weitianc/ClawBox/**/*.jsonl',recursive=True):
    if '15five' not in p: continue
    try: rows=[json.loads(x) for x in open(p)]
    except: continue
    if not rows or rows[0].get('schema_version')!=6: continue
    starts=[r for r in rows if r.get('kind')=='llm' and r.get('record_type')=='span_start']
    ends=[r for r in rows if r.get('kind')=='llm' and r.get('record_type')=='span_end']
    good=len(starts)>0 and all(isinstance((r.get('input') or {}).get('messages'),list) and (r.get('input') or {}).get('messages' ) for r in starts) and all((r.get('output') or {}).get('content') for r in ends)
    if good:
      ids={r.get('trace_id') for r in starts}
      print('GOOD',p,os.path.getsize(p),len(starts),len(ids)); break
    if n<4: print('cand',p,len(starts))
    n+=1
print('scanned',n)
