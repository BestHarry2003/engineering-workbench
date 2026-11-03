# Build only from the sanitized staging directory produced by build_release.py.
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

root = Path(SPECPATH)
resources = [(str(root / 'dist'), 'dist'), (str(root / 'assets'), 'assets'),
             (str(root / 'THIRD_PARTY_NOTICES.txt'), '.')]
for name in ('Open-Abaqus-Project.ps1', 'Open-Abaqus-Results.ps1',
             'Export-Abaqus-Job.ps1', 'abaqus_results_viewer.py',
             'abaqus_job_export.py', 'remote_runner.py'):
    resources.append((str(root / name), '.'))
resources += collect_data_files('webview') + copy_metadata('paramiko')
a = Analysis([str(root / 'desktop.py')], pathex=[str(root)], binaries=[],
             datas=resources, hiddenimports=['webview.platforms.edgechromium', 'webview.platforms.winforms',
                                           'paramiko', 'clr', 'pythonnet'],
             excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'cefpython3', 'tkinter', 'pytest'],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='Workbench',
          debug=False, strip=False, upx=False, console=False,
          icon=str(root / 'assets' / 'workbench.ico'),
          version=str(root / 'version_info.txt'))
