"""Exact operator-owned handoff identity shared by workers and review dispatch.

This is scheduling state only: it neither changes an author clock nor runs ACS.
"""
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat

from author_submission_gate import read_operator_file, validate_identity
from evaluation_queue import atomic_json

BINDING_FIELDS = ('job_id', 'idea_id', 'commit', 'task', 'received_at')


@contextmanager
def feedback_lock(root):
    fd = os.open(Path(root)/'.review-feedback.lock',
                 os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise ValueError('Invalid operator feedback lock')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def read_json(path):
    try:
        value = json.loads(read_operator_file(Path(path), 1024*1024))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def binding(status, require_idea=True):
    """Reject incomplete/legacy identities rather than guessing their source."""
    role, job, idea = (status.get(k) for k in ('role', 'job_id', 'idea_id'))
    if (not isinstance(role, str) or not re.fullmatch(r'author-[0-9]+', role)
            or not isinstance(job, str) or not re.fullmatch('[a-f0-9]{32}', job)):
        return None
    if not (idea is None and not require_idea) and (
            not isinstance(idea, str) or not re.fullmatch('[a-z0-9][a-z0-9-]{0,95}', idea)):
        return None
    commit, task, received = (status.get(k) for k in
                              ('source_commit', 'source_task_path', 'received_at'))
    try:
        validate_identity(commit, task)
    except ValueError:
        return None
    if type(received) not in (float, int) or not math.isfinite(received) or received <= 0:
        return None
    return dict(role=role, job_id=job, idea_id=idea, commit=commit, task=task, received_at=received)


def same_binding(left, right):
    return all(left.get(k) is not None and left.get(k) == right.get(k) for k in BINDING_FIELDS)


def current_request(root):
    return read_json(Path(root)/'selftest/current-request.json')


def record_current_request(root, status):
    """New accepted work invalidates an older platform pause immediately."""
    # A plain self-test may lack --idea. It still supersedes an old request,
    # while never qualifying for an idea-bound platform pause itself.
    value = binding(status, require_idea=False)
    if value is None:
        return False
    root = Path(root)
    with feedback_lock(root):
        current = current_request(root)
        if current:
            received = current.get('received_at')
            if type(received) not in (float, int) or not math.isfinite(received):
                return False
            if received > value['received_at'] or (received == value['received_at']
                    and any(current.get(k)!=value.get(k) for k in BINDING_FIELDS)):
                return False
        atomic_json(root/'selftest/current-request.json', value, 0o644)
        feedback=read_json(root/'review-feedback.json')
        assignment=read_json(root/'review-current-assignment.json')
        if (feedback.get('feedback_source')=='selftest_handoff'
                and assignment.get('feedback_source')=='selftest_handoff'
                and same_binding(feedback,assignment) and not same_binding(feedback,value)):
            (root/'review-current-assignment.json').unlink(missing_ok=True)
            (root/'review-feedback.json').unlink(missing_ok=True)
    return True


def current_idea_matches(root, idea):
    """Read the author's current selection; never alter or attest its budget."""
    directory = Path(root)/'home/role/.state/author-iteration'
    path = directory/'ledger.json'
    try:
        if directory.resolve() != directory or path.is_symlink():
            return False
        fd = os.open(path, os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024*1024:
                return False
            ledger = json.load(stream)
        active = [name for name, row in ledger.get('ideas', {}).items()
                  if row.get('status') in ('active', 'acs_wait', 'platform_wait', 'session_paused')]
        return active == [idea]
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def release_handoff(root, source):
    """Clear this job's scheduling hold, preserving any newer review feedback."""
    with feedback_lock(root):
        feedback = read_json(root/'review-feedback.json')
        assignment = read_json(root/'review-current-assignment.json')
        from post_acceptance import accepted
        if accepted(feedback):return
        if (same_binding(feedback, source) and same_binding(assignment, source)
                and feedback.get('feedback_source') == 'selftest_handoff'
                and assignment.get('feedback_source') == 'selftest_handoff'):
            (root/'review-current-assignment.json').unlink(missing_ok=True)
            (root/'review-feedback.json').unlink(missing_ok=True)


def publish_handoff(work, state, entry, status, result):
    """Consume a retryable handoff without allowing historical jobs to park work."""
    root = Path(entry['root'])
    source = binding(status)
    if source is None or Path(work).name != source['job_id']:
        return
    # Recover the newest accepted identity for jobs received before this marker
    # existed. Use operator receipt times, never result mtimes or Git ordering.
    for candidate in Path(state).glob('*/status.json'):
        other = read_json(candidate)
        if other.get('role') != source['role'] or other.get('job_id') != candidate.parent.name:
            continue
        received = other.get('received_at')
        if (type(received) in (int, float) and math.isfinite(received)
                and received >= source['received_at'] and other.get('job_id') != source['job_id']):
            record_current_request(root, other)
            release_handoff(root, source)
            return
    reused=(result.get('state')=='completed' and result.get('stage')=='previously_accepted_version')
    if not reused and not current_idea_matches(root, source['idea_id']):
        release_handoff(root, source)
        return
    if not record_current_request(root, status):
        return
    if result.get('state') == 'ready_for_independent_review':
        # A recovered handoff is released only if it still owns this pause.
        # The dispatcher will independently deliver the real review assignment.
        release_handoff(root, source)
        return
    if result.get('state') not in ('waiting_platform', 'waiting_author') and not reused:
        return
    from review_dispatcher import publish_author_feedback
    row = dict(result, **source, key='selftest:'+source['job_id'], generation=0,
               feedback_source='selftest_handoff', author_role=source['role'])
    # role denotes an evaluator in dispatcher rows, so do not use author role
    # as an invented evaluator assignment.
    row.pop('role')
    if reused:
        row.update(key=result['review_key'],generation=result['review_generation'],role=result['evaluator'])
    publish_author_feedback({}, row, {'roles': {source['role']: entry}})
