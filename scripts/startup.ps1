param(
    [ValidateSet('Install','Remove','Start','Stop','Status')][string]$Mode = 'Status',
    [string]$Executable,
    [switch]$NoLogon,
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [ValidatePattern('^NjustWifi[A-Za-z0-9]*$')][string]$TaskName = 'NjustWifiLocalMonitor',
    [ValidateRange(1024,65535)][int]$Port = 18089,
    [string]$DataDirectory,
    [switch]$Demo
)
$ErrorActionPreference = 'Stop'
switch ($Mode) {
    'Install' {
        if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { throw 'Installed executable required.' }
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $isPython = [IO.Path]::GetFileName($Executable) -match '^pythonw?\.exe$'
        $arguments = if ($isPython) { '-B "' + (Join-Path $ProjectRoot 'app.py') + '" --serve' } else { '--serve' }
        if (-not $DataDirectory) { $DataDirectory = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'NJUSTWiFi' }
        $arguments += ' --data-dir "' + $dataDirectory + '"'
        $arguments += ' --port ' + $Port
        if ($Demo) { $arguments += ' --demo' }
        $action = New-ScheduledTaskAction -Execute $Executable -Argument $arguments -WorkingDirectory $ProjectRoot
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
        $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
        $registration = @{TaskName=$taskName; Action=$action; Principal=$principal; Settings=$settings; Force=$true}
        if (-not $NoLogon) { $registration.Trigger = $trigger }
        Register-ScheduledTask @registration | Out-Null
        'INSTALLED'
    }
    'Remove' {
        if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        }
        'REMOVED'
    }
    'Start' { Start-ScheduledTask -TaskName $taskName; 'STARTED' }
    'Stop' { Stop-ScheduledTask -TaskName $taskName; 'STOPPED' }
    'Status' { Get-ScheduledTask -TaskName $taskName | Select-Object TaskName,State }
}
