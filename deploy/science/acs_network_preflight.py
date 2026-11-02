"""Resolve actual Harbor task network plans without constructing cloud clients."""
import hashlib
import inspect
from pathlib import Path


class NetworkPreflightError(ValueError):
    def __init__(self, report):
        self.report = report
        issue = report['issues'][0]
        super().__init__('task.toml is incompatible with the current ACS adapter: '
                         + issue['location'] + ': ' + issue['message'] + ' '
                         + issue['remediation'])


def inspect_network(task, *, environment=None, agent=None):
    from harbor.models.task.config import VerifierEnvironmentMode
    from harbor.models.task.verifier_mode import resolve_task_verifier_mode, resolve_step_verifier_mode
    from harbor.models.trial.config import AgentConfig, EnvironmentConfig
    from harbor.trial.network_policy import resolve_trial_network_plan
    from universe_ack import UniverseACK

    runtime = EnvironmentConfig.model_validate(environment or {'type': 'ack'})
    solver = AgentConfig.model_validate(agent or {'name': 'oracle'})
    # UniverseACK inherits ACK's stateless capabilities property. Its real
    # policy validator needs only those capabilities and the explicit policy;
    # bypass __init__ so no kubeconfig/client/Pod/image construction is possible.
    adapter = object.__new__(UniverseACK)
    capabilities = adapter.capabilities
    fields = [name for name in type(capabilities).model_fields
              if name.startswith('network_') or name in ('disable_internet', 'dynamic_network_policy')]
    issues, plans = [], []

    def describe(policy):
        if policy is None:
            return None
        # Host values can come from the operator's job. The role needs the
        # policy/field diagnosis, not an export of internal endpoint names.
        return {'network_mode': policy.network_mode.value,
                'allowed_hosts_count': len(policy.allowed_hosts)}

    def support(policy, location):
        try:
            adapter.validate_network_policy_support(policy)
        except ValueError as error:
            issues.append({'code': 'acs_static_network_policy_unsupported',
                           'location': location, 'message': str(error),
                           'remediation': 'If this task permits public access, update its source task.toml; '
                           'otherwise request platform support for the required isolation. '
                           'Separate verifier mode does not add unsupported network capabilities.'})

    for index, step in enumerate(task.steps or [None]):
        location = 'task.toml' if step is None else f'task.toml steps[{index}] ({step.name})'
        mode = resolve_task_verifier_mode(task) if step is None else resolve_step_verifier_mode(task, step)
        plan = resolve_trial_network_plan(task, solver, runtime, step, verifier_mode=mode)
        plans.append({'step': None if step is None else step.name, 'verifier_mode': mode.value,
                      'agent_env_baseline': describe(plan.agent_env_baseline),
                      'agent_phase': describe(plan.agent_phase),
                      'verifier_env_baseline': describe(plan.verifier_env_baseline),
                      'verifier_phase': describe(plan.verifier_phase),
                      'artifact_transfer': {
                          'separate_environment': mode == VerifierEnvironmentMode.SEPARATE,
                          'declared_entries': len(task.artifacts) + (len(step.artifacts) if step else 0),
                          'convention_directory': '/logs/artifacts', 'contents_validated': False}})
        support(plan.agent_env_baseline, location + ' [environment] baseline')
        support(plan.agent_phase, location + ' [agent] phase')
        if plan.verifier_env_baseline is not None:
            support(plan.verifier_env_baseline, location + ' [verifier.environment] baseline (or inherited [environment])')
        support(plan.verifier_phase, location + ' [verifier] phase')
        for role, baseline, phase in (
            ('agent', plan.agent_env_baseline, plan.agent_phase),
            ('verifier', plan.verifier_phase_baseline, plan.verifier_phase),
        ):
            if baseline != phase and not capabilities.dynamic_network_policy:
                issues.append({'code': 'acs_dynamic_network_policy_unsupported',
                               'location': location + f' [{role}] phase',
                               'message': 'Phase network policy differs from its startup baseline, '
                               'but this adapter cannot switch policy after start.',
                               'remediation': 'Align the task source phase and environment baseline if task semantics permit. '
                               'For a separate verifier use task.toml [verifier] environment_mode="separate", '
                               '[verifier.environment] and explicit output artifacts; all policies must still be supported.'})

    return {'compatible': not issues, 'adapter': 'universe_ack:UniverseACK',
            'configuration_source': 'task.toml task/step schema and supported job extra_allowed_hosts',
            'network_capabilities': {name: getattr(capabilities, name) for name in fields},
            'plans': plans, 'issues': issues, 'task_code_executed': False, 'cloud_clients_created': False,
            'resolver_source_sha256': hashlib.sha256(Path(inspect.getfile(resolve_trial_network_plan)).read_bytes()).hexdigest(),
            'checker_source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def require_compatible(task, *, environment=None, agent=None):
    report = inspect_network(task, environment=environment, agent=agent)
    if not report['compatible']:
        raise NetworkPreflightError(report)
    return report
