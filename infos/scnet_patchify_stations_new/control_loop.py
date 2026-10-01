#!/usr/bin/env python3
"""Account-local observation, locked submission and shared campaign reconciliation."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import statistics
import subprocess
import sys
import time
import uuid
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from grid_extreme_signals import station_contract as ct
from infos.scnet_patchify_stations_new import progress
from infos.scnet_patchify_stations_new.run_job import load_pack, valid_receipt, prepared_contract

TERMINAL = {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'OUT_OF_MEMORY', 'NODE_FAIL',
            'PREEMPTED', 'BOOT_FAIL', 'DEADLINE', 'REVOKED'}


def now():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')


def command(argv):
    return subprocess.check_output(argv, text=True, timeout=60)


@contextmanager
def lock(path, seconds=30):
    with open(path, 'a') as stream:
        until = time.monotonic() + seconds
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= until:
                    raise TimeoutError(f'lock busy: {path}')
                time.sleep(.2)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def append(path, value):
    with Path(path).open('a') as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def save(shared, pack, ledger):
    path = shared / 'runtime/ledger.json'
    history = shared / 'runtime/ledger_history'
    history.mkdir(exist_ok=True)
    if path.exists():
        os.link(path, history / f'ledger-{time.time_ns():020d}-{uuid.uuid4().hex}.json')
    ct.atomic_json(path, ledger)
    retained = sorted(history.glob('ledger-*.json'))
    for old in retained[:-128]:
        old.unlink()
    dest = shared / 'runtime/completion_status/progress.md'
    tmp = ct.temporary(dest)
    tmp.write_text(progress.render(pack, ledger))
    os.replace(tmp, dest)


def queue(user):
    result = {}
    for line in command(['squeue', '-r', '-h', '-u', user, '-o', '%i|%T|%100j|%160k|%V|%M|%R']).splitlines():
        fields = [field.strip() for field in line.split('|')]
        if len(fields) != 7:
            raise ValueError('unrecognized squeue row: '+repr(line))
        result[fields[0]] = dict(zip(('state', 'name', 'comment', 'submit', 'elapsed', 'reason'), fields[1:]))
    return result


def rss_bytes(value):
    if not value:
        return 0
    unit = value[-1].upper()
    return float(value[:-1]) * 1024 ** ('KMGTP'.index(unit) + 1) if unit in 'KMGTP' else float(value)


def observe(pack, shared, cycle):
    user = pwd.getpwuid(os.getuid()).pw_name
    assert user in {w['username'] for w in pack['workers']}
    ledger = ct.read_json(shared / 'runtime/ledger.json')
    facts = dict(username=user, checked_at=now(), cycle=cycle, queue=queue(user), accounting={}, logs={})
    jobs = [s['job_id'] for s in ledger['tasks'].values() if s['username'] == user and s.get('job_id')]
    # Token recovery includes jobs whose response was lost before JobID registration.
    unknown = any(s['username'] == user and s['classification'] in ('unknown', 'submitting')
                  for s in ledger['tasks'].values())
    since = datetime.strptime(pack['campaign']['run_id'][10:18], '%Y%m%d').date().isoformat()
    args = ['sacct', '-n', '-P', '-u', user, '-S', since]
    if jobs and not unknown:
        args += ['-j', ','.join(jobs)]
    args += ['--format=JobIDRaw,State,ExitCode,ElapsedRaw,MaxRSS,JobName%100,Comment%160,Submit']
    for line in command(args).splitlines():
        f = line.split('|')
        if len(f) >= 8 and f[0]:
            facts['accounting'][f[0]] = dict(state=f[1].split()[0].rstrip('+'), exit_code=f[2],
                elapsed_seconds=int(f[3] or 0), max_rss=f[4], name=f[5], comment=f[6], submit=f[7])
    work = Path(os.environ['STATION_WORK_ROOT'])
    for row in pack['jobs']:
        state = ledger['tasks'][row['task_id']]
        if state['username'] != user or not state.get('job_id') or state['classification'] == 'succeeded':
            continue
        tails = {}
        for ext in ('out', 'err'):
            path = work / 'logs' / f"{row['job_name']}-{state['job_id']}.{ext}"
            try:
                with path.open('rb') as stream:
                    stream.seek(max(0, path.stat().st_size - 3000))
                    tails[ext] = stream.read().decode(errors='replace')
            except OSError as exc:
                tails[ext] = str(exc)
        facts['logs'][state['job_id']] = tails
    directory = shared / 'runtime/observations' / cycle
    directory.mkdir(parents=True, exist_ok=True)
    ct.atomic_json(directory / (user + '.json'), facts)
    print(json.dumps({'user': user, 'active': len(facts['queue']), 'cycle': cycle}))


def classify(state, main, batch, receipt_ok):
    if not main:
        return 'unknown'
    status = main['state']
    if status == 'COMPLETED':
        if main['exit_code'] != '0:0' or not batch or batch['state'] != 'COMPLETED' or batch['exit_code'] != '0:0':
            return 'incomplete_output'
        return 'succeeded' if receipt_ok else 'incomplete_output'
    if status in ('NODE_FAIL', 'PREEMPTED', 'BOOT_FAIL'):
        return 'retryable' if state['attempt'] < 3 else 'deterministic_failure'
    if status in ('OUT_OF_MEMORY', 'TIMEOUT'):
        return 'resource_failure'
    return 'deterministic_failure' if status in TERMINAL else 'unknown'


def estimate(pack, ledger, slots):
    samples = [s['elapsed_seconds'] for s in ledger['tasks'].values()
               if s['classification'] == 'succeeded' and '/extract/' in s['unit_id'] and s.get('elapsed_seconds', 0) > 0]
    remaining = sum(s['classification'] != 'succeeded' and '/extract/' in s['unit_id'] for s in ledger['tasks'].values())
    if samples:
        ordered = sorted(samples)
        median = statistics.median(samples)
        high = ordered[min(len(ordered)-1, int(len(ordered)*.9))]
        lower = remaining * median / max(1, slots) / 3600
        upper = remaining * max(high, median) / max(1, slots) / 3600
        global_left = sum(s['classification'] != 'succeeded' and '/extract/' not in s['unit_id'] for s in ledger['tasks'].values())
        low, high = lower + global_left * .25, upper + global_left * 24
        basis = f'{len(samples)} extract elapsed samples; median={median}s p90={ordered[min(len(ordered)-1,int(len(ordered)*.9))]}s; slots={slots}; global stages reserved separately'
        confidence = 'low' if len(samples) < 30 else 'medium'
    else:
        low, high = 48, 360
        basis = 'No accepted samples; 24h requested walltime; 3384/280=13 waves plus prepare/publish; planning interval, not measured lower bound'
        confidence = 'low'
    current = datetime.now(ZoneInfo('Asia/Shanghai'))
    return dict(remaining_hours=[round(low, 2), round(high, 2)],
                finish_range=[(current + timedelta(hours=h)).isoformat(timespec='minutes') for h in (low, high)],
                basis=basis, confidence=confidence, queue_delay='unknown, additional')


def transient_ledger_read(tail, shared):
    return ('ledger = ct.read_json(shared / "runtime/ledger.json")' in tail
            and (tail.endswith('OSError: [Errno 5] Input/output error')
                 or tail.endswith("FileNotFoundError: [Errno 2] No such file or directory: "
                                  + repr(str(shared / 'runtime/ledger.json')))))


def reconcile(pack, shared, cycle):
    with lock(shared / 'runtime/.submit.lock'):
        ledger = ct.read_json(shared / 'runtime/ledger.json')
        observations, errors = {}, {}
        for worker in pack['workers']:
            user = worker['username']
            try:
                fact = ct.read_json(shared / 'runtime/observations' / cycle / (user + '.json'))
                assert fact['cycle'] == cycle
                observations[user] = fact
            except (OSError, ValueError, AssertionError) as exc:
                errors[user] = str(exc)
        for row in pack['jobs']:
            state = ledger['tasks'][row['task_id']]
            if not state['attempt'] or state['classification'] == 'succeeded':
                continue
            fact = observations.get(state['username'])
            if fact is None:
                state.update(classification='unknown', reason='account observation unavailable')
                continue
            token = state.get('submission_token')
            matches = {j for source in (fact['queue'], fact['accounting']) for j, v in source.items()
                       if '.' not in j and token and v.get('comment') == token and v.get('name') == row['job_name']}
            if len(matches) > 1:
                state.update(classification='unknown', reason='duplicate jobs for submission token', duplicate_jobs=sorted(matches))
                continue
            if not state.get('job_id'):
                if len(matches) != 1:
                    state.update(classification='unknown', reason='submission token unresolved; never resubmit')
                    continue
                state['job_id'] = matches.pop()
            job = state['job_id']
            main = fact['accounting'].get(job)
            batch = fact['accounting'].get(job + '.batch')
            state['log_tail'] = fact['logs'].get(job, {})
            if main:
                state.update(scheduler_state=main['state'], exit_code=main['exit_code'], elapsed_seconds=main['elapsed_seconds'],
                             max_rss=max((main.get('max_rss',''), (batch or {}).get('max_rss','')), key=rss_bytes))
            if job in fact['queue']:
                state.update(classification='active', scheduler_state=fact['queue'][job]['state'])
                continue
            receipt = None
            try:
                receipt = valid_receipt(state, row, pack, shared, require_success=False)
                state['reason'] = ''
            except (OSError, ValueError, KeyError) as exc:
                state['reason'] = str(exc)
            result = classify(state, main, batch, receipt is not None)
            tail = state['log_tail'].get('err', '').rstrip()
            if (result == 'deterministic_failure' and state['attempt'] < 3
                    and main and main['state'] == 'FAILED' and main['exit_code'] == '1:0'
                    and transient_ledger_read(tail, shared)
                    and not (shared / 'attempts' / row['task_id'] / job).exists()):
                result = 'retryable'
                state['reason'] = 'Transient shared ledger read failure before any scientific output; bounded retry'
            if result == 'succeeded' and row['stage'] == 'prepare':
                prepared_contract(ct.read_json(receipt['output']), pack)
                gate = shared / 'runtime/prepared_assets_verified.json'
                if not gate.exists() or ct.read_json(gate).get('job_id') != job:
                    result = 'dependency_blocked'
                    state['reason'] = 'prepare scheduler/receipt valid; asset ACL and storage gate pending'
            state['classification'] = result
            if result == 'succeeded':
                state.update(verified_at=now(), receipt=str(shared / 'runtime/receipts' / row['task_id'] / (job + '.json')),
                             receipt_status=receipt['status'])
        ledger.update(checked_at=now(), account_errors=errors, observation_cycle=cycle)
        ledger['available_slots'] = {u: max(0, 20-len(f['queue'])) for u, f in observations.items()}
        eta = estimate(pack, ledger, sum(ledger['available_slots'].values()) + sum(s['classification']=='active' for s in ledger['tasks'].values()))
        record = dict(checked_at=now(), cycle=cycle, phase='summary', counts=dict(Counter(s['classification'] for s in ledger['tasks'].values())),
                      eta=eta, account_errors=errors, available_slots=ledger['available_slots'])
        append(shared / 'runtime/observations.jsonl', dict(cycle=cycle, checked_at=now(), accounts=observations, errors=errors))
        append(shared / 'runtime/cycles.jsonl', record)
        save(shared, pack, ledger)
        print(json.dumps(record, ensure_ascii=False))


def ready_rows(pack, ledger):
    by_unit = {r['unit_id']: r['task_id'] for r in pack['jobs']}
    if ledger.get('preparation', {}).get('status') != 'verified':
        return []
    return [r for r in pack['jobs'] if ledger['tasks'][r['task_id']]['classification'] in ('not_submitted', 'retryable')
            and ledger['tasks'][r['task_id']]['attempt'] < 3
            and all(ledger['tasks'][by_unit[d]]['classification'] == 'succeeded' for d in r['depends_on'])]


def submit(pack, shared, cycle):
    user = pwd.getpwuid(os.getuid()).pw_name
    assert user in {w['username'] for w in pack['workers']}
    work = Path(os.environ['STATION_WORK_ROOT'])
    pack_dir = Path(os.environ['STATION_JOB_PACK'])
    assert load_pack(pack_dir / 'manifest.json')['identity'] == pack['identity']
    assert (work / 'logs').is_dir() and Path(os.environ['STATION_ENV_FILE']).is_file()
    submitted = []
    while True:
        with lock(shared / 'runtime/.submit.lock', 55), lock(Path.home() / '.bcsd_submit.lock', 5):
            ledger = ct.read_json(shared / 'runtime/ledger.json')
            assert ledger['campaign_identity'] == pack['campaign_identity']
            assert ledger.get('observation_cycle') == cycle and user not in ledger.get('account_errors', {})
            active = queue(user)
            if len(active) >= 20:
                reason = 'account-wide 20 active jobs'
                break
            candidates = ready_rows(pack, ledger)
            own = [r for r in candidates if ledger['tasks'][r['task_id']]['username'] == user]
            candidate = next(iter(own or [r for r in candidates if ledger['tasks'][r['task_id']]['classification'] == 'not_submitted']), None)
            if candidate is None:
                reason = 'no safe ready candidates; dependencies or unresolved states'
                break
            state = ledger['tasks'][candidate['task_id']]
            export = '--export=ALL'
            if candidate['stage'] == 'publish':
                revision = ct.read_json(shared / 'runtime/publication_revision.json')
                assert revision['status'] == 'verified' and revision['campaign_identity'] == pack['campaign_identity']
                assert re.fullmatch(r'[0-9a-f]{40}', revision['code_sha'])
                ct.safe_name(revision['environment_file'])
                environment = work / revision['environment_file']
                assert environment.is_file()
                state['execution_code_sha'] = revision['code_sha']
                export = '--export=ALL,STATION_ENV_FILE=' + str(environment)
            script = pack_dir / candidate['script']
            assert ct.digest(script) == candidate['script_sha256']
            if state['classification'] == 'retryable':
                assert state.get('scheduler_state') in TERMINAL and state.get('job_id') not in active
                state['history'].append({k: v for k, v in state.items() if k != 'history'})
            reassignment = None
            if state['username'] != user:
                old = state['username']
                state['assignment_version'] += 1
                reassignment = dict(unit_id=state['unit_id'], old_username=old, new_username=user,
                    old_script=str(Path('/work/home') / old / 'extreme_station_new_runs' / pack['campaign']['run_id'] / 'jobs' / pack['resource_profile'] / candidate['script']),
                    new_script=str(script), old_script_sha256=candidate['script_sha256'], new_script_sha256=candidate['script_sha256'],
                    assignment_version=state['assignment_version'], checked_at=now(), reason='target exhausted own ready queue', job_id=None)
            state.update(classification='submitting', username=user, attempt=state['attempt']+1, job_id=None,
                         submission_token='esnew-'+uuid.uuid4().hex, submitted_at=now(), pack_identity=pack['identity'],
                         resource_profile=pack['resource_profile'])
            if reassignment:
                reassignment['submission_token'] = state['submission_token']
                append(shared / 'runtime/reassignment_log.jsonl', reassignment)
            save(shared, pack, ledger)
            try:
                response = command(['sbatch', '--parsable', '--account='+user, '--chdir='+str(work), export,
                                    '--comment='+state['submission_token'], str(script)]).strip()
                job = response.split(';')[0]
                if not re.fullmatch(r'\d+', job):
                    raise ValueError('unrecognized sbatch response: '+response)
                state.update(classification='active', job_id=job, scheduler_state='SUBMITTED')
                submitted.append(job)
                if reassignment:
                    append(shared / 'runtime/reassignment_log.jsonl', {**reassignment, 'job_id':job, 'event':'registered'})
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                state.update(classification='unknown', reason=str(exc))
                save(shared, pack, ledger)
                reason = 'submission unknown; resolve token first'
                break
            save(shared, pack, ledger)
        time.sleep(.25)
    with lock(shared / 'runtime/.submit.lock', 55):
        ledger = ct.read_json(shared / 'runtime/ledger.json')
        append(shared / 'runtime/cycles.jsonl', dict(checked_at=now(), cycle=cycle, phase='execute', username=user,
               submitted=submitted, active_count=len(active), empty_slot_reason=reason))
        save(shared, pack, ledger)
        print(json.dumps(dict(username=user, submitted=submitted, reason=reason)))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['observe', 'reconcile', 'submit'])
    p.add_argument('--pack', required=True)
    p.add_argument('--cycle', required=True)
    a = p.parse_args()
    ct.safe_name(a.cycle)
    pack = load_pack(a.pack)
    shared = Path(pack['campaign']['aggregate_root'])
    globals()[a.action](pack, shared, a.cycle)


if __name__ == '__main__':
    main()
