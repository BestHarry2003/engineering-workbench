"""Identify CMD launch trees, then stop verified Windows process handles.

Never infer process ownership from a listening port or an executable name alone.
"""
import base64
import ctypes
import json
import os
import subprocess
from pathlib import Path


def launch_command(path):
    cmd = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/cmd.exe'
    return f'"{cmd}" /d /v:off /s /c ""{path}""'


def process_snapshot():
    script = """
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.Encoding]::UTF8
$items=@(Get-CimInstance Win32_Process | ForEach-Object {
  [pscustomobject]@{pid=[int]$_.ProcessId; parent=[int]$_.ParentProcessId;
    name=$_.Name; command=$_.CommandLine;
    created=$_.CreationDate.ToUniversalTime().ToFileTimeUtc().ToString()}
})
ConvertTo-Json -InputObject $items -Compress
"""
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand',
                             base64.b64encode(script.encode('utf-16le')).decode('ascii')],
                            capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError('无法读取程序进程信息，未停止任何进程')
    return json.loads(result.stdout.decode('utf-8-sig'))


def owned_tree(path, snapshot, current_pid=None):
    expected = launch_command(path).casefold()
    candidates = [p for p in snapshot if p['name'].lower() == 'cmd.exe'
                  and (p.get('command') or '').strip().casefold() == expected]
    if not candidates:
        raise ValueError('无法确认该服务的启动进程归属，未停止任何进程。请通过任务管理器核查；不会仅按端口结束程序。')
    if len(candidates) != 1:
        raise ValueError('发现多个同名脚本启动实例，无法安全判定，请先通过任务管理器核查')
    tree = [candidates[0]]
    seen = {tree[0]['pid']}
    for parent in tree:
        for child in snapshot:
            if (child['pid'] not in seen and child['parent'] == parent['pid']
                    and int(child['created']) >= int(parent['created'])):
                tree.append(child); seen.add(child['pid'])
    protected = {os.getpid() if current_pid is None else current_pid}
    by_pid = {p['pid']: p for p in snapshot}
    ancestor = next(iter(protected))
    while ancestor in by_pid and by_pid[ancestor]['parent'] not in protected:
        ancestor = by_pid[ancestor]['parent']; protected.add(ancestor)
    if seen & protected:
        raise ValueError('该进程树包含工作台或其启动进程，已禁止停止')
    return tree


def terminate_verified(tree):
    """Acquire and verify every handle before terminating; PID reuse cannot redirect a kill."""
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handles = []
    try:
        for item in tree:
            handle = kernel.OpenProcess(0x1000 | 0x100000 | 0x1, False, item['pid'])
            if not handle:
                if ctypes.get_last_error() == 87:  # Process already exited.
                    continue
                raise ValueError('无法获得停止权限，未继续操作')
            handles.append(handle)
            created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)):
                raise ValueError('无法核对进程创建时间，停止操作已取消')
            ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
            # CIM CreationDate preserves microseconds, FILETIME has 100ns precision.
            if ticks // 10 != int(item['created']) // 10:
                raise ValueError('进程身份发生变化，停止操作已取消')
        # Stop the launcher first so it cannot create a replacement child.
        for handle in handles:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise ValueError('无法读取进程状态')
            if code.value == 259 and not kernel.TerminateProcess(handle, 1):
                raise ValueError('有进程未能停止，未执行重启')
        for handle in handles:
            if kernel.WaitForSingleObject(handle, 5000) != 0:
                raise ValueError('仍有进程未结束，未执行重启')
    finally:
        for handle in handles:
            kernel.CloseHandle(handle)
