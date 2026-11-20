#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6C1E5D0A-3B7F-4E55-9A41-5A0C0F2B7D11}
AppName=SonarLite
AppVersion={#AppVersion}
DefaultDirName={autopf}\SonarLite
DefaultGroupName=SonarLite
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=SonarLite-Setup
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={app}\SonarLite.exe

[Files]
Source: "dist\SonarLite.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\SonarLite"; Filename: "{app}\SonarLite.exe"
Name: "{autodesktop}\SonarLite"; Filename: "{app}\SonarLite.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"

[Run]
Filename: "{app}\SonarLite.exe"; Description: "Launch SonarLite"; Flags: nowait postinstall skipifsilent
