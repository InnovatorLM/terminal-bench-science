import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from author_iteration_clock import LIMIT, transition
from author_budget_watchdog import scan
from evaluation_queue import W


class BudgetWatchdogTests(unittest.TestCase):
    def exhausted_author(self, base, observations, role, ideas):
        root=base/role;directory=root/'home/role/.state/author-iteration'
        directory.mkdir(parents=True)
        ledger={};at=0
        for idea in ideas:
            transition(ledger,'start',idea,at)
            at+=LIMIT+1
            transition(ledger,'status',idea,at)
        (directory/'ledger.json').write_text(json.dumps(ledger))
        (observations/(role+'.json')).write_text(json.dumps(dict(role=role,ideas={
            idea:dict(status=row['status'],events=[{'action':event['action'],'at':event['at']}
                                                    for event in row['events']])
            for idea,row in ledger['ideas'].items()})))
        return {'root':str(root)}

    def test_a_later_exhausted_idea_is_not_hidden_by_the_first_reset(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            base=Path(raw);observations=base/'observations';observations.mkdir()
            entry=self.exhausted_author(base,observations,'author-01',['idea-first','idea-second'])
            config={'roles':{'author-01':entry}};state=base/'resets'
            reset=Mock(side_effect=[{'reset_id':'first'},{'reset_id':'second'}])
            first=scan(config,base/'sessions',observations,state,reset_action=reset)
            second=scan(config,base/'sessions',observations,state,reset_action=reset)
            self.assertEqual(first[0]['idea'],'idea-first')
            self.assertEqual(second[0]['idea'],'idea-second')
            self.assertEqual(scan(config,base/'sessions',observations,state,reset_action=reset),[])
            self.assertEqual(reset.call_count,2)
            self.assertEqual(set(json.loads((state/'author-01.json').read_text())['attempted_ideas']),
                             {'idea-first','idea-second'})

    def test_corrupt_reset_history_is_retained_and_other_authors_are_checked(self):
        with tempfile.TemporaryDirectory(dir=W/'.tmp') as raw:
            base=Path(raw);observations=base/'observations';observations.mkdir()
            config={'roles':{role:self.exhausted_author(base,observations,role,['idea-a'])
                             for role in ['author-01','author-02']}}
            state=base/'resets';state.mkdir();broken=state/'author-01.json'
            broken.write_text('{partial history')
            reset=Mock(return_value={'reset_id':'other-author'})
            result=scan(config,base/'sessions',observations,state,reset_action=reset)
            self.assertEqual(result[0]['status'],'inspection_required')
            self.assertEqual(result[1]['status'],'reset_ready')
            self.assertEqual(reset.call_args.args[0],'author-02')
            self.assertEqual(broken.read_text(),'{partial history')

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
