@echo off
setlocal

cd /d "%~dp0"

if not exist "dist\KaliteOperator.exe" (
  echo dist\KaliteOperator.exe bulunamadi.
  echo Once build_exe.bat ile exe olusturun.
  pause
  exit /b 1
)

set "INSTALL_DIR=%LOCALAPPDATA%\PBS\KaliteOperator"
set "TARGET_EXE=%INSTALL_DIR%\KaliteOperator.exe"
set "DESKTOP_SHORTCUT=%USERPROFILE%\Desktop\Kalite Operator.lnk"

if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

copy /y "dist\KaliteOperator.exe" "%TARGET_EXE%" >nul

if exist "app_settings.json" copy /y "app_settings.json" "%INSTALL_DIR%\app_settings.json" >nul
if exist "error_code_groups.json" copy /y "error_code_groups.json" "%INSTALL_DIR%\error_code_groups.json" >nul
if not exist "%INSTALL_DIR%\operator_records.json" echo []>"%INSTALL_DIR%\operator_records.json"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws=New-Object -ComObject WScript.Shell; $sc=$ws.CreateShortcut('%DESKTOP_SHORTCUT%'); $sc.TargetPath='%TARGET_EXE%'; $sc.WorkingDirectory='%INSTALL_DIR%'; $sc.IconLocation='%TARGET_EXE%,0'; $sc.Save()"
if not %errorlevel%==0 (
  echo Masaustu kisayolu olusturulamadi.
  pause
  exit /b 1
)

echo.
echo Kurulum tamamlandi.
echo Uygulama: %TARGET_EXE%
echo Kisayol: %DESKTOP_SHORTCUT%
pause