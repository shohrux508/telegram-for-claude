; Inno Setup script for Telegram for Claude. Built by packaging/build.ps1, which
; passes AppVersion and SourceDir on the command line.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\TelegramForClaude"
#endif

[Setup]
AppId={{6B0C3F1E-8E2A-4C1B-9B7D-2F4A5E6C7D81}
AppName=Telegram для Claude
AppVersion={#AppVersion}
AppPublisher=Telegram для Claude
DefaultDirName={localappdata}\Programs\TelegramForClaude
DefaultGroupName=Telegram для Claude
DisableProgramGroupPage=yes
; Per-user install: no administrator password needed.
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=TelegramForClaude-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Claude keeps the server running; closing it lets the update replace the files.
CloseApplications=yes
RestartApplications=no
UninstallDisplayName=Telegram для Claude
UninstallDisplayIcon={app}\TelegramForClaude.exe

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Значок на рабочем столе"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Telegram для Claude — настройки"; Filename: "{app}\TelegramForClaude.exe"; Parameters: "settings"
Name: "{userdesktop}\Telegram для Claude"; Filename: "{app}\TelegramForClaude.exe"; Parameters: "settings"; Tasks: desktopicon

[Run]
Filename: "{app}\TelegramForClaude.exe"; Parameters: "settings"; Description: "Открыть настройки"; Flags: nowait postinstall skipifsilent

[Messages]
ru.FinishedLabel=Программа установлена. Сейчас откроются настройки в браузере: войдите в Telegram и нажмите «Подключить».
