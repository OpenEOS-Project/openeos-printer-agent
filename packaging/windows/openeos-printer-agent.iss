; OpenEOS Printer Agent - Windows installer (Inno Setup 6)
;
;   iscc /DAppVersion=0.3.0 packaging\windows\openeos-printer-agent.iss
;
; Expects the PyInstaller builds in dist\:
;   dist\openeos-printer-agent.exe            (service, one file)
;   dist\openeos-printer-agent-ui\            (window + tray, one folder)
;
; Installs both into Program Files, registers and starts the Windows service
; (via "openeos-printer-agent.exe install-service", which also lets logged-in
; users start/stop it), adds the window to the Start menu and - optional -
; to the autostart of all users and to the desktop. The uninstaller removes
; the service; pairing and settings in %ProgramData%\OpenEOS Printer Agent
; stay, so a reinstall does not need a new code.
;
; Silent install (CI, mass rollout):
;   setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
;             [/SERVER=https://kasse.example.org/api] [/TASKS="autostart,desktopicon"]
;
; SPDX-License-Identifier: AGPL-3.0-or-later
; Copyright (C) 2026 LUXCODE by Lukas Lukic-Spitznagel and contributors

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "OpenEOS Printer Agent"
#define ServiceName "OpenEOSPrinterAgent"
#define ServiceExe "openeos-printer-agent.exe"
#define UiExe "openeos-printer-agent-ui.exe"
#define DefaultServer "https://api.openeos.de/api"

[Setup]
AppId={{6B0E4C5A-3F7D-4E21-9B8C-0E0A5E0E05A1}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=LUXCODE by Lukas Lukic-Spitznagel
AppPublisherURL=https://openeos.de
AppSupportURL=https://docs.openeos.de
AppUpdatesURL=https://github.com/OpenEOS-Project/openeos-printer-agent/releases
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=openeos-printer-agent-setup-windows-x64
SetupIconFile=..\icons\openeos-printer-agent.ico
UninstallDisplayIcon={app}\ui\{#UiExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
; The window is closed by PrepareToInstall/taskkill; no Restart Manager dance.
CloseApplications=no
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}

[Languages]
Name: "de"; MessagesFile: "compiler:Languages\German.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
de.UiName=OpenEOS Drucker-Agent
en.UiName=OpenEOS Printer Agent
de.TaskAutostart=Beim Anmelden im Infobereich starten (empfohlen)
en.TaskAutostart=Start in the notification area at login (recommended)
de.TaskDesktop=Verknüpfung auf dem Desktop anlegen
en.TaskDesktop=Create a desktop shortcut
de.ServerTitle=Server
en.ServerTitle=Server
de.ServerDesc=Mit welchem OpenEOS soll sich der Drucker-Agent verbinden?
en.ServerDesc=Which OpenEOS should the printer agent connect to?
de.ServerText=Nutzt du app.openeos.de, lass die Adresse so. Betreibst du OpenEOS selbst, trag die Adresse deiner API mit /api am Ende ein.
en.ServerText=Using app.openeos.de? Leave the address as it is. Self-hosting OpenEOS? Enter the address of your API, ending in /api.
de.ServerLabel=Server-Adresse:
en.ServerLabel=Server address:
de.InstallingService=Hintergrunddienst wird eingerichtet ...
en.InstallingService=Setting up the background service ...
de.LaunchUi=OpenEOS Drucker-Agent jetzt öffnen
en.LaunchUi=Open OpenEOS Printer Agent now
de.ServiceFailed=Der Hintergrunddienst konnte nicht eingerichtet werden (Code %1). Details: %2
en.ServiceFailed=The background service could not be set up (code %1). Details: %2

[Tasks]
Name: "autostart"; Description: "{cm:TaskAutostart}"
Name: "desktopicon"; Description: "{cm:TaskDesktop}"; Flags: unchecked

[Files]
Source: "..\..\dist\{#ServiceExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\dist\openeos-printer-agent-ui\*"; DestDir: "{app}\ui"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"
Source: "..\..\NOTICE"; DestDir: "{app}"; DestName: "NOTICE.txt"

[InstallDelete]
; Old window build from a previous version (file names change with Qt updates)
Type: filesandordirs; Name: "{app}\ui"

[Icons]
Name: "{autoprograms}\{cm:UiName}"; Filename: "{app}\ui\{#UiExe}"; WorkingDir: "{app}\ui"
Name: "{autodesktop}\{cm:UiName}"; Filename: "{app}\ui\{#UiExe}"; WorkingDir: "{app}\ui"; Tasks: desktopicon
Name: "{commonstartup}\{cm:UiName}"; Filename: "{app}\ui\{#UiExe}"; Parameters: "--minimized"; WorkingDir: "{app}\ui"; Tasks: autostart

[Run]
Filename: "{app}\ui\{#UiExe}"; Description: "{cm:LaunchUi}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#UiExe}"; Flags: runhidden; RunOnceId: "StopUi"
Filename: "{app}\{#ServiceExe}"; Parameters: "remove-service"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveService"

[Code]
var
  ServerPage: TInputQueryWizardPage;

function ExistingConfig(): Boolean;
begin
  Result := FileExists(ExpandConstant('{commonappdata}\OpenEOS Printer Agent\config.yaml'));
end;

function ServerUrl(): String;
begin
  Result := ExpandConstant('{param:SERVER|}');
  if (Result = '') and (ServerPage <> nil) then
    Result := Trim(ServerPage.Values[0]);
  if Result = '' then
    Result := '{#DefaultServer}';
end;

procedure InitializeWizard();
begin
  ServerPage := CreateInputQueryPage(wpSelectTasks, CustomMessage('ServerTitle'),
    CustomMessage('ServerDesc'), CustomMessage('ServerText'));
  ServerPage.Add(CustomMessage('ServerLabel'), False);
  ServerPage.Values[0] := ExpandConstant('{param:SERVER|}');
  if ServerPage.Values[0] = '' then
    ServerPage.Values[0] := '{#DefaultServer}';
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  // Update: the service keeps its config.yaml (server, pairing) - nothing to ask.
  Result := (ServerPage <> nil) and (PageID = ServerPage.ID) and ExistingConfig();
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  // Files of a running service / window cannot be replaced.
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#UiExe}', '', SW_HIDE, ewWaitUntilTerminated, Code);
  if Exec(ExpandConstant('{sys}\sc.exe'), 'stop {#ServiceName}', '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0) then
    Sleep(4000);
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
  Params: String;
begin
  if CurStep = ssPostInstall then
  begin
    WizardForm.StatusLabel.Caption := CustomMessage('InstallingService');
    // Writes config.yaml only when there is none yet (updates keep theirs),
    // (re)creates the service, sets crash restarts and the user ACL, starts it.
    Params := 'install-service --server-url "' + ServerUrl() + '"';
    if not Exec(ExpandConstant('{app}\{#ServiceExe}'), Params, ExpandConstant('{app}'),
                SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
      // (no line may start with "[" in [Code] - Inno reads it as a section)
      SuppressibleMsgBox(FmtMessage(CustomMessage('ServiceFailed'), [IntToStr(Code),
        ExpandConstant('{commonappdata}\OpenEOS Printer Agent\logs\agent.log')]), mbError, MB_OK, IDOK);
  end;
end;
