; Inno Setup script for Summit. Run build_windows.bat first (it creates dist\Summit,
; and compiles this file for you if Inno Setup 6 is installed).
; Or open this file in Inno Setup and press Compile (F9) -> Output\SummitSetup.exe

#define AppName "Summit"
#define AppVersion "1.2"
#define AppPublisher "Nat"

[Setup]
; Keep this GUID the same in every version so a new setup upgrades the old install.
AppId={{B7A1D2C4-5E3F-4A86-9C21-5A1B2C3D4E5F}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
; Installs for the current user only (no admin needed). The app writes its
; downloads, log, settings, API key and the XTTS model files (xtts_data, about
; 1.8 GB) next to itself, so it must NOT go in Program Files.
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=SummitSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; close a running Summit during an upgrade instead of failing on locked files
CloseApplications=yes
RestartApplications=no
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\Summit.exe
#ifexist "summit.ico"
SetupIconFile=summit.ico
#endif

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
; Everything build_windows.bat put in dist\Summit: Summit.exe, the _internal folder,
; xtts_worker.py, the icons, tomas_Nikl.mp3, ffmpeg.exe and aria2c.exe (when present).
Source: "dist\Summit\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\Summit.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\Summit.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Summit.exe"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Files the app creates while running. The saved Gemini key and settings are removed
; on uninstall; downloads and the xtts_data model folder are left on purpose.
Type: files; Name: "{app}\settings.json"
Type: files; Name: "{app}\gemini_key.txt"
Type: files; Name: "{app}\last_run_log.txt"
