[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$AbaqusCommand,
    [Parameter(Mandatory = $true)][string]$ScriptFile,
    [Parameter(Mandatory = $true)][string]$WorkingDirectory
)
$ErrorActionPreference = 'Stop'
try {
    $process = Start-Process -FilePath $AbaqusCommand `
        -ArgumentList @('cae', ('noGUI="{0}"' -f $ScriptFile)) `
        -WorkingDirectory $WorkingDirectory -WindowStyle Hidden -PassThru -Wait `
        -RedirectStandardOutput (Join-Path $WorkingDirectory 'abaqus-export.log') `
        -RedirectStandardError (Join-Path $WorkingDirectory 'abaqus-export-error.log')
    exit $process.ExitCode
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
