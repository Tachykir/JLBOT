; installer.iss
; ==============
; Skrypt Inno Setup budujący instalator Windows (installer.exe) dla
; Dust Mine Bot. Tworzy kreator instalacyjny ze skrótem w Menu Start,
; opcjonalnym skrótem na Pulpicie, wpisem w "Dodaj/Usuń programy" oraz
; odinstalowywaczem.
;
; WYMAGANIA PRZED URUCHOMIENIEM TEGO SKRYPTU:
;   1. Zainstaluj Inno Setup (darmowy): https://jrsoftware.org/isdl.php
;   2. Zbuduj najpierw aplikację przez PyInstaller:
;        pyinstaller dustbot_gui.spec
;      To musi utworzyć folder `dist\DustMineBot\` z DustMineBot.exe
;      w środku - ten skrypt pakuje właśnie tę zawartość.
;
; BUDOWANIE INSTALATORA:
;   Otwórz ten plik w Inno Setup Compiler (albo z linii poleceń:
;   `ISCC.exe installer.iss`) i skompiluj. Wynik trafi do `installer_output\
;   DustMineBot-Setup.exe` - to jest gotowy, pojedynczy plik instalacyjny
;   do rozesłania.

#define MyAppName "Dust Mine Bot"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "Twoja nazwa / pseudonim"
#define MyAppExeName "DustMineBot.exe"
#define MyBuildDir "dist\DustMineBot"

[Setup]
; Unikalny identyfikator aplikacji - wygenerowany raz, nie zmieniaj go
; między wersjami (Inno Setup używa go do rozpoznawania aktualizacji
; vs. świeżej instalacji).
AppId={{8F1E9C1A-6B2D-4B7A-9E3F-2C7D5A1B9E44}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Instalacja per-użytkownik nie wymaga uprawnień administratora -
; wygodniejsze przy prostych aplikacjach jak ta. Zmień na "admin",
; jeśli wolisz instalację systemową dla wszystkich użytkowników.
PrivilegesRequired=lowest
OutputDir=installer_output
OutputBaseFilename=DustMineBot-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Odkomentuj i podmień poniższą linię, jeśli masz plik ikony .ico
; SetupIconFile=app_icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "polish"; MessagesFile: "compiler:Languages\Polish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Kopiuje CAŁĄ zawartość folderu zbudowanego przez PyInstaller
; (DustMineBot.exe + _internal\ + vendor\adb\...) do folderu instalacji.
Source: "{#MyBuildDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; Dołącz przykładowy config i README, żeby użytkownik miał je pod ręką
; od razu po instalacji (obok właściwej aplikacji, nie wewnątrz _internal).
Source: "config.example.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Opcja "uruchom po zakończeniu instalacji" na ostatnim ekranie kreatora.
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Sprzątnij pliki configu/logów, które aplikacja mogła stworzyć podczas
; działania w folderze instalacji (jeśli GUI zapisuje tam config.json
; użytkownika) - dostosuj ścieżkę, jeśli config trafia gdzie indziej
; (np. %APPDATA%).
Type: filesandordirs; Name: "{app}\config.json"
Type: filesandordirs; Name: "{app}\unknown_screens"
