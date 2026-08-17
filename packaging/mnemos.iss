; Mnemos Windows installer (Inno Setup).
; Build after PyInstaller onedir:
;   iscc packaging/mnemos.iss
;
; Uninstall offers to delete data/ (default unchecked).

#define MyAppName "Mnemos"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "Mnemos"
#define MyAppExeName "Mnemos.exe"

[Setup]
AppId={{A7C4E2B1-9F3D-4C18-8E55-MNEMOSV1WIN}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=MnemosSetup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=lowest

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\Mnemos\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Only the app dir is removed by default. data/ lives under {userappdata}\Mnemos
; unless the user checks the extra page below.

[Code]
var
  DataPage: TInputOptionWizardPage;

procedure InitializeWizard;
begin
  DataPage := CreateInputOptionPage(wpSelectTasks,
    'Your memory', 'Keep local memory on uninstall?',
    'Mnemos stores meetings, people, and audio under a local data folder. Deleting it erases that memory. Leave this unchecked unless you want a full wipe.',
    False, False);
  DataPage.Add('Also delete my Mnemos memory (data folder)');
  DataPage.Values[0] := False;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if Assigned(DataPage) and DataPage.Values[0] then
    begin
      DataDir := ExpandConstant('{userappdata}\Mnemos');
      DelTree(DataDir, True, True, True);
    end;
  end;
end;
