"""Best-effort periodic backup of a local /tmp universe runtime to CPFS.

The runtime remains responsive when CPFS is slow: each copy has a bounded
timeout and failures are recorded locally for the next cycle.
"""
import argparse, fnmatch, json, os, shutil, subprocess, time
from pathlib import Path

def main():
    p=argparse.ArgumentParser(); p.add_argument('--source',type=Path,required=True)
    p.add_argument('--target',type=Path,required=True); p.add_argument('--interval',type=int,default=600)
    a=p.parse_args(); a.target.mkdir(parents=True,exist_ok=True)
    # The destination may predate this process and have permissive mode.
    # Tighten it before writing any recovery metadata.
    a.target.chmod(0o700)
    log=a.source/'.state/gewu-multiverse/runtime-backup.jsonl'; log.parent.mkdir(parents=True,exist_ok=True)
    while True:
        started=time.time(); row={'started_at':started,'source':str(a.source),'target':str(a.target)}
        try:
            # rsync is incremental and does not block the controller.  Never
            # copy volatile sockets, locks, caches or credentials to logs.
            # Backups are for recovery metadata and trajectories only.  Never
            # copy credentials or authentication material to the durable CPFS
            # target: runtime trees commonly contain role credentials.toml,
            # provider .env files, and Forgejo/token state.
            sensitive_names = {
                'credentials.toml', 'credentials.json', 'token', 'tokens',
                'secrets', 'secret', '.env',
            }
            sensitive_globs = ('*.env', '*.token', '*token*.json', '*secret*')
            def excluded(path):
                rel = path.relative_to(a.source)
                parts = rel.parts
                if any(part in {'.cache', '.tmp', 'secrets', 'secret'} for part in parts):
                    return True
                name = path.name.lower()
                if name in sensitive_names:
                    return True
                return any(fnmatch.fnmatch(name, pat) for pat in sensitive_globs)

            if shutil.which('rsync'):
                cmd=['rsync','-a','--timeout=45',
                     '--exclude=.cache/','--exclude=.tmp/','--exclude=*.lock',
                     '--exclude=credentials.toml','--exclude=credentials.json',
                     '--exclude='+'*.env','--exclude=secrets/','--exclude=secret/',
                     '--exclude=*token*.json','--exclude=*secret*',
                     str(a.source)+'/',str(a.target)+'/']
                subprocess.run(cmd,check=True,timeout=300,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            else:
                # Minimal images may not include rsync. Keep the same exclusion
                # policy with a bounded local copy so backup still works.
                for src in a.source.rglob('*'):
                    rel=src.relative_to(a.source)
                    if excluded(src) or src.name.endswith('.lock'):
                        continue
                    dst=a.target/rel
                    if src.is_dir(): dst.mkdir(parents=True,exist_ok=True)
                    elif src.is_file():
                        dst.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(src,dst)
            row.update(status='ok',elapsed_seconds=round(time.time()-started,2))
        except Exception as e:
            row.update(status='failed',error_type=type(e).__name__,error=str(e)[:300],elapsed_seconds=round(time.time()-started,2))
        with log.open('a') as f: f.write(json.dumps(row,ensure_ascii=False)+'\n')
        time.sleep(max(30,a.interval))
if __name__=='__main__': main()
