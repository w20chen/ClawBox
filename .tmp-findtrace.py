import glob,json,os
n=0
for p in glob.glob('/home/weitianc/ClawBox/.artifacts/**/*15five*.jsonl',recursive=True):
    try: rows=[json.loads(x) for x in open(p)]
    except Exception: continue
    if not rows or rows[0].get('schema_version')!=6: continue
    ids={r.get('trace_id') for r in rows if r.get('kind')=='llm'}
    starts={(r.get('trace_id'),r.get('span_id')) for r in rows if r.get('kind')=='llm' and r.get('record_type')=='span_start'}
    ends={(r.get('trace_id'),r.get('span_id')) for r in rows if r.get('kind')=='llm' and r.get('record_type')=='span_end'}
    good=len(ids)==1 and starts==ends and all(isinstance(r.get('input'),dict) and isinstance((r.get('input') or {}).get('messages'),list) for r in rows if r.get('kind')=='llm' and r.get('record_type')=='span_start')
    if good:
        print('GOOD',p,os.path.getsize(p),len(starts)); break
    if n<12:
        print('CAND',os.path.getsize(p),len(ids),len(starts),p)
    n+=1
