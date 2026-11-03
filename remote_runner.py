"""Uploaded to a single private Linux run directory; never imports local app state."""
import json
import os
import subprocess
import time
from pathlib import Path


def save(status):
    temporary = Path('status.json.tmp')
    temporary.write_text(json.dumps(status), encoding='utf-8')
    temporary.replace('status.json')


def main():
    os.chdir(Path(__file__).resolve().parent)
    # A lost SSH response must never cause the same solve to run twice.
    try:
        fd = os.open('started.lock', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.close(fd)
    status = {'state': 'running', 'startedAt': time.time()}
    save(status)
    try:
        config = json.loads(Path('run.json').read_text(encoding='utf-8'))
        command = [config['abaqus'], 'job=' + config['jobName'],
                   'input=' + config['input'], 'cpus=' + str(config['cpus']), 'interactive']
        if config.get('userFile'):
            command.append('user=' + config['userFile'])
        with open('solver.log', 'wb') as log:
            code = subprocess.call(command, stdout=log, stderr=subprocess.STDOUT)
        # ODB presence alone is not proof of a successful analysis.
        odbs = [p.name for p in Path('.').glob('*.odb') if p.is_file() and not p.is_symlink()]
        log_text = Path('solver.log').read_text(errors='replace')[-131072:].upper()
        sta = Path(config['jobName'] + '.sta')
        sta_text = sta.read_text(errors='replace')[-65536:].upper() if sta.exists() else ''
        successful = ('COMPLETED SUCCESSFULLY' in sta_text or
                      ('ABAQUS JOB ' + config['jobName'].upper() + ' COMPLETED') in log_text)
        if code != 0 or not odbs or list(Path('.').glob('*.lck')) or not successful:
            raise RuntimeError('Analysis not confirmed successful (exit %s). Check solver.log / .sta.' % code)
        status.update(state='completed', odbs=odbs, returnCode=code)
    except Exception as error:
        status.update(state='failed', error=str(error))
    status['finishedAt'] = time.time()
    save(status)


if __name__ == '__main__':
    main()
