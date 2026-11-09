param([Parameter(ValueFromRemainingArguments=$true)][string[]]$BridgeArgs)
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    . (Join-Path $PSScriptRoot 'runtime.ps1')
    $python = Get-E3DBridgePython
    if (-not $BridgeArgs) { $BridgeArgs = @('status') }
    & $python -B -u (Join-Path $PSScriptRoot 'launch.py') @BridgeArgs
    exit $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine('E3DEX startup failed: ' + $_.Exception.Message)
    exit 2
}