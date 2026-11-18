"""Real loopback proxy/supervisor GETs; no process or motion authorization."""
import copy
import http.client
import json
import threading

from f1_harness.web_execution import workspace_server
from test_web_workspace import Processes, workspace


def test_proxy_supervisor_delta_protocol_and_readonly_routes(workspace,tmp_path,monkeypatch):
    rid='harness-'+'a'*20
    item=dict(id=rid,status='running',events=[dict(id='one',detail={'api_key':'private'})],actions=[])
    monkeypatch.setattr(workspace,'run',lambda run_id:dict(copy.deepcopy(item),id=run_id))
    server=workspace_server(workspace,port=0)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    records=workspace.records
    client=records.TaskService(tmp_path/'proxy')
    monkeypatch.setattr(client,'_execution_url',lambda:'configured')
    monkeypatch.setattr(records,'EXECUTION_PORT',server.server_port)
    before={str(p):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    def get(path):
        connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
        try:
            connection.request('GET',path);response=connection.getresponse()
            return response.status,json.loads(response.read())
        finally:connection.close()
    try:
        first=client.run_delta(rid)
        assert first['reset'] and first['events_total']==1
        assert 'private' not in str(first)
        assert not client.run_delta(rid,first['cursor'])['run']['events']
        item['events'].append(dict(id='two'));item['status']='review'
        appended=client.run_delta(rid,first['cursor'])
        assert not appended['reset'] and appended['run']['events']==[dict(id='two')]
        assert appended['run']['status']=='review'
        assert len(client.run(rid)['events'])==2  # old full-detail GET still works
        item['events'][0]['id']='rewritten'
        assert client.run_delta(rid,appended['cursor'])['reset']
        assert get('/runs/'+rid+'?cursor=broken')[1]['reset']
        for suffix in ('?extra=x','?cursor=a&cursor=b','?cursor='+'x'*600):
            assert get('/runs/'+rid+suffix)[0]==404
        assert not Processes.instances
        assert before=={str(p):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    finally:
        server.shutdown();server.server_close();thread.join(2)
