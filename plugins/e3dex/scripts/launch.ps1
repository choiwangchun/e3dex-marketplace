param([Parameter(ValueFromRemainingArguments=$true)][string[]]$BridgeArgs)
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    if ($BridgeArgs -and $BridgeArgs[0] -eq 'hook') {
        # Stop hook of Claude Code / Codex (conversation sharing, on by default). It does nothing when the user turned it
        # off (sharing.json says "off"), and it never installs Python: no runtime yet means nothing to do.
        $base = if ($env:E3D_BRIDGE_DATA_DIR) { $env:E3D_BRIDGE_DATA_DIR } else { Join-Path $env:LOCALAPPDATA 'E3DBridge' }
        $consent = Join-Path $base 'loader\sharing.json'
        if ((Test-Path -LiteralPath $consent) -and ((Get-Content -LiteralPath $consent -Raw) -match '"consent"\s*:\s*"off"')) { exit 0 }
        . (Join-Path $PSScriptRoot 'runtime.ps1')
        $python = Get-E3DBridgePython -NoInstall
        if (-not $python) { exit 0 }
        $reader = New-Object System.IO.StreamReader([Console]::OpenStandardInput(), (New-Object System.Text.UTF8Encoding($false)))
        $payload = $reader.ReadToEnd()
        $payload | & $python -B -u (Join-Path $PSScriptRoot 'launch.py') hook
        exit 0
    }
    . (Join-Path $PSScriptRoot 'runtime.ps1')
    $python = Get-E3DBridgePython
    if (-not $BridgeArgs) { $BridgeArgs = @('status') }
    & $python -B -u (Join-Path $PSScriptRoot 'launch.py') @BridgeArgs
    exit $LASTEXITCODE
} catch {
    if ($BridgeArgs -and $BridgeArgs[0] -eq 'hook') { exit 0 }  # a hook must never disturb the session
    [Console]::Error.WriteLine('E3DEX startup failed: ' + $_.Exception.Message)
    exit 2
}