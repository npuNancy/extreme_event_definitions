"""Scheduler evidence and durable claims for the fixed station campaign."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from grid_extreme_signals import station_contract as ct
from infos.scnet_patchify_stations_new import control_loop as ctl, progress
from tests.test_station_new_workflow import inventory


def test_terminal_success_requires_batch_and_receipt():
    s = {'attempt': 1}
    good = dict(state='COMPLETED', exit_code='0:0')
    assert ctl.classify(s, good, good, True) == 'succeeded'
    assert ctl.classify(s, good, None, True) == 'incomplete_output'
    assert ctl.classify(s, good, good, False) == 'incomplete_output'
    assert ctl.classify(s, None, good, True) == 'unknown'
    assert ctl.classify(s, dict(state='TIMEOUT'), None, False) == 'resource_failure'
    assert ctl.classify(s, dict(state='NODE_FAIL'), None, False) == 'retryable'
    assert ctl.classify({'attempt':3}, dict(state='NODE_FAIL'), None, False) == 'deterministic_failure'


def test_claim_precedes_sbatch_timeout_never_duplicates(inventory, monkeypatch, tmp_path):
    from infos.scnet_patchify_stations_new import create_jobs
    pack, scripts = create_jobs.build(inventory)
    shared = tmp_path / 'shared'
    (shared / 'runtime/completion_status').mkdir(parents=True)
    work = tmp_path / 'work'
    (work / 'logs').mkdir(parents=True)
    target = work / 'jobs'
    target.mkdir()
    ct.atomic_json(target / 'manifest.json', pack)
    row = pack['jobs'][0]
    (target / row['script']).write_text(scripts[row['script']])
    env = work / 'env'
    env.write_text('')
    monkeypatch.setenv('STATION_WORK_ROOT', str(work))
    monkeypatch.setenv('STATION_JOB_PACK', str(target))
    monkeypatch.setenv('STATION_ENV_FILE', str(env))
    monkeypatch.setattr(ctl.Path, 'home', lambda: tmp_path)
    user = row['logical_owner']
    monkeypatch.setattr(ctl.pwd, 'getpwuid', lambda _: SimpleNamespace(pw_name=user))
    ledger = progress.initial_ledger(pack)
    ledger.update(preparation={'status':'verified'}, observation_cycle='test')
    ct.atomic_json(shared / 'runtime/ledger.json', ledger)
    monkeypatch.setattr(ctl, 'queue', lambda _: {str(n):{} for n in range(20)})
    monkeypatch.setattr(ctl, 'command', lambda _: pytest.fail('full account submitted'))
    ctl.submit(pack, shared, 'test')
    assert ct.read_json(shared / 'runtime/ledger.json')['tasks'][row['task_id']]['attempt'] == 0
    monkeypatch.setattr(ctl, 'queue', lambda _: {})
    calls = []
    def uncertain(args):
        state = ct.read_json(shared / 'runtime/ledger.json')['tasks'][row['task_id']]
        assert state['classification'] == 'submitting' and state['job_id'] is None
        assert '--comment='+state['submission_token'] in args
        calls.append(args)
        raise ctl.subprocess.TimeoutExpired(args, 60)
    monkeypatch.setattr(ctl, 'command', uncertain)
    ctl.submit(pack, shared, 'test')
    ctl.submit(pack, shared, 'test')
    assert len(calls) == 1
    state = ct.read_json(shared / 'runtime/ledger.json')['tasks'][row['task_id']]
    assert state['classification'] == 'unknown' and state['attempt'] == 1


def test_unknown_and_active_never_ready(inventory):
    from infos.scnet_patchify_stations_new import create_jobs
    pack, _ = create_jobs.build(inventory)
    ledger = progress.initial_ledger(pack)
    ledger['preparation']['status'] = 'verified'
    row = pack['jobs'][0]
    assert ctl.ready_rows(pack, ledger) == [row]
    for classification in ('active', 'unknown', 'submitting', 'resource_failure', 'deterministic_failure'):
        ledger['tasks'][row['task_id']]['classification'] = classification
        assert not ctl.ready_rows(pack, ledger)


def test_submission_releases_locks_and_preserves_other_controller_updates(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import hashlib
    user = 'worker'
    work = tmp_path / 'work'; (work / 'logs').mkdir(parents=True)
    target = work / 'jobs'; target.mkdir()
    script = target / 'prepare.sh'; script.write_text('#!/bin/bash\n')
    env = work / 'env'; env.write_text('')
    pack = dict(identity='pack', campaign_identity='campaign', resource_profile='v1', workers=[dict(username=user)],
                jobs=[dict(task_id='prepare', unit_id='station-events-new/prepare', stage='prepare', key=None,
                           logical_owner=user, depends_on=[], script=script.name, script_sha256=ct.digest(script))])
    shared = tmp_path / 'shared'; (shared / 'runtime/completion_status').mkdir(parents=True)
    ledger = progress.initial_ledger(pack); ledger.update(preparation={'status':'verified'}, observation_cycle='test')
    ct.atomic_json(shared / 'runtime/ledger.json', ledger)
    monkeypatch.setattr(ctl, 'load_pack', lambda _: pack)
    monkeypatch.setattr(ctl.pwd, 'getpwuid', lambda _: SimpleNamespace(pw_name=user))
    monkeypatch.setattr(ctl.Path, 'home', lambda: tmp_path)
    for name,value in dict(STATION_WORK_ROOT=work,STATION_JOB_PACK=target,STATION_ENV_FILE=env).items():
        monkeypatch.setenv(name,str(value))
    held=[]; entries=[]
    @contextmanager
    def traced_lock(path,seconds=30):
        held.append(Path(path).name);entries.append(tuple(held))
        try:yield
        finally:held.pop()
    monkeypatch.setattr(ctl,'lock',traced_lock)
    queues=iter([{}, {str(n):{} for n in range(20)}])
    monkeypatch.setattr(ctl,'queue',lambda _:next(queues))
    def submit(args):
        assert held==['.submit.lock','.bcsd_submit.lock']
        return '12345\n'
    monkeypatch.setattr(ctl,'command',submit)
    def between(_):
        assert not held
        data=ct.read_json(shared/'runtime/ledger.json');data['other_controller_evidence']='preserve'
        ct.atomic_json(shared/'runtime/ledger.json',data)
    monkeypatch.setattr(ctl.time,'sleep',between)
    ctl.submit(pack,shared,'test')
    result=ct.read_json(shared/'runtime/ledger.json')
    assert result['other_controller_evidence']=='preserve'
    assert result['tasks']['prepare']['job_id']=='12345'
    assert entries.count(('.submit.lock',))==3


def test_ledger_previous_inode_remains_available(tmp_path):
    shared=tmp_path/'shared';(shared/'runtime/completion_status').mkdir(parents=True)
    pack=dict(campaign_identity='campaign',jobs=[])
    ledger=dict(campaign_identity='campaign',preparation={'status':'verified'},tasks={})
    ctl.save(shared,pack,ledger)
    path=shared/'runtime/ledger.json'
    with path.open() as old:
        original=ctl.os.fstat(old.fileno()).st_ino
        ledger['checked_at']='later'
        ctl.save(shared,pack,ledger)
        assert ctl.os.fstat(old.fileno()).st_nlink>=1
        assert 'checked_at' not in json.load(old)
    snapshots=list((shared/'runtime/ledger_history').glob('ledger-*.json'))
    assert len(snapshots)==1 and snapshots[0].stat().st_ino==original
    assert ct.read_json(path)['checked_at']=='later'
