"""Best-effort periodic backup of a local /tmp universe runtime to CPFS.

The runtime remains responsive when CPFS is slow: each copy has a bounded
timeout and failures are recorded locally for the next cycle.
"""
import argparse, json, os, shutil, subprocess, time
from pathlib import Path

def main():
    p=argparse.ArgumentParser(); p.add_argument('--source',type=Path,required=True)
    p.add_argument('--target',type=Path,required=True); p.add_argument('--interval',type=int,default=600)
    a=p.parse_args(); a.target.mkdir(parents=True,exist_ok=True)
    log=a.source/'.state/gewu-multiverse/runtime-backup.jsonl'; log.parent.mkdir(parents=True,exist_ok=True)
    while True:
        started=time.time(); row={'started_at':started,'source':str(a.source),'target':str(a.target)}
        try:
            # rsync is incremental and does not block the controller.  Never
            # copy volatile sockets, locks, caches or credentials to logs.
            if not shutil.which('rsync'): raise RuntimeError('rsync is unavailable')
            cmd=['rsync','-a','--timeout=45','--exclude=.cache/','--exclude=.tmp/','--exclude=*.lock',str(a.source)+'/',str(a.target)+'/']
            subprocess.run(cmd,check=True,timeout=300,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            row.update(status='ok',elapsed_seconds=round(time.time()-started,2))
        except Exception as e:
            row.update(status='failed',error_type=type(e).__name__,error=str(e)[:300],elapsed_seconds=round(time.time()-started,2))
        with log.open('a') as f: f.write(json.dumps(row,ensure_ascii=False)+'\n')
        time.sleep(max(30,a.interval))
if __name__=='__main__': main()
