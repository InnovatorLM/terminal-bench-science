import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from author_iteration_clock import LIMIT, transition
from author_budget_watchdog import scan
from evaluation_queue import W


class BudgetWatchdogTests(unittest.TestCase):
    def test_exhaustion_resets_once_but_waiting_and_finished_do_not(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            base=Path(raw)
            observations=base/'observations';observations.mkdir()
            config={'roles':{}}
            for role, action in [('author-01','status'),('author-02','acs-wait'),
                                 ('author-03','platform-wait'),('author-04','finish')]:
                root=base/role;directory=root/'home/role/.state/author-iteration'
                directory.mkdir(parents=True)
                state={};transition(state,'start','idea-a',0)
                if action=='status':transition(state,'status','idea-a',LIMIT+1)
                elif action=='acs-wait':transition(state,'acs-wait','idea-a',10,run_id='job-1')
                elif action=='platform-wait':transition(state,'platform-wait','idea-a',10,reason='platform')
                else:transition(state,'finish','idea-a',10,reason='complete')
                (directory/'ledger.json').write_text(json.dumps(state))
                row=state['ideas']['idea-a']
                (observations/(role+'.json')).write_text(json.dumps(dict(role=role,ideas={'idea-a':{
                    'status':row['status'], 'events':[{'action':event['action'],'at':event['at']}
                                                       for event in row['events']]}})))
                config['roles'][role]={'root':str(root)}
            reset=Mock(return_value={'reset_id':'receipt-1'})
            state=base/'resets'
            result=scan(config,base/'.state/gewu-multiverse',observations,state,reset_action=reset)
            self.assertEqual(result,[dict(role='author-01',idea='idea-a',status='reset_ready')])
            self.assertEqual(reset.call_count,1)
            self.assertIn('48 hours',reset.call_args.args[1])
            self.assertEqual(scan(config,base/'.state/gewu-multiverse',observations,state,reset_action=reset),[])
            self.assertEqual(reset.call_count,1)

    def test_no_reset_without_matching_operator_observation(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            base=Path(raw);root=base/'author-01'
            directory=root/'home/role/.state/author-iteration';directory.mkdir(parents=True)
            state={};transition(state,'start','idea-a',0);transition(state,'status','idea-a',LIMIT+1)
            (directory/'ledger.json').write_text(json.dumps(state))
            observations=base/'observations';observations.mkdir()
            (observations/'author-01.json').write_text(json.dumps(dict(role='author-01',ideas={
                'idea-a':{'status':'active','events':[{'action':'start','at':0}]}})))
            reset=Mock()
            self.assertEqual(scan({'roles':{'author-01':{'root':str(root)}}},base/'.state/gewu-multiverse',
                                  observations,base/'resets',reset_action=reset),[])
            reset.assert_not_called()
