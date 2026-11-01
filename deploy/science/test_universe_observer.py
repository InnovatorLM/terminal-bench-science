import json
from pathlib import Path
from universe_observer import scan

def test_observer_is_proposal_only(tmp_path):
    result=scan(tmp_path)
    assert result['actions']['mode']=='proposal_only'
    assert result['actions']['requires_operator_approval'] is True

def test_observer_detects_infrastructure_signal(tmp_path):
    run=tmp_path/'.tmp/gewu-terminal-bench-science-runtime-final-v1/.state/gewu-multiverse/author-01/run-1'
    run.mkdir(parents=True); (run/'run.log').write_text('rewards=null ValueError')
    result=scan(tmp_path)
    assert any(x['kind']=='infrastructure_signal' for x in result['findings'])
