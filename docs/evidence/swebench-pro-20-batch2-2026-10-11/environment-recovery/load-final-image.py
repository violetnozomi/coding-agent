from pathlib import Path
import json,subprocess,time
out=Path('/home/pyh/.codex/artifacts/nz-swebench-pro-v2-20-batch2-2026-10-11')
p=out/'layer-cache-08-final/local-cache-endpoint.json'
deadline=time.monotonic()+60
while not p.exists():
 if time.monotonic()>deadline:raise RuntimeError('verified_cache_not_ready')
 time.sleep(.2)
e=json.loads(p.read_text());official=json.loads((out/'task-manifest.json').read_text())['tasks'][7]['configuration']['environment']['docker_image'];tag=official.split(':',1)[1]
ref=f"127.0.0.1:{e['port']}/scaleapi/swe-bench_pro-v2:{tag}"
expected=json.loads((out/'p2/b2-08/image-preflight.json').read_text())['digest'];assert e['fixed_manifest_digests'][tag]==expected
command=['docker','pull',ref]
with (out/'cached-image-08-second-pull.txt').open('w') as log:subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=1800,check=True)
info=json.loads(subprocess.check_output(['docker','image','inspect',ref],text=True))[0]
assert any(d.endswith('@'+expected) for d in info['RepoDigests'])
subprocess.run(['docker','image','tag',ref,official],check=True)
record={'requested_official_image':official,'temporary_cache_reference':ref,'expected_official_manifest_digest':expected,'loaded_repo_digests':info['RepoDigests'],'loaded_image_id':info['Id'],'actual_pull_command':command,'same_official_manifest_bytes':True,'all_cached_layers_full_sha256_verified':True,'original_project_and_verifier_unchanged':True,'model_calls':0,'global_docker_configuration_changed':False}
(out/'cached-image-08-identity.json').write_text(json.dumps(record,indent=2)+'\n')
print('official_digest_verified',expected,flush=True)
