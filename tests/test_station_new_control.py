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
