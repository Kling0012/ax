"""Explicit, scoped runner registration and fixed-profile selection."""
import time

from tests.b6a.test_dispatch_e2e import system, wait_for


def test_explicit_runner_registration_and_fixed_profile_selection(system):
    client, resources, state = system
    resource = resources.get('/v1/resources/example/model').json()
    assert resource['runner'] == 'agent-cli' and resource['supported_runners'] == ['agent-cli']
    body = {'role': 'second_opinion', 'prompt': 'review', 'sensitivity': 'public', 'verify': 'ax:change-safety-review-v1'}
    response = client.post('/v1/jobs', json=body)
    assert response.status_code == 202
    jid = response.json()['job_id']
    time.sleep(.2)
    assert client.get('/v1/jobs/' + jid).json()['state'] == 'queued'
    assert not [c for c in state['calls'] if c['job_id'] == jid]
    update = {'model': 'example/model', 'runners': ['agent-cli', 'chat-json']}
    assert resources.put('/v1/model-runners', json=update).status_code == 401
    headers = {'Authorization': 'Bearer control-test'}
    assert resources.put('/v1/model-runners', headers=headers, json={'model': 'example/model', 'runners': ['chat-json']}).status_code == 422
    assert resources.put('/v1/model-runners', headers=headers, json={'model': 'example/model', 'runners': ['agent-cli', 'agent-cli']}).status_code == 422
    assert resources.put('/v1/model-runners', headers=headers, json={'model': 'missing/model', 'runners': ['agent-cli']}).status_code == 404
    caps_before = resources.get('/v1/slots').json()
    time.sleep(.01)
    updated = resources.put('/v1/model-runners', headers=headers, json=update)
    assert updated.status_code == 200
    after = updated.json()['resource']
    assert {k: v for k, v in after.items() if k not in ('supported_runners', 'updated_at')} == {k: v for k, v in resource.items() if k not in ('supported_runners', 'updated_at')}
    assert after['updated_at'] > resource['updated_at']
    caps_after = resources.get('/v1/slots').json()
    assert caps_after['models']['example/model']['cap'] == caps_before['models']['example/model']['cap']
    assert caps_after['families']['example']['cap'] == caps_before['families']['example']['cap']
    wait_for(lambda: any(c['job_id'] == jid for c in state['calls']))
    call = next(c for c in state['calls'] if c['job_id'] == jid)
    assert call['runner'] == 'chat-json' and call['model'] == 'example/model'
    assert client.post('/v1/jobs', json={'role': 'summary', 'prompt': 'normal', 'sensitivity': 'public', 'runner': 'chat-json'}).status_code == 422
    resource = resources.get('/v1/resources/example/model').json()
    assert resource['runner'] == 'agent-cli' and resource['supported_runners'] == ['agent-cli', 'chat-json']
