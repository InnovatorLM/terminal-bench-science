import unittest
from author_iteration_clock import LIMIT, transition,reconcile_session

class ClockTests(unittest.TestCase):
    def test_automatic_lifecycle_excludes_stopped_interval(self):
        s={};transition(s,'start','idea-a',0)
        reconcile_session(s,False,100,observed_end=90)
        reconcile_session(s,False,10000,observed_end=90)
        reconcile_session(s,True,11000)
        self.assertEqual(transition(s,'status','idea-a',11010)['active_seconds'],100)

    def test_live_runner_does_not_cancel_explicit_wait(self):
        s={};transition(s,'start','idea-a',0)
        transition(s,'acs-wait','idea-a',10,run_id='actual-acs-run')
        reconcile_session(s,False,100);reconcile_session(s,True,200)
        self.assertEqual(s['ideas']['idea-a']['status'],'acs_wait')
        self.assertEqual(s['ideas']['idea-a']['active_seconds'],10)

    def test_automatic_pause_does_not_refund_exhausted_budget(self):
        s={};transition(s,'start','idea-a',0)
        reconcile_session(s,False,LIMIT+1)
        self.assertEqual(s['ideas']['idea-a']['status'],'abandoned')
        reconcile_session(s,True,LIMIT+1000)
        self.assertEqual(s['ideas']['idea-a']['active_seconds'],LIMIT+1)

    def test_platform_and_stopped_session_do_not_spend_active_budget(self):
        s={};transition(s,'start','idea-a',0)
        transition(s,'platform-wait','idea-a',100,reason='selftest request job-123')
        with self.assertRaises(ValueError):transition(s,'start','idea-b',101)
        transition(s,'resume','idea-a',40000)
        transition(s,'session-pause','idea-a',40100,reason='operator shutdown')
        transition(s,'resume','idea-a',80000)
        self.assertEqual(transition(s,'status','idea-a',80100)['active_seconds'],300)

    def test_pause_cannot_erase_time_already_spent(self):
        s={};transition(s,'start','idea-a',0)
        with self.assertRaises(ValueError):transition(s,'session-pause','idea-a',LIMIT+1,reason='too late')
        self.assertEqual(s['ideas']['idea-a']['status'],'abandoned')
    def test_live_runner_ticks_budget_without_role_command(self):
        state={};transition(state,'start','idea-a',0)
        reconcile_session(state,True,LIMIT)
        self.assertEqual(state['ideas']['idea-a']['status'],'active')
        reconcile_session(state,True,LIMIT+1)
        self.assertEqual(state['ideas']['idea-a']['status'],'abandoned')
        self.assertEqual(state['ideas']['idea-a']['events'][-1]['action'],'abandon')
    def test_acs_wait_does_not_expire_during_long_wall_time(self):
        state={};transition(state,'start','idea-a',0)
        transition(state,'acs-wait','idea-a',10,run_id='job-123')
        reconcile_session(state,True,LIMIT*2)
        self.assertEqual(state['ideas']['idea-a']['status'],'acs_wait')
    def test_active_across_sessions_and_acs_excluded(self):
        s={};transition(s,'start','idea-a',100)
        transition(s,'status','idea-a',200)
        transition(s,'acs-wait','idea-a',250,run_id='acs-job-1')
        transition(s,'status','idea-a',10000)
        transition(s,'resume','idea-a',20000)
        r=transition(s,'status','idea-a',20100)
        self.assertEqual(r['active_seconds'],250)
    def test_over_budget_closes_and_cannot_reset(self):
        s={};transition(s,'start','idea-a',0)
        r=transition(s,'status','idea-a',LIMIT+1)
        self.assertEqual(r['status'],'abandoned')
        with self.assertRaises(ValueError):transition(s,'start','idea-a',LIMIT+2)
        transition(s,'start','idea-b',LIMIT+3)
    def test_parallel_ideas_disallowed(self):
        s={};transition(s,'start','idea-a',0)
        with self.assertRaises(ValueError):transition(s,'start','idea-b',1)
    def test_wait_requires_run_id(self):
        s={};transition(s,'start','idea-a',0)
        with self.assertRaises(ValueError):transition(s,'acs-wait','idea-a',1)
    def test_clock_cannot_go_back(self):
        s={};transition(s,'start','idea-a',100)
        with self.assertRaises(ValueError):transition(s,'status','idea-a',99)
    def test_difficulty_abandonment_is_final(self):
        s={};transition(s,'start','idea-a',0)
        transition(s,'abandon','idea-a',1,reason='difficulty not viable; notes.md')
        with self.assertRaises(ValueError):transition(s,'resume','idea-a',2)
