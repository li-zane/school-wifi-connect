param(
    [ValidateSet('Register','Remove')][string]$Mode='Register',
    [Parameter(Mandatory=$true)][string]$Executable,
    [Parameter(Mandatory=$true)][ValidatePattern('^NJUST-WiFi-Probe-[0-9a-f]{12}$')][string]$TaskName,
    [switch]$Elevated
)
$ErrorActionPreference='Stop'
if ($Executable.Contains('"')) {throw 'Invalid executable path'}
if (-not $Elevated) {
    $systemPowerShell=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $arguments='-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "'+$PSCommandPath+'" -Elevated -Mode '+$Mode+' -Executable "'+$Executable+'" -TaskName '+$TaskName
    $child=Start-Process -FilePath $systemPowerShell -Verb RunAs -WindowStyle Hidden -ArgumentList $arguments -PassThru
    if (-not $child.WaitForExit(90000)) {throw 'Setup timed out'}
    exit $child.ExitCode
}
if ($Mode -eq 'Remove') {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
} else {
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {throw 'Executable missing'}
    $user=[Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action=New-ScheduledTaskAction -Execute $Executable -WorkingDirectory (Split-Path -Parent $Executable)
    $principal=New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
    $settings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal -Settings $settings -Force | Out-Null
}
