import collections,json,math,zipfile,xml.etree.ElementTree as ET
from pathlib import Path
P=Path(__file__).parent;E=P/'clawbox-three-day-review-20261018'
t=json.loads((P/'tables.json').read_text(encoding='utf-8'));raw=json.loads((E/'arms.json').read_text(encoding='utf-8'))
checks=[]
def check(name,condition):
 checks.append({'check':name,'passed':bool(condition)})
 if not condition:raise AssertionError(name)
check('342 unique arm attempts',len(raw)==len({x['id'] for x in raw})==342)
check('73 primary and 269 other arms',len(t['Experiments'])==73 and len(t['Other_runs'])==269)
for r in raw:
 w=r.get('observed');bins=[x for x in t['Windows'] if x['id']==r['id'] and x['label'].startswith('bin_')]
 if w and bins:
  for k in ['successful_agents','agent_tool_completions','backend_completions']:
   if w.get(k) is not None:check(r['id']+': disjoint bins '+k,sum(x.get(k) or 0 for x in bins)==w[k])
matched={'real-trace-p0-c640-balanced-full-s075-01':(4681,12924,33,2,0),'real-trace-p0-c640-balanced-aoff-s075-01':(4873,14291,83,6,0),'real-trace-p0-c640-balanced-os-s075-01':(6481,17114,183,0,0)}
for r in t['Experiments']:
 if r['run_id'] in matched:check(r['run_id']+': agrees with independent saved raw-window audit',(r['tool_completions'],r['backend_completions'],r['independent_failures'],r['borrow_n'],r['success_agents'])==matched[r['run_id']])
 if r['policy'] in ['os-default-no-admission','d-os-default']:check(r['id']+': OS total charge not labeled NUMA LOCAL',r['LOCAL_peak_GiB'] is None and r['LOCAL_avg_GiB'] is None)
 if r['window_s'] and r['success_agents'] is not None:check(r['id']+': throughput arithmetic',math.isclose(r['Agents_per_s'],r['success_agents']/r['window_s']))
for r in t['Sources']:
 check(r['id']+': POSIX remote paths',all('\\' not in (r[k] or '') for k in ['event_path','result_path','gateway_dir','worker_stderr']))
book=next(P.glob('*.xlsx'))
with zipfile.ZipFile(book) as z:check('XLSX ZIP CRC',z.testzip() is None)
ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main','p':'http://schemas.openxmlformats.org/package/2006/relationships'}
z=zipfile.ZipFile(book)
strings=[]
if 'xl/sharedStrings.xml' in z.namelist():
 strings=[''.join(si.itertext()) for si in ET.fromstring(z.read('xl/sharedStrings.xml')).findall('m:si',ns)]
rels={r.attrib['Id']:r.attrib['Target'] for r in ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
sheetfiles={s.attrib['name']:rels[s.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']] for s in ET.fromstring(z.read('xl/workbook.xml')).findall('m:sheets/m:sheet',ns)}
check('10 worksheets',len(sheetfiles)==10)
def sheetdata(name):
 target=sheetfiles[name];target=target.lstrip('/') if target.startswith('/') else 'xl/'+target
 root=ET.fromstring(z.read(target));rows=[];errors=[]
 for row in root.findall('m:sheetData/m:row',ns):
  cells={}
  for cell in row.findall('m:c',ns):
   letters=''.join(k for k in cell.attrib['r'] if k.isalpha());index=0
   for ch in letters:index=index*26+ord(ch)-64
   typ=cell.attrib.get('t');v=cell.find('m:v',ns);value=v.text if v is not None else None
   if typ=='s':value=strings[int(value)] if value is not None else None
   elif typ=='inlineStr':value=''.join(cell.find('m:is',ns).itertext())
   elif typ=='e':errors.append(value)
   elif typ=='b':value=value=='1'
   elif value is not None and typ not in ['str','d']:value=float(value)
   cells[index-1]=value
  rows.append([cells.get(k) for k in range(max(cells,default=-1)+1)])
 return rows,errors
for name in ['Experiments','Other_runs','Configurations','Operations','Windows','Probes','Sources','Traces']:
 vals,errors=sheetdata(name)
 check(name+': worksheet row count',len(vals)==len(t[name])+1)
 check(name+': no Excel error cells',not errors)
 headers=vals[0]
 if name in ['Experiments','Other_runs']:
  for target,x in zip(t[name],vals[1:]):
   actual=dict(zip(headers,x))
   for col in ['Agents_per_s','Tool_Steps_per_s','LOCAL_peak_GiB','VM_cgroup_peak_GiB','checkpoint_VM_n','restore_VM_n']:
    expected=target.get(col);found=actual.get(col)
    check(name+': '+target['id']+': '+col,found is None if expected is None else math.isclose(found,expected,rel_tol=1e-12,abs_tol=1e-12))
z.close()
summary={'checks_passed':len(checks),'all_passed':True,'validation':'Read-only extraction consistency, independent old-window cross-check, XLSX ZIP/OOXML values and error-cell scan. All sheets rendered for visual review. Native Excel not exercised. No formal experiment rerun.','checks':checks}
(P/'verification.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in summary.items() if k!='checks'}))
