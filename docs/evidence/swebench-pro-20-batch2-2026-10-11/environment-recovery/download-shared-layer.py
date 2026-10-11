from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
import hashlib,json,os,shutil,time,urllib.request
out=Path('/home/pyh/.codex/artifacts/nz-swebench-pro-v2-20-batch2-2026-10-11');repo='scaleapi/swe-bench_pro-v2'
meta=json.loads((out/'shared-layer-network-probe.json').read_text())[0];digest=meta['layer_digest'];size=meta['layer_compressed_bytes'];chunk=32*1024**2
folder=out/'layer-cache';folder.mkdir(exist_ok=False)
assert shutil.disk_usage(out).free>size*2+1024**3
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open('https://ghcr.io/token?service=ghcr.io&scope=repository:'+repo+':pull',timeout=12) as r:token=json.load(r)['token']
manifest_digest=json.loads((out/'p2/b2-17/image-preflight.json').read_text())['digest']
request=urllib.request.Request('https://ghcr.io/v2/'+repo+'/manifests/'+manifest_digest,headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.oci.image.manifest.v1+json'})
with opener.open(request,timeout=15) as r:manifest=r.read(1024*1024)
assert 'sha256:'+hashlib.sha256(manifest).hexdigest()==manifest_digest
(folder/'official-manifest-17.json').write_bytes(manifest)
parts=folder/'parts';parts.mkdir();start=time.monotonic()
def fetch(number):
 low=number*chunk;high=min(size,low+chunk)-1;path=parts/f'{number:04}'
 failures=[]
 for retry in range(2):
  op=urllib.request.build_opener(urllib.request.ProxyHandler({})) if retry==0 else urllib.request.build_opener()
  req=urllib.request.Request('https://ghcr.io/v2/'+repo+'/blobs/'+digest,headers={'Authorization':'Bearer '+token,'Range':f'bytes={low}-{high}'})
  try:
   with op.open(req,timeout=30) as r:
    assert r.status==206 and r.headers.get('Content-Range')==f'bytes {low}-{high}/{size}'
    count=0
    with path.open('wb') as f:
     while count<high-low+1:
      data=r.read(min(1024**2,high-low+1-count))
      if not data:break
      f.write(data);count+=len(data)
    assert count==high-low+1
   return number
  except Exception as e:failures.append(type(e).__name__)
 raise RuntimeError({'chunk':number,'errors':failures})
completed=0
with ThreadPoolExecutor(max_workers=6) as pool:
 futures=[pool.submit(fetch,n) for n in range((size+chunk-1)//chunk)]
 for f in as_completed(futures):
  f.result();completed+=1
  if completed%8==0:print(json.dumps({'completed_chunks':completed,'chunks':len(futures),'seconds':time.monotonic()-start}),flush=True)
final=folder/digest.split(':')[1];h=hashlib.sha256()
with final.open('xb') as sink:
 for n in range((size+chunk-1)//chunk):
  with (parts/f'{n:04}').open('rb') as source:
   for block in iter(lambda:source.read(1024**2),b''):h.update(block);sink.write(block)
assert final.stat().st_size==size and h.hexdigest()==digest.split(':')[1]
receipt={'blob':str(final),'sha256':h.hexdigest(),'bytes':size,'chunks':completed,'concurrency':6,'seconds':time.monotonic()-start,'manifest_sha256':manifest_digest,'source':'official ghcr.io blob, direct first, configured proxy only after per-chunk failure','model_calls':0}
(folder/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');shutil.rmtree(parts)
print(json.dumps(receipt),flush=True)
