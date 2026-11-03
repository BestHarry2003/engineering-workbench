"""Minimal, non-destructive service and data-protection smoke test."""

from __future__ import annotations

import hashlib
import gc
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "models.db"
SETTINGS_PATH = ROOT / "data" / "settings.json"
SERVER_PATH = ROOT / "server.py"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def get_json(url: str) -> tuple[int, object]:
    with urllib.request.urlopen(url, timeout=5) as response:
        body = response.read().decode("utf-8")
        return response.status, json.loads(body)


def read_integrity_and_count(path: Path) -> tuple[str, int]:
    with closing(sqlite3.connect(path)) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        count = connection.execute("SELECT count(*) FROM models").fetchone()[0]
    return str(integrity), int(count)


def backup_files(backup_dir: Path) -> list[Path]:
    return sorted(backup_dir.glob("models.db.*.bak"), key=lambda path: path.stat().st_mtime_ns)


def verify_backup(path: Path, expected_model_count: int) -> None:
    integrity, count = read_integrity_and_count(path)
    if integrity != "ok" or count != expected_model_count:
        raise RuntimeError(f"backup validation failed: integrity={integrity}, models={count}")


def remove_temp_directory(path: Path) -> None:
    for attempt in range(20):
        gc.collect()
        try:
            shutil.rmtree(path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.25)


def main() -> None:
    db_before = sha256(DB_PATH)
    settings_before = sha256(SETTINGS_PATH)
    smoke_dir = Path(tempfile.mkdtemp(prefix="abaqus-model-manager-smoke-"))
    smoke_db = smoke_dir / "models.db"
    backup_dir = smoke_dir / "backups"
    stdout_path = smoke_dir / "server.stdout.txt"
    stderr_path = smoke_dir / "server.stderr.txt"
    shutil.copy2(DB_PATH, smoke_db)

    old_db_env = os.environ.get("ABAQUS_MANAGER_DB")
    old_backup_env = os.environ.get("ABAQUS_MANAGER_BACKUP_DIR")
    process: subprocess.Popen[str] | None = None
    stdout_handle = None
    stderr_handle = None
    try:
        os.environ["ABAQUS_MANAGER_DB"] = str(smoke_db)
        os.environ["ABAQUS_MANAGER_BACKUP_DIR"] = str(backup_dir)
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import server as app

        integrity_before, initial_model_count = read_integrity_and_count(smoke_db)
        if integrity_before != "ok":
            raise RuntimeError(f"isolated SQLite integrity check failed: {integrity_before}")
        if not app.is_loopback_address("127.0.0.1") or not app.is_loopback_address("::1"):
            raise RuntimeError("loopback address recognition failed")
        if app.is_loopback_address("192.0.2.1"):
            raise RuntimeError("non-loopback address was accepted")
        if app.system_info().get("port") != 8765:
            raise RuntimeError("default runtime port information is not 8765")

        blocked_backup_path = smoke_dir / "backup-blocker"
        blocked_backup_path.write_text("not a directory", encoding="utf-8")
        failed_import_source = smoke_dir / "failed-import.json"
        failed_import_source.write_text(
            json.dumps(
                {
                    "format": "abaqus-model-manager-metadata",
                    "version": 2,
                    "models": [{"cae_path": str(smoke_dir / "should-not-import.cae"), "title": "blocked"}],
                }
            ),
            encoding="utf-8",
        )
        app.BACKUP_DIR = blocked_backup_path
        try:
            app.import_metadata(failed_import_source)
        except RuntimeError:
            backup_failure_blocks_import = True
        else:
            backup_failure_blocks_import = False
        if not backup_failure_blocks_import:
            raise RuntimeError("import continued after backup failure")
        _, after_failed_import_count = read_integrity_and_count(smoke_db)
        if after_failed_import_count != initial_model_count:
            raise RuntimeError("failed import changed the isolated database")
        app.BACKUP_DIR = backup_dir

        import_source = smoke_dir / "import.json"
        import_source.write_text(
            json.dumps(
                {
                    "format": "abaqus-model-manager-metadata",
                    "version": 2,
                    "models": [{"cae_path": str(smoke_dir / "imported.cae"), "title": "Phase 1A test"}],
                }
            ),
            encoding="utf-8",
        )
        import_result = app.import_metadata(import_source)
        if import_result.get("imported") != 1:
            raise RuntimeError(f"metadata import check failed: {import_result}")
        import_backups = backup_files(backup_dir)
        if len(import_backups) != 1:
            raise RuntimeError("metadata import did not create exactly one backup")
        verify_backup(import_backups[-1], initial_model_count)

        old_root = smoke_dir / "old-models"
        new_root = smoke_dir / "new-models"
        old_root.mkdir()
        new_root.mkdir()
        old_model = old_root / "migrated.cae"
        old_model.write_bytes(b"synthetic smoke-test placeholder; never opened by Abaqus")
        now = app.utc_now()
        connection = app.connect_db()
        try:
            connection.execute(
                """
                INSERT INTO models(cae_path,title,notes,tags,status,favorite,created_at,updated_at,last_seen_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (str(old_model), "Migration test", "", "", "planned", 0, now, now, now),
            )
            connection.execute(
                """
                INSERT INTO scan_roots(root_path, created_at, last_scanned_at)
                VALUES (?, ?, ?)
                """,
                (str(old_root.resolve()), now, now),
            )
            connection.commit()
        finally:
            connection.close()
        _, before_remap_count = read_integrity_and_count(smoke_db)

        app.BACKUP_DIR = blocked_backup_path
        try:
            app.remap_model_paths(str(old_root), str(new_root))
        except RuntimeError:
            backup_failure_blocks_remap = True
        else:
            backup_failure_blocks_remap = False
        if not backup_failure_blocks_remap:
            raise RuntimeError("path migration continued after backup failure")
        with closing(sqlite3.connect(smoke_db)) as connection:
            unchanged_path = connection.execute(
                "SELECT cae_path FROM models WHERE cae_path = ?", (str(old_model),)
            ).fetchone()
        if unchanged_path is None:
            raise RuntimeError("failed path migration changed the isolated database")
        app.BACKUP_DIR = backup_dir

        remap_result = app.remap_model_paths(str(old_root), str(new_root))
        if remap_result.get("updated") != 1:
            raise RuntimeError(f"path migration check failed: {remap_result}")
        remap_backups = backup_files(backup_dir)
        if len(remap_backups) != 2:
            raise RuntimeError("path migration did not create its backup")
        verify_backup(remap_backups[-1], before_remap_count)
        with closing(sqlite3.connect(remap_backups[-1])) as connection:
            if connection.execute("SELECT 1 FROM models WHERE cae_path = ?", (str(old_model),)).fetchone() is None:
                raise RuntimeError("migration backup does not contain the pre-migration path")
        new_model = new_root / "migrated.cae"
        with closing(sqlite3.connect(smoke_db)) as connection:
            if connection.execute("SELECT 1 FROM models WHERE cae_path = ?", (str(new_model),)).fetchone() is None:
                raise RuntimeError("migration did not write the new path")

        environment = os.environ.copy()
        invalid_host = subprocess.run(
            [sys.executable, str(SERVER_PATH), "--host", "0.0.0.0", "--port", str(free_loopback_port())],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if invalid_host.returncode == 0:
            raise RuntimeError("non-loopback --host was accepted")

        port = free_loopback_port()
        stdout_handle = stdout_path.open("w", encoding="utf-8")
        stderr_handle = stderr_path.open("w", encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, str(SERVER_PATH), "--port", str(port)],
            cwd=ROOT,
            env=environment,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
        )

        base_url = f"http://127.0.0.1:{port}"
        stats_status = None
        stats_payload = None
        for _ in range(60):
            try:
                stats_status, stats_payload = get_json(f"{base_url}/api/stats")
                if stats_status == 200:
                    break
            except (OSError, ValueError):
                time.sleep(0.25)
        if stats_status != 200 or not isinstance(stats_payload, dict):
            raise RuntimeError("server did not become ready on the loopback default")

        system_status, system_payload = get_json(f"{base_url}/api/system")
        if system_status != 200 or not isinstance(system_payload, dict):
            raise RuntimeError("/api/system did not return a JSON object")
        runtime_port = system_payload.get("port")
        if runtime_port != port:
            raise RuntimeError(f"runtime port information mismatch: expected={port}, actual={runtime_port}")

        with urllib.request.urlopen(f"{base_url}/", timeout=5) as response:
            page_status = response.status
            page = response.read().decode("utf-8")
        if page_status != 200 or '<script src="/app.js"></script>' not in page:
            raise RuntimeError("local page or app.js entrypoint check failed")

        models_status, models_payload = get_json(f"{base_url}/api/models")
        if models_status != 200 or not isinstance(models_payload, list):
            raise RuntimeError("/api/models did not return a JSON list")

        db_after = sha256(DB_PATH)
        settings_after = sha256(SETTINGS_PATH)
        if db_before != db_after:
            raise RuntimeError("original SQLite database changed during smoke test")
        if settings_before != settings_after:
            raise RuntimeError("original user settings changed during smoke test")

        current_integrity, current_model_count = read_integrity_and_count(smoke_db)
        if current_integrity != "ok":
            raise RuntimeError(f"final isolated SQLite integrity check failed: {current_integrity}")
        print(
            "SMOKE_PASS "
            f"host_rejected=true loopback_guard=true page={page_status} api_stats={stats_status} "
            f"api_system={system_status} runtime_port={runtime_port} default_port=8765 "
            f"api_models={models_status} model_count={len(models_payload)} "
            f"sqlite_integrity={current_integrity} sqlite_model_count={current_model_count} "
            "import_backup=true remap_backup=true backup_failure_blocks=true "
            "original_db_unchanged=true settings_unchanged=true abaqus_invoked=false"
        )
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if stdout_handle is not None:
            stdout_handle.close()
        if stderr_handle is not None:
            stderr_handle.close()
        if old_db_env is None:
            os.environ.pop("ABAQUS_MANAGER_DB", None)
        else:
            os.environ["ABAQUS_MANAGER_DB"] = old_db_env
        if old_backup_env is None:
            os.environ.pop("ABAQUS_MANAGER_BACKUP_DIR", None)
        else:
            os.environ["ABAQUS_MANAGER_BACKUP_DIR"] = old_backup_env
        remove_temp_directory(smoke_dir)


if __name__ == "__main__":
    main()
