; Sparrow Windows installer (Inno Setup).
; Build after PyInstaller onedir:
;   iscc packaging/mnemos.iss
;
;
; Uninstall offers a full wipe (default unchecked). It must clear every
; capture directory, not just data/ — sessions\ and desktop_agent\sessions\
; are created at RUNTIME under {app}, so Inno does not remove them on its
; own and they would outlive the uninstall. Same list as
; app/services/wipe.py; keep the two in step.
;
; The memory lives under {localappdata}\Sparrow, matching app/runtime.py's
; user_data_root(). NOT {userappdata} (Roaming): a memory directory is
; gigabytes of meeting audio, and on a managed network Roaming replicates the
; profile to a file server — which would break the install and quietly copy
; the one thing this product promises never leaves the machine.

#define MyAppName "Sparrow"
; WS-C: the version comes from app/version.py via dist\VERSION.txt, which the
; PyInstaller spec writes. Never edit a version literal here — the installer
; and the app must not be able to drift.
#define MyAppVersion Trim(FileRead(FileOpen("..\dist\VERSION.txt")))
#define MyAppPublisher "Ravenry LLC"
#define MyAppCopyright "Copyright 2026 Ravenry LLC"
#define MyAppExeName "Sparrow.exe"

[Setup]
AppId={{A7C4E2B1-9F3D-4C18-8E55-MNEMOSV1WIN}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppCopyright={#MyAppCopyright}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=SparrowSetup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
SetupIconFile=mnemos.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=lowest

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
; Opt-in: this product captures meetings. Default-on autostart is the wrong
; first impression. HKCU so PrivilegesRequired=lowest still works.
Name: "startupicon"; Description: "Start Sparrow when I log in"; GroupDescription: "Startup"; Flags: unchecked

[Files]
Source: "..\dist\Sparrow\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; OllamaSetup.exe is deliberately not here. Bundling it would require an admin
; prompt (this installer is per-user), inflate the download, and pin a version
; that then cannot auto-update. First launch pulls local models if ollama.exe
; is already on PATH; otherwise the app runs cloud-only.

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Sparrow"; ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Tasks: startupicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Only the app dir is removed by default. The memory lives under
; {localappdata}\Sparrow unless the user checks the extra page below.

[Code]
var
  DataPage: TInputOptionWizardPage;

procedure InitializeWizard;
begin
  DataPage := CreateInputOptionPage(wpSelectTasks,
    'Your memory', 'Keep local memory on uninstall?',
    'Sparrow stores meetings, people, audio and page captures on this machine. Deleting them erases that memory permanently - there is no server copy to restore from. Leave this unchecked if you plan to reinstall.',
    False, False);
  DataPage.Add('Also delete my Sparrow memory, page captures and API key');
  DataPage.Values[0] := False;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if Assigned(DataPage) and DataPage.Values[0] then
    begin
      DelTree(ExpandConstant('{localappdata}\Sparrow'), True, True, True);
      DelTree(ExpandConstant('{localappdata}\Mnemos'), True, True, True);
      DelTree(ExpandConstant('{app}\sessions'), True, True, True);
      DelTree(ExpandConstant('{app}\desktop_agent\sessions'), True, True, True);
      DeleteFile(ExpandConstant('{app}\.credentials.env'));
      DeleteFile(ExpandConstant('{app}\.env'));
      { The app dir is only removed once it is empty, so clear the runtime
        leftovers first and then take the directory itself. }
      DelTree(ExpandConstant('{app}'), True, True, True);
    end;
  end;
end;
