"""仅转发两个固定官方镜像；共享大层先完整验 SHA，再从回环地址提供。"""
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import hashlib,json,re,time,urllib.request
out=Path('/home/pyh/.codex/artifacts/nz-swebench-pro-v2-20-batch2-2026-10-11');repo='scaleapi/swe-bench_pro-v2'
while not (out/'layer-cache/receipt.json').exists():time.sleep(2)
receipt=json.loads((out/'layer-cache/receipt.json').read_text());blob=Path(receipt['blob'])
h=hashlib.sha256()
with blob.open('rb') as f:
 for data in iter(lambda:f.read(1024**2),b''):h.update(data)
assert h.hexdigest()==receipt['sha256'] and blob.stat().st_size==receipt['bytes']
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open('https://ghcr.io/token?service=ghcr.io&scope=repository:'+repo+':pull',timeout=15) as r:token=json.load(r)['token']
cached={'sha256:'+receipt['sha256']:(receipt,blob)}
extra_receipt=json.loads((out/'layer-cache-08-final/receipt.json').read_text());extra_blob=Path(extra_receipt['blob']);extra_hash=hashlib.sha256()
with extra_blob.open('rb') as f:
 for data in iter(lambda:f.read(1024**2),b''):extra_hash.update(data)
assert extra_hash.hexdigest()==extra_receipt['sha256'] and extra_blob.stat().st_size==extra_receipt['bytes']
cached['sha256:'+extra_receipt['sha256']]=(extra_receipt,extra_blob)
objects={};tags={};allowed={}

manifest=json.loads((out/'task-manifest.json').read_text())
for n in [8,17]:
 tag=manifest['tasks'][n-1]['configuration']['environment']['docker_image'].split(':',1)[1]
 digest=json.loads((out/'live'/f'b2-{n:02}'/'image-preflight.json').read_text())['digest']
 req=urllib.request.Request('https://ghcr.io/v2/'+repo+'/manifests/'+digest,headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.oci.image.manifest.v1+json'})
 with opener.open(req,timeout=20) as r:raw=r.read(1024**2)
 assert 'sha256:'+hashlib.sha256(raw).hexdigest()==digest
 obj=json.loads(raw);objects[digest]=(raw,obj['mediaType']);tags[tag]=digest
 for item in obj['layers']+[obj['config']]:allowed[item['digest']]=item['size']
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_HEAD(self):self.serve(False)
 def do_GET(self):self.serve(True)
 def serve(self,body):
  if self.path=='/v2/':self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'{}') if body else None;return
  prefix='/v2/'+repo+'/'
  if not self.path.startswith(prefix):self.send_error(404);return
  kind,reference=self.path[len(prefix):].split('/',1)
  if kind=='manifests' and (reference in tags or reference in objects):
   digest=tags.get(reference,reference);data,mime=objects[digest]
   self.send_response(200);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(data)));self.send_header('Docker-Content-Digest',digest);self.end_headers()
   if body:self.wfile.write(data)
   return
  if kind!='blobs' or reference not in allowed:self.send_error(404);return
  if reference in cached:
   receipt,blob=cached[reference]
   low=0;high=receipt['bytes']-1
   if self.headers.get('Range'):
    m=re.fullmatch(r'bytes=(\d+)-(\d*)',self.headers['Range'])
    if not m:self.send_error(416);return
    low=int(m[1]);high=int(m[2]) if m[2] else high
   if not 0<=low<=high<receipt['bytes']:self.send_error(416);return
   self.send_response(206 if self.headers.get('Range') else 200);self.send_header('Content-Type','application/octet-stream');self.send_header('Content-Length',str(high-low+1))
   if self.headers.get('Range'):self.send_header('Content-Range',f'bytes {low}-{high}/{receipt["bytes"]}')
   self.end_headers()
   if body:
    with blob.open('rb') as f:
     f.seek(low);left=high-low+1
     while left:
      block=f.read(min(1024**2,left));self.wfile.write(block);left-=len(block)
   return
  req=urllib.request.Request('https://ghcr.io'+self.path,headers={'Authorization':'Bearer '+token})
  try:
   with opener.open(req,timeout=30) as r:
    self.send_response(r.status);self.send_header('Content-Length',r.headers['Content-Length']);self.send_header('Content-Type','application/octet-stream');self.end_headers()
    if body:
     for block in iter(lambda:r.read(1024**2),b''):self.wfile.write(block)
  except Exception as e:print(type(e).__name__,flush=True)
server=ThreadingHTTPServer(('127.0.0.1',45293),Handler)
(out/'layer-cache-08-final/local-cache-endpoint.json').write_text(json.dumps({'host':'127.0.0.1','port':server.server_port,'fixed_manifest_digests':tags,'only_verified_official_objects':True,'model_calls':0},indent=2)+'\n')
print('cache_ready',server.server_port,flush=True);server.serve_forever()
