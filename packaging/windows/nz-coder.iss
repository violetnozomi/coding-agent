#ifndef AppVersion
  #error AppVersion must be defined
#endif
#ifndef SourceDir
  #error SourceDir must be defined
#endif
#ifndef OutputDir
  #error OutputDir must be defined
#endif

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
Name: "startmenu"; Description: "Create a Start Menu shortcut"; Flags: checkedonce

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\NZ-Coder"; Filename: "{app}\nz-coder.exe"; WorkingDir: "{userprofile}"; Tasks: startmenu

[Run]
Filename: "{app}\nz-coder.exe"; Parameters: "--help"; Description: "Verify NZ-Coder installation"; Flags: postinstall nowait skipifsilent unchecked

[Code]
const
  EnvironmentKey = 'Environment';

procedure BroadcastEnvironmentChange();
var
  ResultCode: Integer;
begin
  SendMessageTimeout(HWND_BROADCAST, WM_SETTINGCHANGE, 0,
    CastIntegerToLParam(PChar('Environment')), SMTO_ABORTIFHUNG, 5000, ResultCode);
end;

procedure SetUserPathEntry(AddEntry: Boolean);
var
  CurrentPath: string;
  Entry: string;
  Parts: TArrayOfString;
  Updated: string;
  Index: Integer;
begin
  Entry := ExpandConstant('{app}');
  RegQueryStringValue(HKCU, EnvironmentKey, 'Path', CurrentPath);
  Parts := SplitString(CurrentPath, ';');
  Updated := '';
  for Index := 0 to GetArrayLength(Parts) - 1 do
    if (Parts[Index] <> '') and (CompareText(RemoveQuotes(Parts[Index]), Entry) <> 0) then
      if Updated = '' then Updated := Parts[Index] else Updated := Updated + ';' + Parts[Index];
  if AddEntry then
    if Updated = '' then Updated := Entry else Updated := Updated + ';' + Entry;
  RegWriteExpandStringValue(HKCU, EnvironmentKey, 'Path', Updated);
  BroadcastEnvironmentChange();
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('userpath') then
    SetUserPathEntry(True);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    SetUserPathEntry(False);
end;
