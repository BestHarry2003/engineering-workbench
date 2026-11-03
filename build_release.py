"""Whitelist-only reproducible release staging. Never copies the developer's data."""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

SOURCE = Path(__file__).resolve().parent
ROOT_FILES = (
    'desktop.py', 'server.py', 'remote_jobs.py', 'remote_runner.py',
    'local_programs.py', 'program_processes.py', 'engineering_workbenches.py', 'fds_compat.py',
    'abaqus_job_export.py', 'abaqus_results_viewer.py',
    'Export-Abaqus-Job.ps1', 'Open-Abaqus-Project.ps1', 'Open-Abaqus-Results.ps1',
    'Start-Abaqus-Manager.ps1', 'Restore-Abaqus-Association.ps1',
    'requirements-remote.txt', 'requirements-desktop.txt', 'Workbench.spec',
    'version_info.txt', 'build_release.py', 'README-desktop.md',
)
WEB_FILES = ('index.html', 'styles.css', 'app.js', 'remote.css', 'remote.js',
             'programs.js', 'engineering.js', 'fds.js', 'workbench-theme.css')


def stage_sources(destination: Path):
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Staging directory must be new or empty')
    destination.mkdir(parents=True, exist_ok=True)
    for name in ROOT_FILES:
        shutil.copy2(SOURCE / name, destination / name)
    (destination / 'dist').mkdir()
    for name in WEB_FILES:
        shutil.copy2(SOURCE / 'dist' / name, destination / 'dist' / name)
    (destination / 'tests').mkdir()
    for path in sorted((SOURCE / 'tests').glob('*_test.*')):
        if path.suffix in {'.py', '.js'}:
            shutil.copy2(path, destination / 'tests' / path.name)
    shutil.copy2(destination / 'README-desktop.md', destination / 'README.md')
    (destination / '.gitignore').write_text(
        'data/\nbackups/\nexports/\nlogs/\n.sites-runtime/\n.venv/\nbuild/\nrelease/\n'
        '__pycache__/\n*.py[cod]\n*.log\n*.tmp\n*.bak\n*.db\n*.db-*\n*.exe\n*.zip\n', encoding='utf-8')
    from PIL import Image, ImageDraw
    assets = destination / 'assets'
    assets.mkdir()
    image = Image.new('RGBA', (256, 256), (7, 14, 25, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((12, 12, 244, 244), radius=48, fill='#102238', outline='#375b77', width=5)
    for n in range(5):
        xy = 64 + n * 32
        draw.line((64, xy, 192, xy), fill='#72cafa', width=5)
        draw.line((xy, 64, xy, 192), fill='#72cafa', width=5)
    draw.rectangle((130, 130, 158, 158), fill='#f4b65c')
    image.save(assets / 'workbench.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
    notices = ['Third-party components and their license texts.\n',
               'WebView2 Runtime: separately distributed by Microsoft.\n']
    for package in sorted(importlib.metadata.distributions(), key=lambda d:d.metadata['Name'].lower()):
        notices.append('\n=== ' + package.metadata['Name'] + ' ' + package.version + ' ===\n')
        for file in package.files or []:
            if any(word in str(file).lower() for word in ('license', 'copying', 'notice')) and file.suffix.lower() not in {'.py', '.pyc'}:
                path = Path(package.locate_file(file))
                if path.is_file() and path.stat().st_size < 500000:
                    notices.append(path.read_text(encoding='utf-8', errors='replace'))
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if python_license.exists():
        notices.extend(['\n=== Python ===\n', python_license.read_text(encoding='utf-8')])
    (destination / 'THIRD_PARTY_NOTICES.txt').write_text('\n'.join(notices), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--stage-only', action='store_true')
    args = parser.parse_args()
    stage, output = args.stage.resolve(), args.output.resolve()
    stage_sources(stage)
    output.mkdir(parents=True, exist_ok=True)
    manifest = {str(p.relative_to(stage)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(stage.rglob('*')) if p.is_file()}
    (output / 'source-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    if args.stage_only:
        return
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                    '--distpath', str(output), '--workpath', str(output / 'build'), str(stage / 'Workbench.spec')],
                   cwd=stage, check=True)
    archive = output / 'Workbench-source.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for name in manifest:
            z.write(stage / name, 'workbench/' + name)
    for name in ('README-desktop.md', 'THIRD_PARTY_NOTICES.txt'):
        shutil.copy2(stage / name, output / name)
    hashes = [hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name
              for p in (output / 'Workbench.exe', archive)]
    (output / 'SHA256SUMS.txt').write_text('\n'.join(hashes) + '\n', encoding='utf-8')
    print('Release created:', output)


if __name__ == '__main__':
    main()
