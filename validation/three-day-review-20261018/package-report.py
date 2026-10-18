import datetime,hashlib,json,zipfile
from pathlib import Path
P=Path(__file__).parent
excluded={'evidence.tgz','ClawBox-review-bundle.zip','bundle-sha256.txt','manifest-local.json'}
files=[p for p in P.iterdir() if p.is_file() and p.name not in excluded and p.suffix not in ['.zip','.tgz']]
files+=sorted(p for p in (P/'clawbox-three-day-review-20261018').rglob('*') if p.is_file())
manifest=[]
for p in files:
 h=hashlib.sha256()
 with p.open('rb') as stream:
  for block in iter(lambda:stream.read(4*1024*1024),b''):h.update(block)
 manifest.append({'path':p.relative_to(P).as_posix(),'bytes':p.stat().st_size,'sha256':h.hexdigest()})
(P/'manifest-local.json').write_text(json.dumps({'created_UTC':datetime.datetime.now(datetime.timezone.utc).isoformat(),'files':manifest},indent=2),encoding='utf-8')
files.append(P/'manifest-local.json')
dest=P/'ClawBox-review-20261018-final.zip'
with zipfile.ZipFile(dest,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
 for p in files:z.write(p,p.relative_to(P).as_posix())
with zipfile.ZipFile(dest) as z:
 assert z.testzip() is None
h=hashlib.sha256(dest.read_bytes()).hexdigest()
(P/'bundle-sha256.txt').write_text(h+'  '+dest.name+'\n',encoding='utf-8')
print(json.dumps({'files':len(files),'bytes':dest.stat().st_size,'sha256':h}))
