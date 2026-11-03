"""Non-destructive metadata export/import coverage for models and scan roots."""

from __future__ import annotations

import gc
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REAL_DB = ROOT / "data" / "models.db"
REAL_SETTINGS = ROOT / "data" / "settings.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rows(path: Path, table: str) -> list[dict]:
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()]


def integrity(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])


def backup_files(path: Path) -> list[Path]:
    return sorted(path.glob("models.db.*.bak"), key=lambda item: item.stat().st_mtime_ns)


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


def write_payload(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    db_before = sha256(REAL_DB)
    settings_before = sha256(REAL_SETTINGS)
    test_dir = Path(tempfile.mkdtemp(prefix="abaqus-model-manager-metadata-"))
    source_db = test_dir / "models.db"
    source_backup = test_dir / "source-backups"
    export_dir = test_dir / "exports"
    old_db_env = os.environ.get("ABAQUS_MANAGER_DB")
    old_backup_env = os.environ.get("ABAQUS_MANAGER_BACKUP_DIR")
    try:
        os.environ["ABAQUS_MANAGER_DB"] = str(source_db)
        os.environ["ABAQUS_MANAGER_BACKUP_DIR"] = str(source_backup)
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import server as app

        app.DATA_DIR = test_dir / "data"
        app.DB_PATH = source_db
        app.SETTINGS_PATH = test_dir / "settings.json"
        app.BACKUP_DIR = source_backup
        app.EXPORT_DIR = export_dir
        app.init_db()

        source_models = [
            (str(test_dir / "models" / "alpha.cae"), "Alpha", "notes", "tag-a", "planned", 0),
            (str(test_dir / "models" / "beta.cae"), "Beta", "more notes", "tag-b", "validated", 1),
        ]
        source_roots = [
            (str(test_dir / "source-root-a"), "2024-01-02T03:04:05+00:00", "2024-02-03T04:05:06+00:00"),
            (str(test_dir / "source-root-b"), "2024-03-04T05:06:07+00:00", "2024-04-05T06:07:08+00:00"),
        ]
        now = app.utc_now()
        with app.connect_db() as connection:
            for path, title, notes, tags, status, favorite in source_models:
                connection.execute(
                    """
                    INSERT INTO models(cae_path,title,notes,tags,status,favorite,created_at,updated_at,last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (path, title, notes, tags, status, favorite, now, now, now),
                )
            connection.executemany(
                "INSERT INTO scan_roots(root_path,created_at,last_scanned_at) VALUES (?, ?, ?)",
                source_roots,
            )

        exported_path = app.export_metadata()
        exported = json.loads(exported_path.read_text(encoding="utf-8"))
        if exported.get("version") != 2 or not isinstance(exported.get("scanRoots"), list):
            raise RuntimeError("version 2 export did not contain scanRoots")
        for index, item in enumerate(exported["models"] + exported["scanRoots"], start=1):
            item["id"] = 9000 + index
        roundtrip_source = test_dir / "roundtrip.json"
        write_payload(roundtrip_source, exported)

        roundtrip_db = test_dir / "roundtrip.db"
        roundtrip_backup = test_dir / "roundtrip-backups"
        app.DB_PATH = roundtrip_db
        app.BACKUP_DIR = roundtrip_backup
        app.init_db()
        roundtrip_result = app.import_metadata(roundtrip_source)
        if roundtrip_result["imported"] != 2 or roundtrip_result["importedRoots"] != 2:
            raise RuntimeError(f"round-trip import counts failed: {roundtrip_result}")
        if roundtrip_result["updatedRoots"] != 0 or roundtrip_result["skippedRoots"] != 0:
            raise RuntimeError(f"round-trip root stats failed: {roundtrip_result}")
        if len(rows(roundtrip_db, "models")) != 2 or len(rows(roundtrip_db, "scan_roots")) != 2:
            raise RuntimeError("round-trip did not restore models and scan roots")
        if any(item["id"] >= 9000 for item in rows(roundtrip_db, "models")):
            raise RuntimeError("model id from export was restored")
        roundtrip_roots = {item["root_path"]: item for item in rows(roundtrip_db, "scan_roots")}
        if any(item["id"] >= 9000 for item in roundtrip_roots.values()):
            raise RuntimeError("scan root id from export was restored")
        for path, created_at, last_scanned_at in source_roots:
            if (
                roundtrip_roots.get(path, {}).get("created_at") != created_at
                or roundtrip_roots.get(path, {}).get("last_scanned_at") != last_scanned_at
            ):
                raise RuntimeError("round-trip did not restore scan root timestamps")
        if integrity(roundtrip_db) != "ok" or len(backup_files(roundtrip_backup)) != 1:
            raise RuntimeError("round-trip database or backup integrity failed")

        merge_db = test_dir / "merge.db"
        merge_backup = test_dir / "merge-backups"
        app.DB_PATH = merge_db
        app.BACKUP_DIR = merge_backup
        app.init_db()
        existing_root = source_roots[0][0]
        extra_root = str(test_dir / "local-only-root")
        new_root = str(test_dir / "missing-imported-root")
        local_created = "2020-01-01T00:00:00+00:00"
        local_last_scanned = "2020-02-02T00:00:00+00:00"
        with app.connect_db() as connection:
            connection.executemany(
                "INSERT INTO scan_roots(root_path,created_at,last_scanned_at) VALUES (?, ?, ?)",
                [
                    (existing_root, local_created, local_last_scanned),
                    (extra_root, local_created, local_last_scanned),
                ],
            )
        imported_last_scanned = "2030-03-03T00:00:00+00:00"
        merge_source = test_dir / "merge.json"
        write_payload(
            merge_source,
            {
                "format": "abaqus-model-manager-metadata",
                "version": 2,
                "models": [],
                "scanRoots": [
                    {
                        "id": 700,
                        "root_path": existing_root,
                        "created_at": "2030-01-01T00:00:00+00:00",
                        "last_scanned_at": imported_last_scanned,
                    },
                    {"id": 701, "root_path": new_root, "created_at": "not-a-time", "last_scanned_at": None},
                    {"root_path": "   "},
                    None,
                    {"root_path": 123},
                ],
            },
        )
        merge_result = app.import_metadata(merge_source)
        if merge_result["importedRoots"] != 1 or merge_result["updatedRoots"] != 1 or merge_result["skippedRoots"] != 3:
            raise RuntimeError(f"merge root stats failed: {merge_result}")
        merge_roots = {item["root_path"]: item for item in rows(merge_db, "scan_roots")}
        if len(merge_roots) != 3 or extra_root not in merge_roots:
            raise RuntimeError("merge removed or duplicated local scan roots")
        if merge_roots[existing_root]["created_at"] != local_created:
            raise RuntimeError("merge replaced the local scan root created_at")
        if merge_roots[existing_root]["last_scanned_at"] != imported_last_scanned:
            raise RuntimeError("merge did not update last_scanned_at")
        for field in ("created_at", "last_scanned_at"):
            try:
                datetime.fromisoformat(merge_roots[new_root][field])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(f"merge did not safely default {field}") from exc
        if Path(new_root).exists():
            raise RuntimeError("test root unexpectedly existed; import should not create directories")

        missing_scan_roots_source = test_dir / "missing-scan-roots.json"
        write_payload(
            missing_scan_roots_source,
            {
                "format": "abaqus-model-manager-metadata",
                "version": 2,
                "models": [{"cae_path": str(test_dir / "compat.cae"), "title": "compat"}],
            },
        )
        missing_result = app.import_metadata(missing_scan_roots_source)
        if missing_result["imported"] != 1 or any(missing_result[key] for key in ("importedRoots", "updatedRoots", "skippedRoots")):
            raise RuntimeError(f"missing scanRoots compatibility failed: {missing_result}")

        empty_scan_roots_source = test_dir / "empty-scan-roots.json"
        write_payload(
            empty_scan_roots_source,
            {"format": "abaqus-model-manager-metadata", "version": 2, "models": [], "scanRoots": []},
        )
        empty_result = app.import_metadata(empty_scan_roots_source)
        if any(empty_result[key] for key in ("importedRoots", "updatedRoots", "skippedRoots")):
            raise RuntimeError(f"empty scanRoots compatibility failed: {empty_result}")

        invalid_scan_roots_source = test_dir / "invalid-scan-roots.json"
        write_payload(
            invalid_scan_roots_source,
            {"format": "abaqus-model-manager-metadata", "version": 2, "models": [], "scanRoots": {}},
        )
        before_invalid = (len(rows(merge_db, "models")), len(rows(merge_db, "scan_roots")), len(backup_files(merge_backup)))
        try:
            app.import_metadata(invalid_scan_roots_source)
        except ValueError as exc:
            if "scanRoots" not in str(exc):
                raise RuntimeError(f"invalid scanRoots error was unclear: {exc}") from exc
        else:
            raise RuntimeError("invalid scanRoots type was accepted")
        after_invalid = (len(rows(merge_db, "models")), len(rows(merge_db, "scan_roots")), len(backup_files(merge_backup)))
        if before_invalid != after_invalid:
            raise RuntimeError("invalid scanRoots changed data or created a backup")

        transaction_db = test_dir / "transaction.db"
        transaction_backup = test_dir / "transaction-backups"
        app.DB_PATH = transaction_db
        app.BACKUP_DIR = transaction_backup
        app.init_db()
        with app.connect_db() as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_scan_root_import
                BEFORE INSERT ON scan_roots
                BEGIN
                    SELECT RAISE(ABORT, 'metadata import test failure');
                END;
                """
            )
        transaction_source = test_dir / "transaction.json"
        write_payload(
            transaction_source,
            {
                "format": "abaqus-model-manager-metadata",
                "version": 2,
                "models": [{"cae_path": str(test_dir / "transaction.cae"), "title": "must rollback"}],
                "scanRoots": [{"root_path": str(test_dir / "transaction-root")}],
            },
        )
        try:
            app.import_metadata(transaction_source)
        except sqlite3.DatabaseError:
            pass
        else:
            raise RuntimeError("database error did not abort metadata import")
        if rows(transaction_db, "models") or rows(transaction_db, "scan_roots"):
            raise RuntimeError("models and scan roots were not rolled back together")
        with app.connect_db() as connection:
            connection.execute("DROP TRIGGER fail_scan_root_import")
        connection.close()
        if integrity(transaction_db) != "ok":
            raise RuntimeError("transaction rollback left an invalid database")

        blocked_backup = test_dir / "backup-blocker"
        blocked_backup.write_text("not a directory", encoding="utf-8")
        app.BACKUP_DIR = blocked_backup
        before_blocked = (len(rows(transaction_db, "models")), len(rows(transaction_db, "scan_roots")))
        try:
            app.import_metadata(transaction_source)
        except RuntimeError:
            pass
        else:
            raise RuntimeError("metadata import continued after backup failure")
        after_blocked = (len(rows(transaction_db, "models")), len(rows(transaction_db, "scan_roots")))
        if before_blocked != after_blocked:
            raise RuntimeError("backup failure changed metadata")

        if sha256(REAL_DB) != db_before or sha256(REAL_SETTINGS) != settings_before:
            raise RuntimeError("metadata tests changed the original database or settings")
        print(
            "METADATA_IMPORT_PASS "
            "roundtrip=true merge=true missing_scanRoots=true empty_scanRoots=true invalid_type_rejected=true "
            "invalid_records_skipped=true transaction_rollback=true backup_failure_blocks=true "
            "nonexistent_roots_allowed=true no_scan_or_abaqus=true sqlite_integrity=ok "
            "original_db_unchanged=true settings_unchanged=true"
        )
    finally:
        if old_db_env is None:
            os.environ.pop("ABAQUS_MANAGER_DB", None)
        else:
            os.environ["ABAQUS_MANAGER_DB"] = old_db_env
        if old_backup_env is None:
            os.environ.pop("ABAQUS_MANAGER_BACKUP_DIR", None)
        else:
            os.environ["ABAQUS_MANAGER_BACKUP_DIR"] = old_backup_env
        remove_temp_directory(test_dir)


if __name__ == "__main__":
    main()
