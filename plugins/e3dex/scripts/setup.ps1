param(
    [ValidateSet('inspect','apply')][string]$Action='inspect',
    [string]$E3DRoot,
    [ValidateSet('Design','Draw','Both')][string]$Module='Design',
    [switch]$Elevate,
    [switch]$ExistingDllOnly,
    [string]$ResultFile
)
$ErrorActionPreference='Stop'
$OutputEncoding=[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
function Finish($value) {
    $json=$value | ConvertTo-Json -Depth 12
    if ($ResultFile) { [IO.File]::WriteAllText($ResultFile,$json,(New-Object Text.UTF8Encoding($false))) }
    Write-Output $json
}
try {
    Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility') -Force -ErrorAction Stop
    Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Management') -Force -ErrorAction Stop
    $package=Split-Path $PSScriptRoot -Parent
    $manifestPath=Join-Path $package 'SHA256SUMS.json'
    if (-not (Test-Path -LiteralPath $manifestPath)) { throw 'Build the distributable package before setup.' }
    $manifest=Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    foreach ($entry in $manifest.files.PSObject.Properties) {
        $contained=[IO.Path]::GetFullPath((Join-Path $package $entry.Name))
        if (-not $contained.StartsWith($package.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe manifest path.' }
        if ((Get-FileHash -LiteralPath $contained).Hash -ne $entry.Value) { throw ('Package integrity failed: '+$entry.Name) }
    }
    $bridgeVersion=[Reflection.AssemblyName]::GetAssemblyName((Join-Path $package 'e3d-addin\EhNativeBridge.dll')).Version
    if ($bridgeVersion -lt [Version]'3.2.0.0') { throw 'Build and package the authenticated native bridge (3.2.0 or newer) before setup.' }
    $candidates=New-Object Collections.Generic.List[string]
    if ($E3DRoot) { $candidates.Add([IO.Path]::GetFullPath($E3DRoot).TrimEnd('\')) }
    else {
        foreach ($process in @(Get-Process des,draw -ErrorAction SilentlyContinue)) {
            if ($process.Path) { $candidates.Add((Split-Path $process.Path -Parent)) }
        }
        foreach ($path in @($env:PDMSUI, 'C:\AVEVA\Everything3D2.10', 'C:\Program Files (x86)\AVEVA\Everything3D2.10')) {
            if ($path -and (Test-Path -LiteralPath (Join-Path $path 'des.exe'))) { $candidates.Add([IO.Path]::GetFullPath($path).TrimEnd('\')) }
        }
        foreach ($key in @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*')) {
            foreach ($entry in @(Get-ItemProperty $key -ErrorAction SilentlyContinue)) {
                if ($entry.DisplayName -like '*Everything3D*' -and $entry.InstallLocation -and (Test-Path -LiteralPath (Join-Path $entry.InstallLocation 'des.exe'))) {
                    $candidates.Add([IO.Path]::GetFullPath($entry.InstallLocation).TrimEnd('\'))
                }
            }
        }
    }
    $roots=@($candidates | Sort-Object -Unique)
    if ($roots.Count -ne 1) {
        Finish @{status='NEEDS_E3D_PATH';ready=$false;candidates=$roots;next_action='Provide the exact installed E3D 2.1 directory. No changes made.'}; exit 0
    }
    $root=$roots[0]
    $exe=Join-Path $root 'des.exe'
    if (-not (Test-Path -LiteralPath $exe)) { throw 'des.exe not found at the selected root.' }
    $version=(Get-Item -LiteralPath $exe).VersionInfo.FileVersion
    if ($version -notmatch '^2\.1\.') {
        Finish @{status='UNSUPPORTED_E3D';ready=$false;e3d_root=$root;version=$version;next_action='This DLL is validated for E3D 2.1 only.'}; exit 0
    }
    if ($ExistingDllOnly) {
        # Automatic upgrades apply only to an existing, older managed bridge.
        $target=Join-Path $root 'EhNativeBridge.dll'
        $source=Join-Path $package 'e3d-addin\EhNativeBridge.dll'
        $expected=$manifest.files.'e3d-addin/EhNativeBridge.dll'
        if (-not $expected -or (Get-FileHash -LiteralPath $source).Hash -ne $expected) { throw 'Unverified DLL payload.' }
        $result=@{status='NO_EXISTING_DLL'; e3d_root=$root}
        if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { Finish $result; exit 0 }
        if ((Get-FileHash -LiteralPath $target).Hash -eq $expected) { $result.status='INSTALLED'; Finish $result; exit 0 }
        $old=[Reflection.AssemblyName]::GetAssemblyName($target)
        $new=[Reflection.AssemblyName]::GetAssemblyName($source)
        if ($old.Name -ne $new.Name -or $old.Version -ge $new.Version) {
            $result.status='REVIEW_DLL_VERSION'; Finish $result; exit 0
        }
        function RunningE3D {
            Get-Process des,draw -ErrorAction SilentlyContinue | Where-Object {
                -not $_.Path -or [IO.Path]::GetFullPath((Split-Path $_.Path -Parent)) -eq $root
            } | Select-Object Id,ProcessName
        }
        $result.running=@(RunningE3D)
        if ($result.running.Count) { $result.status='NEEDS_E3D_CLOSED'; Finish $result; exit 0 }
        $result.status='NEEDS_INSTALL'
        if ($Action -eq 'inspect') { Finish $result; exit 0 }
        $mutex=New-Object Threading.Mutex($false,'Local\E3DBridgeInstaller')
        $locked=$false
        $temp=$null
        try {
            try { $locked=$mutex.WaitOne(10000) } catch [Threading.AbandonedMutexException] { $locked=$true }
            if (-not $locked) { throw 'Another E3DEX installer is active.' }
            if ((Get-FileHash -LiteralPath $target).Hash -eq $expected) { $result.status='INSTALLED' }
            else {
                $current=[Reflection.AssemblyName]::GetAssemblyName($target)
                if ($current.FullName -ne $old.FullName) { throw 'Installed DLL changed during upgrade. Inspect again.' }
                $id=[guid]::NewGuid().ToString('N')
                $temp=Join-Path $root ('.EhNativeBridge-'+$id+'.tmp')
                [IO.File]::Copy($source,$temp)
                if ((Get-FileHash -LiteralPath $temp).Hash -ne $expected) { throw 'Copied DLL failed integrity check.' }
                $result.running=@(RunningE3D)
                if ($result.running.Count) { $result.status='NEEDS_E3D_CLOSED' }
                else {
                    $backup=Join-Path $root ('E3DBridgeBackups\dll-'+$id+'\EhNativeBridge.dll')
                    [void][IO.Directory]::CreateDirectory((Split-Path $backup -Parent))
                    [IO.File]::Replace($temp,$target,$backup)
                    $result.backup=$backup
                    $result.status='UPDATED'
                }
            }
        } catch [UnauthorizedAccessException] {
            $result.status='NEEDS_ADMIN'
        } finally {
            if ($temp -and [IO.File]::Exists($temp)) { [IO.File]::Delete($temp) }
            if ($locked) { $mutex.ReleaseMutex() }
            $mutex.Dispose()
        }
        if ($result.status -eq 'NEEDS_ADMIN' -and $Elevate) {
            # The elevated child rechecks the package, target identity and open processes.
            $arguments=@('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',('"'+$PSCommandPath+'"'),
                         '-Action','apply','-ExistingDllOnly','-E3DRoot',('"'+$root+'"'))
            try {
                $child=Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden -ArgumentList $arguments -PassThru -Wait
                if ($child.ExitCode -eq 0 -and (Get-FileHash -LiteralPath $target).Hash -eq $expected) { $result.status='UPDATED' }
                elseif (@(RunningE3D).Count) { $result.status='NEEDS_E3D_CLOSED'; $result.running=@(RunningE3D) }
            } catch [System.ComponentModel.Win32Exception] {
                if ($_.Exception.NativeErrorCode -ne 1223) { throw }
            }
        }
        Finish $result; exit 0
    }
    $xmlNames=@('DesignAddins.xml')
    if ($Module -ne 'Design') { $xmlNames+='DrawAddins.xml' }
    $changes=New-Object Collections.Generic.List[string]
    foreach ($xmlName in $xmlNames) {
        $xmlPath=Join-Path $root $xmlName
        [xml]$doc=Get-Content -LiteralPath $xmlPath -Raw
        if ($doc.DocumentElement.LocalName -ne 'ArrayOfString') { throw ('Unsupported addin XML: '+$xmlName) }
        $matches=@($doc.DocumentElement.ChildNodes | Where-Object {$_.InnerText -eq 'EhNativeBridge'})
        $legacy=@($doc.DocumentElement.ChildNodes | Where-Object {$_.InnerText -eq 'EhAyfBridgeAuto'})
        if ($matches.Count -ne 1 -or $legacy.Count) { $changes.Add($xmlName) }
    }
    foreach ($pair in @(@('EhNativeBridge.dll','EhNativeBridge.dll'),@('NativeBridge\start.pmlmac','start.pmlmac'))) {
        $target=Join-Path $root $pair[0]
        if (-not (Test-Path -LiteralPath $target) -or ((Get-FileHash -LiteralPath $target).Hash -ne (Get-FileHash -LiteralPath (Join-Path $package ('e3d-addin\'+$pair[1]))).Hash)) { $changes.Add($pair[0]) }
    }
    $running=@(Get-Process des,draw -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,Path)
    $result=@{status='INSTALLED';ready=$false;e3d_root=$root;version=$version;module=$Module;changes=@($changes);running=$running;next_action='Run e3d_setup verify after login to the user-selected project.'}
    if ($changes.Count -eq 0) { Finish $result; exit 0 }
    $result.status='NEEDS_INSTALL'
    if ($Action -eq 'inspect') { $result.next_action='Run apply after an explicit initial-setup request.'; Finish $result; exit 0 }
    if ($running.Count) { $result.status='NEEDS_E3D_CLOSED';$result.next_action='Ask the user to save or discard their own work and close all Design/Draw windows. Never terminate or SAVEWORK.';Finish $result;exit 0 }
    $test=Join-Path $root ('.e3d-bridge-access-'+[guid]::NewGuid().ToString('N'))
    try {
        $stream=[IO.File]::Open($test,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        $stream.Dispose()
        Remove-Item -LiteralPath $test
        # Check existing protected files as well, without writing their contents.
        foreach ($relative in @($xmlNames)+@('EhNativeBridge.dll','NativeBridge\start.pmlmac')) {
            $existing=Join-Path $root $relative
            if (Test-Path -LiteralPath $existing) {
                $stream=[IO.File]::Open($existing,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::Read)
                $stream.Dispose()
            }
        }
    } catch [UnauthorizedAccessException] {
        $result.status='NEEDS_ADMIN';$result.next_action='Windows administrator approval is required. Run apply with allow_elevation=true to request UAC.'
        if ($Elevate) {
            $outDir=Join-Path $env:LOCALAPPDATA 'E3DBridge\setup'
            [void][IO.Directory]::CreateDirectory($outDir)
            $out=Join-Path $outDir ('elevated-'+[guid]::NewGuid().ToString('N')+'.json')
            $argsList=@('-NoProfile','-ExecutionPolicy','Bypass','-File',('"'+$PSCommandPath+'"'),'-Action','apply','-E3DRoot',('"'+$root+'"'),'-Module',$Module,'-ResultFile',('"'+$out+'"'))
            try {
                $child=Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden -ArgumentList $argsList -PassThru
                if ($child.WaitForExit(45000) -and (Test-Path -LiteralPath $out)) {
                    Finish (Get-Content -LiteralPath $out -Raw | ConvertFrom-Json); exit 0
                }
                $result.status='ELEVATION_PENDING';$result.result_file=$out;$result.next_action='Wait for the existing UAC/install attempt; inspect result_file before another apply.'
            } catch { $result.status='NEEDS_ADMIN';$result.next_action='UAC was declined or unavailable. Ask IT/admin; do not bypass policy.' }
        }
        Finish $result;exit 0
    }
    $installArgs=@{E3DRoot=$root;Apply=$true}
    if ($Module -ne 'Design') { $installArgs.IncludeDraw=$true }
    $log=& (Join-Path $PSScriptRoot 'install-e3d.ps1') @installArgs
    $result.status='NEEDS_PROJECT_LOGIN';$result.install_log=@($log);$result.changes=@();$result.next_action='Start E3D normally and log into the user-selected project, then run verify. Do not run bridge manual-start commands.'
    Finish $result
} catch {
    Finish @{status='FAILED';ready=$false;error=$_.Exception.Message;next_action='Inspect this error and backups before retrying. Do not bypass execution policy or overwrite running DLLs.'}
    exit 2
}
