#ifndef AppVersion
  #error AppVersion must be defined
#endif
#ifndef SourceDir
  #error SourceDir must be defined
#endif
#ifndef OutputDir
  #error OutputDir must be defined
#endif

#define StartMenuWorkspace "{userdocs}\NZ-Coder Workspace"

[Setup]
AppId={{5E863449-EE71-4862-9EEB-CB07EE36E6EA}
AppName=NZ-Coder
AppVersion={#AppVersion}
AppPublisher=NZ-Coder
DefaultDirName={localappdata}\Programs\NZ-Coder
DefaultGroupName=NZ-Coder
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=NZ-Coder-{#AppVersion}-windows-x64-setup
Compression=lzma2/max
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
ChangesEnvironment=yes
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\nz-coder.exe
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "userpath"; Description: "Add NZ-Coder to the current user's PATH"; Flags: checkedonce
Name: "startmenu"; Description: "Create a Start Menu shortcut"

[Dirs]
Name: "{#StartMenuWorkspace}"; Tasks: startmenu; Flags: uninsneveruninstall

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\NZ-Coder"; Filename: "{app}\nz-coder.exe"; WorkingDir: "{#StartMenuWorkspace}"; Tasks: startmenu

[Run]
Filename: "{app}\nz-coder.exe"; Parameters: "--help"; Description: "Verify NZ-Coder installation"; Flags: postinstall nowait skipifsilent unchecked

[Code]
const
  EnvironmentKey = 'Environment';

procedure SetUserPathEntry(AddEntry: Boolean);
var
  CurrentPath: string;
  Entry: string;
  Remaining: string;
  Part: string;
  Updated: string;
  Separator: Integer;
begin
  Entry := ExpandConstant('{app}');
  RegQueryStringValue(HKCU, EnvironmentKey, 'Path', CurrentPath);
  Remaining := CurrentPath;
  Updated := '';
  while Remaining <> '' do begin
    Separator := Pos(';', Remaining);
    if Separator = 0 then begin
      Part := Remaining;
      Remaining := '';
    end else begin
      Part := Copy(Remaining, 1, Separator - 1);
      Delete(Remaining, 1, Separator);
    end;
    Part := Trim(Part);
    if (Part <> '') and (CompareText(Part, Entry) <> 0) and
       (CompareText(Part, '"' + Entry + '"') <> 0) then
      if Updated = '' then Updated := Part else Updated := Updated + ';' + Part;
  end;
  if AddEntry then
    if Updated = '' then Updated := Entry else Updated := Updated + ';' + Entry;
  RegWriteExpandStringValue(HKCU, EnvironmentKey, 'Path', Updated);
end;

procedure RepairLegacyStartMenuShortcut;
var
  LinkPath, TargetPath, Arguments, OldDirectory, Workspace: string;
  Shell, Shortcut: Variant;
begin
  { 旧版第二次安装时可能记为未选，却仍保留从用户主目录启动的快捷方式。 }
  LinkPath := ExpandConstant('{group}\NZ-Coder.lnk');
  if not FileExists(LinkPath) then Exit;
  try
    Shell := CreateOleObject('WScript.Shell');
    Shortcut := Shell.CreateShortcut(LinkPath);
    TargetPath := Shortcut.TargetPath;
    Arguments := Shortcut.Arguments;
    OldDirectory := Shortcut.WorkingDirectory;
  except
    Log('Could not inspect the existing Start Menu shortcut: ' + GetExceptionMessage);
    Exit;
  end;
  if (CompareText(TargetPath, ExpandConstant('{app}\nz-coder.exe')) <> 0) or
     (Arguments <> '') or
     (CompareText(RemoveBackslashUnlessRoot(OldDirectory),
                  RemoveBackslashUnlessRoot(ExpandConstant('{%USERPROFILE}'))) <> 0) then Exit;

  Workspace := ExpandConstant('{#StartMenuWorkspace}');
  if not ForceDirectories(Workspace) then
    RaiseException('Could not create the Start Menu workspace at ' + Workspace);
  Shortcut.WorkingDirectory := Workspace;
  Shortcut.Save();
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then begin
    if WizardIsTaskSelected('userpath') then SetUserPathEntry(True);
    if not WizardIsTaskSelected('startmenu') then RepairLegacyStartMenuShortcut;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    SetUserPathEntry(False);
end;
