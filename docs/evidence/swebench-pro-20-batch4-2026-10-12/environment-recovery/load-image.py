"""按量取的官方层核算准备峰值；准备后再检查任务证据空间。"""
from pathlib import Path
import json
import shutil
import subprocess
import time

OUT = Path('/home/pyh/.codex/artifacts/nz-swebench-pro-v2-20-batch4-2026-10-12')
receipt = json.loads((OUT / 'measured-image-9.json').read_text())
endpoint = json.loads((OUT / 'image-9-local-endpoint.json').read_text())
assert receipt['all_compressed_and_uncompressed_hashes_verified']
assert endpoint['fixed_manifest_digest'] == receipt['official_manifest_digest']
emergency_slack = 512 * 1024 ** 2
# 此时所有 Agent 已停止；拉取临时压缩层释放后才启动解题和产生新证据。
peak = (receipt['compressed_bytes'] + receipt['uncompressed_tar_bytes']
        + receipt['allocation_slack_bytes'] + emergency_slack)
free = shutil.disk_usage(OUT).free
admission = {'required_image_preparation_bytes': peak, 'free_bytes': free,
             'compressed_layer_temporary_bytes_included': True,
             'all_tar_bytes_and_4096_bytes_per_entry_included': True,
             'emergency_slack_bytes': emergency_slack,
             'task_and_evidence_start_after_pull': True, 'model_calls': 0}
(OUT / 'image-9-measured-admission.json').write_text(json.dumps(admission, indent=2) + '\n')
if free < peak:
    raise RuntimeError('measured_image_preparation_storage_insufficient')
reference = f"127.0.0.1:{endpoint['port']}/scaleapi/swe-bench_pro-v2:{endpoint['tag']}"
command = ['docker', 'pull', reference]
with (OUT / 'image-9-local-pull.txt').open('x') as log:
    process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 1800
    try:
        while process.poll() is None:
            if shutil.disk_usage(OUT).free < emergency_slack:
                raise RuntimeError('owned_image_pull_storage_stop')
            if time.monotonic() > deadline:
                raise RuntimeError('owned_image_pull_deadline')
            time.sleep(1)
        if process.returncode != 0:
            raise RuntimeError(f'official_image_pull_failed:{process.returncode}')
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=30)
info = json.loads(subprocess.check_output(['docker', 'image', 'inspect', reference], text=True))[0]
expected = receipt['official_manifest_digest']
manifest = json.loads((Path(receipt['temporary_cache']) / 'manifest.json').read_text())
config = json.loads((Path(receipt['temporary_cache']) / manifest['config']['digest'].split(':')[1]).read_text())
assert any(name.endswith('@' + expected) for name in info['RepoDigests'])
assert info['Id'] == manifest['config']['digest']
assert info['RootFS']['Layers'] == config['rootfs']['diff_ids']
if shutil.disk_usage(OUT).free < 2 * 1024 ** 3:
    raise RuntimeError('image_ready_but_task_evidence_storage_insufficient')
official = json.loads((OUT / 'task-manifest.json').read_text())['tasks'][8]['configuration']['environment']['docker_image']
subprocess.run(['docker', 'image', 'tag', reference, official], check=True)
record = {'requested_official_image': official, 'temporary_cache_reference': reference,
          'expected_official_manifest_digest': expected, 'loaded_repo_digests': info['RepoDigests'],
          'loaded_image_id': info['Id'], 'actual_image_size_bytes': info['Size'],
          'actual_pull_command': command, 'same_official_manifest_bytes': True,
          'all_cached_layers_full_sha256_verified': True, 'original_project_and_verifier_unchanged': True,
          'model_calls': 0, 'global_docker_configuration_changed': False}
(OUT / 'cached-image-9-identity.json').write_text(json.dumps(record, indent=2) + '\n')
print(json.dumps(record), flush=True)
