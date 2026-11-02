"""Keep Forgejo's database and repositories on the selected runtime volume."""
import configparser
from pathlib import Path


def normalize(instance, port):
    instance=Path(instance).resolve()
    path=instance/'app.ini'
    text=path.read_text()
    cfg=configparser.ConfigParser(interpolation=None,strict=False)
    cfg.optionxform=str
    cfg.read_string('[__root__]\n'+text)
    local=str(instance).startswith('/tmp/gewu-terminal-bench-science-runtime-')
    if local:
        # Never repoint a live instance at a fresh empty database/repository.
        # Migration must first copy and verify the existing authoritative data.
        if not (instance/'data/forgejo.db').is_file() or not (instance/'data/repositories').is_dir():
            raise ValueError('Local Forgejo data is incomplete; migrate the existing database and repositories before startup')
    for section,values in {
        '__root__':{'WORK_PATH':str(instance)},
        'database':{'PATH':str(instance/'data/forgejo.db')},
        'repository':{'ROOT':str(instance/'data/repositories')},
        'server':{'APP_DATA_PATH':str(instance/'data'),'HTTP_PORT':str(port),
                  'ROOT_URL':'http://127.0.0.1:'+str(port)+'/'},
        'log':{'ROOT_PATH':str(instance/'log')},
    }.items():
        if section not in cfg:cfg.add_section(section)
        cfg[section].update(values)
    import io
    output=io.StringIO();cfg.write(output)
    path.write_text(output.getvalue().removeprefix('[__root__]\n'))
    return {'database':str(instance/'data/forgejo.db'),'repositories':str(instance/'data/repositories')}
