$ErrorActionPreference = 'Stop'
$url = 'http://127.0.0.1:8765'
$serverScript = Join-Path $PSScriptRoot 'server.py'

function Test-Manager {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri ($url + '/api/stats') -TimeoutSec 2
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

try {
    if (-not (Test-Manager)) {
        $python = $null
        $pyLauncher = Get-Command 'py.exe' -ErrorAction SilentlyContinue
        if ($pyLauncher) {
            $python = (& $pyLauncher.Source -3 -c "import sys; print(sys.executable)" 2>$null).Trim()
        }
        if (-not $python) {
            $pythonCommand = Get-Command 'python.exe' -ErrorAction SilentlyContinue
            if ($pythonCommand) {
                $python = (& $pythonCommand.Source -c "import sys; print(sys.executable)" 2>$null).Trim()
            }
        }
        if (-not $python) {
            throw 'Python 3 was not found. Install Python 3 and enable Add Python to PATH.'
        }
        $pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
        if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
            $pythonw = $python
        }
        Start-Process -FilePath $pythonw `
            -ArgumentList @('"' + $serverScript + '"', '--host', '127.0.0.1', '--port', '8765') `
            -WorkingDirectory $PSScriptRoot `
            -WindowStyle Hidden

        $ready = $false
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            Start-Sleep -Milliseconds 250
            if (Test-Manager) {
                $ready = $true
                break
            }
        }
        if (-not $ready) {
            throw 'The model manager did not start in time.'
        }
    }
    Start-Process $url
}
catch {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        $_.Exception.Message,
        'Workbench',
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error
    ) | Out-Null
    exit 1
}
