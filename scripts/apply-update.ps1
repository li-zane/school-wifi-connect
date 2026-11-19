param([Parameter(Mandatory=$true)][string]$Plan)
$ErrorActionPreference = 'Stop'
# -File runs in a fresh session that ignores the caller's preference, and a
# terminating error in the body still exits 0. Report every outcome and set the
# exit code so the scheduler record matches update-result.json.
trap { $_ | Out-String | Write-Error; exit 1 }
$p = Get-Content -LiteralPath $Plan -Raw -Encoding UTF8 | ConvertFrom-Json
$resultPath = Join-Path ([IO.Path]::GetFullPath($p.data_root)) 'update-result.json'
function Result([string]$State, [string]$Message, $Detail) {
    @{state=$State; version=$p.expected_version; message=$Message; detail=$Detail} | ConvertTo-Json -Compress | Set-Content -LiteralPath $resultPath -Encoding UTF8
}

function Set-InstalledDisplayVersion {
    # Only the Inno installation matching this update gets a new Apps entry version.
    $key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\NJUSTWiFi.CurrentUser_is1'
    $location = (Get-ItemProperty -LiteralPath $key -Name InstallLocation -ErrorAction SilentlyContinue).InstallLocation
    if ($location -and (Join-Path $location 'app-files\current') -eq $install) {
        Set-ItemProperty -LiteralPath $key -Name DisplayVersion -Value $p.expected_version
    }
}
$install = [IO.Path]::GetFullPath($p.install_root).TrimEnd('\')
$stage = [IO.Path]::GetFullPath($p.stage).TrimEnd('\')
$backup = [IO.Path]::GetFullPath($p.backup).TrimEnd('\')
$parent = Split-Path -Parent $install
$data = [IO.Path]::GetFullPath($p.data_root)
if ((Split-Path -Parent $stage) -ne $parent -or (Split-Path -Parent $backup) -ne $parent -or
    (Split-Path -Leaf $stage) -notlike '.school-wifi-update-*' -or
    (Split-Path -Leaf $backup) -notlike '.school-wifi-backup-*' -or
    $install -eq [IO.Path]::GetPathRoot($install) -or $stage -eq $install -or $backup -eq $install -or
    $data -eq $install -or $data.StartsWith($install + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid update paths' }
foreach ($dir in @($install,$stage)) {
    $item = Get-Item -LiteralPath $dir -ErrorAction SilentlyContinue
    if (-not $item -or -not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        $hint = if ($dir -eq $stage) { '更新暂存目录不存在，请重新下载' } else { '安装目录不可用，请重新打开原版本' }
        Result 'error' $hint 'Invalid install directory'
        throw 'Invalid install directory'
    }
}
$swapped = $false
$moved = $false
$stopped = $false
$monitorTask = $null
$desktopTask = $null

function Stop-App {
    foreach ($name in @('NJUSTWiFi.exe','NJUSTWiFiWindow.exe','NJUSTWiFiBackend.exe')) {
        $path = Join-Path $install $name
        Get-CimInstance Win32_Process -Filter ("Name='" + $name + "'") | Where-Object {
            $_.ExecutablePath -and [string]::Equals($_.ExecutablePath,$path,[StringComparison]::OrdinalIgnoreCase)
        } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    }
    Start-Sleep -Milliseconds 350
}

function File-SHA256([string]$Path) {
    $hash = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try { return ([BitConverter]::ToString($hash.ComputeHash($stream))).Replace('-','').ToLowerInvariant() }
    finally { $stream.Dispose(); $hash.Dispose() }
}

function Health([string]$Expected) {
    for ($i=0; $i -lt 100; $i++) {
        try {
            $request = [Net.HttpWebRequest]::Create('http://127.0.0.1:' + $p.port + '/api/health')
            $request.Proxy = $null; $request.Timeout = 500
            $response = $request.GetResponse()
            try { $reader = New-Object IO.StreamReader($response.GetResponseStream()); $info = $reader.ReadToEnd() | ConvertFrom-Json }
            finally { if ($reader) { $reader.Dispose() }; $response.Dispose() }
            if ($info.app -eq 'NJUSTWiFi' -and ((-not $Expected) -or $info.version -eq $Expected)) { return $true }
        } catch { }
        Start-Sleep -Milliseconds 250
    }
    return $false
}

function Start-App([string]$Expected) {
    $backend = Join-Path $install 'NJUSTWiFiBackend.exe'
    if ($p.demo) {
        Start-Process -FilePath $backend -ArgumentList @('--serve','--demo','--port',[string]$p.port,'--data-dir',('"'+$data+'"')) -WorkingDirectory $install -WindowStyle Hidden
    } else {
        $boot = Start-Process -FilePath $backend -ArgumentList '--bootstrap' -WorkingDirectory $install -WindowStyle Hidden -Wait -PassThru
        if ($boot.ExitCode -ne 0) { throw 'Backend bootstrap failed' }
    }
    if (-not (Health $Expected)) { throw 'Updated backend health check failed' }
    # Preserve pause/start state without changing stored startup preferences.
    if (-not $p.demo) {
        # The new backend may already be mid-cycle right after boot; a rejected
        # pause/start restore must never roll back an installed update.
        $origin = 'http://127.0.0.1:' + $p.port
        $client = New-Object Net.WebClient; $client.Proxy = $null
        try {
            try {
                $session = $client.DownloadString($origin+'/api/session') | ConvertFrom-Json
                $client.Headers['X-NJUST-Token'] = $session.token
                $client.Headers['Content-Type'] = 'application/json'
                $operation = if ($p.was_running) { '/api/start' } else { '/api/stop' }
                $null = $client.UploadString($origin+$operation,'POST','{}')
            } catch { } # Monitoring state is preserved by settings; never fatal here.
        } finally { $client.Dispose() }
        if ($desktopTask) { Start-ScheduledTask -TaskName $desktopTask }
        else { Start-Process -FilePath (Join-Path $install 'NJUSTWiFi.exe') -WorkingDirectory $install -WindowStyle Hidden }
    }
}

try {
    # Verify every staged byte before stopping anything or renaming directories.
    $expected = @($p.manifest.PSObject.Properties)
    $files = @(Get-ChildItem -LiteralPath $stage -Recurse -File)
    if ($files.Count -ne $expected.Count -or $files.Count -eq 0) { throw 'Staging manifest mismatch' }
    if (@(Get-ChildItem -LiteralPath $stage -Recurse -Force | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }).Count) { throw 'Linked staging path' }
    foreach ($entry in $expected) {
        $path = [IO.Path]::GetFullPath((Join-Path $stage $entry.Name))
        if (-not $path.StartsWith($stage+'\',[StringComparison]::OrdinalIgnoreCase) -or
            (File-SHA256 $path) -ne $entry.Value) { throw 'Staged checksum mismatch' }
    }
    if (Test-Path -LiteralPath $backup) { throw 'Backup directory already exists' }
    if (-not $p.demo) {
        foreach ($name in @('NjustWifiLocalMonitor','NjustWifiDesktop')) {
            $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            if ($task -and @($task.Actions | Where-Object { $_.Execute -and $_.Execute.StartsWith($install+'\',[StringComparison]::OrdinalIgnoreCase) }).Count) {
                if ($name -eq 'NjustWifiLocalMonitor') { $monitorTask=$name } else { $desktopTask=$name }
                Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            }
        }
    }
    $stopped = $true
    Stop-App
    Move-Item -LiteralPath $install -Destination $backup
    $moved = $true
    Move-Item -LiteralPath $stage -Destination $install
    $swapped = $true
    Start-App $p.expected_version
    Result 'updated' '更新完成'
    try { Set-InstalledDisplayVersion } catch { } # Registry metadata is not part of the update transaction.
} catch {
    $failed = $_.Exception.Message
    try {
        if ($stopped) { Stop-App }
        if ($swapped) { Move-Item -LiteralPath $install -Destination $stage }
        if ($moved) { Move-Item -LiteralPath $backup -Destination $install }
        if ($stopped) { Start-App '' }
        Result 'rolled_back' '更新未完成，已恢复原版本' $failed
    } catch {
        Result 'error' '更新未完成，请重新打开原版本' $failed
    }
} finally {
    if (-not $p.demo -and $p.task_name -match '^NjustWifiUpdate[a-f0-9]{12}$') {
        Unregister-ScheduledTask -TaskName $p.task_name -Confirm:$false -ErrorAction SilentlyContinue
    }
}
