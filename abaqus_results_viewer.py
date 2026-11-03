# -*- coding: utf-8 -*-
"""Executed by Abaqus Viewer (Python 2.7/3.x), not by the web server."""
from __future__ import print_function

import json
import os

ODB_PATHS = []  # populated by the model manager


def load_results(session, paths):
    report = {"requested": len(paths), "opened": [], "failed": [], "displayError": None}
    first_odb = None
    for path in paths:
        try:
            odb = session.openOdb(name=path, path=path, readOnly=True)
            report["opened"].append(path)
            if first_odb is None:
                first_odb = odb
        except Exception as exc:
            report["failed"].append({"path": path, "error": str(exc)})
    if first_odb is not None:
        try:
            viewport = session.viewports[session.currentViewportName]
            viewport.setValues(displayedObject=first_odb)
            viewport.view.fitView()
        except Exception as exc:
            report["displayError"] = str(exc)
    return report


def main():
    from abaqus import session
    import visualization  # Register visualization objects in the Viewer session.

    report = load_results(session, ODB_PATHS)
    # Viewer starts in an isolated runtime directory, not the CAE/ODB directory.
    report_path = os.path.abspath("odb-load-report.json")
    with open(report_path, "w") as handle:
        json.dump(report, handle, ensure_ascii=True, indent=2)
    print("Model Library: opened %d of %d ODB files (read-only)." %
          (len(report["opened"]), report["requested"]))
    print("Switch ODBs in the Results tree / output database selector.")
    for failure in report["failed"]:
        print(json.dumps(failure, ensure_ascii=True))
    if report["displayError"]:
        print("Viewport: " + report["displayError"])
    print("Load report: " + report_path)


if __name__ == "__main__":
    main()
