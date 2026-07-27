@echo off
setlocal

cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
	set "PYTHON_EXE=.venv\Scripts\python.exe"
) else (
	where py >nul 2>nul
	if %errorlevel%==0 (
		set "PYTHON_EXE=py -3"
	) else (
		where python >nul 2>nul
		if %errorlevel%==0 (
			set "PYTHON_EXE=python"
		) else (
			echo Python bulunamadi.
			echo.
			echo Bu uygulama icin sunlardan biri gerekli:
			echo 1. Hazir .exe surumu
			echo 2. Python 3 ve gerekli kutuphaneler
			echo.
			pause
			exit /b 1
		)
	)
)

%PYTHON_EXE% -c "import serial, win32ui" >nul 2>nul
if not %errorlevel%==0 (
	echo Gerekli Python kutuphaneleri eksik.
	echo.
	echo Lutfen once su komutu calistirin:
	echo pip install -r requirements.txt
	echo.
	pause
	exit /b 1
)

start "Kalite Operator" %PYTHON_EXE% app.py
exit /b 0