param([string]$E3DRoot='C:\AVEVA\Everything3D2.10',[switch]$IncludeDraw,[switch]$Apply)
$ErrorActionPreference='Stop'
$root=[IO.Path]::GetFullPath($E3DRoot).TrimEnd('\')
$payload=Join-Path (Split-Path $PSScriptRoot -Parent) 'e3d-addin'
$files=[ordered]@{'EhNativeBridge.dll'='EhNativeBridge.dll';'NativeBridge\start.pmlmac'='start.pmlmac'}
$xmlFiles=@('DesignAddins.xml')
if($IncludeDraw){$xmlFiles+='DrawAddins.xml'}
foreach($item in $files.GetEnumerator()){
    if(-not(Test-Path -LiteralPath (Join-Path $payload $item.Value) -PathType Leaf)){throw "Missing payload: $($item.Value)"}
}
foreach($relative in $xmlFiles){
    [xml]$doc=Get-Content -LiteralPath (Join-Path $root $relative) -Raw
    if($doc.DocumentElement.LocalName -ne 'ArrayOfString'){throw "Unsupported addin XML: $relative"}
}
if(-not $Apply){
    [pscustomobject]@{mode='PLAN';target=$root;files=@($files.Keys);addin='EhNativeBridge';removeAutostart='EhAyfBridgeAuto';xml=$xmlFiles;note='Close E3D, then use -Apply. No HI Agent/settings required.'}|ConvertTo-Json -Depth 4
    exit 0
}
if(Get-Process des,draw -ErrorAction SilentlyContinue){throw 'Close E3D Design/Draw before installation. Nothing was written.'}
$mutex=New-Object Threading.Mutex($false,'Local\E3DBridgeInstaller')
$locked=$false
try {$locked=$mutex.WaitOne(0)} catch [Threading.AbandonedMutexException] {$locked=$true}
if(-not $locked){$mutex.Dispose();throw 'Another E3DEX installation is active.'}
$backup=Join-Path $root ('E3DBridgeBackups\'+(Get-Date -Format 'yyyyMMdd-HHmmss-ffff'))
New-Item -ItemType Directory -Path $backup -Force|Out-Null
$created=New-Object System.Collections.Generic.List[string]
$replaced=New-Object System.Collections.Generic.List[string]
try{
    foreach($item in $files.GetEnumerator()){
        $source=Join-Path $payload $item.Value
        $target=Join-Path $root $item.Key
        if(Test-Path -LiteralPath $target){
            if((Get-FileHash -LiteralPath $source).Hash -eq (Get-FileHash -LiteralPath $target).Hash){continue}
            $old=Join-Path $backup $item.Key
            New-Item -ItemType Directory -Path (Split-Path $old -Parent) -Force|Out-Null
            Copy-Item -LiteralPath $target -Destination $old
            $replaced.Add($item.Key)
        }else{$created.Add($item.Key)}
        New-Item -ItemType Directory -Path (Split-Path $target -Parent) -Force|Out-Null
        Copy-Item -LiteralPath $source -Destination $target
        if((Get-FileHash -LiteralPath $source).Hash -ne (Get-FileHash -LiteralPath $target).Hash){throw "Copy verification failed: $target"}
    }
    foreach($relative in $xmlFiles){
        $target=Join-Path $root $relative
        [xml]$doc=Get-Content -LiteralPath $target -Raw
        Copy-Item -LiteralPath $target -Destination (Join-Path $backup $relative)
        # Preserve separate HI Agent UI; remove only its old multi-session autostart.
        foreach($node in @($doc.DocumentElement.ChildNodes)){
            if($node.InnerText -eq 'EhAyfBridgeAuto'){[void]$doc.DocumentElement.RemoveChild($node)}
        }
        $native=@($doc.DocumentElement.ChildNodes|Where-Object{$_.InnerText -eq 'EhNativeBridge'})
        foreach($duplicate in @($native | Select-Object -Skip 1)){[void]$doc.DocumentElement.RemoveChild($duplicate)}
        if($native.Count -eq 0){
            $node=$doc.CreateElement('string',$doc.DocumentElement.NamespaceURI)
            $node.InnerText='EhNativeBridge'
            [void]$doc.DocumentElement.AppendChild($node)
        }
        $doc.Save($target)
    }
}catch{
    $failure=$_
    foreach($relative in @($replaced)+@($xmlFiles)){
        $saved=Join-Path $backup $relative
        if(Test-Path -LiteralPath $saved){Copy-Item -LiteralPath $saved -Destination (Join-Path $root $relative) -Force}
    }
    foreach($relative in $created){
        $newFile=[IO.Path]::GetFullPath((Join-Path $root $relative))
        if(-not $newFile.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Unsafe rollback path.'}
        if(Test-Path -LiteralPath $newFile){Remove-Item -LiteralPath $newFile}
    }
    throw $failure
}finally{
    [pscustomobject]@{created=@($created);replaced=@($replaced);backup=$backup;target=$root;xml=$xmlFiles}|ConvertTo-Json -Depth 3|Set-Content -LiteralPath (Join-Path $backup 'installation.json') -Encoding UTF8
    if($locked){$mutex.ReleaseMutex()}
    $mutex.Dispose()
}
Write-Output "Installed independent bridge. Restart E3D and log into the user-selected project. Backup: $backup"

