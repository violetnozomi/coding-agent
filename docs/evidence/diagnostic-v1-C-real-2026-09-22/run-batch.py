"""Exactly one authorized C run. Historical Q transport; frozen Core."""
import base64
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingMixIn, UnixStreamServer

import httpx
from capture import Capture, append, clean, hashes, save

ROOT=Path(__file__).resolve().parents[3]
OUT=Path(__file__).resolve().parent
PRE=OUT/'prepared'
SUITE=ROOT/'tests/evaluation/fixtures/agent_core_diagnostic_v1'
import importlib.util
_spec=importlib.util.spec_from_file_location('diagnostic_validator',SUITE/'validate.py')
validator=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(validator)
REF=ROOT/'references/InfCodeX'
NODE=Path.home()/'.local/lib/python3.13/site-packages/playwright/driver/node'
ISOLATE=ROOT/'tests/evaluation/fixtures/offline_exec.py'
WORK=Path('/tmp/diagnostic-v1-C-real-20260922')
MAN=json.loads((OUT/'manifest.json').read_text())


def response_projection(raw):
    text=raw.decode('utf-8','replace')
    if text.lstrip().startswith('{'):
        return {'json':clean(json.loads(text))}
    return {'chunks':[clean(json.loads(line[6:])) for line in text.splitlines()
                      if line.startswith('data: ') and line[6:]!='[DONE]'],
            'done':'data: [DONE]' in text}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        server=self.server
        if self.path!='/v1/chat/completions':
            self.send_error(404);return
        raw=self.rfile.read(int(self.headers['content-length']))
        payload=json.loads(raw)
        purpose=self.headers.get('x-nz-capture-purpose')
        if not purpose:
            self.send_error(400,'Missing purpose');return
        with server.lock:
            key='main' if purpose=='coding' else 'auxiliary'
            cap=24 if key=='main' else 8
            if server.counts[key]>=cap:
                append(server.out/'budget-denials.jsonl',{'purpose':purpose,'counts':server.counts,'time':time.time()})
                self.send_error(429,'Authorized request cap exhausted');return
            server.counts[key]+=1
            rid=sum(server.counts.values())
            server.capture.request_id=rid
            server.capture.purpose=purpose
            server.capture.main=server.counts['main']
            server.capture.auxiliary=server.counts['auxiliary']
            snapshot=server.capture.snapshot('before_provider_request')
            record={'request_id':rid,'time':time.time(),'purpose':purpose,
                    'ordinal':server.counts[key],'main_budget_before':24-server.counts['main']+(key=='main'),
                    'counts':dict(server.counts),'transport_snapshot':snapshot,
                    'runtime_snapshot':self.headers.get('x-nz-capture-snapshot'),
                    'payload':clean(payload)}
            stack=self.headers.get('x-nz-capture-stack')
            if stack:record['call_stack']=base64.b64decode(stack).decode('utf-8','replace')
            append(server.out/'provider-requests.jsonl',record)
        response_record={'request_id':rid,'purpose':purpose}
        started=time.monotonic()
        try:
            key,base=server.credentials
            with httpx.Client(trust_env=False,timeout=httpx.Timeout(180,connect=30)) as client:
                with client.stream('POST',base+'/v1/chat/completions',content=raw,
                    headers={'Authorization':'Bearer '+key,'Content-Type':'application/json',
                             'Accept':self.headers.get('accept','*/*')}) as response:
                    response_record.update(status=response.status_code,provider_request_id=response.headers.get('x-request-id'))
                    self.send_response(response.status_code)
                    for k,v in response.headers.items():
                        if k.lower() in ('content-type','cache-control','x-request-id'):self.send_header(k,v)
                    self.end_headers()
                    chunks=[];downstream=True
                    for chunk in response.iter_bytes():
                        chunks.append(chunk)
                        if downstream:
                            try:self.wfile.write(chunk);self.wfile.flush()
                            except (BrokenPipeError,ConnectionResetError):downstream=False
                    response_record.update(response_projection(b''.join(chunks)))
                    response_record.update(time=time.time(),downstream_closed=not downstream)
        except Exception as exc:
            response_record['transport_error_type']=type(exc).__name__
        finally:
            response_record['duration_ms']=(time.monotonic()-started)*1000
            append(server.out/'provider-responses.jsonl',response_record)
            server.capture.snapshot('after_provider_response')

    def log_message(self,*_args):pass


class Server(ThreadingMixIn,UnixStreamServer):
    daemon_threads=False


def consume(stream,path,capture):
    for line in stream:
        try:row=json.loads(line)
        except json.JSONDecodeError:
            append(path,{'type':'non_json_output','text':line.rstrip()});continue
        safe=clean(row)
        append(path,{'capture_time':time.time(),'capture_request_id':capture.request_id,**safe})
        kind=row.get('type') or row.get('name')
        if kind in ('tool.result','iteration.end','turn.completed','run.result','turn.failed','tool.start'):
            capture.snapshot('reference:'+str(kind),event_id=row.get('id'))


def invoke(argv,ws,env,out,capture,timeout=900):
    save(out/'command.json',{'argv':argv,'cwd':str(ws),'env':{k:v for k,v in env.items() if k!='SMOKE_API_KEY'}})
    started=time.monotonic()
    p=subprocess.Popen(argv,cwd=ws,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                       text=True,start_new_session=True)
    threads=[threading.Thread(target=consume,args=(stream,out/name,capture))
             for stream,name in [(p.stdout,'stdout.jsonl'),(p.stderr,'stderr.jsonl')]]
    for t in threads:t.start()
    timeout_hit=False
    try:code=p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timeout_hit=True;os.killpg(p.pid,signal.SIGTERM)
        try:code=p.wait(timeout=10)
        except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);code=p.wait()
    for t in threads:t.join(timeout=10)
    save(out/'process.json',{'exit_code':code,'elapsed_s':time.monotonic()-started,'harness_timeout':timeout_hit})


def freeze_and_accept(case,out,ws):
    frozen=out/'final-files'
    current=hashes(ws)
    for name in current:
        target=frozen/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((ws/name).read_bytes())
    save(out/'final-hashes.json',current)
    initial=json.loads((out/'initial-hashes.json').read_text())
    changes=sorted(n for n in initial.keys()|current.keys() if initial.get(n)!=current.get(n))
    diff=''
    for name in changes:
        a=PRE/'initial'/case/name;b=frozen/name
        aa=a.read_text(errors='replace').splitlines(True) if a.exists() else []
        bb=b.read_text(errors='replace').splitlines(True) if b.exists() else []
        diff+=''.join(difflib.unified_diff(aa,bb,fromfile='a/'+name,tofile='b/'+name))
    (out/'workspace.diff').write_text(diff)
    with tempfile.TemporaryDirectory(prefix='C-final-check-') as td:
        checkws=Path(td)/'workspace';shutil.copytree(frozen,checkws)
        public=validator.project_tests('C_long_horizon',checkws)
        hidden=validator.acceptance('C_long_horizon',checkws)
    save(out/'project-tests.json',public)
    save(out/'behavioral-acceptance.json',hidden)
    save(out/'acceptance.json',{'passed':hidden['exit']==0,'behavioral_only':True,
         'checks':{'public':{'exit_code':public['exit'],'stdout':public['stdout'],'stderr':public['stderr']},
                   'independent':{'exit_code':hidden['exit'],'stdout':hidden['summary'],'stderr':''}},
         'changed_files':changes,'source_hashes':current})


def run_one(task,side,credentials):
    assert side == 'nzcoder', 'InfCodeX not authorized'
    case=task['id'];out=OUT/case/side;out.mkdir(parents=True,exist_ok=False)
    work=WORK/case/side;ws=work/'workspace';home=work/'home'
    shutil.copytree(PRE/'initial'/case,ws);home.mkdir()
    assert hashes(ws)==task['initial_hashes'];save(out/'initial-hashes.json',hashes(ws))
    subprocess.run(['git','init','-q',str(ws)],check=True)
    subprocess.run(['git','-C',str(ws),'add','.'],check=True)
    git_env={**os.environ,'GIT_AUTHOR_DATE':'2026-09-20T00:00:00+08:00','GIT_COMMITTER_DATE':'2026-09-20T00:00:00+08:00'}
    subprocess.run(['git','-C',str(ws),'-c','user.name=Fixture','-c','user.email=fixture@local.invalid',
                    'commit','-qm','Initial task fixture'],check=True,env=git_env)
    save(out/'initial-git.json',{'head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ws,text=True).strip()})
    transport_out=out/'transport';transport_out.mkdir();capture=Capture(transport_out,ws)
    with tempfile.TemporaryDirectory(prefix='complex-pair-uds-') as td:
        sock=Path(td)/'provider.sock';server=Server(str(sock),Handler)
        server.out=out;server.capture=capture;server.credentials=credentials
        server.lock=threading.RLock();server.counts={'main':0,'auxiliary':0}
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        env={'HOME':str(home),'KODAX_HOME':str(home/'.kodax'),
             'PATH':f'{WORK}/bin:{Path(sys.executable).parent}:/usr/bin:/bin',
             'LANG':'C.UTF-8','PYTHONPATH':str(ROOT),'PYTHONDONTWRITEBYTECODE':'1',
             'PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1','SMOKE_API_KEY':'local-paid',
             'SMOKE_SOCKET':str(sock),'SMOKE_OUTPUT':str(out),'TERM':'dumb','NO_COLOR':'1',
             'CASE':case,'TASK_PROMPT':task['prompt']}
        try:
            argv=[sys.executable,str(ISOLATE),sys.executable,str(OUT/'run-nz.py')]
            append(OUT/'progress.jsonl',{'event':'run_started','case':case,'side':side,'time':time.time()})
            invoke(argv,ws,env,out,capture)
        except Exception as exc:
            save(out/'harness-error.json',{'error_type':type(exc).__name__,'message':str(exc)})
        finally:
            server.shutdown();thread.join(timeout=3);server.server_close()
            capture.snapshot('process_finished');save(out/'request-counts.json',server.counts)
    freeze_and_accept(case,out,ws)
    result={'event':'run_finished','case':case,'side':side,'time':time.time(),
            'counts':server.counts,'acceptance':json.loads((out/'acceptance.json').read_text())['passed']}
    append(OUT/'progress.jsonl',result);print(json.dumps(result),flush=True)


def main():
    case=sys.argv[1]
    assert case == 'C'
    assert json.loads((OUT/'authorization.json').read_text())['user_message']=='确认'
    assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()==MAN['nz_commit']
    assert not subprocess.check_output(['git','diff','HEAD','--','nz_coder'],cwd=ROOT)
    for path,digest in json.loads((OUT/'preflight/source-hashes.json').read_text()).items():
        assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest, path
    for path,digest in json.loads((OUT/'preflight/harness-hashes.json').read_text()).items():
        assert hashlib.sha256((OUT/path).read_bytes()).hexdigest()==digest, path
    with (OUT/('LAUNCH_'+case)).open('x') as f:f.write(str(time.time())+'\n')
    WORK.mkdir(exist_ok=True);(WORK/'bin').mkdir(exist_ok=True)
    for name,target in [('node',NODE),('python',Path(sys.executable))]:
        link=WORK/'bin'/name
        if not link.exists():link.symlink_to(target)
    values={}
    for line in (ROOT/'.env').read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k,v=line.split('=',1);values[k.strip()]=v.strip().strip("\"'")
    credentials=values['API_KEY'],values.get('API_BASE_URL','https://api.deepseek.com').rstrip('/')
    assert credentials[1]=='https://api.deepseek.com', 'Historical Provider mismatch'
    save(OUT/'connection.json',{'base_url':credentials[1],'credentials':'omitted'})
    run_one(next(t for t in MAN['tasks'] if t['id']==case),'nzcoder',credentials)

if __name__=='__main__':main()
