from __future__ import annotations
import asyncio,json,os,re,shlex,sys,time
from dataclasses import asdict
from pathlib import Path
import httpx
from openai import OpenAI
from nz_coder.providers.openai_compatible import OpenAICompatibleProvider
from nz_coder.runtime.conversation import prompt
from nz_coder.runtime.core import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition,RunOptions,RunRequest
from nz_coder.runtime.execution import native_sdk,loop
from nz_coder.runtime.model_gateway import ResolvedModelRuntime
from capture import Capture, append, save
case=os.environ["CASE"]; out=Path(os.environ["SMOKE_OUTPUT"]); ws=Path.cwd()
capture=Capture(out,ws)
provider=OpenAICompatibleProvider(api_key="local-paid",base_url="http://localhost/v1")
client=OpenAI(api_key="local-paid",base_url="http://localhost/v1",max_retries=0,http_client=httpx.Client(transport=httpx.HTTPTransport(uds=os.environ["SMOKE_SOCKET"]),trust_env=False,event_hooks={"request":[capture.request_hook]}))
runtime=ResolvedModelRuntime(provider_id=provider.name,model_id="deepseek-v4-flash",request_model_id="deepseek-v4-flash",variant=None,provider=provider,client=client,capabilities=provider.capabilities("deepseek-v4-flash"),owns_client=True)
native_sdk.resolve_model_runtime=lambda *_a,**_k: runtime
loop.resolve_model_runtime=lambda *_a,**_k: runtime
def event(e):
 append(out/"events.jsonl",asdict(e))
def permission(name,args):
 command=str(args.get("command", "")); ok=False
 try:
  clean=command.replace("2>&1", ""); parts=clean.split("|"); toks=shlex.split(parts[0]); bad=any(x in clean for x in (";","&&","||","`","$(",">","<"))
  suffix_ok=all(re.fullmatch(r"\s*(tail|head)(\s+-[n]?\s*\d+)?\s*",x) for x in parts[1:])
  ok=name=="bash" and bool(toks) and not bad and suffix_ok and ((toks[0]=="pytest") or toks[:2]==["node","--test"] or toks[:3] in (["python","-m","pytest"],["python3","-m","pytest"]))
 except Exception: pass
 with (out/"permissions.jsonl").open("a") as f:f.write(json.dumps({"tool":name,"input":args,"allowed":ok})+"\n")
 return ok
rq=RunRequest(agent=AgentDefinition(name="paid-comparison",instructions=prompt.build(memory_block="",skill_descriptions="")),profile=MAIN_PROFILE,workspace=ws,session_id="paid-"+case,stream=True,provider="openai-compatible",model="deepseek-v4-flash",reasoning_effort=None,messages=({"role":"user","content":os.environ["TASK_PROMPT"]},),metadata={"permission_mode":"auto","persist_session":False,"max_turns":12})
opts=RunOptions(on_event=event,permission_asker=permission)
start=time.monotonic(); env=native_sdk.build_product_run_environment(rq,opts)
capture.install(env)
try:
 (out/"effective.json").write_text(json.dumps({"retrieval":env.repo_retrieval_strategy,"runner":type(env.runner).__name__},default=str))
 result=asyncio.run(native_sdk.NativeSDKRunner(env).run_result(rq,opts))
 save(out/"result.json",asdict(result))
 capture.snapshot("final_result")
 save(out/"final-state.json",{"state":env.runtime_state.to_dict(),"run_evidence":env.run_evidence.to_dict(),"verification_manager":env.vm.status()})
 (out/"runtime.jsonl").write_bytes(env.tracer.path.read_bytes())
 print(json.dumps({"status":result.status.value,"error":result.error,"elapsed_s":time.monotonic()-start}))
finally: env.close()
