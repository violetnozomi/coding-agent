"""只复制本机 Python 标准库及其共享库；不复制私人 site-packages。"""
from pathlib import Path
import hashlib,json,os,re,shutil,subprocess
out=Path('/home/pyh/.codex/artifacts/nz-swebench-pro-v2-20-2026-10-11')
src=Path('/home/pyh/miniconda3'); build=out/'runtime/isolated-agent-313'; prefix=build/'python'
build.mkdir(exist_ok=True);(prefix/'bin').mkdir(parents=True,exist_ok=True)
if not (prefix/'lib/python3.13').exists():
 shutil.copytree(src/'lib/python3.13',prefix/'lib/python3.13',ignore=shutil.ignore_patterns('site-packages','__pycache__','*.pyc'))
shutil.copy2((src/'bin/python').resolve(),prefix/'bin/python3.13')
libs=set()
for f in [src/'bin/python',*(src/'lib/python3.13/lib-dynload').glob('*.so')]:
 text=subprocess.run(['ldd',str(f)],capture_output=True,text=True,check=True).stdout
 for path in re.findall(r'=> (/[^ ]+)',text):
  p=Path(path).resolve()
  if p.is_relative_to(src/'lib'):libs.add(p)
for f in sorted(libs):
 shutil.copy2(f,prefix/'lib'/f.name)
 for link in f.parent.iterdir():
  if link.is_symlink() and link.resolve()==f:
   target=prefix/'lib'/link.name
   if not target.exists():target.symlink_to(f.name)
files={str(p.relative_to(prefix)):hashlib.sha256(p.read_bytes()).hexdigest() for p in prefix.rglob('*') if p.is_file()}
(out/'isolated-python-source.json').write_text(json.dumps({'source':str(src/'bin/python'),'source_version':subprocess.check_output([str(src/'bin/python'),'--version'],text=True).strip(),'excluded':['site-packages','__pycache__','*.pyc'],'files_sha256':files,'copied_bytes':sum(p.stat().st_size for p in prefix.rglob('*') if p.is_file() and not p.is_symlink()),'shared_libraries':[p.name for p in sorted(libs)],'project_python_unchanged':True},indent=2)+'\n')
print('copied portable standard library and',len(libs),'libraries',flush=True)
