"""隔离容器内调用原 SWE CLI；仅将模型边界接到宿主预算转发器。"""
from contextvars import ContextVar
from pathlib import Path
import sys
import uuid

import httpx
from openai import OpenAI

from nz_coder.providers.openai_compatible import OpenAICompatibleProvider
from nz_coder.runtime.execution import loop, native_sdk
from nz_coder.runtime import model_gateway as gateway
from nz_coder.runtime.model_gateway import ResolvedModelRuntime


def install_model_transport():
    purpose = ContextVar("model_purpose", default="coding")

    def request_id(request):
        request.headers["X-NZ-Call-Id"] = uuid.uuid4().hex

    clients = {
        role: OpenAI(
            api_key=[REDACTED], base_url="http://localhost/v1", max_retries=0,
            http_client=httpx.Client(
                transport=httpx.HTTPTransport(uds=str(Path("/model") / (role + ".sock"))),
                trust_env=False, event_hooks={"request": [request_id]},
            ),
        ) for role in ("main", "auxiliary")
    }
    provider = OpenAICompatibleProvider(api_key=[REDACTED], base_url="http://localhost/v1")
    original_complete = provider.create_completion

    def complete(_client, **fields):
        role = "main" if purpose.get() == "coding" else "auxiliary"
        return original_complete(clients[role], **fields)

    provider.create_completion = complete
    runtime = ResolvedModelRuntime(
        provider_id=provider.name, model_id="deepseek-v4-flash", request_model_id="deepseek-v4-flash",
        variant=None, provider=provider, client=clients["main"],
        capabilities=provider.capabilities("deepseek-v4-flash"), owns_client=True,
    )
    for target in (native_sdk, loop, gateway):
        target.resolve_model_runtime = lambda *_a, **_kw: runtime
    original_kwargs = gateway.ProductionModelGateway._request_kwargs

    def request_kwargs(self, call):
        # 非流式请求在线程中生成，必须在实际请求线程绑定角色。
        purpose.set(call.purpose.value)
        return original_kwargs(self, call)

    gateway.ProductionModelGateway._request_kwargs = request_kwargs


# spawn 的子进程也只安装模型传输；CLI、Runner 和全部文件工具均保持真实执行。
install_model_transport()

from nz_coder.runtime.execution import composition
from nz_coder.swebench.policy import strict_bash_violation
from nz_coder.intelligence.verification_planner import classify_verification_command
from nz_coder.runtime.process.workdir import current_workdir
import json
original_environment = composition.build_product_environment

def approved_test(name, arguments):
    command = str(arguments.get('command') or '')
    workspace = current_workdir().resolve()
    allowed = (workspace.parent == Path('<workspace>') and name == 'bash'
        and not strict_bash_violation(command)
        and classify_verification_command(command) in {'targeted', 'static'})
    with Path('/result/permissions.jsonl').open('a') as output:
        output.write(json.dumps(dict(tool=name,input=arguments,allowed=allowed,workspace=str(workspace))) + '\n')
    return allowed

def configured_environment(system_prompt, **kwargs):
    return original_environment(system_prompt, permission_asker=approved_test, **kwargs)

composition.build_product_environment = configured_environment

if __name__ == "__main__":
    from nz_coder.swebench.cli import main
    result = main(sys.argv[1:])
    import json, subprocess
    workspace = Path('<workspace>') / json.loads(Path('/input/instances.json').read_text())['instances'][0]['instance_id']
    probe = subprocess.run(['/opt/miniconda3/envs/testbed/bin/python', '-c',
        'import sys,json,importlib.util; import mpmath; '
        'print(json.dumps(dict(executable=sys.executable,version=sys.version,mpmath=mpmath.__version__, '
        'sympy_origin=getattr(importlib.util.find_spec("sympy"),"origin",None))))'],
        cwd=workspace, capture_output=True, text=True, check=True)
    Path('/result/environment.json').write_text(json.dumps(dict(agent_executable=sys.executable,
        agent_version=sys.version,project=json.loads(probe.stdout))))
    raise SystemExit(result)
