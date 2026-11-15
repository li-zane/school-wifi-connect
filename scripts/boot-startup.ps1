param(
    [ValidateSet('Install','Remove','Status')][string]$Mode = 'Status',
    [string]$Executable = (Join-Path $PSScriptRoot 'NJUSTWiFiBackend.exe'),
    [string]$OwnerSid,
    [switch]$Elevated
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$taskName = 'NjustWifiBootNetwork'
$stateRoot = Join-Path $env:ProgramData 'SchoolWiFiConnect\BootNetwork'
$programRoot = Join-Path $env:ProgramFiles 'SchoolWiFiConnect\BootNetwork'

function Assert-PlainPath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    $part = $full
    while ($part) {
        if ((Test-Path -LiteralPath $part) -and ((Get-Item -LiteralPath $part -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Reparse points are not supported for boot runtime/configuration paths.'
        }
        $part = Split-Path -Parent $part
    }
    return $full
}

function Protect-Directory([string]$Path) {
    $null = New-Item -ItemType Directory -Path $Path -Force
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true,$false)
    $acl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    foreach ($sid in @('S-1-5-18','S-1-5-32-544')) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($sid),'FullControl','ContainerInherit,ObjectInherit','None','Allow'))
    }
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        [Security.Principal.SecurityIdentifier]::new($OwnerSid),'ReadAndExecute','ContainerInherit,ObjectInherit','None','Allow'))
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Write-ProtectedText([string]$Path, [string]$Text) {
    $null = Assert-PlainPath $Path
    $temporary = Join-Path (Split-Path -Parent $Path) ([guid]::NewGuid().ToString('N')+'.tmp')
    [IO.File]::WriteAllText($temporary,$Text,[Text.UTF8Encoding]::new($false))
    # Replace the directory entry with a fresh file inheriting the protected ACL.
    # Do not keep a pre-existing file's explicit access rules or follow a link.
    if (Test-Path -LiteralPath $Path) {Remove-Item -LiteralPath $Path -Force}
    Move-Item -LiteralPath $temporary -Destination $Path
}

if ($Mode -eq 'Status') {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) {
        [pscustomobject]@{TaskName=$taskName; State=[string]$task.State; Account=$task.Principal.UserId;
            LogonType=[string]$task.Principal.LogonType; Trigger=@($task.Triggers.CimClass.CimClassName)} | ConvertTo-Json -Compress
        $statusFile = Join-Path $stateRoot 'status.json'
        if (Test-Path -LiteralPath $statusFile) {Get-Content -LiteralPath $statusFile -Raw -Encoding UTF8}
    } elseif (Test-Path -LiteralPath (Join-Path $stateRoot 'manifest.json')) {
        'BOOT_STATUS_UNAVAILABLE; administrator query may be required'
        exit 2
    } else {'NOT_INSTALLED'}
    exit 0
}

if (-not $OwnerSid) {$OwnerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value}
if ($OwnerSid -notmatch '^S-1-5-21-\d+-\d+-\d+-\d+$') {throw 'A local/domain user SID is required.'}
foreach ($value in @($Executable,$PSCommandPath)) {
    if ($value -match '["\r\n]') {throw 'Invalid setup path.'}
}
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    if ($Elevated) {throw 'Administrator confirmation was not granted.'}
    $shell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "'+$PSCommandPath+'" -Elevated -Mode '+$Mode+' -OwnerSid '+$OwnerSid+' -Executable "'+$Executable+'"'
    $child = Start-Process -FilePath $shell -Verb RunAs -WindowStyle Hidden -ArgumentList $arguments -PassThru
    if (-not $child.WaitForExit(120000)) {throw 'Boot setup is still running; check Status before retrying.'}
    if ($child.ExitCode -ne 0) {throw 'Boot setup was canceled or did not finish.'}
    'BOOT_SETUP_OK'
    exit 0
}

$programRoot = Assert-PlainPath $programRoot
$stateRoot = Assert-PlainPath $stateRoot
$manifestFile = Join-Path $stateRoot 'manifest.json'
$null = Assert-PlainPath $manifestFile
if (Test-Path -LiteralPath $manifestFile) {
    $old = Get-Content -LiteralPath $manifestFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($old.owner_sid -ne $OwnerSid) {throw 'Boot networking belongs to another Windows account.'}
}
if ($Mode -eq 'Remove') {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    'BOOT_REMOVED; saved runtime and status retained; user login tasks unchanged'
    exit 0
}

$Executable = Assert-PlainPath $Executable
if ([IO.Path]::GetFileName($Executable) -ne 'NJUSTWiFiBackend.exe' -or -not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw 'Select NJUSTWiFiBackend.exe from a complete desktop release.'
}
$source = Split-Path -Parent $Executable
$internal = Assert-PlainPath (Join-Path $source '_internal')
if (-not (Test-Path -LiteralPath (Join-Path $internal 'python314.dll'))) {throw 'Complete desktop runtime required.'}
if (Get-ChildItem -LiteralPath $internal -Force -Recurse | Where-Object {$_.Attributes -band [IO.FileAttributes]::ReparsePoint}) {
    throw 'Runtime contains reparse points.'
}
$profileKey = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\'+$OwnerSid
$profile = [Environment]::ExpandEnvironmentVariables((Get-ItemProperty -LiteralPath $profileKey).ProfileImagePath)
$data = Assert-PlainPath (Join-Path $profile 'AppData\Local\NJUSTWiFi')
$settingsFile = Assert-PlainPath (Join-Path $data 'settings.json')
$credentialsFile = Assert-PlainPath (Join-Path $data 'credentials.json')
$null = Get-Content -LiteralPath $settingsFile -Raw -Encoding UTF8 | ConvertFrom-Json

# SYSTEM never executes code from a user-writable portable folder.
$null = Assert-PlainPath (Split-Path -Parent $programRoot)
$null = Assert-PlainPath (Split-Path -Parent $stateRoot)
Protect-Directory (Split-Path -Parent $programRoot)
Protect-Directory (Split-Path -Parent $stateRoot)
Protect-Directory $programRoot
Protect-Directory $stateRoot
$digest = (Get-FileHash -LiteralPath $Executable -Algorithm SHA256).Hash.ToLowerInvariant()
$runtime = Join-Path $programRoot ('runtime-'+$digest.Substring(0,16))
$null = Assert-PlainPath $runtime
if (-not (Test-Path -LiteralPath $runtime)) {
    Protect-Directory $runtime
    Copy-Item -LiteralPath $Executable -Destination (Join-Path $runtime 'NJUSTWiFiBackend.exe')
    Copy-Item -LiteralPath $internal -Destination (Join-Path $runtime '_internal') -Recurse
}
if ((Get-FileHash -LiteralPath (Join-Path $runtime 'NJUSTWiFiBackend.exe') -Algorithm SHA256).Hash.ToLowerInvariant() -ne $digest) {
    throw 'Protected runtime verification failed.'
}
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Write-ProtectedText (Join-Path $stateRoot 'previous-task.xml') (Export-ScheduledTask -TaskName $taskName)
}
$manifest = @{schema=1; owner_sid=$OwnerSid; data_directory=$data; runtime=$runtime; runtime_sha256=$digest}
Write-ProtectedText $manifestFile ($manifest | ConvertTo-Json)
$action = New-ScheduledTaskAction -Execute (Join-Path $runtime 'NJUSTWiFiBackend.exe') -Argument ('--boot-manifest "'+$manifestFile+'"') -WorkingDirectory $runtime
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = 'PT20S'
$principal = New-ScheduledTaskPrincipal -UserId 'S-1-5-18' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Pre-logon WiFi and campus authentication only; user backend resumes after sign-in.' -Force | Out-Null
$scheduler = New-Object -ComObject Schedule.Service
$scheduler.Connect()
$registered = $scheduler.GetFolder('\').GetTask($taskName)
$registered.SetSecurityDescriptor(('D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GR;;;'+$OwnerSid+')'),0)
Write-ProtectedText (Join-Path $stateRoot 'task.xml') (Export-ScheduledTask -TaskName $taskName)
Start-ScheduledTask -TaskName $taskName
'BOOT_INSTALLED; SYSTEM/AtStartup+20s; no Windows login password stored; login tasks unchanged'
