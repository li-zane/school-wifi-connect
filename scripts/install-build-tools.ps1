param([string]$CompilerDir = (Join-Path (Split-Path -Parent $PSScriptRoot) '.tmp\inno-setup'))
$ErrorActionPreference = 'Stop'
$CompilerDir = [IO.Path]::GetFullPath($CompilerDir)
$null = New-Item -ItemType Directory -Path $CompilerDir -Force

function Get-VerifiedFile([string]$Uri, [string]$File, [string]$Sha256) {
    if (-not (Test-Path -LiteralPath $File) -or (Get-FileHash -LiteralPath $File).Hash -ne $Sha256) {
        Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $File
    }
    if ((Get-FileHash -LiteralPath $File).Hash -ne $Sha256) { throw "SHA256 mismatch: $File" }
}

$compiler = Join-Path $CompilerDir 'ISCC.exe'
$expected = 'F02FA0E5F237B47D7ED1EDAE9B991866EC94FE02AF61EB6A73E94F92FE8E0604'
if (-not (Test-Path -LiteralPath $compiler) -or (Get-FileHash -LiteralPath $compiler).Hash -ne $expected) {
    $setup = Join-Path (Split-Path -Parent $CompilerDir) 'innosetup-6.7.4.exe'
    Get-VerifiedFile 'https://github.com/jrsoftware/issrc/releases/download/is-6_7_4/innosetup-6.7.4.exe' `
        $setup 'A549929319F9120716677C685535CA4671E4B937B5FDC87E2585F32D8EB30E8C'
    $process = Start-Process -FilePath $setup -ArgumentList @('/CURRENTUSER','/PORTABLE=1','/VERYSILENT',
        '/SUPPRESSMSGBOXES','/NORESTART',('/DIR="' + $CompilerDir + '"')) -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit(120000) -or $process.ExitCode -ne 0) { throw 'Inno Setup compiler installation failed' }
    if ((Get-FileHash -LiteralPath $compiler).Hash -ne $expected) { throw 'Inno Setup compiler hash mismatch' }
}
$languages = Join-Path $CompilerDir 'Languages'
$null = New-Item -ItemType Directory -Path $languages -Force
Get-VerifiedFile 'https://raw.githubusercontent.com/jrsoftware/issrc/is-6_7_4/Files/Languages/Unofficial/ChineseSimplified.isl' `
    (Join-Path $languages 'ChineseSimplified.isl') '7D544B9BB1D142CFA11F2E5D3CC8ABE2E55F8E066C5124E3772675AA236E1278'
Write-Output $compiler
