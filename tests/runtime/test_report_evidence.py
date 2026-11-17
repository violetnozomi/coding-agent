from copy import deepcopy
import json
from pathlib import Path
from nz_coder.runtime.verification.report_evidence import project_reporting_evidence


def _pair(interaction='run-new', output='ACTUAL', *, input_value='echo x'):
    args = {'command':input_value}
    owner = {'role':'assistant', '_nz_session_id':'session', '_nz_interaction_run_id':interaction,
        '_nz_message_id':f'msg-{interaction}', 'tool_calls':[{'id':'same', 'function':{'name':'bash','arguments':json.dumps(args)}}],
        '_nz_parts':[{'id':f'part-{interaction}', 'type':'tool','tool':'bash','call_id':'same',
            'message_id':f'msg-{interaction}','interaction_run_id':interaction,
            'state':{'status':'completed','input':args,'output':output,'metadata':{'exit_code':0}}}]}
    tool = {'role':'tool','tool_call_id':'same','_nz_session_id':'session','content':output}
    return [owner,tool]


def test_full_frozen_run_retains_early_http_results_and_ignored_command():
    data = json.loads((Path(__file__).parents[1] / 'fixtures/delivery_followup/session-history.json').read_text())
    command = 'python3 -m unittest discover -s tests -t . -v 2>&1 | tail -60'
    text, digest = project_reporting_evidence(data, {'verification':{
        'verification_needed':True, 'ignored_verifications':[{'command':command,'reason':'unreliable shell'}]}}, final_text='9 paths all 404')
    assert '"doing": 0' in text
    assert 'HTTP/1.1 200 OK' in text
    assert 'HTTP/1.1 404 Not Found' in text
    assert command in text
    assert 'omitted_entries=0' in text
    assert 'projection_truncated' in text
    assert len(text) <= 64000
    assert len(digest) == 64


def test_pairing_uses_owner_scope_not_global_call_id():
    old = _pair('run-old','POISON_OLD')
    new = _pair('run-new','NEW_RESULT')
    text,_ = project_reporting_evidence(old+new,{},final_text='done')
    assert 'NEW_RESULT' in text
    assert 'POISON_OLD' not in text
    # 错误 Part 不可以当作该调用输出；对应工具消息仍可提供同归属原件。
    new[0]['_nz_parts'][0]['state']['input']={'command':'different'}
    new[0]['_nz_parts'][0]['state']['output']='WRONG_PART'
    text,_ = project_reporting_evidence(new,{},final_text='done')
    assert 'WRONG_PART' not in text
    assert 'NEW_RESULT' in text


def test_completed_part_alone_does_not_establish_success():
    data = _pair()
    data[0]['_nz_parts'][0]['state']['metadata'] = {}
    text,_ = project_reporting_evidence(data,{},final_text='done')
    assert '"exit_code": null' in text
    assert '"execution_status": "unknown"' in text


def test_total_budget_covers_arguments_outputs_headers_and_coverage():
    data=[]
    for index in range(100):
        pair=_pair(output=f'ENTRY_{index} '+ 'z'*5000, input_value='echo '+ 'x'*5000)
        pair[0]['_nz_message_id']=f'msg-{index}'
        pair[0]['_nz_parts'][0]['message_id']=f'msg-{index}'
        data.extend(pair)
    text,digest=project_reporting_evidence(data,{'verification':{'verification_needed':True}},final_text='done',max_chars=10000)
    assert len(text)<=10000
    assert 'ENTRY_0 ' in text and 'ENTRY_99 ' in text
    assert 'coverage_gap' in text
    changed=deepcopy(data)
    changed[-1]['content']='changed'
    changed[-2]['_nz_parts'][0]['state']['output']='changed'
    assert project_reporting_evidence(changed,{},final_text='done',max_chars=10000)[1]!=digest


def test_real_bash_exit_metadata_does_not_certify_unreliable_test_pipeline():
    data=_pair(input_value='pytest tests/test_a.py | tail -8')
    data[0]['_nz_parts'][0]['state']['metadata']={'exit':0}
    text,_=project_reporting_evidence(data,{},final_text='done')
    assert '"exit_code": 0' in text
    assert '"verification_reliability": "unreliable_shell"' in text
    assert '"execution_status": "unknown"' in text


def test_exact_session_archive_restores_only_declared_head_messages(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.sessions import session_transcript_dir
    data=_pair()
    for index,message in enumerate(data):
        message['_nz_message_id']=f'msg-{index}'
    data[0]['_nz_parts'][0]['message_id']='msg-0'
    data[0]['_nz_path']={'root':str(tmp_path)}
    archive='transcript_1234.jsonl'
    with scoped_workdir(tmp_path):
        folder=session_transcript_dir('session')
        folder.mkdir(parents=True)
        (folder/archive).write_text('\n'.join(json.dumps(m) for m in data))
    compact={'role':'assistant','_nz_session_id':'session','_nz_interaction_run_id':'run-new',
        '_nz_message_id':'summary','_nz_path':{'root':str(tmp_path)},'_nz_compaction':{
            'archive':'user-state://transcripts/session/'+archive,'head_message_ids':['msg-0','msg-1']}}
    text,_=project_reporting_evidence([compact],{},final_text='done')
    assert 'ACTUAL' in text
    compact['_nz_compaction']['archive']='user-state://transcripts/another/'+archive
    text,_=project_reporting_evidence([compact],{},final_text='done')
    assert 'ACTUAL' not in text
    assert 'unavailable_sources=1' in text
