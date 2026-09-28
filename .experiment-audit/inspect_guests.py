from clawbox.cube import CubeSandboxClient
ids=['d5e9f96fd6c74195b4c19a68621a9198','978408e31eed451fb8bed9ce05dc2902','cf553f27d4e247f68eceb2af12ee6994']
c=CubeSandboxClient()
for sid in ids:
 try:
  s=c.connect_sandbox(sid)
  r=c.run_command(s,"tail -n 120 /tmp/openclaw/openclaw-2026-09-28.log",timeout_s=10,cwd='/workspace')
  print('\n###',sid,'\n',r.stdout[-16000:],r.stderr)
 except Exception as e: print(sid,type(e).__name__,e)
