"""Record an operator's formal decision for one completed, frozen review."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time


IDENTITY = ('key', 'commit', 'task', 'snapshot_sha256', 'evaluation_id', 'role')


def name_for(key):
    return hashlib.sha256(key.encode()).hexdigest() + '.json'


def validate(row, receipt):
    if (receipt.get('schema_version') != 1 or receipt.get('decision') != 'accepted'
            or row.get('state') != 'completed'
            or any(receipt.get(field) != row.get(field) for field in IDENTITY)
            or receipt.get('generation') != row.get('generation')
            or receipt.get('review_reason_sha256') != hashlib.sha256(
                row.get('reason', '').encode()).hexdigest()
            or row.get('review_recommendation') == 'reject'
            or not isinstance(receipt.get('operator'), str) or not receipt['operator'].strip()
            or not isinstance(receipt.get('rationale'), str) or len(receipt['rationale'].strip()) < 20):
        raise ValueError('Formal decision does not match this completed frozen review')


def apply(state, ledger):
    directory = Path(state) / 'formal-decisions'
    if not directory.exists():
        return
    info = directory.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('Formal decision directory must be root-private')
    for key, row in ledger.items():
        path = directory / name_for(key)
        if not path.exists():
            continue
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o077):
            raise ValueError('Formal decision receipt must be root-private')
        raw = path.read_bytes()
        receipt = json.loads(raw)
        validate(dict(row, key=key), receipt)
        row.update(scientific_acceptance=True, formal_decision='accepted',
                   acceptance_scope='completed_frozen_version_only',
                   formal_decision_sha256=hashlib.sha256(raw).hexdigest())
        row.pop('acceptance_blockers', None)


def record(state, key, operator, rationale, *, evidence=None):
    if os.geteuid() != 0:
        raise PermissionError('Formal decisions require the operator account')
    state = Path(state)
    ledger = json.loads((state / 'ledger.json').read_text())
    row = ledger[key]
    receipt = dict(schema_version=1, decision='accepted', generation=row['generation'],
                   review_reason_sha256=hashlib.sha256(row.get('reason', '').encode()).hexdigest(),
                   operator=operator, rationale=rationale, decided_at=time.time(),
                   **{field: key if field == 'key' else row[field] for field in IDENTITY})
    if evidence is not None:
        receipt['verified_evidence'] = evidence
    validate(dict(row, key=key), receipt)
    directory = state / 'formal-decisions'
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise ValueError('Formal decision directory must be root-private')
    path = directory / name_for(key)
    if path.exists():
        existing = json.loads(path.read_text())
        validate(dict(row, key=key), existing)
        return path
    fd, temporary = tempfile.mkstemp(prefix='.decision-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(receipt, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--operator', required=True)
    parser.add_argument('--rationale', required=True)
    args = parser.parse_args()
    print(record(args.state, args.key, args.operator, args.rationale))


if __name__ == '__main__':
    main()
