[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$AbaqusCommand,
    [Parameter(Mandatory = $true)][string]$ScriptFile
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
try {
    if (-not (Test-Path -LiteralPath $AbaqusCommand -PathType Leaf)) {
        throw 'Abaqus command was not found.'
    }
    if (-not (Test-Path -LiteralPath $ScriptFile -PathType Leaf)) {
        throw 'Viewer script was not found.'
    }
    $scriptPath = [IO.Path]::GetFullPath($ScriptFile)
    $scriptArgument = 'script="{0}"' -f $scriptPath
    Start-Process -FilePath $AbaqusCommand `
        -ArgumentList @('viewer', 'noStartupDialog', $scriptArgument) `
        -WorkingDirectory ([IO.Path]::GetDirectoryName($scriptPath)) `
        -WindowStyle Hidden -ErrorAction Stop
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
