"""SSH workstations and recoverable Abaqus runs; no writes to the model database."""
from __future__ import annotations

import base64
import copy
import ctypes
import hashlib
import io
import json
import os
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ACTIVE = {'preparing', 'uploading', 'launching', 'running', 'downloading'}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def protect(value, decrypt=False):
    """Windows current-user DPAPI: passwords never persisted in clear text."""
    if not value:
        return ''
    if os.name != 'nt':
        raise ValueError('保存密码需要 Windows；其他系统请使用 SSH 私钥')
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    raw = base64.b64decode(value) if decrypt else value.encode('utf-8')
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.windll.crypt32
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ValueError('无法读取本机加密凭据，请重新输入密码')
    try:
        data = ctypes.string_at(target.data, target.size)
        return data.decode('utf-8') if decrypt else base64.b64encode(data).decode('ascii')
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


class HostKeyRequired(ValueError):
    def __init__(self, fingerprint):
        super().__init__('首次连接：请核对并信任工作站的 SSH 主机指纹')
        self.fingerprint = fingerprint


def key_fingerprint(key):
    return 'SHA256:' + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')


def ssh_connect(station, trusted=''):
    try:
        import paramiko
    except ImportError:
        raise ValueError('远程模块缺少依赖，请运行安装远程依赖.cmd') from None
    pin = station.get('fingerprint') or trusted
    class Policy(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            fingerprint = key_fingerprint(key)
            if not pin:
                raise HostKeyRequired(fingerprint)
            if fingerprint != pin:
                raise ValueError('SSH 主机指纹改变，连接已阻止；请联系工作站管理员核实')
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(Policy())
    try:
        client.connect(station['host'], port=station['port'], username=station['username'],
                       password=protect(station.get('secret', ''), decrypt=True) or None,
                       key_filename=station.get('keyFile') or None,
                       look_for_keys=False, allow_agent=False, timeout=8,
                       banner_timeout=8, auth_timeout=8, channel_timeout=15)
        client.get_transport().set_keepalive(20)
        return client
    except Exception:
        client.close()
        raise


def execute(client, command, timeout=30):
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    # Commands here only produce bounded diagnostic JSON / short messages.
    output = stdout.read(1024 * 1024).decode('utf-8', errors='replace')
    error = stderr.read(16384).decode('utf-8', errors='replace')
    code = stdout.channel.recv_exit_status()
    if code:
        raise ValueError(error.strip() or output.strip() or '远程命令执行失败 (%s)' % code)
    return output


RESOURCE_SCRIPT = r'''
import json,os,time,shutil,platform
def cpu():
    values=list(map(int,open('/proc/stat').readline().split()[1:9]))
    return sum(values),values[3]+values[4]
a=cpu();time.sleep(0.4);b=cpu()
mem={line.split(':')[0]:int(line.split()[1])*1024 for line in open('/proc/meminfo') if ':' in line}
d=shutil.disk_usage(os.path.expanduser('~'))
print(json.dumps(dict(system=platform.system(),cpuCount=os.cpu_count(),
cpuPercent=round(100*(1-(b[1]-a[1])/max(1,b[0]-a[0])),1),
memoryTotal=mem['MemTotal'],memoryAvailable=mem.get('MemAvailable',mem['MemFree']),
diskTotal=d.total,diskFree=d.free,load=os.getloadavg(),sampledAt=time.time())))
'''


def input_bundle(source, root, extras=()):
    """Preserve relative references; reject unavailable or escaping dependencies."""
    root = Path(root).resolve()
    result = {}
    def add(path, parse=False):
        path = Path(path).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('依赖文件不存在或超出所选模型文件夹：' + str(path))
        relative = path.relative_to(root).as_posix()
        if relative in result:
            return
        result[relative] = path
        if not parse:
            return
        text = path.read_bytes().decode('utf-8', errors='replace')
        for line in text.splitlines():
            if not line.lstrip().startswith('*') or line.lstrip().startswith('**'):
                continue
            # INPUT=/FILE= dependencies on keyword lines (including *INCLUDE).
            for match in re.finditer(r'(?:^|,)\s*(?:INPUT|FILE)\s*=\s*("[^"]+"|\x27[^\x27]+\x27|[^,]+)', line, re.I):
                ref = match.group(1).strip().strip('"\x27')
                if '\\' in ref or ':' in ref or ref.startswith('/'):
                    raise ValueError('远程输入引用请使用文件夹内的相对路径和 /：' + ref)
                add(root / ref, parse=line.lstrip().upper().startswith('*INCLUDE'))
    add(source, parse=True)
    for extra in extras:
        add(root / extra, parse=str(extra).lower().endswith('.inp'))
    return result


def cae_operation(cae, abaqus, directory, job=''):
    directory.mkdir(parents=True, exist_ok=False)
    # The source CAE is copied, and only the disposable copy can be opened/saved.
    copied = directory / 'model.cae'
    shutil.copy2(cae, copied)
    shutil.copy2(ROOT / 'abaqus_job_export.py', directory / 'export.py')
    atomic_json(directory / 'request.json', {'cae': str(copied), 'job': job})
    process = subprocess.Popen([
        'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
        str(ROOT / 'Export-Abaqus-Job.ps1'), '-AbaqusCommand', abaqus,
        '-ScriptFile', str(directory / 'export.py'), '-WorkingDirectory', str(directory)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=0x08000000 if os.name == 'nt' else 0)
    # Running in a background worker; never block the HTTP request.
    try:
        code = process.wait(timeout=600)
    except subprocess.TimeoutExpired:
        # Do not kill unrelated Abaqus sessions. Do not launch a duplicate export.
        raise ValueError('CAE 导出超过 10 分钟；请检查本机 Abaqus 和导出日志，未提交远程计算')
    result_path = directory / 'result.json'
    if not result_path.exists():
        raise ValueError('无法读取 CAE Job，请检查本机 Abaqus/CAE 许可和导出日志')
    result = json.loads(result_path.read_text(encoding='utf-8'))
    if code or result.get('error'):
        raise ValueError(result.get('error') or 'CAE 导出失败')
    return result


class RemoteManager:
    def __init__(self, directory, connector=ssh_connect, start=False):
        self.directory = Path(directory)
        self.lock = threading.RLock()
        self.connector = connector
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='remote-job')
        self.inflight = set()
        self.inspections = {}
        self.stations = self._load('workstations.json', [])
        self.jobs = self._load('jobs.json', [])
        self.stopped = threading.Event()
        if start:
            threading.Thread(target=self._loop, daemon=True, name='remote-monitor').start()

    def _load(self, name, default):
        path = self.directory / name
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default

    def _save(self):
        with self.lock:
            atomic_json(self.directory / 'workstations.json', self.stations)
            atomic_json(self.directory / 'jobs.json', self.jobs)

    def snapshot(self):
        with self.lock:
            stations = [{k: v for k, v in s.items() if k != 'secret'} |
                        {'hasPassword': bool(s.get('secret'))} for s in self.stations]
            jobs = [{k: v for k, v in j.items() if k not in {'files', 'stationSnapshot'}} for j in self.jobs]
            return copy.deepcopy({'stations': stations, 'jobs': jobs,
                                  'inspections': list(self.inspections.values())})

    def station(self, station_id):
        with self.lock:
            for item in self.stations:
                if item['id'] == station_id:
                    return copy.deepcopy(item)
        raise ValueError('工作站不存在')

    def save_station(self, payload):
        with self.lock:
            existing = self.station(payload['id']) if payload.get('id') else {}
            active = any(j['stationId'] == existing.get('id') and j['state'] in ACTIVE for j in self.jobs)
            result = {'id': existing.get('id', uuid.uuid4().hex)}
            for name, default in [('name', ''), ('host', ''), ('username', ''), ('keyFile', ''),
                                  ('abaqus', '/usr/bin/abaqus'), ('remoteRoot', '~/abaqus-manager-runs')]:
                value = payload.get(name, existing.get(name, default))
                if not isinstance(value, str) or any(ord(c) < 32 for c in value):
                    raise ValueError('工作站字段格式无效：' + name)
                result[name] = value.strip()
            if not re.fullmatch(r'[A-Za-z0-9_.:\-]+', result['host']) or result['host'].startswith('-'):
                raise ValueError('请输入有效 IP 或主机名')
            if not re.fullmatch(r'[A-Za-z0-9_.\-]+', result['username']) or not result['name']:
                raise ValueError('工作站名称和 SSH 用户名不能为空且格式须正确')
            result['port'] = int(payload.get('port', existing.get('port', 22)))
            if not 1 <= result['port'] <= 65535:
                raise ValueError('SSH 端口应为 1–65535')
            if not result['abaqus'].startswith('/') or not result['remoteRoot'].startswith(('/', '~/')):
                raise ValueError('Abaqus 命令请填绝对路径；运行目录请用绝对路径或 ~/ 开头')
            identity = ('host', 'port', 'username')
            if active and any(result[k] != existing.get(k) for k in (*identity, 'remoteRoot', 'abaqus')):
                raise ValueError('工作站有未完成任务，暂不能改变地址或运行配置')
            same = all(result[k] == existing.get(k) for k in identity)
            result['fingerprint'] = existing.get('fingerprint', '') if same else ''
            result['secret'] = existing.get('secret', '') if same else ''
            if payload.get('password'):
                result['secret'] = protect(payload['password'])
            if payload.get('clearPassword'):
                result['secret'] = ''
            self.stations = [s for s in self.stations if s['id'] != result['id']] + [result]
            self._save()
            return {'id': result['id']}

    def remove_station(self, station_id):
        with self.lock:
            if any(j['stationId'] == station_id and j['state'] in ACTIVE for j in self.jobs):
                raise ValueError('工作站有未完成任务，不能移除')
            self.stations = [s for s in self.stations if s['id'] != station_id]
            self._save()
        return {'ok': True}

    def test_station(self, station_id, trusted='', resources=False):
        station = self.station(station_id)
        client = self.connector(station, trusted)
        try:
            system = execute(client, 'uname -s').strip()
            if system != 'Linux':
                raise ValueError('当前远程求解模块支持 Linux 工作站')
            execute(client, 'command -v python3 >/dev/null && test -x ' + shlex.quote(station['abaqus']))
            result = {'ok': True, 'system': system, 'message': 'SSH 登录成功，Python 3 与 Abaqus 命令可用',
                      'checkedAt': now()}
            if resources:
                result['resources'] = json.loads(execute(client, 'python3 -c ' + shlex.quote(RESOURCE_SCRIPT)))
            with self.lock:
                current = next(s for s in self.stations if s['id'] == station_id)
                if not current.get('fingerprint') and trusted:
                    current['fingerprint'] = trusted
                current['lastCheck'] = result
                self._save()
            return result
        finally:
            client.close()

    def inspect_cae(self, model, abaqus):
        if not abaqus:
            raise ValueError('请先在设置中选择本机 Abaqus 启动命令')
        token = uuid.uuid4().hex
        record = {'id': token, 'modelId': model['id'], 'state': 'reading'}
        with self.lock:
            self.inspections[token] = record
        def work():
            try:
                result = cae_operation(model['path'], abaqus, self.directory / 'staging' / token)
                with self.lock:
                    record.update(state='completed', jobs=result['jobs'])
            except Exception as error:
                with self.lock:
                    record.update(state='failed', error=str(error))
        self.pool.submit(work)
        return {'inspectionId': token}

    def submit(self, model, payload, abaqus=''):
        station = self.station(payload.get('stationId'))
        if not station.get('fingerprint'):
            raise ValueError('请先测试工作站连接并确认 SSH 指纹')
        cae = Path(model['path']).resolve()
        if not cae.is_file():
            raise ValueError('CAE 文件不存在')
        source = payload.get('source', 'inp')
        if source not in {'inp', 'cae'}:
            raise ValueError('未知提交来源')
        cpus = int(payload.get('cpus', 1))
        if not 1 <= cpus <= 4096:
            raise ValueError('CPU 数必须在 1–4096 之间')
        job_name = payload.get('jobName', '')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}', job_name):
            raise ValueError('远程 Job 名请使用英文字母开头，后接字母、数字或下划线（最多80字符）')
        extras = payload.get('extraFiles', [])
        if not isinstance(extras, list) or not all(isinstance(x, str) for x in extras):
            raise ValueError('附加文件列表无效')
        user_file = payload.get('userFile', '')
        if user_file:
            if user_file not in extras:
                extras.append(user_file)
        files = {}
        if source == 'inp':
            inp = (cae.parent / payload.get('input', '')).resolve()
            if inp.suffix.lower() != '.inp':
                raise ValueError('请选择 INP 求解输入文件')
            files = input_bundle(inp, cae.parent, extras)
            input_name = inp.relative_to(cae.parent).as_posix()
        else:
            if not abaqus or not payload.get('caeJob'):
                raise ValueError('请先读取并选择 CAE 中的 Job，需要本机 Abaqus/CAE')
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_\- ]{0,79}', payload['caeJob']):
                raise ValueError('CAE Job 名含不支持的路径字符，请使用已有 INP 模式')
            input_name = ''
        job = {'id': uuid.uuid4().hex, 'stationId': station['id'], 'stationName': station['name'],
               'stationHost': station['host'], 'modelId': model['id'], 'cae': str(cae),
               'outputDir': str(cae.parent), 'jobName': job_name, 'cpus': cpus,
               'source': source, 'caeJob': payload.get('caeJob', ''), 'abaqusLocal': abaqus,
               'input': input_name, 'files': {k: str(v) for k, v in files.items()},
               'extraFiles': extras, 'userFile': user_file, 'state': 'preparing',
               'createdAt': now(), 'downloads': [], 'lastError': ''}
        with self.lock:
            self.jobs.append(job)
            self._save()
        self.schedule(job)
        return {'jobId': job['id'], 'state': job['state']}

    def _update(self, job, **fields):
        with self.lock:
            job.update(fields, updatedAt=now())
            self._save()

    def schedule(self, job):
        with self.lock:
            if job['id'] in self.inflight or job['state'] not in ACTIVE:
                return
            self.inflight.add(job['id'])
        self.pool.submit(self._step, job)

    def _loop(self):
        while not self.stopped.wait(8):
            with self.lock:
                jobs = list(self.jobs)
            for job in jobs:
                self.schedule(job)

    def _prepare(self, job):
        # Interrupted CAE preparation is not silently repeated (license/resource use).
        stage = self.directory / 'staging' / job['id']
        if job['source'] == 'cae':
            if stage.exists():
                raise ValueError('CAE 导出曾中断，请检查导出日志后重新提交')
            result = cae_operation(job['cae'], job['abaqusLocal'], stage, job['caeJob'])
            inp = Path(result['input']).resolve()
            if not inp.is_relative_to(stage.resolve()) or not inp.is_file():
                raise ValueError('CAE 返回的输入文件无效')
            # Relative external references remain relative to the original CAE folder.
            content = inp.read_bytes().decode('utf-8', errors='replace')
            for line in content.splitlines():
                if line.lstrip().startswith('*') and not line.lstrip().startswith('**') and re.search(r',\s*(INPUT|FILE)\s*=', line, re.I):
                    raise ValueError('导出的 Job 引用外部文件，请使用已有 INP 模式并附带依赖文件提交')
            files = {inp.name: str(inp)}
            for name in job['extraFiles']:
                for rel, path in input_bundle(Path(job['cae']).parent / name, Path(job['cae']).parent).items():
                    files[rel] = str(path)
            self._update(job, input=inp.name, files=files)
        self._update(job, state='uploading')

    def _step(self, job):
        client = None
        sftp = None
        try:
            if job['state'] == 'preparing':
                try:
                    self._prepare(job)
                except Exception as error:
                    self._update(job, state='failed', lastError=str(error))
                    return
            station = self.station(job['stationId'])
            client = self.connector(station)
            sftp = client.open_sftp()
            if job['state'] == 'uploading':
                home = sftp.normalize('.')
                base = station['remoteRoot']
                if base.startswith('~/'):
                    base = posixpath.join(home, base[2:])
                directory = posixpath.join(base, job['id'])
                execute(client, 'mkdir -p -m 700 -- ' + shlex.quote(directory))
                for relative, local in job['files'].items():
                    # Revalidate persisted state rather than trusting paths in JSON.
                    if relative.startswith('/') or '..' in relative.split('/') or '\\' in relative:
                        raise ValueError('无效上传相对路径')
                    if relative in {'runner.py', 'run.json', 'status.json', 'started.lock', 'solver.log', 'runner.log'}:
                        raise ValueError('附加文件名与远端任务控制文件冲突：' + relative)
                    remote = posixpath.join(directory, 'inputs', relative)
                    execute(client, 'mkdir -p -- ' + shlex.quote(posixpath.dirname(remote)))
                    sftp.put(local, remote, confirm=True)
                config = {'abaqus': station['abaqus'], 'jobName': job['jobName'], 'cpus': job['cpus'],
                          'input': 'inputs/' + job['input'],
                          'userFile': 'inputs/' + job['userFile'] if job['userFile'] else ''}
                # INP relative references are resolved from its working directory.
                # Run in inputs/, with generated result files there as well.
                sftp.put(str(ROOT / 'remote_runner.py'), directory + '/inputs/runner.py', confirm=True)
                config['input'] = job['input']
                config['userFile'] = job['userFile']
                self._put_json(sftp, directory + '/inputs/run.json', config)
                self._update(job, state='launching', remoteDir=directory + '/inputs', lastError='')
                # Persist launch state before command: no automatic retry on ambiguous delivery.
                execute(client, 'cd ' + shlex.quote(directory + '/inputs') +
                        ' && nohup python3 runner.py >runner.log 2>&1 </dev/null &')
            self._poll(job, sftp)
        except Exception as error:
            self._update(job, lastError=str(error))
        finally:
            if sftp is not None:
                sftp.close()
            if client is not None:
                client.close()
            with self.lock:
                self.inflight.discard(job['id'])

    @staticmethod
    def _put_json(sftp, path, data):
        content = json.dumps(data).encode('utf-8')
        sftp.putfo(io.BytesIO(content), path, file_size=len(content), confirm=True)

    def _poll(self, job, sftp):
        directory = job['remoteDir']
        try:
            with sftp.open(directory + '/status.json', 'rb') as stream:
                status = json.loads(stream.read(1024 * 1024))
        except FileNotFoundError:
            self._update(job, lastError='等待远端启动回执；不会自动重复提交。若长期无回执，请检查远端 runner.log。')
            return
        if status['state'] == 'running':
            self._update(job, state='running', lastError='')
        elif status['state'] == 'failed':
            self._update(job, state='failed', lastError=status.get('error', '远程求解失败'))
        elif status['state'] == 'completed':
            self._update(job, state='downloading', lastError='')
            names = status.get('odbs', [])
            if not names:
                raise ValueError('远程任务未返回 ODB 文件')
            for name in names:
                self._download(job, sftp, name)
            self._update(job, state='completed', lastError='', finishedAt=now())

    def _download(self, job, sftp, name):
        if not isinstance(name, str) or '/' in name or '\\' in name or ':' in name or not name.lower().endswith('.odb'):
            raise ValueError('远端返回了无效 ODB 文件名')
        if any(d['remote'] == name for d in job['downloads']):
            return
        remote = job['remoteDir'] + '/' + name
        info = sftp.lstat(remote)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            raise ValueError('远端 ODB 不是完整的普通文件')
        folder = Path(job['outputDir'])
        if not folder.is_dir():
            raise ValueError('本地 CAE 文件夹不可用，结果保留在远程，稍后重试下载')
        temporary = folder / ('.' + name + '.' + job['id'] + '.part')
        try:
            sftp.get(remote, str(temporary))
            after = sftp.lstat(remote)
            if temporary.stat().st_size != info.st_size or (after.st_size, after.st_mtime) != (info.st_size, info.st_mtime):
                raise ValueError('ODB 传输期间文件发生改变，稍后重试')
            candidate = folder / name
            count = 0
            while True:
                try:
                    # Hard-link creation is atomic and cannot replace an existing ODB.
                    if os.name == 'nt':
                        # Windows rename refuses replacement, including on FAT/network drives.
                        os.rename(temporary, candidate)
                    else:
                        os.link(temporary, candidate)
                    break
                except FileExistsError:
                    count += 1
                    candidate = folder / (Path(name).stem + '__remote_' + job['id'][:8] +
                                          ('_' + str(count) if count > 1 else '') + '.odb')
            with self.lock:
                job['downloads'].append({'remote': name, 'local': str(candidate), 'bytes': info.st_size})
                self._save()
        finally:
            temporary.unlink(missing_ok=True)

    def refresh(self):
        with self.lock:
            jobs = list(self.jobs)
        for job in jobs:
            self.schedule(job)
        return self.snapshot()
