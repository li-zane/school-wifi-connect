param(
    [Parameter(Mandatory=$true)][ValidatePattern('^\d+\.\d+\.\d+$')][string]$Version,
    [string]$OutputDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) 'dist'),
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$OutputDirectory = (Resolve-Path -LiteralPath $OutputDirectory).Path
$setup = "NJUSTWiFi-$Version-Windows-x64-Setup.exe"
$portable = "NJUSTWiFi-$Version-Windows-x64.zip"
$expected = @($setup, $portable, "NJUSTWiFi-$Version-source.zip")
$manifest = @(Get-Content -LiteralPath (Join-Path $OutputDirectory 'SHA256.json') -Raw | ConvertFrom-Json)
if ($manifest.Count -ne $expected.Count) { throw 'Unexpected set of release assets' }
foreach ($name in $expected) {
    $entry = @($manifest | Where-Object file -CEQ $name)
    $file = Get-Item -LiteralPath (Join-Path $OutputDirectory $name)
    if ($entry.Count -ne 1 -or $entry[0].bytes -ne $file.Length -or
        $entry[0].sha256 -ne (Get-FileHash -LiteralPath $file.FullName).Hash) {
        throw "Size or SHA256 mismatch: $name"
    }
}
$zip = [IO.Compression.ZipFile]::OpenRead((Join-Path $OutputDirectory $portable))
try {
    foreach ($name in @('NJUSTWiFi.exe','NJUSTWiFiBackend.exe','NJUSTWiFiWindow.exe','NJUSTWiFi.ico',
        '_internal/frontend/index.html','_internal/scripts/NJUSTWiFiUpdate.exe','_internal/scripts/apply-update.ps1')) {
        $entry = $zip.GetEntry("NJUSTWiFi/$name")
        if (-not $entry -or $entry.Length -eq 0) { throw "Missing from portable package: $name" }
    }
} finally { $zip.Dispose() }
'Release assets: SHA256, sizes and updater payload verified.'
if ($CheckOnly) { return }

# Disposable smoke test: never overwrite an existing local installation.
$install = Join-Path $env:LOCALAPPDATA 'Programs\NJUSTWiFi'
$shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'NJUST WiFi.lnk'
$key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\NJUSTWiFi.CurrentUser_is1'
$machineKey = $key.Replace('HKCU:', 'HKLM:')
if (@($install, $shortcut, $key, $machineKey | Where-Object { Test-Path -LiteralPath $_ }).Count) {
    throw 'Installer smoke test requires an unused default install directory and Start Menu entry'
}

function Run-Hidden([string]$Executable, [string[]]$Arguments) {
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit(120000) -or $process.ExitCode -ne 0) {
        throw "Process timed out or failed: $Executable"
    }
}

$flags = @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART')
$runtime = Join-Path $install 'app-files\current'
$program = Join-Path $runtime 'NJUSTWiFi.exe'
$uninstall = Join-Path $install 'installer\unins000.exe'
try {
    Run-Hidden (Join-Path $OutputDirectory $setup) ($flags + '/LANG=chinesesimplified')
    if (-not (Test-Path -LiteralPath $program -PathType Leaf) -or
        -not (Test-Path -LiteralPath $shortcut -PathType Leaf) -or
        -not (Test-Path -LiteralPath $uninstall -PathType Leaf) -or
        (Test-Path -LiteralPath $machineKey)) { throw 'Expected per-user shortcuts or files missing' }
    $properties = Get-ItemProperty -LiteralPath $key
    $link = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcut)
    if ($link.TargetPath -ne $program -or $link.WorkingDirectory -ne $runtime -or
        $properties.DisplayVersion -ne $Version -or $properties.UninstallString.Trim('"') -ne $uninstall) {
        throw 'Start Menu or uninstall registration does not match the installed app'
    }
    'Setup smoke test: per-user install, Start Menu shortcut and uninstall entry verified.'
} finally {
    if (Test-Path -LiteralPath $uninstall -PathType Leaf) { Run-Hidden $uninstall $flags }
}
for ($i = 0; $i -lt 40 -and (Test-Path -LiteralPath $install); $i++) { Start-Sleep -Milliseconds 250 }
if (@($install, $shortcut, $key, $machineKey | Where-Object { Test-Path -LiteralPath $_ }).Count) {
    throw 'Uninstaller left behind program files or registration'
}
'Setup smoke test: uninstall verified.'
