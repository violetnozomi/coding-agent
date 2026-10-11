from pathlib import Path
import hashlib,json,subprocess,zipfile
out=Path('/home/pyh/.codex/artifacts/nz-swebench-pro-v2-20-2026-10-11');build=out/'runtime/isolated-agent-313'
info=json.loads(subprocess.check_output(['docker','image','inspect','a3bbca9b15c4'],text=True))[0];base=info['RepoDigests'][0]
lines=[]
for p in sorted((build/'wheels').glob('*.whl')):
 with zipfile.ZipFile(p) as z:m=z.read(next(n for n in z.namelist() if n.endswith('.dist-info/METADATA'))).decode()
 name=next(v[6:] for v in m.splitlines() if v.startswith('Name: '));version=next(v[9:] for v in m.splitlines() if v.startswith('Version: '))
 lines.append(f'{name}=={version} --hash=sha256:{hashlib.sha256(p.read_bytes()).hexdigest()}')
assert len(lines)==38
(build/'requirements.txt').write_text('\n'.join(lines)+'\n')
(build/'Dockerfile').write_text(f'FROM {base}\nCOPY python /opt/nz-agent\nCOPY wheels /nz-wheels\nCOPY requirements.txt /nz-requirements.txt\nRUN /opt/nz-agent/bin/python3.13 -m ensurepip && /opt/nz-agent/bin/python3.13 -m pip install --no-index --no-deps --require-hashes --target=/opt/nz-site --find-links=/nz-wheels -r /nz-requirements.txt && rm -rf /nz-wheels\n')
(out/'isolated-python-build-config.json').write_text(json.dumps({'base':base,'base_image_id':info['Id'],'agent_executable':'/opt/nz-agent/bin/python3.13','project_executable':'python3','lock_sha256':hashlib.sha256((out/'runtime/requirements-pinned.txt').read_bytes()).hexdigest(),'requirements_sha256':hashlib.sha256((build/'requirements.txt').read_bytes()).hexdigest(),'network_during_build':'none','user_authorized':'补隔离 Agent Python（建议）'},indent=2)+'\n')
r=subprocess.run(['docker','build','--network=none','--pull=false','-t','nz-pro20-isolated-python:20261011',str(build)])
raise SystemExit(r.returncode)
