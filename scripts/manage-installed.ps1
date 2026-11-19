param(
    [Parameter(Mandatory=$true)][ValidateSet('Stop','Remove')][string]$Mode,
    [Parameter(Mandatory=$true)][string]$Runtime
)
$ErrorActionPreference = 'Stop'
$Runtime = [IO.Path]::GetFullPath($Runtime)
$tray = Join-Path $Runtime 'NJUSTWiFi.exe'
$backend = Join-Path $Runtime 'NJUSTWiFiBackend.exe'
$window = Join-Path $Runtime 'NJUSTWiFiWindow.exe'

function Get-InstalledProcesses {
    Get-CimInstance Win32_Process -Filter "Name='NJUSTWiFi.exe' OR Name='NJUSTWiFiBackend.exe' OR Name='NJUSTWiFiWindow.exe'" |
        Where-Object { $_.ExecutablePath -in @($tray, $backend, $window) }
}

$tasks = Get-ScheduledTask -TaskName 'NjustWifiLocalMonitor','NjustWifiDesktop','NjustWifiDesktopDemo*' -ErrorAction SilentlyContinue
foreach ($task in $tasks) {
    if ($task.TaskPath -ne '\' -or @($task.Actions).Count -ne 1 -or $task.Actions[0].Execute -notin @($tray, $backend)) { continue }
    if ($task.State -in @('Running', 'Queued')) { Stop-ScheduledTask -InputObject $task }
    if ($Mode -eq 'Remove') { Unregister-ScheduledTask -InputObject $task -Confirm:$false }
}
# Stop the tray first so its window job and children can exit with it.
foreach ($path in @($tray, $window, $backend)) {
    Get-InstalledProcesses | Where-Object ExecutablePath -eq $path |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
for ($i = 0; $i -lt 30; $i++) {
    if (-not (Get-InstalledProcesses)) { exit 0 }
    Start-Sleep -Milliseconds 200
}
throw 'An installed NJUST WiFi process is still running.'
