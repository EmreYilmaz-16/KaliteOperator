# Fabric Counter

Simple desktop Python app for reading data from a serial port and tracking fabric measurements in meters and kilograms.

## Features

- Lists available COM ports and connects with a selectable baud rate
- Reads live serial lines from machines, scales, or counters
- Detects `kg` and `m` values from incoming text
- Falls back to a selected default unit when raw data only contains a number
- Keeps running totals for meters and kilograms
- Includes a built-in demo stream for testing without any hardware
- Accepts manual test lines to verify how a device format will be parsed
- Shows diagnostics for parsed and ignored lines
- Includes an operator screen for barcode lookup and save payload preparation
- Exports the current session to CSV

## Expected Serial Data

The app accepts lines such as:

- `KG: 12.40`
- `M: 18.75`
- `12.40 kg`
- `18.75 metre`
- `42.10`

If the incoming line only contains a number, the app stores it using the selected default unit.

## Setup

1. Install Python 3.13 or later.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Start the app:

```bash
python app.py
```

## Customer Delivery

`python app.py` yaklaşımı son kullanıcı için garanti değildir. Hedef makinede doğru Python sürümü, `pyserial`, `pywin32` ve uygun çalışma klasörü gerekir.

Bu proje için iki çalıştırma yolu vardır:

1. `runapp.bat`
2. `dist/KaliteOperator/KaliteOperator.exe`

### runapp.bat

- Uygulamayı proje klasöründen başlatır.
- Önce `.venv\Scripts\python.exe` arar.
- Yoksa sırasıyla `py -3` ve `python` dener.
- Gerekli modüller eksikse kullanıcıya bilgi verir.

Bu yöntem daha çok iç kullanım ve destek ekibi içindir.

### Recommended: Build a Single EXE

Müşteriye terminal açtırmamak için önerilen yöntem tek dosya/paket `.exe` dağıtımıdır.

Hazırlık:

```bash
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt pyinstaller
```

Derleme:

```bash
build_exe.bat
```

Çıktı:

```text
dist\KaliteOperator.exe
```

Bu paket `assets` klasörünü de içerir; login ekranı logoları bu sayede çalışır.

### Customer Install

Derlemeden sonra müşteri kurulumu için:

```bash
install_customer.bat
```

Bu script:

- `dist\KaliteOperator.exe` dosyasını `%LOCALAPPDATA%\PBS\KaliteOperator` altına kopyalar
- `app_settings.json` ve `error_code_groups.json` dosyalarını aynı klasöre taşır
- yoksa boş `operator_records.json` oluşturur
- masaüstüne `Kalite Operator` kısayolu bırakır

Not: Uygulama paketli çalışırken ayar, log ve hata kodu dosyalarını exe'nin bulunduğu klasörde tutar. Bu sayede PyInstaller `onefile` modunda veri kaybı yaşanmaz.

## Terminal Test Without a Physical Device

1. Start the mock sender in a terminal:

```bash
python mock_scale_server.py
```

2. In the app, type `socket://127.0.0.1:7001` into the Port field.
3. Click `Connect`.
4. The app should begin receiving repeating sample lines and update meter/kg totals.

## Built-In Demo Test

1. Open the app.
2. Click `Start Demo`.
3. Watch the totals and diagnostics update without opening any extra terminal.
4. Click `Stop Demo` to end the built-in test stream.

## Manual Device Format Test

- Paste a raw device line into `Manual Test Line`.
- Click `Send Test Line`.
- Check the diagnostics panel to see whether the line was parsed or ignored.

## Operator Screen

- Use the `Operator Screen` tab to scan or type a barcode.
- `Fetch` will call a barcode web service after `BARCODE_LOOKUP_URL` is filled in.
- Returned fields such as customer, party number, and party id are shown on screen.
- `Save` prepares meter, kg, barcode, party information, and notes for a save web service.
- `BARCODE_LOOKUP_URL` and `SAVE_MEASUREMENT_URL` are intentionally blank in `app.py` until real endpoints are provided.

## Notes

- The UI uses `tkinter`, which ships with standard Python on Windows.
- If your device uses a different line format, adjust the `parse_measurement` method in `app.py`.
- The app now accepts both normal COM ports such as `COM4` and URL-style endpoints such as `socket://127.0.0.1:7001`.