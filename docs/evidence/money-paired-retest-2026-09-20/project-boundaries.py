"""Offline causal projections; retain source request/event IDs, never infer hidden reasoning."""
import hashlib,json
from pathlib import Path
R=Path(__file__).resolve().parent
N=R/'M/nzcoder'
REPO=R.parents[2]
def read(p):return json.loads(p.read_text())
def lines(p):return [json.loads(s) for s in p.read_text().splitlines() if s.strip()]
def save(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')
requests=lines(N/'provider-requests.jsonl');runtime=lines(N/'runtime-full.jsonl');snapshots=lines(N/'state-snapshots.jsonl')
responses=read(N/'normalized-responses.json')
reviews=[]
spec=(R.parent/'complex-paired-preflight-2026-09-20/initial/M/REQUIREMENTS.md').read_text()
needle='All five consumers accept the same lines and keyword-only currency/discount_bps defaults.'
for q in requests:
 if q['purpose']!='verifier':continue
 rid=q['request_id']; response=next(x for x in responses if x['request_id']==rid)
 text='\n\n'.join(m['role']+'\n'+m.get('content','') for m in q['payload']['messages'])
 (N/f'verifier-{rid}-input-visible.txt').write_text(text)
 state=next(s for s in snapshots if s['reason']=='before_provider_http_request' and s['request_id']==rid)
 verdict=json.loads(response['tool_calls'][0]['arguments'])
 reviews.append({'request_id':rid,'verdict':verdict,'input_file':f'verifier-{rid}-input-visible.txt','spec_full_text_present':spec in text,'explicit_consumer_contract_sentence_present':needle in text,'before_request_state':state})
save(N/'semantic-review.json',reviews)
terminals=[e for e in runtime if e['event']=='terminal_boundary_settled']
save(N/'terminal-boundary.json',terminals)
save(N/'verification-events.json',[e for e in runtime if e['event']=='verification_result'])
source=REPO/'nz_coder/runtime/verification/sidecar_verifier.py';ls=source.read_text().splitlines()
save(R/'semantic-source-corroboration.json',{'commit':read(R/'manifest.json')['nz_commit'],'path':str(source.relative_to(REPO)),'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'excerpts':[{'first_line':a,'last_line':b,'text':'\n'.join(ls[a-1:b])} for a,b in [(24,28),(105,111),(199,225),(264,315)]],'finding':'24-message rolling transcript plus actual edit evidence; original context document body absent in both observed verifier payloads. This is a source-supported explanation, not a fixed regression or counterfactual model experiment.'})
save(R/'boundaries.json',{'A':{'strength':'strong frozen-version evidence','main_request':14,'transport_version':'v005','mutation_generation':14,'note':'First sampled complete artifact state passing all 19 independent checks, evaluated offline only after run. v001-v004 passed 18 but lacked documentation.'},'B':{'strength':'strong actual subprocess','main_request':15,'http_request':15,'command':'python -m pytest -q tests','result':'53 passed','exit':0},'C':{'strength':'strong runtime state','before_http_request':16,'mutation_generation':14,'verification_generation':14,'acceptance_mutation_generation':13,'note':'Documentation mutation is generation 14; required verification bound to current generation. REQUIREMENTS is unchanged and not required mutation.'},'D':{'strength':'strong runtime state, incomplete semantic acceptance','after_main_request':15,'statuses':{'R1':'satisfied','R2':'satisfied','R3':'candidate','R4':'satisfied'},'note':'Deterministic completion candidate exists; not all completion conditions satisfied until review accepts, which never occurs.'},'E':{'strength':'strong terminal event','first_completion_review_candidate_after_main':15,'boundary':'streamed_tool_batch','early_tool_completion_candidate':True,'decision':'continue','reason':'semantic_review_requires_revision','accepted_terminal_boundary':None},'F':{'strength':'strong provider and terminal records','main_requests':24,'auxiliary_requests':2,'main_finish_reason':'stop','final_status':'max_turns','terminal_reason':'semantic_review_requires_revision','final_acceptance':'17 passed, 2 failed','normal_completion':False}})
# Prove the reference document was read by the main agent, but not preserved in verifier context.
reads=[t for t in lines(N/'tool-results.jsonl') if t['result']['name']=='read_file' and t['result']['tool_input'].get('path')=='REQUIREMENTS.md']
save(R/'reference-context-provenance.json',{'read_results':reads,'spec_sha256':hashlib.sha256(spec.encode()).hexdigest(),'verifier_inputs':[{'http_request':r['request_id'],'spec_full_text_present':r['spec_full_text_present'],'consumer_contract_sentence_present':r['explicit_consumer_contract_sentence_present']} for r in reviews],'late_main_correction_http':19,'before_correction_version':'v005','after_correction_version':'v006','independent_results':'19 passed -> 17 passed / 2 failed','causal_strength':'Observed verdict -> visible synthetic follow-up -> matching code change; no model reasoning inference; no counterfactual provider rerun.'})
print('Saved A–F, raw terminal/verification events, semantic inputs and context provenance.')
