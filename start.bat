@echo off
rem Calosc w nawiasach: cmd wczytuje blok od razu, wiec git pull moze
rem bezpiecznie podmienic ten plik w trakcie dzialania.
(
  cd /d "%~dp0"
  echo Aktualizacja bota...
  where git >nul 2>nul
  if errorlevel 1 (
    echo Brak gita - uruchamiam bez aktualizacji.
  ) else (
    git pull --ff-only
    if errorlevel 1 echo Nie udalo sie pobrac aktualizacji - uruchamiam obecna wersje.
  )
  python -m pip install -q -r requirements.txt
  start "" pythonw gui.py
)
