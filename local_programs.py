"""User-selected local CMD/BAT launchers; configuration stays on this computer."""
import json
import os
import subprocess
import threading
import urllib.parse
import urllib.request
import urllib.error
import uuid
from datetime import datetime
from pathlib import Path
from program_processes import launch_command, process_snapshot, owned_tree, terminate_verified


def local_service_url(value):
    if not isinstance(value, str):
        raise ValueError('服务地址必须是文本')
    value = value.strip()
    if not value:
        return ''
    url = urllib.parse.urlsplit(value)
    if (url.scheme not in {'http', 'https'} or url.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or url.username or url.password or url.fragment or any(ord(c) < 32 for c in value)):
        raise ValueError('服务地址仅支持本机 http / https 地址，如 http://127.0.0.1:8890/')
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError('服务端口无效')
    return value


def service_ready(url):
    if not url:
        return False
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(local_service_url(url), timeout=1) as response:
            if response.status != 200:
                return False
            if 'application/json' in response.headers.get('Content-Type', ''):
                body = json.loads(response.read(65536))
                if isinstance(body, dict) and body.get('status') in {'error', 'failed', 'unhealthy'}:
                    return False
            return True
    except urllib.error.HTTPError as error:
        error.close()
        return False
    except Exception:
        return False


class ProgramManager:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = threading.RLock()
        self.processes = {}
        self.operation_lock = threading.RLock()

    def _read(self):
        path = self.directory / 'programs.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []

    def _write(self, entries):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / 'programs.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)

    @staticmethod
    def validate(raw):
        if not isinstance(raw, str) or any(c in raw for c in '%"\r\n\x00'):
            raise ValueError('脚本路径格式不正确（不能包含 % 或双引号）')
        path = Path(raw)
        if not path.is_absolute() or path.suffix.lower() not in {'.cmd', '.bat'}:
            raise ValueError('请选择 CMD 或 BAT 脚本的完整路径')
        path = path.resolve()
        if '%' in str(path):
            raise ValueError('脚本实际路径不能包含 %')
        if not path.is_file():
            raise ValueError('脚本文件不存在，请重新选择')
        return path

    def list(self):
        with self.lock:
            entries = self._read()
            for entry in entries:
                process = self.processes.get(entry['id'])
                entry['running'] = bool(process and process.poll() is None)
                entry['exitCode'] = process.poll() if process else None
                entry['exists'] = Path(entry['path']).is_file()
        # Probe outside the configuration lock; never launch or stop a service here.
        for entry in entries:
            endpoint = entry.get('healthUrl') or entry.get('webUrl')
            entry['serviceReady'] = service_ready(endpoint)
            entry['serviceConfigured'] = bool(endpoint)
        return entries

    def save(self, payload):
        path = self.validate(payload.get('path'))
        name = str(payload.get('name', '')).strip()
        if not name or len(name) > 100:
            raise ValueError('请输入程序名称（最多100字）')
        with self.operation_lock, self.lock:
            entries = self._read()
            entry = next((e for e in entries if e['id'] == payload.get('id')), None)
            if entry and entry['path'] != str(path) and self.processes.get(entry['id']) and self.processes[entry['id']].poll() is None:
                raise ValueError('程序正在运行，暂不能修改启动入口')
            if any(e['path'].casefold() == str(path).casefold() and e is not entry for e in entries):
                raise ValueError('这个脚本已经添加过了')
            if entry is None:
                if payload.get('id'):
                    raise ValueError('程序入口不存在')
                entry = {'id': uuid.uuid4().hex}
                entries.append(entry)
            web_url = local_service_url(payload.get('webUrl', entry.get('webUrl', '')))
            health_url = local_service_url(payload.get('healthUrl', entry.get('healthUrl', '')))
            entry.update(name=name, path=str(path), webUrl=web_url, healthUrl=health_url)
            self._write(entries)
            return {'id': entry['id']}

    def remove(self, identifier):
        with self.operation_lock, self.lock:
            self._write([entry for entry in self._read() if entry['id'] != identifier])
        return {'ok': True}

    def run(self, identifier):
        with self.operation_lock, self.lock:
            entries = self._read()
            entry = next((e for e in entries if e['id'] == identifier), None)
            if not entry:
                raise ValueError('程序入口不存在')
            if service_ready(entry.get('healthUrl') or entry.get('webUrl')):
                return {'alreadyRunning': True, 'serviceReady': True, 'message': '服务已就绪，无需重复启动'}
            existing = self.processes.get(identifier)
            if existing and existing.poll() is None:
                return {'alreadyRunning': True, 'message': '这个程序已经在运行'}
            if os.name != 'nt':
                raise ValueError('CMD 程序启动仅支持 Windows')
            path = self.validate(entry['path'])
            logs = self.directory / 'logs'; logs.mkdir(parents=True, exist_ok=True)
            log = logs / (identifier + '-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.log')
            # A raw Windows command line preserves CMD's required outer quotes.
            # No arbitrary command arguments are accepted from the browser.
            command = launch_command(path)
            with log.open('wb') as stream:
                process = subprocess.Popen(command, cwd=str(path.parent), stdin=subprocess.DEVNULL,
                                           stdout=stream, stderr=subprocess.STDOUT,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
            self.processes[identifier] = process
            entry.pop('lastStopped', None)
            entry.update(lastStarted=datetime.now().astimezone().isoformat(timespec='seconds'), log=str(log))
            self._write(entries)
            return {'message': '已发出启动请求', 'pid': process.pid, 'log': str(log)}

    def stop(self, identifier, confirmed=False):
        if confirmed is not True:
            raise ValueError('请先确认停止服务；正在进行的任务会中断')
        if os.name != 'nt':
            raise ValueError('停止 CMD 程序仅支持 Windows')
        with self.operation_lock:
            with self.lock:
                entry = next((e for e in self._read() if e['id'] == identifier), None)
            if not entry:
                raise ValueError('程序入口不存在')
            tree = owned_tree(Path(entry['path']), process_snapshot())
            terminate_verified(tree)
            endpoint = entry.get('healthUrl') or entry.get('webUrl')
            if service_ready(endpoint):
                raise ValueError('已结束识别出的启动进程，但服务地址仍有响应；未重启，请核查是否存在其他实例')
            with self.lock:
                entries = self._read()
                saved = next(e for e in entries if e['id'] == identifier)
                saved['lastStopped'] = datetime.now().astimezone().isoformat(timespec='seconds')
                self._write(entries)
            return {'message': '服务已停止', 'stopped': True}

    def restart(self, identifier, confirmed=False):
        with self.operation_lock:
            self.stop(identifier, confirmed)
            result = self.run(identifier)
            result['message'] = '已停止原进程并重新启动，正在等待服务就绪'
            return result

    def log_tail(self, identifier):
        with self.lock:
            entry = next((e for e in self._read() if e['id'] == identifier), None)
        if not entry:
            raise ValueError('程序入口不存在')
        if not entry.get('log'):
            return {'text': '尚未生成启动日志'}
        path = Path(entry['log']).resolve()
        if not path.is_relative_to((self.directory / 'logs').resolve()):
            raise ValueError('日志路径不在程序日志目录中')
        if not path.is_file():
            return {'text': '日志文件不存在'}
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - 32768))
            content = stream.read(32768)
        try:
            text = content.decode('utf-8')
        except UnicodeDecodeError:
            text = content.decode('gb18030', errors='replace')
        return {'text': text or '日志暂时为空', 'path': str(path)}
