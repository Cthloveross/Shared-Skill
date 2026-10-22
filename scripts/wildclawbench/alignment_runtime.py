"""Mechanical deployment checks in the actual task image, without model calls."""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from scripts.wildclawbench import environment
from scripts.wildclawbench.alignment_catalog import inspect_bank, render_catalog


def docker(config, *args, **kwargs):
    budgeted = kwargs.pop('_budgeted', True)
    if budgeted and config.get('execution_protocol'):
        from scripts.injection import resilient_runner
        if resilient_runner.enabled(dict(config)):
            budget = resilient_runner.active_budget(dict(config))
            if budget is not None:
                requested = float(kwargs.get('timeout', 600))
                kwargs['timeout'] = max(0.1, min(requested, budget.require_remaining('candidate-preflight')))
    return environment._run(['docker', *args], config=config, **kwargs)


def install_bank(name, bank_dir, config):
    manifest = inspect_bank(bank_dir)
    for skill in manifest['skills']:
        directory = skill['directory']
        # Remove obsolete supporting files, while preserving unrelated image skills.
        docker(config, 'exec', name, 'rm', '-rf', '--', '/root/skills/' + directory)
        docker(config, 'cp', str(Path(bank_dir) / directory), name + ':/root/skills/', timeout=180)
    return manifest


_READ_PROBE = '''import hashlib,json,stat
from pathlib import Path
records=[]
for directory in NAMES:
 root=Path('/root/skills')/directory
 for path in sorted(root.rglob('*')):
  if path.is_symlink():raise RuntimeError('Symlink in deployed bank')
  if path.is_file():
   data=path.read_bytes()
   records.append({'path':directory+'/'+path.relative_to(root).as_posix(),'size':len(data),
                   'mode':stat.S_IMODE(path.stat().st_mode),'sha256':hashlib.sha256(data).hexdigest()})
print(json.dumps(records))
'''

_PARSER_PROBE = r'''
const fs = require('node:fs'), crypto = require('node:crypto');
const req = require('node:module').createRequire('/usr/lib/node_modules/openclaw/package.json');
const yaml = req('yaml');
const rows = NAMES.map(directory => {
 const path = '/root/skills/' + directory + '/SKILL.md';
 const raw = fs.readFileSync(path), text = raw.toString('utf8').replace(/\r\n/g,'\n').replace(/\r/g,'\n');
 const end = text.indexOf('\n---',3);
 if (!text.startsWith('---\n') || end < 0) throw Error('Missing frontmatter: '+path);
 const fm = yaml.parse(text.slice(4,end));
 if (!fm || typeof fm.name !== 'string' || typeof fm.description !== 'string' || !fm.description.trim())
   throw Error('Invalid frontmatter fields: '+path);
 return {directory, name:fm.name, path, skill_sha256:crypto.createHash('sha256').update(raw).digest('hex')};
});
const parserPath = req.resolve('yaml');
console.log(JSON.stringify({parser_path:parserPath,
 parser_source_sha256:crypto.createHash('sha256').update(fs.readFileSync(parserPath)).digest('hex'), rows}));
'''


def verify_deployed_bank(name, bank_dir, config):
    expected = inspect_bank(bank_dir)
    names = [skill['directory'] for skill in expected['skills']]
    read_script = 'NAMES=' + repr(names) + '\n' + _READ_PROBE
    files = json.loads(docker(config, 'exec', name, 'python3', '-c', read_script, timeout=60).stdout)
    if files != expected['files']:
        raise RuntimeError('Deployed shared files, modes, or readable bytes differ from bank')
    script = 'const NAMES=' + json.dumps(names) + ';\n' + _PARSER_PROBE
    parser = json.loads(docker(config, 'exec', name, 'node', '-e', script, timeout=60).stdout)
    if {row['directory']: row['skill_sha256'] for row in parser['rows']} != {
            skill['directory']: skill['skill_sha256'] for skill in expected['skills']}:
        raise RuntimeError('Actual Node YAML parser read different skill bytes')
    raw = docker(config, 'exec', name, 'openclaw', 'skills', 'list', '--json', timeout=60).stdout
    catalog = json.loads(raw[raw.index('{'):])
    native = {skill['name']: skill for skill in catalog['skills']}
    for skill in expected['skills']:
        if skill['disabled']:
            continue
        if skill['name'] not in native or not native[skill['name']].get('eligible'):
            raise RuntimeError('Skill rejected by actual OpenClaw loader: ' + skill['name'])
    injected, mode = render_catalog(expected['skills'])
    for skill in expected['skills']:
        if not skill['disabled'] and skill['location'] not in injected:
            raise RuntimeError('Shared catalog omitted actual readable path: ' + skill['directory'])
    return {'passed': True, 'bank_sha256': expected['bank_sha256'], 'files': files,
            'parser': parser, 'native_catalog': catalog, 'injected_catalog_mode': mode,
            'injected_catalog_sha256': hashlib.sha256(injected.encode()).hexdigest(),
            'readability_probe_kind': 'container_python_full_bytes_not_Agent_Read',
            'native_builtin_name_collisions': [skill['name'] for skill in expected['skills']
                                             if native.get(skill['name'], {}).get('bundled')],
            'no_model_calls': True}


def preflight_candidate_bank(bank_dir, candidate, config):
    runtime = Path(config['runtime_dir'])
    name = 'sharedskill-preflight-' + uuid.uuid4().hex[:16]
    output = runtime / 'preflights' / (candidate.get('candidate_id') or name)
    output.mkdir(parents=True, exist_ok=True)
    record = {'passed': False, 'candidate_id': candidate.get('candidate_id'), 'no_model_calls': True}
    try:
        expected = inspect_bank(bank_dir)
        record['bank_sha256'] = expected['bank_sha256']
        docker(config, 'run', '--detach', '--name', name, *environment.provider_network_args(),
               environment.TASK_IMAGE, 'tail', '-f', '/dev/null', timeout=120)
        docker(config, 'exec', name, 'mkdir', '-p', '/tmp_workspace', '/root/skills')
        install_bank(name, bank_dir, config)
        configuration = output / 'openclaw-config.json'
        environment._write_json(configuration, environment.openclaw_config(config, '/tmp_workspace'))
        docker(config, 'cp', str(configuration), name + ':/root/.openclaw/openclaw.json')
        record.update(verify_deployed_bank(name, bank_dir, config))
        if inspect_bank(bank_dir)['bank_sha256'] != expected['bank_sha256']:
            raise RuntimeError('Source bank mutated during preflight')
    except Exception as error:
        if config.get('execution_protocol'):
            from scripts.injection import resilient_runner
            if isinstance(error, (resilient_runner.StageBudgetExceeded,
                                  resilient_runner.CircuitBreakerOpen)):
                raise
            error_type, message = type(error).__name__, str(error)
            signature = resilient_runner.infrastructure_signature(error_type, message)
            record.update(passed=False, error_type=error_type, error=message,
                          unknown=signature is not None,
                          reason='infrastructure_preflight_error' if signature else 'candidate_preflight_failed',
                          infrastructure_signature=signature)
        else:
            record.update(passed=False, error=f'{type(error).__name__}: {error}')
    finally:
        docker(config, 'rm', '--force', name, check=False, timeout=5, _budgeted=False)
        environment._write_json(output / 'result.json', record)
    return record
