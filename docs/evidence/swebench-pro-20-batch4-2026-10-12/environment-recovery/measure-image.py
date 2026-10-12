"""仅为第9题量取固定官方镜像；临时压缩层留在已授权内存目录。"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import gzip
import hashlib
import json
import shutil
import tarfile
import urllib.request

OUT = Path('/home/pyh/.codex/artifacts/nz-swebench-pro-v2-20-batch4-2026-10-12')
DOC = Path(__file__).resolve().parents[1]
authorization = json.loads((DOC / 'memory-workspace-authorization.json').read_text())
assert authorization['authorized'] and authorization['numbers'] == [9, 15]
folder = Path(authorization['temporary_root']) / 'b4-09-image-cache'
folder.mkdir(mode=0o700, exist_ok=False)
repository = 'scaleapi/swe-bench_pro-v2'
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open('https://ghcr.io/token?service=ghcr.io&scope=repository:' + repository + ':pull', timeout=20) as response:
    token = json.load(response)['token']
raw = (OUT / 'official-image-9-manifest.json').read_bytes()
digest = json.loads((OUT / 'supplement-memory/b4-09/image-preflight.json').read_text())['digest']
assert 'sha256:' + hashlib.sha256(raw).hexdigest() == digest
manifest = json.loads(raw)
(folder / 'manifest.json').write_bytes(raw)
objects = {item['digest']: item for item in manifest['layers'] + [manifest['config']]}
compressed_total = sum(item['size'] for item in objects.values())
assert shutil.disk_usage(folder).free > 2 * compressed_total + 1024 ** 3


def fetch(item):
    target = folder / item['digest'].split(':')[1]
    size = item['size']
    failures = []
    for attempt in range(2):
        # 直连失败才使用当前进程环境中的代理，不更改全局配置。
        client = opener if attempt == 0 else urllib.request.build_opener()
        request = urllib.request.Request('https://ghcr.io/v2/' + repository + '/blobs/' + item['digest'],
                                        headers={'Authorization': 'Bearer ' + token})
        try:
            h = hashlib.sha256()
            count = 0
            with client.open(request, timeout=45) as response, target.open('wb') as sink:
                while block := response.read(1024 ** 2):
                    count += len(block)
                    if count > size:
                        raise RuntimeError('official_blob_exceeds_declared_size')
                    sink.write(block)
                    h.update(block)
            assert count == size and 'sha256:' + h.hexdigest() == item['digest']
            print(json.dumps({'verified_blob': item['digest'], 'bytes': count}), flush=True)
            return
        except Exception as exc:
            failures.append(type(exc).__name__)
    raise RuntimeError({'digest': item['digest'], 'errors': failures})


with ThreadPoolExecutor(max_workers=4) as pool:
    for future in as_completed([pool.submit(fetch, item) for item in objects.values()]):
        future.result()
config = json.loads((folder / manifest['config']['digest'].split(':')[1]).read_text())
assert len(config['rootfs']['diff_ids']) == len(manifest['layers'])


class MeasuredStream:
    def __init__(self, source):
        self.source, self.digest, self.count = source, hashlib.sha256(), 0

    def read(self, length):
        block = self.source.read(length)
        self.digest.update(block)
        self.count += len(block)
        return block


measurements = []
for item, expected in zip(manifest['layers'], config['rootfs']['diff_ids']):
    assert item['mediaType'] == 'application/vnd.oci.image.layer.v1.tar+gzip'
    with gzip.open(folder / item['digest'].split(':')[1], 'rb') as source:
        stream = MeasuredStream(source)
        entries = 0
        with tarfile.open(fileobj=stream, mode='r|') as archive:
            for member in archive:
                if member.sparse:
                    raise RuntimeError('sparse_layer_requires_separate_storage_analysis')
                entries += 1
        while stream.read(1024 ** 2):
            pass
        assert 'sha256:' + stream.digest.hexdigest() == expected
    measurements.append({'digest': item['digest'], 'compressed_bytes': item['size'],
                         'diff_id': expected, 'uncompressed_tar_bytes': stream.count,
                         'entries': entries})
    print(json.dumps(measurements[-1]), flush=True)
receipt = {'official_manifest_digest': digest, 'temporary_cache': str(folder),
           'all_compressed_and_uncompressed_hashes_verified': True, 'layers': measurements,
           'compressed_bytes': compressed_total,
           'uncompressed_tar_bytes': sum(x['uncompressed_tar_bytes'] for x in measurements),
           'allocation_slack_bytes': sum(x['entries'] for x in measurements) * 4096,
           'retained_evidence_slack_bytes': 2 * 1024 ** 3,
           'model_calls': 0, 'source_or_verifier_modified': False}
receipt['required_disk_bytes'] = (receipt['compressed_bytes'] + receipt['uncompressed_tar_bytes']
                                  + receipt['allocation_slack_bytes'] + receipt['retained_evidence_slack_bytes'])
(OUT / 'measured-image-9.json').write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps({'required_disk_bytes': receipt['required_disk_bytes'],
                  'current_disk_free_bytes': shutil.disk_usage(OUT).free}), flush=True)
