@echo off
rem ===================================================================
rem  build_installer.bat - buduje instalator Dust Mine Bot jednym klikiem
rem
rem  Uruchom dwuklikiem w folderze projektu. Skrypt sam:
rem    1. tworzy srodowisko .venv i instaluje zaleznosci + PyInstaller,
rem    2. pobiera ADB (Android Platform Tools) do vendor\adb, jesli brak,
rem    3. buduje dist\DustMineBot\DustMineBot.exe (PyInstaller),
rem    4. instaluje Inno Setup 6 przez winget, jesli brak,
rem    5. buduje installer_output\DustMineBot-Setup.exe (wersja z version.py).
rem
rem  Wymaga: Windows 10/11, Python 3.10-3.13 z python.org ("Add to PATH"),
rem  internet przy pierwszym uruchomieniu.
rem ===================================================================
setlocal
cd /d "%~dp0"
title Dust Mine Bot - budowanie instalatora

rem ---- wersja z version.py (APP_VERSION = "x.y.z") ----
set "VER=0.0.0"
for /f "tokens=2 delims==" %%v in ('findstr /b /c:"APP_VERSION" version.py') do set "VER=%%v"
set "VER=%VER: =%"
set "VER=%VER:"=%"
echo Dust Mine Bot %VER%
echo.

rem ---- dzialajacy bot blokuje pliki w dist\ ----
tasklist /fi "imagename eq DustMineBot.exe" 2>nul | find /i "DustMineBot.exe" >nul
if not errorlevel 1 (
    echo [UWAGA] DustMineBot.exe jest uruchomiony - zamknij go i nacisnij dowolny klawisz.
    pause >nul
)

rem ---- Python ----
where py >nul 2>nul
if not errorlevel 1 (
    set "PY=py -3"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo [BLAD] Nie znaleziono Pythona. Zainstaluj Python 3.12 z python.org
        echo        i zaznacz "Add python.exe to PATH".
        pause
        exit /b 1
    )
    set "PY=python"
)

if not exist .venv\Scripts\activate.bat (
    echo [1/5] Tworze srodowisko .venv ...
    %PY% -m venv .venv || goto :error
)
call .venv\Scripts\activate.bat || goto :error

echo [1/5] Instaluje zaleznosci ...
python -m pip install --upgrade pip >nul
pip install -r requirements.txt pyinstaller || goto :error

rem ---- serwer scrcpy (obraz z emulatora) jest w repo ----
if not exist vendor\scrcpy\scrcpy-server (
    echo [BLAD] Brak vendor\scrcpy\scrcpy-server - pobierz projekt ponownie ^(git pull^).
    goto :error
)

rem ---- ADB ----
if exist vendor\adb\adb.exe goto :adb_ok
echo [2/5] Pobieram ADB ^(Android Platform Tools^) ...
set "PT_ZIP=%TEMP%\dmb-platform-tools.zip"
set "PT_DIR=%TEMP%\dmb-platform-tools"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri 'https://dl.google.com/android/repository/platform-tools-latest-windows.zip' -OutFile $env:PT_ZIP; if (Test-Path $env:PT_DIR) { Remove-Item -Recurse -Force $env:PT_DIR }; Expand-Archive -Force $env:PT_ZIP $env:PT_DIR"
if errorlevel 1 (
    echo [UWAGA] Nie udalo sie pobrac ADB - instalator nie bedzie mial wlasnego ADB
    echo         ^(bot uzyje adb z PATH, np. z LDPlayera^).
    goto :adb_done
)
if not exist vendor\adb mkdir vendor\adb
copy /y "%PT_DIR%\platform-tools\adb.exe" vendor\adb\ >nul
copy /y "%PT_DIR%\platform-tools\AdbWinApi.dll" vendor\adb\ >nul
copy /y "%PT_DIR%\platform-tools\AdbWinUsbApi.dll" vendor\adb\ >nul
rmdir /s /q "%PT_DIR%" 2>nul
del "%PT_ZIP%" 2>nul
:adb_ok
echo [2/5] ADB: vendor\adb\adb.exe
:adb_done

echo [3/5] Buduje aplikacje ^(PyInstaller^) ...
pyinstaller --noconfirm --clean dustbot_gui.spec || goto :error
if not exist dist\DustMineBot\DustMineBot.exe goto :error

rem ---- Inno Setup 6 ----
call :find_iscc
if defined ISCC goto :iscc_ok
echo [4/5] Instaluje Inno Setup 6 ^(winget^) ...
where winget >nul 2>nul
if errorlevel 1 goto :no_iscc
winget install --id JRSoftware.InnoSetup -e --silent --accept-package-agreements --accept-source-agreements
call :find_iscc
if not defined ISCC goto :no_iscc
:iscc_ok
echo [4/5] Inno Setup: %ISCC%

echo [5/5] Buduje instalator ...
"%ISCC%" /Q /DMyAppVersion=%VER% installer.iss || goto :error
if not exist installer_output\DustMineBot-Setup.exe goto :error

echo.
echo ===================================================================
echo  GOTOWE: installer_output\DustMineBot-Setup.exe  ^(wersja %VER%^)
echo ===================================================================
explorer installer_output
pause
exit /b 0

:find_iscc
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
exit /b 0

:no_iscc
echo.
echo Gotowa aplikacja ^(bez instalatora^): dist\DustMineBot\DustMineBot.exe
echo Nie znaleziono Inno Setup 6 - zainstaluj go z https://jrsoftware.org/isdl.php
echo i uruchom build_installer.bat ponownie.
pause
exit /b 0

:error
echo.
echo [BLAD] Budowanie nie powiodlo sie - zobacz komunikaty powyzej.
pause
exit /b 1
