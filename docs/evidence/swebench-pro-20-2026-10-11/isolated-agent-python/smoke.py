"""真实 SDK 和项目工具的离线检查；本地 Provider 不执行任何文件操作。"""
from pathlib import Path
import importlib.util,hashlib,json,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
ROOT=Path('/home/pyh/nzcoder');OUT=Path('/home/pyh/.codex/artifacts/nz-swebench-pro-v2-20-2026-10-11')
spec=importlib.util.spec_from_file_location('pro_run',ROOT/'docs/evidence/swebench-pro-20-2026-10-11/run-pro.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
from nz_coder.evaluation.model_relay import RelayLedger,RelayLimits,InputAccounting
from nz_coder.swebench.trace_budget import TraceBudget,archive_instance_diagnostics
manifest=json.loads((OUT/'task-manifest.json').read_text());row=manifest['tasks'][3]
slot=OUT/'iso';slot.mkdir();m.save(slot/'identity.json',{'instance_id':row['instance_id'],'attempt':0,'mode':'controlled_local_zero_paid'})
case=m.prepare_case(slot,'nz-pro20-isolated-python:20261011',row,False)
m.save(case/'input/run.json',{'task':'Read setup.cfg, then use Bash to report the installed project Python version and its pytest package path. Do not modify files.','model':m.MODEL,'session_id':'pro20-isolated-python-smoke','max_turns':4})
class Handler(BaseHTTPRequestHandler):
 count=0
 def log_message(self,*a):pass
 def do_POST(self):
  raw=self.rfile.read(int(self.headers['Content-Length']));p=json.loads(raw);n=Handler.count
  with (slot/'actual-requests.jsonl').open('a') as f:f.write(json.dumps(p)+'\n')
  if n==2:
   text=str(p['messages'])
   if '3.8.20' not in text or '/opt/nz-site' in str(p['messages'][-1]):self.send_error(502,'project interpreter/dependencies not isolated');return
  Handler.count+=1
  actions=[('read_file',{'path':'setup.cfg'}),('bash',{'command':'python3 -c "import sys,pytest; print(sys.version); print(pytest.__file__)"'})]
  msg={'role':'assistant','content':'The project uses Python 3.8.20; observed its original pytest package. No files modified.'}
  if n<2:
   name,args=actions[n];msg={'role':'assistant','content':None,'tool_calls':[{'id':f'local-{n}','type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}
  body=json.dumps({'id':f'offline-{n}','object':'chat.completion','created':0,'model':p['model'],'choices':[{'index':0,'message':msg,'finish_reason':'tool_calls' if n<2 else 'stop'}],'usage':{'prompt_tokens':len(raw),'completion_tokens':50,'total_tokens':len(raw)+50}}).encode()
  self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
ledger=RelayLedger(slot/'ledger.jsonl',RelayLimits(requests=12,input_total=1000000,output_total=100000))
counter=lambda raw,p:InputAccounting(len(raw),'local-byte-tokens',trusted=True,exact=True,evidence_level='exact',contract_id='local-only',payload_sha256=hashlib.sha256(raw).hexdigest())
try:
 m.run_case(slot,case,'nz-pro20-isolated-python:20261011',ledger,counter,upstream=f'http://127.0.0.1:{server.server_port}/v1/chat/completions',smoke=True,agent_python='/opt/nz-agent/bin/python3.13')
 patch=m.freeze(slot,case);assert patch==b''
 result=json.loads((case/'result/result.json').read_text());assert result['status']=='completed',result
 assert Handler.count==3,Handler.count
 assert '/opt/nz-agent/bin/python3.13' in json.loads((slot/'launch.json').read_text())
 m.archive_case(slot,case,row,'isolated-python-smoke',TraceBudget(OUT/'archives'),archive_instance_diagnostics)
 m.save(slot/'verification.json',{'agent_status':result['status'],'project_python':'3.8.20','agent_python':'3.13','actual_native_sdk':True,'actual_read_and_bash':True,'changed_files':0,'local_provider_requests':Handler.count,'paid_model_requests':0,'project_pytest_not_from_agent_site':True})
 print('isolated Agent Python: actual SDK completed; project Python 3.8.20 preserved; 0 paid requests',flush=True)
finally:ledger.close();server.shutdown();server.server_close()
