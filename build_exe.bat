@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo .venv bulunamadi. Once sanal ortam ve bagimliliklari hazirlayin.
    echo Ornek:
    echo py -3 -m venv .venv
    echo .venv\Scripts\python -m pip install -r requirements.txt pyinstaller
    pause
    exit /b 1
)

set "PYTHON_EXE=.venv\Scripts\python.exe"

%PYTHON_EXE% -m pip install -r requirements.txt pyinstaller
if not %errorlevel%==0 (
    echo Bagimlilik veya PyInstaller kurulumu basarisiz.
    pause
    exit /b 1
)

%PYTHON_EXE% -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --windowed ^
  --onefile ^
  --name KaliteOperator ^
  --add-data "assets;assets" ^
  --hidden-import win32ui ^
  --hidden-import win32con ^
  --hidden-import win32print ^
  --collect-all barcode ^
  --collect-all PIL ^
  app.py

if not %errorlevel%==0 (
    echo EXE olusturma basarisiz.
    pause
    exit /b 1
)

echo.
echo Hazir cikti: dist\KaliteOperator.exe
pause
