"""Keep the solver budget separate from the Pod and controller lifetime.

A Pod starts aging before agent setup. It must survive agent cancellation,
artifact collection and verification; using the solver timeout as its TTL
kills the only copy of the trajectory before Harbor can retrieve it.
"""
import math
from pathlib import Path
import tomllib

PHASE_LIMIT = 14400
SETUP_ALLOWANCE = 600
LIFECYCLE_ALLOWANCE = 1800  # scheduling, transfers and final log collection
CLEANUP_ALLOWANCE = 600


def _seconds(value, default):
    value = default if value is None else value
    if type(value) not in (float, int) or not math.isfinite(value) or not 0 < value <= PHASE_LIMIT:
        raise ValueError('Execution phase budget must be positive and at most four hours')
    return math.ceil(value)


def budgets(config):
    agent = config['agents'][0]
    task = {}
    datasets = config.get('datasets', [])
    if len(datasets) == 1 and len(datasets[0].get('task_names', [])) == 1:
        path = Path(datasets[0]['path']) / datasets[0]['task_names'][0] / 'task.toml'
        if path.is_file():
            task = tomllib.loads(path.read_text())
    agent_seconds = _seconds(agent.get('override_timeout_sec'), PHASE_LIMIT)
    verifier = config.get('verifier', {})
    verifier_seconds = _seconds(verifier.get('override_timeout_sec'),
                                (task.get('verifier') or {}).get('timeout_sec', 600))
    setup_seconds = _seconds(agent.get('override_setup_timeout_sec'), SETUP_ALLOWANCE)
    # One common deadline also covers shared-verifier tasks. Separate Pods
    # finish earlier and are deleted immediately; this is a ceiling, not a wait.
    pod_seconds = setup_seconds + agent_seconds + verifier_seconds + LIFECYCLE_ALLOWANCE
    return {'agent_seconds': agent_seconds, 'verifier_seconds': verifier_seconds,
            'setup_seconds': setup_seconds, 'pod_seconds': pod_seconds,
            'controller_seconds': pod_seconds + CLEANUP_ALLOWANCE,
            'worker_seconds': pod_seconds + 2 * CLEANUP_ALLOWANCE}


def apply(config):
    """Set only infrastructure lifetime; never extend the solver's four hours."""
    value = budgets(config)
    config['environment']['kwargs']['pod_overrides']['spec']['activeDeadlineSeconds'] = value['pod_seconds']
    return value
