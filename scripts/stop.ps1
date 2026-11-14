$ErrorActionPreference = 'Stop'
if (Get-ScheduledTask -TaskName NjustWifiLocalMonitor -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName NjustWifiLocalMonitor
    'STOPPED NjustWifiLocalMonitor'
}
