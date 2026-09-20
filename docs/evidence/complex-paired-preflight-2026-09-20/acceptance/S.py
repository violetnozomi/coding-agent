import importlib
import json
import os
from pathlib import Path
import stat
import pytest


@pytest.fixture
def api():
    module=importlib.import_module('store')
    for name in ('load_snapshot','Snapshot','StoreError','CorruptStoreError','ConflictError'):
        assert hasattr(module,name), f'Missing required API {name}'
    return module


def test_migration_revision_and_callers(api,tmp_path):
    from cli import update
    from sync import merge
    p=tmp_path/'settings.json';p.write_text('{"name":"雪","version":"legacy-user-value"}')
    snapshot=api.load_snapshot(p)
    assert snapshot.revision==0 and snapshot.data=={'name':'雪','version':'legacy-user-value'}
    snapshot.data['name']='local'
    assert api.load(p)['name']=='雪'
    assert update(p,'enabled',True,expected_revision=0)['name']=='雪'
    raw=json.loads(p.read_text());assert set(raw)=={'version','revision','data'} and raw['version']==2 and raw['revision']==1
    assert merge(p,{'count':2},expected_revision=1)==2
    assert api.load_snapshot(p).revision==2
    assert api.load(p)=={'name':'雪','version':'legacy-user-value','enabled':True,'count':2}


def test_conflict_preserves_exact_bytes(api,tmp_path):
    p=tmp_path/'s.json';assert api.save(p,{'a':1},expected_revision=0)==1
    before=p.read_bytes()
    with pytest.raises(api.ConflictError):api.save(p,{'a':2},expected_revision=0)
    assert p.read_bytes()==before and list(tmp_path.iterdir())==[p]


@pytest.mark.parametrize('raw', ['{bad','[]','{"a":1,"a":2}',
    '{"version":3,"revision":1,"data":{}}','{"version":2,"revision":true,"data":{}}',
    '{"version":2,"revision":0,"data":{}}','{"version":2,"revision":1,"data":{},"extra":1}',
    '{"version":2,"revision":1,"data":{"a":1,"a":2}}','{"x":NaN}','{"x":{}}'])
def test_corrupt_reads_and_saves_never_rewrite(api,tmp_path,raw):
    p=tmp_path/'s.json';p.write_text(raw);before=p.read_bytes()
    with pytest.raises(api.CorruptStoreError):api.load(p)
    with pytest.raises(api.CorruptStoreError):api.save(p,{'ok':True})
    assert p.read_bytes()==before and list(tmp_path.iterdir())==[p]


def test_validation_before_touching_storage(api,tmp_path):
    p=tmp_path/'missing-parent'/'s.json'
    for data in [{1:'key'},{'x':float('inf')},{'x':[]},{'x':{}}]:
        with pytest.raises(ValueError):api.save(p,data)
    for rev in [-1,True,1.5,'1']:
        with pytest.raises(ValueError):api.save(p,{},expected_revision=rev)
    assert not p.exists()


@pytest.mark.parametrize('failure_point',['fsync','replace'])
@pytest.mark.parametrize('existing',[True,False])
def test_io_failure_atomicity_cleanup_and_exception_identity(api,tmp_path,monkeypatch,failure_point,existing):
    p=tmp_path/'s.json'
    if existing:p.write_text('{"old":true}')
    before=p.read_bytes() if existing else None
    err=OSError('injected '+failure_point)
    def fail(*args,**kwargs):raise err
    monkeypatch.setattr(os,failure_point,fail)
    with pytest.raises(OSError) as caught:api.save(p,{'new':True})
    assert caught.value is err
    assert (p.read_bytes() if p.exists() else None)==before
    assert set(tmp_path.iterdir())==({p} if existing else set())


def test_replace_uses_same_dir_after_fsync_and_preserves_mode(api,tmp_path,monkeypatch):
    p=tmp_path/'s.json';p.write_text('{"old":true}');p.chmod(0o640)
    original_replace=os.replace;original_fsync=os.fsync;calls=[]
    def fsync(fd):calls.append('fsync');return original_fsync(fd)
    def replace(src,dst):
        assert calls and calls[-1]=='fsync'
        assert Path(src).parent.resolve()==p.parent.resolve()
        assert Path(src).resolve()!=p.resolve() and Path(dst).resolve()==p.resolve()
        assert json.loads(Path(src).read_text())['data']=={'new':'雪'}
        calls.append('replace');return original_replace(src,dst)
    monkeypatch.setattr(os,'fsync',fsync);monkeypatch.setattr(os,'replace',replace)
    data={'new':'雪'};assert api.save(p,data,expected_revision=0)==1
    assert data=={'new':'雪'} and stat.S_IMODE(p.stat().st_mode)==0o640
    assert calls[:2]==['fsync','replace'] and set(tmp_path.iterdir())=={p}


def test_documents_concurrency_limit():
    text=(Path(os.environ['TASK_WORKSPACE'])/'README.md').read_text().lower()
    assert 'revision' in text and 'atomic' in text and ('lock' in text or 'serializ' in text)
