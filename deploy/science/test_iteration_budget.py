import unittest
from author_iteration_clock import transition
from iteration_budget import LIMIT, verify


class BudgetTests(unittest.TestCase):
    def test_recomputes_and_ignores_forged_total(self):
        state={};transition(state,'start','idea',0)
        row=state['ideas']['idea'];row['active_seconds']=0
        self.assertEqual(verify(row,LIMIT,{})['active_iteration_seconds'],LIMIT)
        with self.assertRaises(ValueError):verify(row,LIMIT+1,{})

    def test_acs_wait_requires_actual_interval_not_just_run_name(self):
        state={};transition(state,'start','idea',0)
        transition(state,'acs-wait','idea',100,run_id='claimed-run')
        transition(state,'resume','idea',30000)
        row=state['ideas']['idea']
        with self.assertRaises(ValueError):verify(row,30100,{})
        with self.assertRaises(ValueError):verify(row,30100,{'acs-wait':[(101,30000)]})
        self.assertEqual(verify(row,30100,{'acs-wait':[(100,20000),(20000,30000)]})['active_iteration_seconds'],200)

    def test_session_reset_preserves_original_time(self):
        state={};transition(state,'start','idea',0)
        transition(state,'session-pause','idea',LIMIT-1000,reason='stopped')
        transition(state,'resume','idea',LIMIT+22000)
        row=state['ideas']['idea'];proof={'session-pause':[(LIMIT-1000,LIMIT+22000)]}
        self.assertEqual(verify(row,LIMIT+23000,proof)['active_iteration_seconds'],LIMIT)
        with self.assertRaises(ValueError):verify(row,LIMIT+23001,proof)

    def test_invalid_or_closed_ideas_refused(self):
        for row in (None, [], {'events':[None]}, {'events':[]},{'events':[{'action':'resume','at':0}]},
                    {'events':[{'action':'start','at':0},{'action':'start','at':1}]},
                    {'events':[{'action':'start','at':0},{'action':'abandon','at':1}]},
                    {'events':[{'action':'start','at':float('nan')}]},
                    {'events':[{'action':'start','at':10},{'action':'finish','at':9}]}):
            with self.subTest(row=row),self.assertRaises(ValueError):verify(row,100,{})


if __name__=='__main__':unittest.main()
