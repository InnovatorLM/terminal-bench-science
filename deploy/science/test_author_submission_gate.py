import copy
import os
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import author_submission_gate as gate
from iteration_budget import LIMIT

class AuthorGateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=gate.W/'.tmp')
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'execution.json'
        self.path.write_text('{"container_exit": 0}')
        self.path.chmod(0o600)
        self.receipt=dict(commit='a'*40,task='tasks/science/example',
            snapshot_sha256='b'*64,runtime_image='registry/ns/task@sha256:'+'c'*64,
            checks={k:True for k in gate.CHECKS},iteration_applied=True,
            pre_submission_order=list(gate.REQUIRED_ORDER),
            active_iteration_seconds=17999,difficulty_viable=True,
            evidence_files=[dict(path=str(self.path),sha256=hashlib.sha256(self.path.read_bytes()).hexdigest())])
    def check(self,receipt):
        return gate.validate_receipt(receipt,'a'*40,'tasks/science/example')
    def test_complete_passes(self):
        self.assertIs(self.check(self.receipt),self.receipt)
    def test_malformed_payloads_are_validation_errors(self):
        for receipt in (None,[],True,dict(self.receipt,checks=[]),dict(self.receipt,snapshot_sha256=None),dict(self.receipt,runtime_image=[]),dict(self.receipt,evidence_files={}),dict(self.receipt,evidence_files=[None]),dict(self.receipt,evidence_files=[{}])):
            with self.subTest(receipt=receipt),self.assertRaises(ValueError):self.check(receipt)

    def test_invalid_identity_rejected_before_lookup(self):
        for commit,task in [('../other','tasks/example'),('a'*40,'../example'),('a'*40,'/example'),('a'*40,'tasks//example'),('a'*40,'tasks/./example'),('a'*40,'tasks\\example')]:
            with self.subTest(commit=commit,task=task),self.assertRaises(ValueError):gate.approved(commit,task)
    def test_each_missing_check_blocks(self):
        for key in gate.CHECKS:
            with self.subTest(key=key):
                r=copy.deepcopy(self.receipt);r['checks'][key]=False
                with self.assertRaises(ValueError):self.check(r)
    def test_iteration_and_difficulty_required(self):
        for key in ('iteration_applied','difficulty_viable'):
            r=copy.deepcopy(self.receipt);r[key]=False
            with self.assertRaises(ValueError):self.check(r)
    def test_submission_order_required(self):
        for order in (None, list(reversed(gate.REQUIRED_ORDER)), list(gate.REQUIRED_ORDER[:-1])):
            r=copy.deepcopy(self.receipt);r['pre_submission_order']=order
            with self.subTest(order=order),self.assertRaises(ValueError):self.check(r)
    def test_budget(self):
        for value in (LIMIT+1,-1,True,float('nan'),float('inf')):
            r=copy.deepcopy(self.receipt);r['active_iteration_seconds']=value
            with self.subTest(value=value),self.assertRaises(ValueError):self.check(r)
        self.receipt['active_iteration_seconds']=LIMIT
        self.check(self.receipt)
    def test_stale_commit_blocks(self):
        self.receipt['commit']='d'*40
        with self.assertRaises(ValueError):self.check(self.receipt)
    def test_changed_evidence_blocks(self):
        self.path.write_text('changed')
        with self.assertRaises(ValueError):self.check(self.receipt)
    def test_missing_receipt_blocks(self):
        with patch.object(gate,'W',Path(self.temp.name)):
            self.assertFalse(gate.approved('a'*40,'tasks/science/example'))

    def test_special_files_links_and_size_are_rejected_without_blocking(self):
        root=Path(self.temp.name);fifo=root/'fifo';os.mkfifo(fifo)
        link=root/'link';link.symlink_to(self.path)
        for path in (fifo,link):
            with self.subTest(path=path),self.assertRaises((OSError,ValueError)):
                gate.read_operator_file(path)
        with self.assertRaises(ValueError):gate.read_operator_file(self.path,1)
