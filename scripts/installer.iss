#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef PayloadDir
  #error PayloadDir is required
#endif

#define RuntimeDir "{app}\app-files\current"
#define MaintenanceDir "{app}\installer"
#define AppExe "NJUSTWiFi.exe"
#define MaintenanceScript "manage-installed.ps1"

[Setup]
AppId=NJUSTWiFi.CurrentUser
AppName=NJUST WiFi
AppVersion={#AppVersion}
AppPublisher=School WiFi Connect
AppPublisherURL=https://github.com/li-zane/school-wifi-connect
AppUpdatesURL=https://github.com/li-zane/school-wifi-connect/releases/latest
DefaultDirName={localappdata}\Programs\NJUSTWiFi
DisableDirPage=auto
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
UninstallFilesDir={#MaintenanceDir}
UninstallDisplayIcon={#RuntimeDir}\{#AppExe}
SetupIconFile=..\assets\NJUSTWiFi.ico
OutputBaseFilename=NJUSTWiFi-{#AppVersion}-Windows-x64-Setup
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
; The maintenance script only closes this installation, never another copy.
CloseApplications=no
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[CustomMessages]
english.OtherInstallDir=Uninstall the existing NJUST WiFi installation before choosing a different folder.
chinesesimplified.OtherInstallDir=如需更改安装目录，请先卸载已有的 NJUST WiFi 安装版。
english.ReservedSubdir=The chosen folder already contains installer files. Choose an empty folder.
chinesesimplified.ReservedSubdir=所选目录已包含安装程序目录，请选择一个空目录。
english.MaintenanceFailed=NJUST WiFi could not close this installation. Exit NJUST WiFi from the tray and try again.
chinesesimplified.MaintenanceFailed=关闭此安装版的后台失败，请从托盘退出 NJUST WiFi 后重试。

[Files]
Source: "{#MaintenanceScript}"; DestDir: "{#MaintenanceDir}"; Flags: ignoreversion
Source: "{#PayloadDir}\*"; DestDir: "{#RuntimeDir}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\NJUST WiFi"; Filename: "{#RuntimeDir}\{#AppExe}"; WorkingDir: "{#RuntimeDir}"

[Run]
Filename: "{#RuntimeDir}\{#AppExe}"; WorkingDir: "{#RuntimeDir}"; Description: "{cm:LaunchProgram,NJUST WiFi}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; This reserved tree also holds any in-app update backups and newer files not in the setup log.
Type: filesandordirs; Name: "{app}\app-files"

[Code]
function MaintainRuntime(const ScriptPath, Mode: String): Boolean;
var
  ExitCode: Integer;
begin
  Result := Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + ScriptPath +
    '" -Mode ' + Mode + ' -Runtime "' + ExpandConstant('{#RuntimeDir}') + '"',
    '', SW_HIDE, ewWaitUntilTerminated, ExitCode) and (ExitCode = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  PreviousDir: String;
begin
  Result := '';
  PreviousDir := GetPreviousData('InstallDir', '');
  if PreviousDir <> '' then begin
    if CompareText(RemoveBackslashUnlessRoot(PreviousDir), RemoveBackslashUnlessRoot(WizardDirValue)) <> 0 then
      Result := CustomMessage('OtherInstallDir')
    else begin
      ExtractTemporaryFile('{#MaintenanceScript}');
      if not MaintainRuntime(ExpandConstant('{tmp}\{#MaintenanceScript}'), 'Stop') then
        Result := CustomMessage('MaintenanceFailed');
    end;
  end else if DirExists(ExpandConstant('{app}\app-files')) or DirExists(ExpandConstant('{#MaintenanceDir}')) then
    Result := CustomMessage('ReservedSubdir');
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'InstallDir', WizardDirValue);
end;

procedure CurUninstallStepChanged(Step: TUninstallStep);
begin
  { This step runs after the user confirms removal and before any files are deleted. }
  if Step = usAppMutexCheck then
    if not MaintainRuntime(ExpandConstant('{#MaintenanceDir}\{#MaintenanceScript}'), 'Remove') then
      RaiseException(CustomMessage('MaintenanceFailed'));
end;
