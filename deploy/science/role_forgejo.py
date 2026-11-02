#!/usr/local/bin/python3
"""Scoped Forgejo CLI inside role roots. Never prints authentication material."""
import argparse
import json
import os
import urllib.request

BASE = os.environ.get('GEWU_FORGEJO_URL', 'http://127.0.0.1:3000').rstrip('/') + '/api/v1'

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['assigned','read','comments','comment'])
    parser.add_argument('--repo',default='team/terminal-bench-science')
    parser.add_argument('--issue',type=int)
    parser.add_argument('--body-file')
    args=parser.parse_args()
    if args.repo!='team/terminal-bench-science':raise ValueError('Repository outside current role task')
    credential=json.load(open('/home/role/git-credential.json'))
    prefix='/repos/'+args.repo+'/issues'
    if args.action=='assigned':
        route=prefix+'?state=open&type=issues&limit=50&assigned=true';method='GET';body=None
    elif args.issue is None or args.issue<=0:raise ValueError('Positive issue number required')
    elif args.action=='read':route=prefix+'/'+str(args.issue);method='GET';body=None
    elif args.action=='comments':
        # Read complete Markdown replies, including older pages. The native
        # model tool truncates comments and may omit a late retrospective.
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        seen=set()
        for page in range(1,1001):
            route=prefix+'/'+str(args.issue)+'/comments?limit=50&page='+str(page)
            request=urllib.request.Request(BASE+route,headers={'Authorization':'token '+credential['token']})
            with opener.open(request,timeout=30) as response:batch=json.load(response)
            fresh=[comment for comment in batch if comment['id'] not in seen]
            if not fresh:return
            for comment in fresh:
                seen.add(comment['id'])
                print(json.dumps(comment,ensure_ascii=False))
            if len(batch)<50:return
        raise ValueError('Comment pagination did not terminate')
    else:
        if not args.body_file:raise ValueError('Comment file required')
        with open(args.body_file) as stream:text=stream.read(65537)
        if len(text)>65536:raise ValueError('Comment too large')
        route=prefix+'/'+str(args.issue)+'/comments';method='POST';body=json.dumps({'body':text}).encode()
    request=urllib.request.Request(BASE+route,data=body,method=method,headers={'Authorization':'token '+credential['token'],'Content-Type':'application/json'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request,timeout=30) as response:result=json.load(response)
    if args.action=='assigned':result=[x for x in result if any(a['login']==credential['login'] for a in x.get('assignees',[]))]
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
