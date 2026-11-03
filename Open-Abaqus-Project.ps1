[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$CaeFile,

    # Resolve and print paths without starting Abaqus.
    [switch]$Preview
)

$ErrorActionPreference = 'Stop'

function Show-ErrorMessage {
    param([string]$Message)

    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        $Message,
        'Abaqus Project Launcher',
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error
    ) | Out-Null
}

try {
    if ([string]::IsNullOrWhiteSpace($CaeFile)) {
        Add-Type -AssemblyName System.Windows.Forms

        $dialog = New-Object System.Windows.Forms.OpenFileDialog
        $dialog.Title = 'Select an Abaqus model file'
        $dialog.Filter = 'Abaqus model (*.cae)|*.cae|All files (*.*)|*.*'
        $dialog.CheckFileExists = $true
        $dialog.Multiselect = $false
        $dialog.RestoreDirectory = $true

        if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
            exit 0
        }

        $CaeFile = $dialog.FileName
    }

    $modelPath = [System.IO.Path]::GetFullPath($CaeFile)
    if (-not (Test-Path -LiteralPath $modelPath -PathType Leaf)) {
        throw "Model file not found:`n$modelPath"
    }
    if ([System.IO.Path]::GetExtension($modelPath) -ine '.cae') {
        throw "Please select a .cae model file:`n$modelPath"
    }

    $workDirectory = [System.IO.Path]::GetDirectoryName($modelPath)
    $abaqusExecutable = $null
    $settingsPath = Join-Path $PSScriptRoot 'data\settings.json'
    if (Test-Path -LiteralPath $settingsPath -PathType Leaf) {
        try {
            $settings = Get-Content -LiteralPath $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($settings.abaqusCommand -and (Test-Path -LiteralPath $settings.abaqusCommand -PathType Leaf)) {
                $abaqusExecutable = [System.IO.Path]::GetFullPath($settings.abaqusCommand)
            }
        }
        catch {
            $abaqusExecutable = $null
        }
    }

    if (-not $abaqusExecutable) {
        $abaqusCommand = Get-Command 'abaqus.bat' -ErrorAction SilentlyContinue
        if ($abaqusCommand) {
            $abaqusExecutable = $abaqusCommand.Source
        }
    }

    if (-not $abaqusExecutable) {
        $knownCommands = @(
            'C:\SIMULIA\Commands\abaqus.bat',
            'E:\APP\ABAQUS2022\SIMULIA\Commands\abaqus.bat'
        )
        $abaqusExecutable = $knownCommands | Where-Object {
            Test-Path -LiteralPath $_ -PathType Leaf
        } | Select-Object -First 1
    }

    if (-not $abaqusExecutable) {
        throw 'abaqus.bat was not found. Select it in Model Manager Settings.'
    }

    if ($Preview) {
        [pscustomobject]@{
            ModelFile       = $modelPath
            WorkingFolder   = $workDirectory
            AbaqusCommand   = $abaqusExecutable
        }
        return
    }

    # Abaqus uses the launch process current directory as its work directory.
    # Keep quotes around the database path for folders containing spaces.
    $databaseArgument = 'database="{0}"' -f $modelPath
    Start-Process -FilePath $abaqusExecutable `
        -ArgumentList @('cae', $databaseArgument) `
        -WorkingDirectory $workDirectory
}
catch {
    Show-ErrorMessage -Message $_.Exception.Message
    exit 1
}
