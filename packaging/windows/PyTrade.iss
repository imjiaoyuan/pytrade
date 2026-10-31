#ifndef SourceDir
  #error SourceDir must point to a validated standalone directory
#endif

[Setup]
AppId={{25D2187D-27BA-41AB-ACF8-D83804DF6B50}
AppName=PyTrade Preview
AppVersion=0.1.0rc1
DefaultDirName={localappdata}\Programs\PyTrade
DefaultGroupName=PyTrade
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
OutputBaseFilename=PyTrade-0.1.0rc1-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\PyTrade.exe

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\PyTrade"; Filename: "{app}\PyTrade.exe"

[Run]
Filename: "{app}\PyTrade.exe"; Description: "Launch PyTrade Preview"; Flags: nowait postinstall skipifsilent
