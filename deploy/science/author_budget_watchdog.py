"""Reset an author once when an observed idea exhausts its active-time budget."""
import json
from pathlib import Path
import time

from author_iteration_clock import EXHAUSTED_REASON
from dead_end_watchdog import read_json, save
from iteration_budget import LIMIT


def exhausted_idea(role, entry, observations, *, attempted_ideas=()):
    root = Path(entry['root'])
    ledger = root/'home/role/.state/author-iteration/ledger.json'
    observation = Path(observations)/(role+'.json')
    if not ledger.exists() or not observation.exists() or ledger.is_symlink():
        return None
    observed = read_json(observation)
    if observed.get('role') != role:
        raise ValueError('Budget observation role mismatch')
    state = read_json(ledger)
    for idea, row in state.get('ideas', {}).items():
        # Closed ideas remain in the ledger permanently. A prior reset must
        # not hide a later idea that independently exhausts its own budget.
        if idea in attempted_ideas:
            continue
        events = row.get('events', [])
        proof = observed.get('ideas', {}).get(idea, {})
        if (row.get('status') == 'abandoned' and row.get('reason') == EXHAUSTED_REASON
                and row.get('active_seconds', 0) > LIMIT and events
                and events[-1].get('action') == 'abandon'
                and events[-1].get('reason') == EXHAUSTED_REASON
                and proof.get('status') == 'abandoned'
                and proof.get('events', [])[-1:] == [{'action': 'abandon', 'at': events[-1]['at']}]):
            return idea
    return None


def scan(config, session_state, observations, state, now=None, reset_action=None):
    """Persist an attempt before reset so a stalled controller cannot cause a reset loop."""
    import role_reset
    session_state, state = Path(session_state), Path(state)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = time.time() if now is None else now
    results = []
    for role, entry in config['roles'].items():
        if not role.startswith('author-'):
            continue
        idea = None
        previous = None
        path = state/(role+'.json')
        try:
            previous = read_json(path) if path.exists() else {}
            if not isinstance(previous, dict) or not isinstance(previous.get('attempted_ideas', {}), dict):
                raise ValueError('Invalid budget reset history')
            idea = exhausted_idea(role, entry, observations,
                                  attempted_ideas=previous.get('attempted_ideas', {}))
            if idea is None:
                continue
            previous.setdefault('attempted_ideas', {})[idea] = dict(at=now, status='reset_requested')
            save(path, previous)
            reason = f'Idea {idea} exceeded 48 hours of active author iteration; ACS and recorded waits excluded.'
            guidance = (f'Close {idea} and start a new idea. Preserve its files, trajectory and original clock; '
                        'briefly record lessons learned, then choose a different direction. Do not restart its budget.')
            if reset_action is None:
                old_u, old_state = role_reset.U, role_reset.DEPLOYMENT_STATE
                try:
                    role_reset.U = session_state.parent.parent
                    role_reset.DEPLOYMENT_STATE = session_state
                    receipt = role_reset.online_reset(role, reason, guidance, apply=True)
                finally:
                    role_reset.U, role_reset.DEPLOYMENT_STATE = old_u, old_state
            else:
                receipt = reset_action(role, reason, guidance, apply=True)
            previous['attempted_ideas'][idea].update(status='reset_ready', reset_id=receipt['reset_id'])
            save(path, previous)
            results.append(dict(role=role, idea=idea, status='reset_ready'))
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
            # An unreadable history must neither crash the whole watchdog nor
            # be replaced with an empty one, which could repeat an old reset.
            if idea is not None and isinstance(previous, dict) and idea in previous.get('attempted_ideas', {}):
                previous['attempted_ideas'][idea].update(status='inspection_required', error_type=type(error).__name__)
                save(path, previous)
            results.append(dict(role=role, status='inspection_required', error_type=type(error).__name__))
    return results
