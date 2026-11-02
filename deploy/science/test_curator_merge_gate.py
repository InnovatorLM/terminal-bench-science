"""Explicit live-local Forgejo merge-gate regression on a disposable private repo.

No real candidate is approved or merged. Requires the local controller server
and previously read-back review protection policy; credentials never leave the
workspace. Every created repository is deleted and checked for404in finally.
"""
import sys
if '--live' not in sys.argv:
    if __name__ != '__main__':
        import unittest
        raise unittest.SkipTest('Live Forgejo fixture requires explicit --live; not part of offline discovery')
    raise SystemExit('Pass --live to exercise the local Forgejo with a disposable private repository')
import base64,json,secrets,tomllib,time
from pathlib import Path
import requests
w=Path('/mnt/data/zhanglinfeng/workspace'); c=tomllib.loads((w/'universes/my-terminal-bench-science/.config/gewu-multiverse/credentials.toml').read_text());base='http://127.0.0.1:3000/api/v1';repo='gate-validation-'+secrets.token_hex(4);report={'synthetic_private_repository':repo,'real_task_modified':False};created=False
s={}
for role in ['owner','author','evaluator','curator']:
 token=c['forgejo_admin_token'] if role=='owner' else json.loads((w/'.config/universe-roles'/(role+'.json')).read_text())['token'];session=requests.Session();session.trust_env=False;session.headers['Authorization']='token '+token;s[role]=session

def call(role,method,path,data=None):
 return s[role].request(method,base+path,json=data,timeout=120)
def require(response,status):
 assert response.status_code==status, 'Unexpected HTTP '+str(response.status_code)
 return response.json() if response.content else None
path='/repos/team/'+repo
try:
 require(call('owner','POST','/orgs/team/repos',{'name':repo,'private':True,'auto_init':True,'default_branch':'main'}),201);created=True
 for role,permission in [('author','write'),('evaluator','read'),('curator','write')]:require(call('owner','PUT',path+'/collaborators/science-'+role,{'permission':permission}),204)
 policy=json.loads((w/'.state/harbor-eval/curator-review-protection.json').read_text())['policy'];policy['branch_name']='main';require(call('owner','POST',path+'/branch_protections',policy),201)
 require(call('author','POST',path+'/branches',{'new_branch_name':'gate-test','old_branch_name':'main'}),201)
 commit=require(call('author','POST',path+'/contents/probe.txt',{'branch':'gate-test','message':'Synthetic gate validation','content':base64.b64encode(b'test only\n').decode()}),201)['commit']['sha']
 pr=require(call('author','POST',path+'/pulls',{'head':'gate-test','base':'main','title':'Synthetic private merge-gate validation'}),201)['number'];pull=path+'/pulls/'+str(pr)
 def denied(label,role='curator'):
  deadline=time.monotonic()+180
  while True:
   response=call(role,'POST',pull+'/merge',{'Do':'merge'})
   message=response.json().get('message') if response.content else None
   if response.status_code==405 and message=='Please try again later' and time.monotonic()<deadline:
    time.sleep(2);continue
   break
  assert response.status_code in [403,405,409,422] and message!='Please try again later', label+': HTTP '+str(response.status_code)+' '+str(message)
  report[label]={'denied':True,'http_status':response.status_code,'message':message}
 def approve(role,sha):
  response=call(role,'POST',pull+'/reviews',{'event':'APPROVED','commit_id':sha,'body':'Synthetic test approval; not a real task acceptance.'})
  require(response,200)
 denied('zero_approvals');approve('evaluator',commit);denied('one_evaluator_approval');denied('owner_cannot_bypass_gate','owner');approve('owner',commit)
 current=require(call('author','GET',path+'/contents/probe.txt?ref=gate-test'),200)
 commit=require(call('author','PUT',path+'/contents/probe.txt',{'branch':'gate-test','message':'Invalidate stale synthetic approvals','sha':current['sha'],'content':base64.b64encode(b'changed test only\n').decode()}),200)['commit']['sha']
 denied('stale_approvals');approve('evaluator',commit);approve('owner',commit)
 reviews=require(call('owner','GET',pull+'/reviews'),200);report['final_reviews']=[{k:x.get(k) for k in ['state','commit_id','official','dismissed']} for x in reviews]
 deadline=time.monotonic()+180
 while True:
  response=call('curator','POST',pull+'/merge',{'Do':'merge'})
  message=response.json().get('message') if response.content else None
  if response.status_code==405 and message=='Please try again later' and time.monotonic()<deadline:
   time.sleep(2);continue
  break
 report['final_merge_status']=response.status_code;report['final_merge_message']=message
 assert response.status_code in [200,201], 'Two approvals merge: HTTP '+str(response.status_code)+' '+str(message)
 report['two_current_approvals_curator_merge_passed']=True
finally:
 if created:
  response=call('owner','DELETE',path);report['synthetic_repository_deleted']=response.status_code==204
  report['synthetic_repository_404']=call('owner','GET',path).status_code==404
 (w/'.state/harbor-eval/curator-merge-gate-live.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))
assert report.get('synthetic_repository_deleted') and report.get('synthetic_repository_404'), 'Synthetic repository cleanup was not confirmed'
