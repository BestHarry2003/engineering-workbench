from __future__ import annotations

import argparse
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import stat as stat_module
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
import zipfile
from remote_jobs import RemoteManager, HostKeyRequired
from local_programs import ProgramManager
from engineering_workbenches import EngineeringWorkbenches
import fds_compat
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
STATE_ROOT = Path(os.environ.get("WORKBENCH_HOME", ROOT))
DATA_DIR = STATE_ROOT / "data"
DB_PATH = Path(os.environ.get("ABAQUS_MANAGER_DB", DATA_DIR / "models.db"))
SETTINGS_PATH = DATA_DIR / "settings.json"
REMOTE_DIR = Path(os.environ.get("ABAQUS_MANAGER_REMOTE_DIR", DB_PATH.parent / "remote"))
PROGRAMS = ProgramManager(DB_PATH.parent / "programs")
ENGINEERING = EngineeringWorkbenches(DB_PATH.parent / "engineering")
EXPORT_DIR = STATE_ROOT / "exports"
BACKUP_DIR = Path(os.environ.get("ABAQUS_MANAGER_BACKUP_DIR", STATE_ROOT / "backups"))
LAUNCHER = ROOT / "Open-Abaqus-Project.ps1"
RESULTS_LAUNCHER = ROOT / "Open-Abaqus-Results.ps1"
RESULTS_TEMPLATE = ROOT / "abaqus_results_viewer.py"
RESULTS_RUNTIME = STATE_ROOT / ".sites-runtime" / "odb-viewer"
DEFAULT_PORT = 8765
RUNTIME_PORT = DEFAULT_PORT
REFRESH_FILE_STATE_CACHE_TTL = 5.0
SAFE_CAE_NAME = re.compile(r"^[A-Za-z0-9_]+\.cae$", re.IGNORECASE)
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
VALID_STATUSES = {"planned", "in_progress", "validated", "archived"}
DEFAULT_SETTINGS = {"abaqusCommand": ""}
_refresh_file_state_caches: dict[str, tuple[float, dict[str, tuple[bool, int | None, str | None]]]] = {}
_refresh_file_state_cache_lock = threading.Lock()
_remote_manager = None
_remote_manager_lock = threading.Lock()


def remote_manager():
    global _remote_manager
    with _remote_manager_lock:
        if _remote_manager is None:
            _remote_manager = RemoteManager(REMOTE_DIR, start=True)
        return _remote_manager


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def is_loopback_address(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


def create_database_backup() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = None
    temporary = None
    source = None
    destination = None
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        target = BACKUP_DIR / f"models.db.{timestamp}.bak"
        suffix = 1
        while target.exists():
            target = BACKUP_DIR / f"models.db.{timestamp}-{suffix}.bak"
            suffix += 1
        temporary = target.with_name(target.name + ".tmp")
        source = connect_db()
        destination = sqlite3.connect(temporary)
        source.backup(destination)
        destination.commit()
        integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise sqlite3.DatabaseError(f"备份完整性检查失败：{integrity}")
        destination.close()
        destination = None
        source.close()
        source = None
        temporary.replace(target)
        return target
    except Exception as exc:
        raise RuntimeError(f"SQLite 备份失败，已停止当前操作：{exc}") from exc
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
        if temporary is not None and temporary.exists():
            temporary.unlink()


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS models (
                id INTEGER PRIMARY KEY,
                cae_path TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'planned',
                favorite INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_models_status ON models(status);
            CREATE INDEX IF NOT EXISTS idx_models_favorite ON models(favorite);

            CREATE TABLE IF NOT EXISTS scan_roots (
                id INTEGER PRIMARY KEY,
                root_path TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                last_scanned_at TEXT NOT NULL
            );
            """
        )
        db.execute("PRAGMA optimize")


def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.is_file():
        try:
            loaded = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                settings.update(loaded)
        except (OSError, json.JSONDecodeError):
            pass
    return settings


def save_settings(settings: dict) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    clean = {"abaqusCommand": str(settings.get("abaqusCommand", "")).strip()}
    temporary = SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(SETTINGS_PATH)
    return clean


def detect_abaqus() -> dict:
    configured = str(load_settings().get("abaqusCommand", "")).strip()
    candidates = [
        configured,
        os.environ.get("ABAQUS_COMMAND", ""),
        shutil.which("abaqus.bat") or "",
        shutil.which("abaqus") or "",
        r"C:\SIMULIA\Commands\abaqus.bat",
        r"E:\APP\ABAQUS2022\SIMULIA\Commands\abaqus.bat",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return {
                "found": True,
                "path": str(Path(candidate).resolve()),
                "configured": bool(configured and os.path.normcase(candidate) == os.path.normcase(configured)),
            }
    return {"found": False, "path": configured, "configured": bool(configured)}


def open_model_results(model_id: int) -> dict:
    """Open the selected CAE's sibling ODBs in one read-only Viewer session."""
    model = get_model(model_id)
    cae_path = Path(model["path"])
    if not cae_path.is_file():
        raise FileNotFoundError("CAE 文件已移动或删除，请先更新模型索引")
    try:
        odb_paths = sorted(
            (entry for entry in cae_path.parent.iterdir()
             if entry.suffix.lower() == ".odb" and entry.is_file()),
            key=lambda entry: (entry.name.casefold(), entry.name),
        )
    except OSError as exc:
        raise ValueError(f"无法读取 CAE 所在文件夹：{exc}") from exc
    if not odb_paths:
        raise ValueError("CAE 所在文件夹没有 ODB 结果文件（不搜索子文件夹）")
    abaqus = detect_abaqus()
    if not abaqus["found"]:
        raise ValueError("未找到 Abaqus，请先在“设置与迁移”中选择 abaqus.bat")
    template = RESULTS_TEMPLATE.read_text(encoding="utf-8")
    marker = "ODB_PATHS = []  # populated by the model manager"
    if template.count(marker) != 1 or not RESULTS_LAUNCHER.is_file():
        raise RuntimeError("后处理启动文件不完整，请重新解压完整程序")
    # Serialize paths as data; never interpolate file names as Python code.
    paths_json = json.dumps([str(path.resolve()) for path in odb_paths], ensure_ascii=True)
    script = template.replace(marker, f"ODB_PATHS = json.loads({paths_json!r})")
    RESULTS_RUNTIME.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="session-", dir=RESULTS_RUNTIME))
    script_path = run_dir / "open_results.py"
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(RESULTS_LAUNCHER), "-AbaqusCommand", abaqus["path"],
         "-ScriptFile", str(script_path)],
        cwd=str(run_dir), capture_output=True, text=True, encoding="utf-8",
        errors="replace", creationflags=CREATE_NO_WINDOW, timeout=20, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("无法启动 Abaqus Viewer：" + (completed.stderr.strip() or "启动器退出"))
    # Starting a process does not prove that licensing or ODB loading succeeded.
    return {"status": "starting", "odbCount": len(odb_paths), "folder": str(cae_path.parent),
            "reportPath": str(run_dir / "odb-load-report.json")}


def system_info() -> dict:
    abaqus = detect_abaqus()
    return {
        "python": {
            "found": True,
            "version": ".".join(str(part) for part in sys.version_info[:3]),
            "path": sys.executable,
            "launcherFound": bool(shutil.which("py.exe") or shutil.which("python.exe")),
        },
        "abaqus": abaqus,
        "database": str(DB_PATH),
        "platformRoot": str(ROOT),
        "port": RUNTIME_PORT,
    }


def normalize_cae_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser().resolve()
    if path.suffix.lower() != ".cae":
        raise ValueError("请选择 .cae 模型文件")
    return path


def normalize_import_timestamp(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    timestamp = value.strip()
    if not timestamp:
        return fallback
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    return timestamp


def upsert_model(path: Path) -> tuple[int, bool]:
    now = utc_now()
    path_text = str(path)
    with connect_db() as db:
        row = db.execute("SELECT id FROM models WHERE cae_path = ?", (path_text,)).fetchone()
        if row:
            db.execute(
                "UPDATE models SET last_seen_at = ?, updated_at = updated_at WHERE id = ?",
                (now, row["id"]),
            )
            return int(row["id"]), False
        cursor = db.execute(
            """
            INSERT INTO models(cae_path, title, created_at, updated_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (path_text, path.stem, now, now, now),
        )
        return int(cursor.lastrowid), True


def scan_folder(root_path: str) -> dict:
    root = Path(root_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("扫描文件夹不存在")
    found = 0
    added = 0
    skipped = 0
    for current_root, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if not name.startswith(".")]
        for filename in files:
            if not filename.lower().endswith(".cae"):
                continue
            found += 1
            candidate = Path(current_root) / filename
            try:
                _, created = upsert_model(candidate.resolve())
                added += int(created)
            except (OSError, sqlite3.Error):
                skipped += 1
    now = utc_now()
    with connect_db() as db:
        db.execute(
            """
            INSERT INTO scan_roots(root_path, created_at, last_scanned_at)
            VALUES (?, ?, ?)
            ON CONFLICT(root_path) DO UPDATE SET last_scanned_at = excluded.last_scanned_at
            """,
            (str(root), now, now),
        )
    return {"root": str(root), "found": found, "added": added, "skipped": skipped}


def _read_file_state(path: Path) -> tuple[bool, int | None, str | None]:
    try:
        file_stat = path.stat()
    except OSError:
        return False, None, None
    if not stat_module.S_ISREG(file_stat.st_mode):
        return False, None, None
    try:
        modified_at = datetime.fromtimestamp(file_stat.st_mtime).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        modified_at = None
    return True, file_stat.st_size, modified_at


def _prune_refresh_file_state_caches(now: float) -> None:
    expired = [
        refresh_id
        for refresh_id, (created_at, _) in _refresh_file_state_caches.items()
        if now - created_at > REFRESH_FILE_STATE_CACHE_TTL
    ]
    for refresh_id in expired:
        _refresh_file_state_caches.pop(refresh_id, None)


def get_refresh_file_state_cache(refresh_id: str) -> dict[str, tuple[bool, int | None, str | None]] | None:
    if not refresh_id:
        return None
    now = time.monotonic()
    with _refresh_file_state_cache_lock:
        _prune_refresh_file_state_caches(now)
        entry = _refresh_file_state_caches.get(refresh_id)
        if entry:
            return entry[1]
        cache: dict[str, tuple[bool, int | None, str | None]] = {}
        _refresh_file_state_caches[refresh_id] = (now, cache)
        return cache


def consume_refresh_file_state_cache(refresh_id: str) -> dict[str, tuple[bool, int | None, str | None]] | None:
    if not refresh_id:
        return None
    now = time.monotonic()
    with _refresh_file_state_cache_lock:
        _prune_refresh_file_state_caches(now)
        entry = _refresh_file_state_caches.pop(refresh_id, None)
    return entry[1] if entry else None


def model_to_dict(
    row: sqlite3.Row,
    file_state_cache: dict[str, tuple[bool, int | None, str | None]] | None = None,
) -> dict:
    path = Path(row["cae_path"])
    cache_key = str(path)
    if file_state_cache is not None and cache_key in file_state_cache:
        exists, size, modified_at = file_state_cache[cache_key]
    else:
        exists, size, modified_at = _read_file_state(path)
        if file_state_cache is not None:
            file_state_cache[cache_key] = (exists, size, modified_at)
    return {
        "id": row["id"],
        "path": row["cae_path"],
        "fileName": path.name,
        "folder": str(path.parent),
        "title": row["title"],
        "notes": row["notes"],
        "tags": [tag.strip() for tag in row["tags"].split(",") if tag.strip()],
        "status": row["status"],
        "favorite": bool(row["favorite"]),
        "exists": exists,
        "safeName": bool(SAFE_CAE_NAME.fullmatch(path.name)),
        "size": size,
        "modifiedAt": modified_at,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def get_model(model_id: int) -> dict:
    with connect_db() as db:
        row = db.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()
    if not row:
        raise KeyError("模型记录不存在")
    return model_to_dict(row)


def list_models(
    query: str = "",
    status: str = "all",
    favorite: bool = False,
    file_state_cache: dict[str, tuple[bool, int | None, str | None]] | None = None,
) -> list[dict]:
    sql = "SELECT * FROM models WHERE 1=1"
    params: list[object] = []
    if query:
        sql += " AND (title LIKE ? OR notes LIKE ? OR tags LIKE ? OR cae_path LIKE ?)"
        token = f"%{query}%"
        params.extend([token, token, token, token])
    if status != "all":
        sql += " AND status = ?"
        params.append(status)
    if favorite:
        sql += " AND favorite = 1"
    sql += " ORDER BY favorite DESC, updated_at DESC, title COLLATE NOCASE"
    with connect_db() as db:
        rows = db.execute(sql, params).fetchall()
    return [model_to_dict(row, file_state_cache) for row in rows]


def update_model(model_id: int, payload: dict) -> dict:
    allowed = {}
    if "title" in payload:
        title = str(payload["title"]).strip()
        if not title:
            raise ValueError("中文名称不能为空")
        allowed["title"] = title
    if "notes" in payload:
        allowed["notes"] = str(payload["notes"]).strip()
    if "tags" in payload:
        tags = payload["tags"]
        if isinstance(tags, list):
            tags = ",".join(str(tag).strip() for tag in tags if str(tag).strip())
        allowed["tags"] = str(tags).strip()
    if "status" in payload:
        status = str(payload["status"])
        if status not in VALID_STATUSES:
            raise ValueError("无效的模型状态")
        allowed["status"] = status
    if "favorite" in payload:
        allowed["favorite"] = 1 if bool(payload["favorite"]) else 0
    if not allowed:
        return get_model(model_id)
    allowed["updated_at"] = utc_now()
    columns = ", ".join(f"{key} = ?" for key in allowed)
    values = list(allowed.values()) + [model_id]
    with connect_db() as db:
        cursor = db.execute(f"UPDATE models SET {columns} WHERE id = ?", values)
        if cursor.rowcount == 0:
            raise KeyError("模型记录不存在")
    return get_model(model_id)


def delete_models(model_ids: object) -> dict:
    if not isinstance(model_ids, list):
        raise ValueError("请选择要移除的模型")

    normalized_ids: list[int] = []
    for raw_id in model_ids:
        if isinstance(raw_id, bool):
            raise ValueError("模型 ID 无效")
        try:
            model_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("模型 ID 无效") from exc
        if model_id <= 0:
            raise ValueError("模型 ID 无效")
        if model_id not in normalized_ids:
            normalized_ids.append(model_id)
    if not normalized_ids:
        raise ValueError("请选择要移除的模型")

    placeholders = ", ".join("?" for _ in normalized_ids)
    with connect_db() as db:
        cursor = db.execute(
            f"DELETE FROM models WHERE id IN ({placeholders})",
            normalized_ids,
        )
        deleted = int(cursor.rowcount)
    return {"ok": True, "requested": len(normalized_ids), "deleted": deleted}


def path_is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(str(path)), os.path.normcase(str(root))]) == os.path.normcase(str(root))
    except ValueError:
        return False


def remap_model_paths(old_root_text: str, new_root_text: str) -> dict:
    old_root = Path(old_root_text).expanduser().resolve()
    new_root = Path(new_root_text).expanduser().resolve()
    if not old_root_text.strip() or not new_root_text.strip():
        raise ValueError("旧目录和新目录都不能为空")
    if not new_root.is_dir():
        raise ValueError("新模型根目录不存在")
    with connect_db() as db:
        rows = db.execute("SELECT id, cae_path FROM models").fetchall()
        changes = []
        for row in rows:
            current = Path(row["cae_path"])
            if not path_is_within(current, old_root):
                continue
            relative = Path(os.path.relpath(current, old_root))
            changes.append((str(new_root / relative), utc_now(), row["id"]))
        if not changes:
            raise ValueError("没有找到位于旧目录下的模型记录")
        create_database_backup()
        try:
            db.executemany("UPDATE models SET cae_path = ?, updated_at = ? WHERE id = ?", changes)
        except sqlite3.IntegrityError as exc:
            raise ValueError("新目录会产生重复模型路径，未执行迁移") from exc
        db.execute("DELETE FROM scan_roots WHERE root_path = ?", (str(old_root),))
        now = utc_now()
        db.execute(
            """
            INSERT INTO scan_roots(root_path, created_at, last_scanned_at)
            VALUES (?, ?, ?)
            ON CONFLICT(root_path) DO UPDATE SET last_scanned_at = excluded.last_scanned_at
            """,
            (str(new_root), now, now),
        )
    existing = sum(1 for path, _, _ in changes if Path(path).is_file())
    return {"updated": len(changes), "existing": existing, "oldRoot": str(old_root), "newRoot": str(new_root)}


def export_metadata() -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    with connect_db() as db:
        models = [dict(row) for row in db.execute("SELECT * FROM models ORDER BY id").fetchall()]
        roots = [dict(row) for row in db.execute("SELECT * FROM scan_roots ORDER BY id").fetchall()]
    payload = {
        "format": "abaqus-model-manager-metadata",
        "version": 2,
        "exportedAt": utc_now(),
        "models": models,
        "scanRoots": roots,
    }
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = EXPORT_DIR / f"abaqus-model-metadata-{timestamp}.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def import_metadata(source: Path) -> dict:
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("资料文件无法读取或格式不正确") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("format") != "abaqus-model-manager-metadata"
        or not isinstance(payload.get("models"), list)
    ):
        raise ValueError("这不是 Abaqus 模型管理平台导出的资料文件")
    scan_roots = payload.get("scanRoots", [])
    if not isinstance(scan_roots, list):
        raise ValueError("资料文件中的 scanRoots 必须是列表")
    create_database_backup()
    imported = 0
    updated = 0
    imported_roots = 0
    updated_roots = 0
    skipped_roots = 0
    now = utc_now()
    db = connect_db()
    try:
        with db:
            for item in payload["models"]:
                path_text = str(item.get("cae_path", "")).strip()
                if not path_text.lower().endswith(".cae"):
                    continue
                existing = db.execute("SELECT id FROM models WHERE cae_path = ?", (path_text,)).fetchone()
                values = (
                    str(item.get("title") or Path(path_text).stem),
                    str(item.get("notes", "")),
                    str(item.get("tags", "")),
                    item.get("status") if item.get("status") in VALID_STATUSES else "planned",
                    1 if item.get("favorite") else 0,
                    now,
                    now,
                )
                if existing:
                    db.execute(
                        """
                        UPDATE models SET title=?, notes=?, tags=?, status=?, favorite=?, updated_at=?, last_seen_at=?
                        WHERE id=?
                        """,
                        values + (existing["id"],),
                    )
                    updated += 1
                else:
                    db.execute(
                        """
                        INSERT INTO models(cae_path,title,notes,tags,status,favorite,created_at,updated_at,last_seen_at)
                        VALUES (?,?,?,?,?,?,?,?,?)
                        """,
                        (path_text,) + values[:5] + (now, now, now),
                    )
                    imported += 1
            for item in scan_roots:
                if not isinstance(item, dict):
                    skipped_roots += 1
                    continue
                root_path = item.get("root_path")
                if not isinstance(root_path, str) or not root_path.strip():
                    skipped_roots += 1
                    continue
                root_path = root_path.strip()
                created_at = normalize_import_timestamp(item.get("created_at"), now)
                last_scanned_at = normalize_import_timestamp(item.get("last_scanned_at"), now)
                existing_root = db.execute(
                    "SELECT id FROM scan_roots WHERE root_path = ?", (root_path,)
                ).fetchone()
                if existing_root:
                    db.execute(
                        "UPDATE scan_roots SET last_scanned_at = ? WHERE id = ?",
                        (last_scanned_at, existing_root["id"]),
                    )
                    updated_roots += 1
                else:
                    db.execute(
                        """
                        INSERT INTO scan_roots(root_path, created_at, last_scanned_at)
                        VALUES (?, ?, ?)
                        """,
                        (root_path, created_at, last_scanned_at),
                    )
                    imported_roots += 1
    finally:
        db.close()
    return {
        "imported": imported,
        "updated": updated,
        "importedRoots": imported_roots,
        "updatedRoots": updated_roots,
        "skippedRoots": skipped_roots,
        "source": str(source),
    }


def create_portable_package() -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if getattr(sys, "frozen", False):
        # The one-file extraction directory contains dependencies, not a source checkout.
        # Transfer the executable and explicitly exported metadata, never credentials.
        target = EXPORT_DIR / f"Workbench-transfer-{timestamp}.zip"
        metadata = export_metadata()
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(sys.executable, "Workbench.exe")
            archive.write(metadata, "metadata.json")
            archive.writestr("README.txt", "运行 Workbench.exe，然后在设置与迁移中导入 metadata.json。\n"
                             "此文件包含用户主动导出的模型索引与备注，请勿公开分享。\n"
                             "不包含 CAE 文件、SSH 密码或其他工作台的数据。\n")
        return target
    target = EXPORT_DIR / f"Abaqus-Model-Manager-portable-{timestamp}.zip"
    excluded_roots = {".git", ".sites-runtime", "__pycache__", "exports", "backups", "release", "build", ".venv"}
    excluded_files = {"models.db", "models.db-wal", "models.db-shm"}
    with tempfile.TemporaryDirectory(prefix="abaqus-manager-") as temp_dir:
        snapshot = Path(temp_dir) / "models.db"
        source = connect_db()
        destination = sqlite3.connect(snapshot)
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
            source.close()
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for source in ROOT.rglob("*"):
                relative = source.relative_to(ROOT)
                if not source.is_file() or any(part in excluded_roots for part in relative.parts):
                    continue
                if relative.name in excluded_files or relative.parts[:2] in {("data", "remote"), ("data", "programs"), ("data", "engineering")}:
                    continue
                archive.write(source, Path("abaqus-model-manager") / relative)
            archive.write(snapshot, Path("abaqus-model-manager") / "data" / "models.db")
    return target


def reveal_in_explorer(path: Path) -> None:
    subprocess.Popen(["explorer.exe", f"/select,{path}"])


def native_folder_dialog(title: str) -> str | None:
    """Open a folder picker owned by the foreground application on Windows."""
    if os.name != "nt":
        return None

    import ctypes
    from ctypes import wintypes

    class BrowseInfo(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", wintypes.LPWSTR),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", wintypes.UINT),
            ("lpfn", ctypes.c_void_p),
            ("lParam", wintypes.LPARAM),
            ("iImage", ctypes.c_int),
        ]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BrowseInfo)]
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND

    # RETURNONLYFSDIRS + NEWDIALOGSTYLE + EDITBOX + NONEWFOLDERBUTTON.
    flags = 0x0001 | 0x0040 | 0x0010 | 0x0200
    display_name = ctypes.create_unicode_buffer(260)
    browse_info = BrowseInfo(
        user32.GetForegroundWindow(),
        None,
        ctypes.cast(display_name, wintypes.LPWSTR),
        title,
        flags,
        None,
        0,
        0,
    )

    initialized = ole32.CoInitialize(None) >= 0
    try:
        item_id_list = shell32.SHBrowseForFolderW(ctypes.byref(browse_info))
        if not item_id_list:
            return ""
        try:
            selected_path = ctypes.create_unicode_buffer(32768)
            if shell32.SHGetPathFromIDListW(item_id_list, selected_path):
                return selected_path.value
            return ""
        finally:
            ole32.CoTaskMemFree(item_id_list)
    except (OSError, ctypes.ArgumentError):
        return None
    finally:
        if initialized:
            ole32.CoUninitialize()


def powershell_dialog(kind: str, folder_title: str = "请选择包含 Abaqus 模型的文件夹") -> str:
    if kind == "folder":
        try:
            selected = native_folder_dialog(folder_title)
        except Exception:
            selected = None
        if selected is not None:
            return selected

    if kind == "file":
        setup = (
            "$d=New-Object System.Windows.Forms.OpenFileDialog;"
            "$d.Title='Select an Abaqus model file';"
            "$d.Filter='Abaqus model (*.cae)|*.cae';"
            "$d.CheckFileExists=$true;"
        )
    elif kind == "folder":
        setup = (
            "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
            "$d.Description='Select a model folder';"
            "$d.ShowNewFolderButton=$false;"
        )
    elif kind == "metadata":
        setup = (
            "$d=New-Object System.Windows.Forms.OpenFileDialog;"
            "$d.Title='Select exported model metadata';"
            "$d.Filter='Model metadata (*.json)|*.json';"
            "$d.CheckFileExists=$true;"
        )
    elif kind == "abaqus":
        setup = (
            "$d=New-Object System.Windows.Forms.OpenFileDialog;"
            "$d.Title='Select abaqus.bat';"
            "$d.Filter='Abaqus command (abaqus.bat)|abaqus.bat|Batch file (*.bat)|*.bat|All files (*.*)|*.*';"
            "$d.CheckFileExists=$true;"
        )
    elif kind == "program":
        setup = (
            "$d=New-Object System.Windows.Forms.OpenFileDialog;"
            "$d.Title='Select a CMD or BAT program';"
            "$d.Filter='Windows scripts (*.cmd;*.bat)|*.cmd;*.bat';"
            "$d.CheckFileExists=$true;"
        )
    elif kind in {"fluent", "pyrosim", "fds", "engineering-exe"}:
        filters = {"fluent": "Fluent case (*.cas.h5;*.cas;*.cas.gz)|*.cas.h5;*.cas;*.cas.gz",
                   "pyrosim": "PyroSim / FDS model (*.psm;*.fds)|*.psm;*.fds",
                   "fds": "FDS input (*.fds)|*.fds",
                   "engineering-exe": "Application (*.exe)|*.exe"}
        setup = ("$d=New-Object System.Windows.Forms.OpenFileDialog;"
                 "$d.Title='Select a file';$d.CheckFileExists=$true;"
                 + "$d.Filter='" + filters[kind] + "';")
    else:
        raise ValueError("未知的选择窗口类型")
    command = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$owner=New-Object System.Windows.Forms.Form;"
        "$owner.TopMost=$true;$owner.ShowInTaskbar=$false;"
        "$owner.StartPosition='CenterScreen';$owner.Size=New-Object System.Drawing.Size(1,1);"
        "$owner.Opacity=0.01;$owner.Show();$owner.Activate();$owner.BringToFront();"
        + setup
        + "$result=$d.ShowDialog($owner);$owner.Close();"
        + "if($result -eq [System.Windows.Forms.DialogResult]::OK){"
        + ("[Console]::Write($d.SelectedPath)" if kind == "folder" else "[Console]::Write($d.FileName)")
        + "}"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-STA", "-Command", command],
        capture_output=True,
        encoding="utf-8",
        creationflags=CREATE_NO_WINDOW,
        timeout=300,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "无法打开选择窗口")
    return completed.stdout.strip()


class AppHandler(BaseHTTPRequestHandler):
    server_version = "AbaqusModelManager/2.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, payload: object, status: int = 200) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("请求内容过大")
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def reject_non_local_api_request(self) -> bool:
        client_host = self.client_address[0] if self.client_address else ""
        if is_loopback_address(client_host):
            return False
        self.send_json({"error": "仅允许本机访问"}, 403)
        return True

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/") and self.reject_non_local_api_request():
            return
        try:
            if parsed.path.startswith(("/api/remote/", "/api/programs/", "/api/engineering/")):
                self.handle_remote(parsed, "GET")
                return
            if parsed.path == "/api/models":
                params = urllib.parse.parse_qs(parsed.query)
                refresh_id = params.get("refresh", [""])[0].strip()
                self.send_json(
                    list_models(
                        params.get("q", [""])[0],
                        params.get("status", ["all"])[0],
                        params.get("favorite", ["0"])[0] == "1",
                        get_refresh_file_state_cache(refresh_id),
                    )
                )
                return
            if parsed.path == "/api/stats":
                params = urllib.parse.parse_qs(parsed.query)
                refresh_id = params.get("refresh", [""])[0].strip()
                models = list_models(file_state_cache=consume_refresh_file_state_cache(refresh_id))
                self.send_json(
                    {
                        "total": len(models),
                        "favorites": sum(1 for model in models if model["favorite"]),
                        "missing": sum(1 for model in models if not model["exists"]),
                        "unsafe": sum(1 for model in models if not model["safeName"]),
                    }
                )
                return
            if parsed.path == "/api/system":
                self.send_json(system_info())
                return
            if parsed.path == "/api/settings":
                self.send_json(load_settings())
                return
            self.serve_static(parsed.path)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if self.reject_non_local_api_request():
            return
        try:
            if parsed.path.startswith(("/api/remote/", "/api/programs/", "/api/engineering/")):
                self.handle_remote(parsed, "POST")
                return
            results_match = re.fullmatch(r"/api/models/(\d+)/results", parsed.path)
            if results_match:
                self.send_json(open_model_results(int(results_match.group(1))))
                return
            if parsed.path == "/api/select-file":
                selected = powershell_dialog("file")
                if not selected:
                    self.send_json({"cancelled": True})
                    return
                path = normalize_cae_path(selected)
                if not path.is_file():
                    raise ValueError("模型文件不存在")
                model_id, created = upsert_model(path)
                self.send_json({"cancelled": False, "created": created, "model": get_model(model_id)})
                return
            if parsed.path == "/api/select-folder":
                selected = powershell_dialog("folder")
                if not selected:
                    self.send_json({"cancelled": True})
                    return
                self.send_json({"cancelled": False, **scan_folder(selected)})
                return
            if parsed.path == "/api/choose-folder":
                selected = powershell_dialog("folder")
                self.send_json({"cancelled": not bool(selected), "path": selected})
                return
            if parsed.path == "/api/select-abaqus":
                selected = powershell_dialog("abaqus")
                if not selected:
                    self.send_json({"cancelled": True})
                    return
                path = Path(selected).resolve()
                if not path.is_file():
                    raise ValueError("选择的 Abaqus 启动文件不存在")
                settings = load_settings()
                settings["abaqusCommand"] = str(path)
                save_settings(settings)
                self.send_json({"cancelled": False, "system": system_info()})
                return
            if parsed.path == "/api/settings":
                payload = self.read_json()
                command = str(payload.get("abaqusCommand", "")).strip()
                if command and not Path(command).is_file():
                    raise ValueError("Abaqus 启动文件不存在")
                settings = save_settings({"abaqusCommand": command})
                self.send_json({"settings": settings, "system": system_info()})
                return
            if parsed.path == "/api/remap":
                payload = self.read_json()
                self.send_json(remap_model_paths(str(payload.get("oldRoot", "")), str(payload.get("newRoot", ""))))
                return
            if parsed.path == "/api/export":
                target = export_metadata()
                reveal_in_explorer(target)
                self.send_json({"ok": True, "path": str(target)})
                return
            if parsed.path == "/api/import":
                selected = powershell_dialog("metadata")
                if not selected:
                    self.send_json({"cancelled": True})
                    return
                self.send_json({"cancelled": False, **import_metadata(Path(selected))})
                return
            if parsed.path == "/api/package":
                target = create_portable_package()
                reveal_in_explorer(target)
                self.send_json({"ok": True, "path": str(target)})
                return
            if parsed.path == "/api/scan":
                payload = self.read_json()
                self.send_json(scan_folder(str(payload.get("root", ""))))
                return
            if parsed.path == "/api/models/batch-delete":
                payload = self.read_json()
                if not isinstance(payload, dict):
                    raise ValueError("请求格式不正确")
                self.send_json(delete_models(payload.get("ids")))
                return
            match = re.fullmatch(r"/api/models/(\d+)/(open|reveal)", parsed.path)
            if match:
                model = get_model(int(match.group(1)))
                path = Path(model["path"])
                if not path.is_file():
                    raise FileNotFoundError("模型文件已移动或删除")
                if match.group(2) == "open":
                    subprocess.Popen(
                        [
                            "powershell.exe",
                            "-NoLogo",
                            "-NoProfile",
                            "-ExecutionPolicy",
                            "Bypass",
                            "-STA",
                            "-WindowStyle",
                            "Hidden",
                            "-File",
                            str(LAUNCHER),
                            str(path),
                        ],
                        cwd=str(path.parent),
                        creationflags=CREATE_NO_WINDOW,
                    )
                else:
                    reveal_in_explorer(path)
                self.send_json({"ok": True, "model": model})
                return
            self.send_json({"error": "接口不存在"}, 404)
        except (ValueError, FileNotFoundError, KeyError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def handle_remote(self, parsed, method):
        # SSH credentials and remote execution are available only to this local UI.
        host = self.headers.get("Host", "")
        host_url = urllib.parse.urlparse("http://" + host)
        origin = self.headers.get("Origin")
        if (host_url.hostname not in {"127.0.0.1", "localhost", "::1"}
                or (origin is not None and origin != "http://" + host)
                or self.headers.get("Sec-Fetch-Site") == "cross-site"):
            self.send_json({"error": "此接口仅允许本机同源访问"}, 403)
            return
        if method == "POST" and self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self.send_json({"error": "请使用 JSON 请求"}, 415)
            return
        try:
            if parsed.path.startswith("/api/engineering/"):
                self.handle_engineering(parsed, method)
                return
            if parsed.path.startswith("/api/programs/"):
                if method == "GET" and parsed.path == "/api/programs/list":
                    self.send_json(PROGRAMS.list())
                    return
                if method == "POST":
                    payload = self.read_json()
                    if not isinstance(payload, dict):
                        raise ValueError("请求内容应为 JSON 对象")
                    action = parsed.path.removeprefix("/api/programs/")
                    if action == "choose":
                        selected = powershell_dialog("program")
                        result = {"path": selected, "cancelled": not bool(selected)}
                    elif action == "save":
                        result = PROGRAMS.save(payload)
                    elif action == "run":
                        result = PROGRAMS.run(payload.get("id"))
                    elif action == "stop":
                        result = PROGRAMS.stop(payload.get("id"), payload.get("confirmed"))
                    elif action == "restart":
                        result = PROGRAMS.restart(payload.get("id"), payload.get("confirmed"))
                    elif action == "log":
                        result = PROGRAMS.log_tail(payload.get("id"))
                    elif action == "remove":
                        result = PROGRAMS.remove(payload.get("id"))
                    else:
                        self.send_json({"error": "程序接口不存在"}, 404)
                        return
                    self.send_json(result)
                    return
                self.send_json({"error": "程序接口不存在"}, 404)
                return
            manager = remote_manager()
            if method == "GET":
                if parsed.path == "/api/remote/state":
                    self.send_json(manager.snapshot())
                    return
                if parsed.path == "/api/remote/inputs":
                    params = urllib.parse.parse_qs(parsed.query)
                    model = get_model(int(params["modelId"][0]))
                    folder = Path(model["path"]).parent
                    files = sorted(p.name for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".inp")
                    self.send_json({"inputs": files, "folder": str(folder)})
                    return
            else:
                payload = self.read_json()
                if not isinstance(payload, dict):
                    raise ValueError("请求内容应为 JSON 对象")
                action = parsed.path.removeprefix("/api/remote/")
                if action == "stations/save":
                    result = manager.save_station(payload)
                elif action == "stations/remove":
                    result = manager.remove_station(payload.get("id"))
                elif action in {"stations/test", "stations/resources"}:
                    result = manager.test_station(payload.get("id"), payload.get("trustedFingerprint", ""), action.endswith("resources"))
                elif action == "cae-jobs":
                    model = get_model(int(payload["modelId"]))
                    result = manager.inspect_cae(model, detect_abaqus().get("path", ""))
                elif action == "submit":
                    model = get_model(int(payload["modelId"]))
                    result = manager.submit(model, payload, detect_abaqus().get("path", ""))
                elif action == "refresh":
                    result = manager.refresh()
                else:
                    self.send_json({"error": "远程接口不存在"}, 404)
                    return
                self.send_json(result)
                return
            self.send_json({"error": "远程接口不存在"}, 404)
        except HostKeyRequired as error:
            self.send_json({"error": str(error), "code": "host_key_required", "fingerprint": error.fingerprint}, 409)
        except Exception as error:
            self.send_json({"error": str(error)}, 400)

    def handle_engineering(self, parsed, method):
        action = parsed.path.removeprefix("/api/engineering/")
        if method == "GET":
            params = urllib.parse.parse_qs(parsed.query)
            kind = params.get("kind", [""])[0]
            if action == "models":
                self.send_json({"models": ENGINEERING.list(kind), "software": ENGINEERING.config()})
            elif action == "results":
                self.send_json(ENGINEERING.results(kind, params.get("id", [""])[0]))
            else:
                self.send_json({"error": "接口不存在"}, 404)
            return
        payload = self.read_json()
        if not isinstance(payload, dict): raise ValueError("请求格式不正确")
        kind = payload.get("kind")
        if action == "fds-check":
            if kind != "pyrosim": raise ValueError("FDS 转换仅用于 PyroSim 工作台")
            selected = payload.get("path") or powershell_dialog("fds")
            result = {"cancelled": True} if not selected else fds_compat.analyze(selected)[0]
        elif action == "fds-convert":
            if kind != "pyrosim": raise ValueError("FDS 转换仅用于 PyroSim 工作台")
            result = fds_compat.convert(payload.get("path"), payload.get("sha256"))
            result["id"] = ENGINEERING.add("pyrosim", result["output"])["id"]
            ENGINEERING.update("pyrosim", result["id"], {"title": Path(result["source"]).stem + "（兼容副本）"})
        elif action == "choose-exe":
            selected = powershell_dialog("engineering-exe")
            result = {"path": selected, "cancelled": not bool(selected)}
        elif action == "settings":
            result = ENGINEERING.save_config(payload.get("settings", {}))
        elif action in {"add", "scan"}:
            if kind not in {"fluent", "pyrosim"}: raise ValueError("未知工作台")
            selected = (powershell_dialog("folder", "请选择包含 " + kind + " 模型的文件夹")
                        if action == "scan" else powershell_dialog(kind))
            result = ({"cancelled": True} if not selected else
                      ENGINEERING.scan(kind, selected) if action == "scan" else ENGINEERING.add(kind, selected))
        elif action == "update":
            result = ENGINEERING.update(kind, payload.get("id"), payload.get("changes", {}))
        elif action == "remove":
            result = ENGINEERING.remove(kind, payload.get("id"))
        elif action == "batch-delete":
            result = ENGINEERING.remove_many(kind, payload.get("ids"))
        elif action == "open":
            result = ENGINEERING.launch(kind, payload.get("id"), payload)
        elif action == "reveal":
            model = ENGINEERING.get(kind, payload.get("id"))
            path = Path(model["path"])
            if not path.is_file(): raise ValueError("模型文件不存在")
            reveal_in_explorer(path)
            result = {"ok": True}
        else:
            self.send_json({"error": "接口不存在"}, 404)
            return
        self.send_json(result)

    def do_PATCH(self) -> None:
        if self.reject_non_local_api_request():
            return
        match = re.fullmatch(r"/api/models/(\d+)", urllib.parse.urlparse(self.path).path)
        if not match:
            self.send_json({"error": "接口不存在"}, 404)
            return
        try:
            self.send_json(update_model(int(match.group(1)), self.read_json()))
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def do_DELETE(self) -> None:
        if self.reject_non_local_api_request():
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/models":
            try:
                payload = self.read_json()
                self.send_json(delete_models(payload.get("ids")))
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json({"error": str(exc)}, 400)
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return

        match = re.fullmatch(r"/api/models/(\d+)", path)
        if not match:
            self.send_json({"error": "接口不存在"}, 404)
            return
        with connect_db() as db:
            cursor = db.execute("DELETE FROM models WHERE id = ?", (int(match.group(1)),))
        self.send_json({"ok": bool(cursor.rowcount)})

    def serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path == "/" else request_path.lstrip("/")
        target = (DIST / relative).resolve()
        if DIST.resolve() not in target.parents and target != DIST.resolve():
            self.send_error(403)
            return
        if not target.is_file():
            target = DIST / "index.html"
        content = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") else mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)


def main() -> None:
    global RUNTIME_PORT

    parser = argparse.ArgumentParser(description="Local Abaqus model manager")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        choices=("127.0.0.1", "localhost"),
        help="仅允许回环地址 127.0.0.1 或 localhost",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", dest="open_browser")
    args = parser.parse_args()
    init_db()
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    RUNTIME_PORT = int(server.server_address[1])
    # Resume monitoring existing jobs without automatically submitting new solves.
    if (REMOTE_DIR / "jobs.json").exists():
        remote_manager()
    url = f"http://{args.host}:{RUNTIME_PORT}"
    print(f"Workbench: {url}")
    if args.open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
