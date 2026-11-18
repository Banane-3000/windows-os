@echo off
chcp 65001 >nul
title Installateur Windows-OS
cd /d "%~dp0"
rem Double-clic : lance l installateur Windows-OS et ouvre la page dans le navigateur.
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY goto nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul || goto nopython
%PY% -c "import libusb_package" >nul 2>nul
if errorlevel 1 (
  echo Installation de libusb pour Python...
  %PY% -m pip install --user --quiet libusb-package
)
%PY% server.py %*
pause
exit /b 0
:nopython
echo Python 3 est introuvable.
echo Installe-le depuis https://www.python.org/downloads/
echo et coche "Add python.exe to PATH" pendant l installation, puis relance ce fichier.
pause
exit /b 1
