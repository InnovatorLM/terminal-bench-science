"""Recompute active iteration time; pauses require operator-observed coverage.

Role ledger totals are not attestations. Callers must supply independently
verified ACS/platform/session-stop intervals, not author-provided run IDs alone.
"""
import math

LIMIT = 48 * 3600


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Invalid iteration timestamp')
    return value


def covered(start, end, intervals):
    cursor = start
    for lower, upper in sorted(intervals):
        lower, upper = number(lower), number(upper)
        if upper < lower:
            raise ValueError('Invalid observed interval')
        if upper < cursor:
            continue
        if lower > cursor:
            return False
        cursor = max(cursor, upper)
        if cursor >= end:
            return True
    return cursor >= end


def verify(row, at, observed_pauses):
    at = number(at)
    if not isinstance(row, dict) or not isinstance(observed_pauses, dict):
        raise ValueError('Invalid budget evidence')
    events = row.get('events')
    if (not isinstance(events, list) or not events or not isinstance(events[0], dict)
            or events[0].get('action') != 'start'):
        raise ValueError('Missing original idea start')
    status = None
    previous = None
    active = 0
    pause_kind = None
    for event in [*events, {'action': 'submission', 'at': at}]:
        if not isinstance(event, dict):
            raise ValueError('Invalid iteration event')
        instant = number(event.get('at'))
        if previous is not None:
            if instant < previous:
                raise ValueError('Iteration events out of order')
            if status == 'active':
                active += instant - previous
            elif status == 'paused' and not covered(previous, instant, observed_pauses.get(pause_kind, [])):
                raise ValueError('Pause lacks independent operator coverage')
        action = event.get('action')
        if action == 'start':
            if status is not None:
                raise ValueError('Idea budget cannot restart')
            status = 'active'
        elif action in ('acs-wait', 'platform-wait', 'session-pause'):
            if status != 'active':
                raise ValueError('Invalid pause transition')
            status = 'paused'; pause_kind = action
        elif action == 'resume':
            if status != 'paused':
                raise ValueError('Invalid resume transition')
            status = 'active'; pause_kind = None
        elif action in ('finish', 'abandon'):
            if status not in ('active', 'paused'):
                raise ValueError('Idea already closed')
            status = action
        elif action == 'submission':
            pass
        else:
            raise ValueError('Unknown iteration transition')
        previous = instant
    if status == 'abandon' or row.get('status') == 'abandoned' or active > LIMIT:
        raise ValueError('Idea abandoned or 48-hour budget exceeded')
    return dict(active_iteration_seconds=active, budget_seconds=LIMIT,
                recomputed_from_events=True, pause_coverage_verified=True,
                event_origin_attested=False, scientific_acceptance=False)
