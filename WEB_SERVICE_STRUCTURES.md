# Kalite Operator Web Service Structures

Bu dokuman, uygulamanin kullandigi web servisler icin beklenen istek ve yanit yapilarini ozetler.

## Genel Notlar

- Tum JSON isteklerinde `Content-Type: application/json` kullanilir.
- Uygulama tarafinda URL alanlari `Ayarlar` ekranindan verilir.
- Bir URL bos birakilirsa uygulama o servisi cagirmak yerine yerel test/fallback davranisina gecer.
- Tum tarih saatler uygulama tarafinda ISO formatinda uretilir: `YYYY-MM-DDTHH:MM:SS`

## 1. Login Servisi

Amaç: Operator sifresi ile kullaniciyi dogrulamak.

### Request

- Method: `POST`
- URL: `Login URL`
- Body:

```json
{
  "password": "operator123"
}
```

### Required Response

Asagidaki alanlar zorunludur:

```json
{
  "name": "Ahmet",
  "surname": "Yilmaz",
  "userid": "42",
  "userrole": "admin"
}
```

### Response Field Rules

- `name`: Kullanici adi
- `surname`: Kullanici soyadi
- `userid`: Tekil kullanici kimligi
- `userrole`: En azindan `admin` veya `user` olmasi beklenir

### Role Behavior

- `user`: Sadece operator ekranini gorur
- `admin`: Operator ekranina ek olarak `Ayarlar` ve `Debug/Test` ekranlarini gorur

## 2. Parti Barkod Lookup Servisi

Amaç: Refakat kart barkodundan parti bilgilerini getirmek.

### Request

- Method: `GET`
- URL format:

```text
{Barkod Servisi}?barcode={OKUTULAN_BARKOD}
```

Ornek:

```text
http://server/api/party-lookup?barcode=ABC123456
```

### Accepted Response

Uygulama asagidaki alanlardan ilk buldugunu kullanir.

```json
{
  "barcode": "ABC123456",
  "customer": "Ornek Musteri",
  "party_no": "PARTI-2026-001",
  "party_id": "1001",
  "sarj_no": "SARJ-88",
  "kalite": "30/1 PENYE",
  "kalite_talimati": "Top basi kontrol edilecek, boya lekesi icin gozle muayene yapilacak.",
  "renk": "Lacivert"
}
```

### Accepted Alternative Field Names

#### Musteri

Bu alanlardan biri kabul edilir:

- `customer`
- `musteri`
- `customer_name`
- `cari_unvan`

#### Parti No

Bu alanlardan biri kabul edilir:

- `party_no`
- `parti_no`
- `batch_no`
- `lot_no`

#### Parti ID

Bu alanlardan biri kabul edilir:

- `party_id`
- `parti_id`
- `batch_id`
- `id`

#### Sarj No

Bu alanlardan biri kabul edilir:

- `sarj_no`
- `sarj`
- `charge_no`
- `seri_no`

#### Kalite

Bu alanlardan biri kabul edilir:

- `kalite`
- `quality`
- `quality_name`
- `kalite_adi`

#### Kalite Talimati

Bu alanlardan biri kabul edilir:

- `kalite_talimati`
- `quality_instruction`
- `quality_instructions`
- `quality_note`
- `talimat`
- `instruction`

#### Renk

Bu alanlardan biri kabul edilir:

- `renk`
- `color`
- `colour`
- `color_name`
- `renk_adi`

### Minimum Practical Response

En azindan bunlarin dolu olmasi tavsiye edilir:

```json
{
  "customer": "Ornek Musteri",
  "party_no": "PARTI-2026-001",
  "party_id": "1001",
  "sarj_no": "SARJ-88",
  "kalite": "30/1 PENYE",
  "kalite_talimati": "Top basi kontrol edilecek, boya lekesi icin gozle muayene yapilacak.",
  "renk": "Lacivert"
}
```

## 3. Kaydet Servisi

Amaç: Operatorun olctugu top bilgisini veritabanina kaydetmek.

### Request

- Method: `POST`
- URL: `Kayit Servisi`
- Body:

```json
{
  "barcode": "ABC123456",
  "customer": "Ornek Musteri",
  "party_no": "PARTI-2026-001",
  "party_id": "1001",
  "sarj_no": "SARJ-88",
  "kalite": "30/1 PENYE",
  "kalite_talimati": "Top basi kontrol edilecek, boya lekesi icin gozle muayene yapilacak.",
  "renk": "Lacivert",
  "error_category": "BOYAHANE HATA KODLARI",
  "error_code": "7",
  "error_description": "BOYA LEKESI",
  "operator_name": "Ahmet",
  "operator_surname": "Yilmaz",
  "operator_userid": "42",
  "operator_role": "user",
  "meter": 18.75,
  "kg": 12.4,
  "notes": "Operatorden gelen not",
  "last_measurement": "18.75 m / 12.40 kg",
  "saved_at": "2026-07-10T14:22:10"
}
```

### Request Field Notes

- `barcode`: Refakat barkodu
- `customer`: Musteri bilgisi
- `party_no`: Parti numarasi
- `party_id`: Parti kimligi
- `sarj_no`: Sarj numarasi
- `kalite`: Kalite bilgisi
- `kalite_talimati`: Party lookup servisinden gelen kalite kontrol veya is talimati
- `renk`: Renk bilgisi
- `error_category`: Hata kodu grubu, bos olabilir
- `error_code`: Secilen hata kodu, bos olabilir
- `error_description`: Secilen hata aciklamasi, bos olabilir
- `operator_name`: Login cevabindan gelir
- `operator_surname`: Login cevabindan gelir
- `operator_userid`: Login cevabindan gelir
- `operator_role`: Login cevabindan gelir
- `meter`: Toplam metre, `float`
- `kg`: Toplam kg, `float`
- `notes`: Operator notu
- `last_measurement`: Ekranda gosterilen son metin
- `saved_at`: Kayit zamani

### Save Response

Uygulama save response icinde body olmasa da calisir.

Bos response kabul edilebilir:

```json
{}
```

Ama barkod basiminda kullanilmak uzere asagidaki alanlardan biri donulurse uygulama bunu etikette kullanir:

```json
{
  "barcode": "URETILMIS-YENI-BARKOD"
}
```

Alternatif kabul edilen alanlar:

- `barcode`
- `label_barcode`
- `etiket_barkod`
- `generated_barcode`

### Recommended Save Response

```json
{
  "success": true,
  "record_id": 98765,
  "barcode": "URETILMIS-YENI-BARKOD"
}
```

## 4. Health Check Servisi

Amaç: Operator ekraninin footer alaninda web servis baglanti durumunu gostermek.

### Request

- Method: `GET`
- URL kaynaklari:

1. Eger `Health URL` ayarlarda doluysa direkt o kullanilir.
2. Eger bos ise uygulama sirasiyla su alanlardan birini baz alip `/health` turetir:
   - `Kayit Servisi`
   - `Barkod Servisi`
   - `Login URL`

Ornek:

```text
http://server/api/health
```

### Response

- Body icerigi uygulama tarafinda kullanilmaz.
- Sadece HTTP status code kontrol edilir.
- `2xx` durumlar `WEB: BAGLI` kabul edilir.
- `2xx` disindaki veya timeout/connection hatalari `WEB: ULASILAMIYOR` kabul edilir.

Ornek response:

```json
{
  "status": "ok"
}
```

## 5. Yerel Test Modu Davranislari

URL alanlari bos birakilirsa uygulama su sekilde davranir:

### Login URL bos ise

- `operator123` sifresi ile `user` login olur
- `admin123` sifresi ile `admin` login olur

### Barkod Servisi bos ise

Asagidaki test verisi yuklenir:

```json
{
  "barcode": "OKUTULAN_BARKOD",
  "customer": "Test Musteri",
  "party_no": "TEST-PARTI-001",
  "party_id": "1001"
}
```

### Kayit Servisi bos ise

- Veri `operator_records.json` dosyasina yazilir
- Yazdirma gerekiyorsa yerel payload ile baski tetiklenir

## 6. Onerilen Tam Uygulama Ornegi

### Login Response

```json
{
  "name": "Ahmet",
  "surname": "Yilmaz",
  "userid": "42",
  "userrole": "admin"
}
```

### Barcode Lookup Response

```json
{
  "barcode": "ABC123456",
  "customer": "Ornek Musteri",
  "party_no": "PARTI-2026-001",
  "party_id": "1001",
  "sarj_no": "SARJ-88",
  "kalite": "30/1 PENYE",
  "renk": "Lacivert"
}
```

### Save Response

```json
{
  "success": true,
  "record_id": 98765,
  "barcode": "ETIKET-2026-000045"
}
```

### Health Response

```json
{
  "status": "ok",
  "service": "kalite-operator-api"
}
```
