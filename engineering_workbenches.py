"""Local Fluent/PyroSim catalogs and GUI launchers, isolated from the Abaqus DB."""
import json
import os
import sqlite3
import subprocess
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

FORMATS = {'fluent': ('.cas.h5', '.cas.gz', '.cas'), 'pyrosim': ('.psm', '.fds')}
RESULTS = {'fluent': ('.dat.h5', '.dat.gz', '.dat'), 'pyrosim': ('.smv',)}
STATUSES = {'planned', 'in_progress', 'validated', 'archived'}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def kind_check(kind):
    if kind not in FORMATS:
        raise ValueError('未知工作台')


def matches(path, suffixes):
    return str(path).lower().endswith(suffixes)


def discover_software():
    roots = {Path(os.environ.get('ProgramFiles', 'C:/Program Files')), Path('D:/Program Files')}
    fluent = [Path(v)/'fluent/ntbin/win64/fluent.exe' for k,v in os.environ.items() if k.startswith('AWP_ROOT')]
    pyro = []
    for root in roots:
        if root.is_dir():
            fluent.extend(sorted(root.glob('ANSYS Inc/v*/fluent/ntbin/win64/fluent.exe'), reverse=True))
            pyro.extend(sorted(root.glob('PyroSim */PyroSim.exe'), reverse=True))
    fluent_path = next((str(p) for p in fluent if p.is_file()), '')
    pyro_path = next((p for p in pyro if p.is_file()), None)
    result_path = pyro_path.with_name('PyroSimResults.exe') if pyro_path else None
    return {'fluent': fluent_path, 'pyrosim': str(pyro_path) if pyro_path else '',
            'pyrosimResults': str(result_path) if result_path and result_path.is_file() else ''}


class EngineeringWorkbenches:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = threading.RLock()

    @contextmanager
    def db(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.directory/'catalog.db', timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('''CREATE TABLE IF NOT EXISTS models (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, path TEXT NOT NULL COLLATE NOCASE,
                title TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'planned', favorite INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(kind,path))''')
            yield connection
            connection.commit()
        except Exception:
            connection.rollback(); raise
        finally:
            connection.close()

    def config(self):
        detected = discover_software()
        config_path = self.directory/'settings.json'
        if config_path.is_file():
            saved = json.loads(config_path.read_text(encoding='utf-8'))
            detected.update({k:v for k,v in saved.items() if v})
        return {k:{'path':v, 'found':bool(v and Path(v).is_file())} for k,v in detected.items()}

    def save_config(self, payload):
        clean = {}
        expected = {'fluent':'fluent.exe', 'pyrosim':'pyrosim.exe', 'pyrosimResults':'pyrosimresults.exe'}
        for key, value in payload.items():
            if key not in expected:
                raise ValueError('未知软件配置')
            if not isinstance(value, str):
                raise ValueError('软件路径格式错误')
            if value:
                path = Path(value).resolve()
                if not path.is_file() or path.name.lower() != expected[key]:
                    raise ValueError('请选择 ' + expected[key])
                value = str(path)
            clean[key] = value
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            target = self.directory/'settings.json'
            saved = json.loads(target.read_text(encoding='utf-8')) if target.exists() else {}
            saved.update(clean)
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(target)
        return self.config()

    @staticmethod
    def validate_model(kind, path):
        kind_check(kind)
        if not isinstance(path, str) or any(ord(c)<32 for c in path):
            raise ValueError('模型路径格式错误')
        target = Path(path).resolve()
        if not target.is_file() or not matches(target.name, FORMATS[kind]):
            raise ValueError('请选择存在的模型文件：' + ' / '.join(FORMATS[kind]))
        return target

    def add(self, kind, raw_path):
        path = self.validate_model(kind, raw_path)
        with self.db() as db:
            existing = db.execute('SELECT id FROM models WHERE kind=? AND path=?',(kind,str(path))).fetchone()
            if existing:
                return {'id':existing['id'], 'created':False}
            identifier = uuid.uuid4().hex
            suffix = next(s for s in FORMATS[kind] if path.name.lower().endswith(s))
            db.execute('INSERT INTO models (id,kind,path,title,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                       (identifier,kind,str(path),path.name[:-len(suffix)],now(),now()))
            return {'id':identifier,'created':True}

    def scan(self, kind, folder):
        kind_check(kind)
        root = Path(folder).resolve()
        if not root.is_dir():
            raise ValueError('扫描文件夹不存在')
        added = existing = 0
        errors = []
        def scan_error(error):
            if len(errors)<10: errors.append(str(error))
        for parent, folders, files in os.walk(root, followlinks=False, onerror=scan_error):
            folders[:] = [n for n in folders if not n.startswith('.') and n not in {'node_modules','__pycache__'}]
            for name in files:
                if matches(name, FORMATS[kind]):
                    try:
                        result = self.add(kind, str(Path(parent)/name))
                        if result['created']: added += 1
                        else: existing += 1
                    except (OSError,ValueError) as error: scan_error(error)
        return {'added':added,'existing':existing,'errors':errors}

    def get(self, kind, identifier):
        kind_check(kind)
        with self.db() as db:
            row = db.execute('SELECT * FROM models WHERE id=? AND kind=?',(identifier,kind)).fetchone()
        if not row: raise ValueError('当前工作台中没有这条记录')
        return dict(row)

    def list(self, kind):
        kind_check(kind)
        with self.db() as db:
            rows = db.execute('SELECT * FROM models WHERE kind=? ORDER BY updated_at DESC,title',(kind,)).fetchall()
        models = []
        for row in rows:
            model = dict(row)
            try:
                path = Path(model['path']); info = path.stat()
                import stat
                model.update(exists=stat.S_ISREG(info.st_mode), size=info.st_size,
                             modifiedAt=datetime.fromtimestamp(info.st_mtime).astimezone().isoformat())
            except OSError:
                model.update(exists=False,size=None,modifiedAt=None)
            model['favorite'] = bool(model['favorite'])
            models.append(model)
        return models

    def update(self, kind, identifier, payload):
        model = self.get(kind,identifier)
        for key in ('title','notes','tags','status'):
            if key in payload:
                if not isinstance(payload[key],str): raise ValueError('字段应为文本')
                model[key] = payload[key].strip() if key != 'notes' else payload[key]
        if not model['title'] or model['status'] not in STATUSES:
            raise ValueError('名称不能为空，状态必须有效')
        if 'favorite' in payload:
            if not isinstance(payload['favorite'],bool): raise ValueError('收藏状态应为布尔值')
            model['favorite'] = int(payload['favorite'])
        with self.db() as db:
            db.execute('UPDATE models SET title=?,notes=?,tags=?,status=?,favorite=?,updated_at=? WHERE id=? AND kind=?',
                       (model['title'],model['notes'],model['tags'],model['status'],model['favorite'],now(),identifier,kind))
        return self.get(kind,identifier)

    def remove(self, kind, identifier):
        self.get(kind,identifier)
        with self.db() as db:
            db.execute('DELETE FROM models WHERE id=? AND kind=?',(identifier,kind))
        return {'ok':True}

    def remove_many(self, kind, identifiers):
        """Remove only this workbench's index records, in one transaction."""
        kind_check(kind)
        if not isinstance(identifiers, list) or not identifiers:
            raise ValueError('请选择要移除的模型索引')
        if any(not isinstance(value, str) or not value.strip() for value in identifiers):
            raise ValueError('模型索引 ID 必须是非空字符串')
        identifiers = list(dict.fromkeys(identifiers))
        with self.db() as db:
            before = db.total_changes
            db.executemany('DELETE FROM models WHERE id=? AND kind=?',
                           [(identifier, kind) for identifier in identifiers])
            deleted = db.total_changes - before
        return {'deleted': deleted}

    def results(self, kind, identifier):
        model = self.get(kind,identifier)
        path = Path(model['path'])
        folders = [path.parent]
        # PyroSim's default result folder is beside the PSM with the same stem.
        if kind == 'pyrosim': folders.append(path.parent/path.stem)
        found = []
        for folder in folders:
            if folder.is_dir():
                for item in sorted(folder.iterdir()):
                    if item.is_file() and matches(item.name, RESULTS[kind]):
                        found.append({'name':item.relative_to(path.parent).as_posix(),'path':str(item.resolve())})
        return found

    def launch(self, kind, identifier, payload):
        model = self.get(kind,identifier)
        path = self.validate_model(kind,model['path'])
        result = payload.get('result', '')
        if result:
            candidates = {item['path'] for item in self.results(kind,identifier)}
            if result not in candidates: raise ValueError('请选择此模型文件夹中的结果文件')
        settings = self.config()
        key = 'pyrosimResults' if kind == 'pyrosim' and result else kind
        if not settings[key]['found']:
            raise ValueError('未找到软件，请先在本工作台的“软件设置”中选择启动程序')
        executable = settings[key]['path']
        if kind == 'fluent':
            mode = payload.get('mode','3ddp')
            if mode not in {'2d','3d','2ddp','3ddp'}: raise ValueError('Fluent 维度/精度选项无效')
            cpus = int(payload.get('cpus',1))
            if not 1 <= cpus <= 128: raise ValueError('启动 CPU 数应为 1–128')
            directory = self.directory/'launches'/uuid.uuid4().hex
            directory.mkdir(parents=True)
            journal = directory/'open.jou'
            # These generated journals only read files; never iterate, initialize, or write results.
            text = '/file/read-case ' + json.dumps(path.as_posix(),ensure_ascii=False) + '\n'
            if result:
                text += '/file/read-data ' + json.dumps(Path(result).as_posix(),ensure_ascii=False) + '\n'
            journal.write_text(text,encoding='utf-8')
            command = [executable,mode,'-t'+str(cpus),'-i',str(journal.resolve())]
        else:
            # FDS is imported through PyroSim's Import FDS/CAD action, not the
            # PSM opener. Do not invent an unsupported command-line switch.
            command = [executable] if path.suffix.lower() == '.fds' and not result else [executable,result or str(path)]
        process = subprocess.Popen(command,cwd=str(path.parent),
                                   creationflags=0x08000000 if os.name == 'nt' else 0)
        message = '已启动软件读取文件，请在软件窗口中查看；不会自动开始求解'
        if kind == 'pyrosim' and path.suffix.lower() == '.fds' and not result:
            from fds_compat import IMPORT_HELP
            message = '已启动 PyroSim。' + IMPORT_HELP
        return {'started':True,'pid':process.pid,'message':message}
