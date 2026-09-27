@echo off
rem ===================================================================
rem  build.bat - buduje DustMineBot.exe (PyInstaller) i instalator
rem  DustMineBot-Setup.exe (Inno Setup 6). Uruchom dwuklikiem w folderze
rem  projektu. Wymaga: Python 3.10+ (python.org, z "Add to PATH"),
rem  opcjonalnie Inno Setup 6 (jrsoftware.org) dla instalatora.
rem ===================================================================
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    where python >nul 2>nul
    if errorlevel 1 (
        echo [BLAD] Nie znaleziono Pythona. Zainstaluj Python 3.11 lub 3.12 z python.org
        echo        i zaznacz "Add python.exe to PATH".
        pause
        exit /b 1
    )
    set PY=python
) else (
    set PY=py -3
)

if not exist .venv (
    echo [1/4] Tworze srodowisko .venv ...
    %PY% -m venv .venv || goto :error
)
call .venv\Scripts\activate.bat || goto :error

echo [2/4] Instaluje zaleznosci ...
python -m pip install --upgrade pip >nul
pip install -r requirements.txt pyinstaller || goto :error

if not exist vendor\adb\adb.exe (
    echo [UWAGA] Brak vendor\adb\adb.exe - instalator nie bedzie mial wlasnego ADB
    echo         ^(bot uzyje adb z PATH, np. z LDPlayera^). Patrz BUILD_INSTRUCTIONS.md.
)

echo [3/4] Buduje aplikacje (PyInstaller) ...
pyinstaller --noconfirm --clean dustbot_gui.spec || goto :error
if not exist dist\DustMineBot\DustMineBot.exe goto :error

set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC (
    echo.
    echo Gotowa aplikacja: dist\DustMineBot\DustMineBot.exe
    echo Nie znaleziono Inno Setup 6 - zainstaluj go z https://jrsoftware.org/isdl.php
    echo i uruchom build.bat ponownie, zeby zbudowac instalator.
    pause
    exit /b 0
)

echo [4/4] Buduje instalator (Inno Setup) ...
"%ISCC%" installer.iss || goto :error

echo.
echo GOTOWE: installer_output\DustMineBot-Setup.exe
pause
exit /b 0

:error
echo.
echo [BLAD] Budowanie nie powiodlo sie - zobacz komunikaty powyzej.
pause
exit /b 1
