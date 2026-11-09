# Resolve Windows PowerShell modules explicitly: a parent PowerShell 7/Python
# process can otherwise pass incompatible PSModulePath entries to powershell.exe.
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility') -Force -ErrorAction Stop
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Management') -Force -ErrorAction Stop
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Archive') -Force -ErrorAction Stop
function Get-E3DBridgePython {
    $runtime = Join-Path (Split-Path $PSScriptRoot -Parent) 'runtime'
    $manifest = Get-Content -LiteralPath (Join-Path $runtime 'python.json') -Raw | ConvertFrom-Json
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'Windows x64 is required.' }
    $archive = Join-Path $runtime $manifest.archive
    $base = Join-Path $env:LOCALAPPDATA 'E3DBridge\runtime'
    [void][IO.Directory]::CreateDirectory($base)
    $target = Join-Path $base ('python-' + $manifest.version + '-' + $manifest.sha256.Substring(0,12))
    $mutex = New-Object Threading.Mutex($false, 'Local\E3DBridgePython31313')
    $locked = $false
    try {
        try { $locked = $mutex.WaitOne(90000) } catch [Threading.AbandonedMutexException] { $locked = $true }
        if (-not $locked) { throw 'Python extraction is busy; inspect the other setup process.' }
        if (-not (Test-Path -LiteralPath $target)) {
            # A cached runtime never needs the archive or network again. An official
            # archive copied here by IT also allows a completely offline first start.
            if (-not (Test-Path -LiteralPath $archive)) {
                $archive = Join-Path $base $manifest.archive
            }
            if (-not (Test-Path -LiteralPath $archive)) {
                $uri = [Uri]$manifest.source
                if ($uri.Scheme -ne 'https' -or $uri.Host -ne 'www.python.org') {
                    throw 'Python download must use the official HTTPS source.'
                }
                $download = Join-Path $base ('download-' + [guid]::NewGuid().ToString('N') + '.zip')
                $oldProgress = $ProgressPreference
                $oldTls = [Net.ServicePointManager]::SecurityProtocol
                try {
                    $ProgressPreference = 'SilentlyContinue'
                    [Net.ServicePointManager]::SecurityProtocol = $oldTls -bor [Net.SecurityProtocolType]::Tls12
                    [Console]::Error.WriteLine('E3DEX: preparing Python on first start (official download, SHA-256 verified).')
                    Invoke-WebRequest -Uri $uri.AbsoluteUri -OutFile $download -UseBasicParsing -TimeoutSec 60 -ErrorAction Stop
                    if ((Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash -ne $manifest.sha256) {
                        throw 'Downloaded Python archive integrity check failed.'
                    }
                    Move-Item -LiteralPath $download -Destination $archive
                } catch {
                    throw ('Python bootstrap failed: ' + $_.Exception.Message +
                        '. Allow HTTPS to www.python.org, or place the verified official archive at ' +
                        $archive + '. Then run scripts/launch.ps1 schema and reconnect the plugin.')
                } finally {
                    $ProgressPreference = $oldProgress
                    [Net.ServicePointManager]::SecurityProtocol = $oldTls
                    if (Test-Path -LiteralPath $download) { Remove-Item -LiteralPath $download -Force }
                }
            }
            if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $manifest.sha256) {
                throw ('Python archive integrity check failed: ' + $archive)
            }
            $stage = Join-Path $base ('staging-' + [guid]::NewGuid().ToString('N'))
            Expand-Archive -LiteralPath $archive -DestinationPath $stage
            foreach ($entry in $manifest.files.PSObject.Properties) {
                if ((Get-FileHash -LiteralPath (Join-Path $stage $entry.Name)).Hash -ne $entry.Value) {
                    throw ('Extracted Python integrity failed: ' + $entry.Name)
                }
            }
            Move-Item -LiteralPath $stage -Destination $target
        }
        foreach ($entry in $manifest.files.PSObject.Properties) {
            if ((Get-FileHash -LiteralPath (Join-Path $target $entry.Name)).Hash -ne $entry.Value) {
                throw ('Cached Python integrity failed: ' + $entry.Name + '. Do not run it.')
            }
        }
        return (Join-Path $target 'python.exe')
    } finally {
        if ($locked) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}
