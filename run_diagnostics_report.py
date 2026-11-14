"""Summarize video mirror attempts and compare complete run reports."""
from collections import Counter, defaultdict
import json


def count_video_attempts(results):
    counts = defaultdict(Counter)
    for result in results:
        for mirror, outcome in result.video_attempts:
            counts[mirror][outcome] += 1
    return {mirror:dict(outcomes) for mirror,outcomes in sorted(counts.items())}


def merge_video_counts(*groups):
    counts = defaultdict(Counter)
    for group in groups:
        for mirror,outcomes in group.items():
            counts[mirror].update(outcomes)
    return {mirror:dict(outcomes) for mirror,outcomes in sorted(counts.items())}


def read_previous_report(path):
    try:
        report = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return report if isinstance(report,dict) else {}


def compare_runs(current, previous):
    timestamp = previous.get('generated_epoch')
    if not isinstance(timestamp,(int,float)):
        return {}
    summary = {'previous_generated_epoch':timestamp,'previous_mode':previous.get('mode')}
    for key in ('published','working_this_run'):
        before,after = previous.get(key),current.get(key)
        if type(before) is int and type(after) is int:
            summary[f'{key}_delta'] = after-before
    if 'video_mirrors' not in previous or current.get('probe_health')=='video_unavailable':
        return summary
    old = previous.get('video_mirrors') or {}
    new = current.get('video_mirrors') or {}
    mirrors = {}
    for mirror in sorted(old.keys() | new.keys()):
        before,after = old.get(mirror,{}),new.get(mirror,{})
        delta = {reason:after.get(reason,0)-before.get(reason,0) for reason in sorted(before.keys() | after.keys())}
        old_total,new_total = sum(before.values()),sum(after.values())
        entry = {'attempts_delta':new_total-old_total,'outcome_deltas':{key:n for key,n in delta.items() if n}}
        if old_total and new_total:
            entry['pass_rate_delta_pp'] = round(100*(after.get('pass',0)/new_total-before.get('pass',0)/old_total),1)
        mirrors[mirror] = entry
    summary['video_mirrors'] = mirrors
    return summary
