#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{B7D2F4E1-8C3A-4F6B-9E21-5D7A0C4E9A30}
AppName=Salty's Stream Mixer
AppVersion={#AppVersion}
DefaultDirName={autopf}\SaltysStreamMixer
DefaultGroupName=Salty's Stream Mixer
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=SaltysStreamMixer-Setup
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={app}\SaltysStreamMixer.exe

[Files]
Source: "dist\SaltysStreamMixer.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Salty's Stream Mixer"; Filename: "{app}\SaltysStreamMixer.exe"
Name: "{autodesktop}\Salty's Stream Mixer"; Filename: "{app}\SaltysStreamMixer.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"

[Run]
Filename: "{app}\SaltysStreamMixer.exe"; Description: "Launch Salty's Stream Mixer"; Flags: nowait postinstall skipifsilent
