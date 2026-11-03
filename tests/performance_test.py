"""Non-destructive coverage for refresh file-state probing and global statistics."""

from __future__ import annotations

import gc
import json
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def request_json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"unexpected HTTP status: {response.status}")
        return json.loads(response.read().decode("utf-8"))


def remove_temp_directory(path: Path) -> None:
    for _ in range(40):
        gc.collect()
        try:
            shutil.rmtree(path)
            return
        except OSError:
            time.sleep(0.1)
    shutil.rmtree(path)


def main() -> None:
    test_dir = Path(tempfile.mkdtemp(prefix="abaqus-model-manager-performance-"))
    server = None
    worker = None
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import server as app

        db_path = test_dir / "models.db"
        app.DATA_DIR = test_dir / "data"
        app.DB_PATH = db_path
        app.SETTINGS_PATH = test_dir / "settings.json"
        app.BACKUP_DIR = test_dir / "backups"
        app.init_db()
        app._refresh_file_state_caches.clear()

        safe_path = test_dir / "safe.cae"
        second_path = test_dir / "second.cae"
        unsafe_path = test_dir / "unsafe model.cae"
        missing_path = test_dir / "missing.cae"
        safe_path.write_bytes(b"safe model")
        second_path.write_bytes(b"second model")
        unsafe_path.write_bytes(b"unsafe model")
        now = app.utc_now()
        seed_rows = [
            (str(safe_path), "Safe", "", "", "planned", 1),
            (str(second_path), "Second", "", "", "validated", 0),
            (str(unsafe_path), "Unsafe", "", "", "planned", 1),
            (str(missing_path), "Missing", "", "", "in_progress", 0),
        ]
        with app.connect_db() as connection:
            connection.executemany(
                """
                INSERT INTO models(cae_path,title,notes,tags,status,favorite,created_at,updated_at,last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [row + (now, now, now) for row in seed_rows],
            )

        with closing(app.connect_db()) as connection:
            row = connection.execute("SELECT * FROM models WHERE title = 'Safe'").fetchone()
        if row is None:
            raise RuntimeError("seed model was not created")

        original_stat = app.Path.stat
        stat_calls = 0

        def counted_stat(path: Path, *args, **kwargs):
            nonlocal stat_calls
            stat_calls += 1
            return original_stat(path, *args, **kwargs)

        with patch.object(app.Path, "is_file", side_effect=AssertionError("model_to_dict called is_file")):
            with patch.object(app.Path, "stat", new=counted_stat):
                direct_model = app.model_to_dict(row)
        if direct_model["exists"] is not True or direct_model["size"] != len(b"safe model"):
            raise RuntimeError("single stat model state was not returned correctly")
        if stat_calls != 1:
            raise RuntimeError(f"single model expected one stat call, got {stat_calls}")

        def failing_stat(path: Path, *args, **kwargs):
            raise OSError("transient file status failure")

        with patch.object(app.Path, "stat", new=failing_stat):
            unavailable_model = app.model_to_dict(row)
        if unavailable_model["exists"] is not False or unavailable_model["size"] is not None or unavailable_model["modifiedAt"] is not None:
            raise RuntimeError("OSError file state was not handled safely")

        server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.AppHandler)
        port = server.server_address[1]
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base_url = f"http://127.0.0.1:{port}"
        cae_paths = {safe_path, second_path, unsafe_path, missing_path}
        stat_calls = 0

        def counted_cae_stat(path: Path, *args, **kwargs):
            nonlocal stat_calls
            if path in cae_paths:
                stat_calls += 1
            return original_stat(path, *args, **kwargs)

        with patch.object(app.Path, "stat", new=counted_cae_stat):
            refresh_one = "refresh-one"
            models_one = request_json(f"{base_url}/api/models?refresh={refresh_one}")
            stats_one = request_json(f"{base_url}/api/stats?refresh={refresh_one}")
            if not isinstance(models_one, list) or not isinstance(stats_one, dict):
                raise RuntimeError("refresh API responses have the wrong shape")
            if stat_calls != len(seed_rows) or stat_calls >= len(seed_rows) * 2:
                raise RuntimeError(f"refresh repeated file probes: {stat_calls}")
            refresh_stat_calls = stat_calls

            filter_query = urllib.parse.urlencode(
                {"status": "planned", "favorite": "1", "refresh": "refresh-filter"}
            )
            filtered_models = request_json(f"{base_url}/api/models?{filter_query}")
            filtered_stats = request_json(f"{base_url}/api/stats?refresh=refresh-filter")
            if stat_calls != len(seed_rows) * 2:
                raise RuntimeError(f"filtered refresh repeated file probes: {stat_calls}")

        if len(models_one) != 4:
            raise RuntimeError("unfiltered model count is incorrect")
        expected_stats = {"total": 4, "favorites": 2, "missing": 1, "unsafe": 1}
        if stats_one != expected_stats:
            raise RuntimeError(f"global stats mismatch: {stats_one}")
        if not isinstance(filtered_models, list) or {model["title"] for model in filtered_models} != {"Safe", "Unsafe"}:
            raise RuntimeError("filtered model response is incorrect")
        if filtered_stats != expected_stats:
            raise RuntimeError("stats changed to filtered-result semantics")

        with patch.object(app.Path, "stat", new=counted_cae_stat):
            safe_path.unlink()
            deleted_models = request_json(f"{base_url}/api/models?refresh=refresh-deleted")
            deleted_stats = request_json(f"{base_url}/api/stats?refresh=refresh-deleted")
            if stat_calls != len(seed_rows) * 3:
                raise RuntimeError(f"deleted refresh file probes were not bounded: {stat_calls}")
            deleted_safe = next(model for model in deleted_models if model["title"] == "Safe")
            if deleted_safe["exists"] is not False or deleted_safe["size"] is not None or deleted_safe["modifiedAt"] is not None:
                raise RuntimeError("deleted file state was not refreshed")
            if deleted_stats["missing"] != 2:
                raise RuntimeError("deleted file was not reflected in global stats")

            safe_path.write_bytes(b"restored model")
            restored_models = request_json(f"{base_url}/api/models?refresh=refresh-restored")
            restored_stats = request_json(f"{base_url}/api/stats?refresh=refresh-restored")
            if stat_calls != len(seed_rows) * 4:
                raise RuntimeError(f"restored refresh file probes were not bounded: {stat_calls}")
            restored_safe = next(model for model in restored_models if model["title"] == "Safe")
            if restored_safe["exists"] is not True or restored_safe["size"] != len(b"restored model"):
                raise RuntimeError("restored file state was not refreshed")
            if restored_stats["missing"] != 1:
                raise RuntimeError("restored file was not reflected in global stats")

        print(
            "PERFORMANCE_PASS "
            f"single_stat_calls=1 refresh_stat_calls={refresh_stat_calls} baseline_double_probe={len(seed_rows) * 2} "
            "total=4 favorites=2 missing=1 unsafe=1 filtered_models=2 global_stats_preserved=true "
            "os_error_safe=true deleted_refresh=true restored_refresh=true no_abaqus=true temporary_db=true"
        )
    finally:
        if server is not None:
            server.shutdown()
            if worker is not None:
                worker.join(timeout=5)
            server.server_close()
        if "app" in locals():
            app._refresh_file_state_caches.clear()
        remove_temp_directory(test_dir)


if __name__ == "__main__":
    main()
