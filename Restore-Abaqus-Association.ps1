$ErrorActionPreference = 'Stop'

try {
    $overridePath = 'Registry::HKEY_CURRENT_USER\Software\Classes\cae_auto_file'
    if (Test-Path -LiteralPath $overridePath) {
        Remove-Item -LiteralPath $overridePath -Recurse -Force
    }

    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        'The original Abaqus .cae association has been restored.',
        'Abaqus Association',
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Information
    ) | Out-Null
}
catch {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        $_.Exception.Message,
        'Abaqus Association',
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error
    ) | Out-Null
    exit 1
}
