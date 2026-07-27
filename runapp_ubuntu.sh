#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 bulunamadi. Ubuntu'da once Python 3 kurun."
  echo "Ornek: sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-tk"
  exit 1
fi

if ! python3 -c "import tkinter" >/dev/null 2>&1; then
  echo "tkinter bulunamadi. Ubuntu'da python3-tk paketi gerekli."
  echo "Ornek: sudo apt-get update && sudo apt-get install -y python3-tk"
  exit 1
fi

if ! python3 -m venv --help >/dev/null 2>&1; then
  echo "python3-venv modulu kullanilamiyor. Ubuntu'da python3-venv paketi gerekli."
  echo "Ornek: sudo apt-get update && sudo apt-get install -y python3-venv"
  exit 1
fi

if [ -d ".venv-linux" ] && [ ! -f ".venv-linux/bin/activate" ]; then
  echo "Bozuk veya yarim kalmis .venv-linux bulundu. Yeniden olusturuluyor..."
  rm -rf .venv-linux
fi

if [ ! -f ".venv-linux/bin/activate" ]; then
  python3 -m venv .venv-linux
fi

if [ ! -f ".venv-linux/bin/activate" ]; then
  echo ".venv-linux olusturulamadi. python3-venv paketini kontrol edin."
  exit 1
fi

source .venv-linux/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "Not: Ubuntu'da Windows GDI tabanli etiket yazdirma desteklenmez."
echo "Uygulama aciliyor..."

python app.py