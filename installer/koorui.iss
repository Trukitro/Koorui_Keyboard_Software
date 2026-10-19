; Instalador de Koorui Keyboard (Inno Setup 6).
; Compilar con:  iscc installer\koorui.iss   (o /DAppVersion=x.y.z para otra version)
; Requiere haber generado antes dist\KooruiKeyboard.exe con PyInstaller.

#define AppName "Koorui Keyboard"
#ifndef AppVersion
  #define AppVersion "1.7.0"
#endif
#define AppExe "KooruiKeyboard.exe"

[Setup]
AppId={{8E5B6C1A-3F2D-4B7E-9A10-2C4D6E8F0A12}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Rikion
AppPublisherURL=https://github.com/Trukitro/Koorui_Keyboard_Software
DefaultDirName={localappdata}\Programs\KooruiKeyboard
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=KooruiKeyboardSetup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
spanish.AutoStart=Iniciar con Windows
english.AutoStart=Start with Windows

[Tasks]
Name: "autostart"; Description: "{cm:AutoStart}"

[Files]
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"

[Registry]
; Mismo valor que escribe la opcion "Iniciar con Windows" de la app.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
    ValueName: "KooruiBattery"; ValueData: """{app}\{#AppExe}"""; \
    Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; \
    Flags: nowait postinstall skipifsilent
; Actualizacion desde la app (instalacion silenciosa con /AUTOUPDATE): volver a abrirla.
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: IsAutoUpdate

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#AppExe}"; Flags: runhidden; \
    RunOnceId: "CloseApp"

[Code]
function IsAutoUpdate: Boolean;
begin
  Result := Pos('/AUTOUPDATE', UpperCase(GetCmdTail)) > 0;
end;

// Cierra la app si esta abierta para poder reemplazar el ejecutable.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
  Result := '';
end;
