"""Validate public task declarations before any build or cloud execution."""
from pathlib import Path
import tomllib


def validate_task(task):
    task=Path(task)
    for path in [task/'task.toml',*sorted((task/'steps').glob('*/step.toml'))]:
        document=tomllib.loads(path.read_text())
        def visit(value):
            if isinstance(value,dict):
                for key,item in value.items():
                    if key=='env' and isinstance(item,dict):
                        for name,setting in item.items():
                            if not isinstance(setting,str) or ('$'+'{') in setting:
                                raise ValueError('Public task env values must be literals; host environment interpolation is not supported')
                            if name in {'OPENAI_API_KEY','OPENAI_BASE_URL','OPENAI_API_BASE','PUBLIC_MODEL_ACCESS_TOKEN','CODEX_HOME','PYTHONPATH','LD_PRELOAD','BASH_ENV'} or name.startswith('CODEX_'):
                                raise ValueError('Public task cannot override operator/runtime environment variables')
                    if key=='mcp_servers' and item:
                        raise ValueError('Public tasks with external MCP servers need operator registration')
                    if key=='skills_dir' and item:
                        raise ValueError('Public tasks cannot request host skills directories')
                    if key=='os' and isinstance(item,str) and item.lower()!='linux':
                        raise ValueError('Current ACS runtime supports Linux tasks')
                    if key in {'gpus','tpu'} and item:
                        raise ValueError('GPU/TPU tasks need a separately configured resource profile')
                    visit(item)
            elif isinstance(value,list):
                for item in value:visit(item)
        visit(document)
    return True
