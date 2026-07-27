import csv
import copy
import json
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import serial
from serial.tools import list_ports

import label_printer


# Seri porttan gelen ham satırdaki sayıyı yakalar.
# 12.40 / 18,75 / -5 / +42.10 gibi formatları destekler.
NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:[.,]\d+)?")

# "Makine sıfırda mı?" kontrolünde kullanılan hassasiyet eşiği.
# Ham değer bu sınırın altındaysa sıfır kabul edilir → yeni partiye geçişe izin verilir.
MACHINE_ZERO_TOLERANCE = 0.01

# Web servis adresleri. Boş bırakılırsa uygulama test/offline modda çalışır.
# Gerçek adresler Ayarlar ekranından girilir ve app_settings.json'a kaydedilir.
LOGIN_URL = ""
BARCODE_LOOKUP_URL = ""
SAVE_MEASUREMENT_URL = ""

# Kaydedilen ölçüm kayıtlarının tutulduğu yerel JSON dosyası (app.py ile aynı klasör).
LOCAL_SAVE_PATH = Path(__file__).with_name("operator_records.json")
ERROR_CODE_SETTINGS_PATH = Path(__file__).with_name("error_code_groups.json")

# Port, baud, URL ve yazıcı adı gibi uygulama ayarlarının saklandığı JSON dosyası.
SETTINGS_PATH = Path(__file__).with_name("app_settings.json")

# Barkod servisi URL'si tanımlı değilken lookup_barcode() tarafından yüklenen test verisi.
# Offline geliştirme ve demo çalışmaları için kullanılır.
DEFAULT_PARTY_DATA = {
    "customer": "Test Musteri",
    "party_no": "TEST-PARTI-001",
    "party_id": "1001",
    "kalite_talimati": "",
}

# Login URL'si yokken test şifresiyle giriş yapıldığında dönen sahte kullanıcı verisi.
# _login_request() içinde offline test modunda kullanılır.
DEFAULT_LOGIN_RESPONSE = {
    "name": "Test",
    "surname": "Operator",
    "userid": "operator-test",
    "userrole": "operator",
}

# Offline test şifreleri. Login URL girilince bu şifreler devre dışı kalır.
# "operator123" → normal operatör rolü, "admin123" → admin rolü (Ayarlar/Debug görünür).
TEST_USER_PASSWORD = "operator123"
TEST_ADMIN_PASSWORD = "admin123"

# Hata kodu seçim penceresindeki butonların kaynağı.
# Her grup bir sütun, her tuple (kod, açıklama) bir buton olur.
# Yeni hata kodu eklemek için ilgili listeye satır eklemek yeterlidir.
ERROR_CODE_GROUPS = {
    "BOYAHANE HATA KODLARI": [
        ("1", "AMBRAJ"),
        ("2", "KIRIK"),
        ("3", "SURTME"),
        ("4", "TUYLENME"),
        ("5", "METAL SURTME"),
        ("6", "DELIK-YIRTIK"),
        ("7", "BOYA LEKESI"),
        ("8", "SUZME"),
        ("9", "SU LEKESI"),
        ("10", "KIMYEVI LEKESI"),
    ],
    "DOKUMA HATA KODLARI": [
        ("51", "BANT"),
        ("52", "COZGUDEN IZ"),
        ("53", "ATKIDAN IZ"),
        ("54", "DOK. YAG IZ"),
        ("55", "DOK. IP CEKMESI"),
        ("56", "DOK. HATASI"),
        ("57", "DOK. DELIK YIRTIK"),
        ("58", "DOK. SU LEKESI"),
    ],
}


def centimeters_to_meters(value: float) -> float:
    """Santimetre cinsinden verilen değeri metreye çevirir."""
    return value / 100


@dataclass
class Measurement:
    raw_line: str
    value: float
    unit: str
    timestamp: datetime
    meter_value: float | None = None
    kg_value: float | None = None


class SerialReader(threading.Thread):
    """
    Arka planda çalışan seri port okuyucu thread'i.
    Gelen verileri satır satır ayrıştırıp output_queue'ya koyar.
    Prefix formatı: 'DATA|satır', 'INFO|mesaj', 'ERROR|hata'.
    """

    def __init__(self, port_name: str, baud_rate: int, output_queue: queue.Queue[str]):
        """Port adı, baud rate ve çıktı kuyruğu ile thread'i hazırlar."""
        super().__init__(daemon=True)
        self.port_name = port_name
        self.baud_rate = baud_rate
        self.output_queue = output_queue
        self._stop_event = threading.Event()
        self._serial = None
        self._buffer = bytearray()
        self._last_data_time = 0.0

    def run(self) -> None:
        """
        Thread ana döngüsü. Porta bağlanır, veri geldiğinde buffer'a ekler,
        tam satırları kuyruğa iter. Hata veya durdurma sinyalinde temizlik yapar.
        """
        try:
            self._serial = serial.serial_for_url(self.port_name, self.baud_rate, timeout=1)
            self.output_queue.put(f"INFO|Connected to {self.port_name} at {self.baud_rate} baud")
            while not self._stop_event.is_set():
                chunk = self._serial.read(self._serial.in_waiting or 1)
                if chunk:
                    self._last_data_time = time.monotonic()
                    self._buffer.extend(chunk)
                    self._emit_complete_lines()
                    continue
                self._emit_stale_buffer()
        except serial.SerialException as exc:
            self.output_queue.put(f"ERROR|{exc}")
        finally:
            self._flush_buffer(force=True)
            if self._serial and self._serial.is_open:
                self._serial.close()
            self.output_queue.put("INFO|Disconnected")

    def stop(self) -> None:
        """Thread'e durma sinyali gönderir. Döngü bir sonraki iterasyonda sonlanır."""
        self._stop_event.set()

    def _emit_complete_lines(self) -> None:
        """Buffer'da \r veya \n ile biten tam satırları bulur ve kuyruğa gönderir."""
        while True:
            match = re.search(rb"[\r\n]+", self._buffer)
            if match is None:
                break
            raw_line = bytes(self._buffer[: match.start()])
            del self._buffer[: match.end()]
            self._emit_line(raw_line)

    def _emit_stale_buffer(self) -> None:
        """
        Son veriden bu yana 350ms geçtiyse ve buffer'da satır sonu olmayan
        yarım veri kalmışsa onu zorla boşaltır.
        """
        if not self._buffer:
            return
        if self._last_data_time and time.monotonic() - self._last_data_time >= 0.35:
            self._flush_buffer(force=True)

    def _flush_buffer(self, force: bool = False) -> None:
        """force=True verildiğinde buffer'daki tüm veriyi tek satır olarak emit eder."""
        if not self._buffer:
            return
        if force:
            raw_line = bytes(self._buffer)
            self._buffer.clear()
            self._emit_line(raw_line)

    def _emit_line(self, raw_line: bytes) -> None:
        """Ham byte satırını UTF-8 ile decode eder, boş değilse 'DATA|...' formatında kuyruğa koyar."""
        decoded = raw_line.decode("utf-8", errors="ignore").strip()
        if decoded:
            self.output_queue.put(f"DATA|{decoded}")


class FabricCounterApp:
    """
    Kumaş kalite operatoru baş uygulaması.
    Seri port'tan metre/kg ölçümü okur, parti bilgisi tutar,
    barkod sorgular, kayıt eder ve etiket yazdırır.
    """

    def __init__(self, root: tk.Tk):
        """
        Uygulamanın tamamını başlatır:
        - Pencere özellikleri ve boyutları
        - Tüm durum değişkenleri ve StringVar'lar
        - UI yapısını inşa eder
        - Ayarları dosyadan yükler
        - Kaydedilmiş porta otomatik bağlanır
        - Periyodik kontroller (seri, web, yazıcı) ve kuyruk işlemeyi başlatır.
        """
        self.root = root
        self.root.title("Kalite Operator Ekrani")
        self.root.geometry("1366x900")
        self.root.minsize(1280, 840)
        self.root.attributes("-fullscreen", True)

        self.serial_queue: queue.Queue[str] = queue.Queue()
        self.reader: SerialReader | None = None
        self.service_window: tk.Toplevel | None = None
        self.settings_window: tk.Toplevel | None = None
        self.logs_window: tk.Toplevel | None = None
        self.error_code_admin_window: tk.Toplevel | None = None
        self.measurements: list[Measurement] = []
        self.error_code_groups = copy.deepcopy(ERROR_CODE_GROUPS)
        self.totals = {"m": 0.0, "kg": 0.0}
        self.demo_samples = ["KG: 12.40", "M: 18.75", "18.75 metre", "42.10", "noise", "KG: 5.60"]
        self.demo_index = 0
        self.demo_job: str | None = None
        self.parsed_count = 0
        self.ignored_count = 0
        self.current_meter_raw = 0.0
        self.current_kg_raw = 0.0
        self.meter_offset = 0.0
        self.last_meter_value = 0.0
        self.last_kg_value = 0.0
        self.auto_save_armed = False
        self.meter_cycle_active = False
        self.last_auto_save_signature = ""

        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value="9600")
        self.default_unit_var = tk.StringVar(value="kg")
        self.machine_zero_tolerance_var = tk.StringVar(value=f"{MACHINE_ZERO_TOLERANCE:.2f}")
        self.login_url_var = tk.StringVar(value=LOGIN_URL)
        self.barcode_lookup_url_var = tk.StringVar(value=BARCODE_LOOKUP_URL)
        self.save_measurement_url_var = tk.StringVar(value=SAVE_MEASUREMENT_URL)
        self.health_url_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")
        self.source_var = tk.StringVar(value="Idle")
        self.log_date_filter_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        self.log_summary_var = tk.StringVar(value="Kayit bulunamadi")
        self.serial_connection_var = tk.StringVar(value="COM: kontrol ediliyor")
        self.webservice_connection_var = tk.StringVar(value="WEB: kontrol ediliyor")
        self.printer_connection_var = tk.StringVar(value="YAZICI: kontrol ediliyor")
        self.last_value_var = tk.StringVar(value="-")
        self.meter_total_var = tk.StringVar(value="0.00 m")
        self.kg_total_var = tk.StringVar(value="0.00 kg")
        self.last_meter_var = tk.StringVar(value="0.00 m")
        self.last_kg_var = tk.StringVar(value="0.00 kg")
        self.manual_line_var = tk.StringVar(value="KG: 12.40")
        self.diagnostics_enabled_var = tk.BooleanVar(value=True)
        self.parse_status_var = tk.StringVar(value="No data yet")
        self.parsed_count_var = tk.StringVar(value="0")
        self.ignored_count_var = tk.StringVar(value="0")
        self.operator_status_var = tk.StringVar(value="Servis hazir degil")
        self.login_status_var = tk.StringVar(value="Operator sifresini girin")
        self.barcode_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.customer_var = tk.StringVar(value="-")
        self.party_no_var = tk.StringVar(value="-")
        self.party_id_var = tk.StringVar(value="-")
        self.sarj_no_var = tk.StringVar(value="-")
        self.kalite_var = tk.StringVar(value="-")
        self.kalite_talimati_var = tk.StringVar(value="-")
        self.renk_var = tk.StringVar(value="-")
        self.logged_user_var = tk.StringVar(value="Giris yapilmadi")
        self.printer_name_var = tk.StringVar(value="Argox X-1000VL series PPLA")
        self.auto_print_var = tk.BooleanVar(value=True)
        self.operator_notes_var = tk.StringVar()
        self.selected_error_code_var = tk.StringVar(value="Hata kodu secilmedi")
        self.error_code_admin_status_var = tk.StringVar(value="Hata kodlari hazir")
        self.error_code_group_var = tk.StringVar()
        self.error_code_value_var = tk.StringVar()
        self.error_code_description_var = tk.StringVar()
        self.current_party_data: dict[str, str] = {}
        self.current_user_data: dict[str, str] = {}
        self.current_error_code: dict[str, str] = {}
        self.error_code_window: tk.Toplevel | None = None
        self.available_printers: list[str] = []
        self.web_health_check_inflight = False

        self._configure_styles()
        self._build_ui()
        self.root.bind("<Escape>", self._exit_fullscreen)
        self.root.bind("<F11>", self._enter_fullscreen)
        self.root.bind("<Control-Shift-D>", self._toggle_service_window)
        self._load_settings()
        self._load_error_code_groups()
        self._setup_setting_traces()
        self.refresh_ports()
        self.root.after(200, self._auto_connect_saved_port)
        self.root.after(500, self._schedule_connectivity_checks)
        self.root.after(100, self.process_serial_queue)

    def _configure_styles(self) -> None:
        """
        ttk widget temasinı ve uygulamaya özel buton/kart renklerini tanımlar.
        'OperatorSuccess', 'OperatorWarn', 'OperatorInfo', 'OperatorDanger',
        'OperatorNeutral' buton stilleri ile 'InfoBlue/Warm/Green' kart stilleri burada.
        """
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#f2f4f7")
        style.configure("TLabelframe", background="#f2f4f7")
        style.configure("TLabelframe.Label", font=("Segoe UI", 16, "bold"), foreground="#17212b")
        style.configure("TLabel", background="#f2f4f7", foreground="#17212b", font=("Segoe UI", 14))
        style.configure("TButton", font=("Segoe UI", 16, "bold"), padding=(18, 12))
        style.configure("TEntry", font=("Segoe UI", 18), padding=10)
        style.configure("TCombobox", font=("Segoe UI", 16))
        style.configure("TCheckbutton", background="#f2f4f7", font=("Segoe UI", 14))
        style.configure("OperatorSuccess.TButton", background="#1f7a3e", foreground="#ffffff", borderwidth=0)
        style.map("OperatorSuccess.TButton", background=[("active", "#176132")])
        style.configure("OperatorWarn.TButton", background="#d97706", foreground="#ffffff", borderwidth=0)
        style.map("OperatorWarn.TButton", background=[("active", "#b65f00")])
        style.configure("OperatorInfo.TButton", background="#0f5f8f", foreground="#ffffff", borderwidth=0)
        style.map("OperatorInfo.TButton", background=[("active", "#0c4c72")])
        style.configure("OperatorDanger.TButton", background="#a63d40", foreground="#ffffff", borderwidth=0)
        style.map("OperatorDanger.TButton", background=[("active", "#832e31")])
        style.configure("OperatorNeutral.TButton", background="#5b6777", foreground="#ffffff", borderwidth=0)
        style.map("OperatorNeutral.TButton", background=[("active", "#495463")])
        style.configure("InfoBlue.TLabelframe", background="#e6f4fb", borderwidth=1, relief="solid")
        style.configure("InfoBlue.TLabelframe.Label", background="#e6f4fb", foreground="#0c4a6e", font=("Segoe UI", 15, "bold"))
        style.configure("InfoWarm.TLabelframe", background="#fdf1e7", borderwidth=1, relief="solid")
        style.configure("InfoWarm.TLabelframe.Label", background="#fdf1e7", foreground="#9a3412", font=("Segoe UI", 15, "bold"))
        style.configure("InfoGreen.TLabelframe", background="#e9f7ef", borderwidth=1, relief="solid")
        style.configure("InfoGreen.TLabelframe.Label", background="#e9f7ef", foreground="#166534", font=("Segoe UI", 15, "bold"))
        style.configure("CardValueBlue.TLabel", background="#e6f4fb", foreground="#0c4a6e", font=("Segoe UI", 20, "bold"))
        style.configure("CardValueWarm.TLabel", background="#fdf1e7", foreground="#9a3412", font=("Segoe UI", 20, "bold"))
        style.configure("CardValueGreen.TLabel", background="#e9f7ef", foreground="#166534", font=("Segoe UI", 30, "bold"))

    def _build_ui(self) -> None:
        """
        Tüm sayfaları ve pop-up pencereleri oluşturur:
        - login_page: şifre giriş ekranı (başlangıçta görünür)
        - operator_page: operatör çalışma ekranı
        - settings_window: port/URL/yazıcı ayarları (Toplevel, gizli)
        - logs_window: kayıt logları tablosu (Toplevel, gizli)
        - service_window: admin debug/test ekranı (Toplevel, gizli)
        """
        self.login_page = ttk.Frame(self.root, padding=24)
        self.login_page.pack(fill="both", expand=True)
        self._build_login_page(self.login_page)

        self.operator_page = ttk.Frame(self.root, padding=24)
        self._build_operator_page(self.operator_page)

        self.settings_window = tk.Toplevel(self.root)
        self.settings_window.title("Ayarlar")
        self.settings_window.geometry("1100x760+60+60")
        self.settings_window.configure(background="#f2f4f7")
        self.settings_window.protocol("WM_DELETE_WINDOW", self.settings_window.withdraw)

        settings_page = ttk.Frame(self.settings_window, padding=16)
        settings_page.pack(fill="both", expand=True)
        self._build_settings_page(settings_page)
        self.settings_window.withdraw()

        self.error_code_admin_window = tk.Toplevel(self.root)
        self.error_code_admin_window.title("Hata Kodu Yonetimi")
        self.error_code_admin_window.geometry("1260x820+80+80")
        self.error_code_admin_window.configure(background="#f2f4f7")
        self.error_code_admin_window.protocol("WM_DELETE_WINDOW", self.error_code_admin_window.withdraw)

        error_code_admin_page = ttk.Frame(self.error_code_admin_window, padding=16)
        error_code_admin_page.pack(fill="both", expand=True)
        self._build_error_code_admin_page(error_code_admin_page)
        self.error_code_admin_window.withdraw()

        self.logs_window = tk.Toplevel(self.root)
        self.logs_window.title("Gunluk Kayit Loglari")
        self.logs_window.geometry("1380x820+70+70")
        self.logs_window.configure(background="#f2f4f7")
        self.logs_window.protocol("WM_DELETE_WINDOW", self.logs_window.withdraw)

        logs_page = ttk.Frame(self.logs_window, padding=16)
        logs_page.pack(fill="both", expand=True)
        self._build_logs_page(logs_page)
        self.logs_window.withdraw()

        self.service_window = tk.Toplevel(self.root)
        self.service_window.title("Debug ve Test Ekrani")
        self.service_window.geometry("1280x860+40+40")
        self.service_window.configure(background="#f2f4f7")
        self.service_window.protocol("WM_DELETE_WINDOW", self.service_window.withdraw)

        serial_page = ttk.Frame(self.service_window, padding=16)
        serial_page.pack(fill="both", expand=True)
        self._build_serial_page(serial_page)
        self.service_window.withdraw()

    def _build_logs_page(self, container: ttk.Frame) -> None:
        """
        Kayıt logları sayfasını inşa eder.
        Tarih filtresi + 'Bugün' butonu, operator_records.json'dan okunan
        tüm kayıtları treeview tablosunda gösterir.
        """
        container.columnconfigure(0, weight=1)
        container.rowconfigure(3, weight=1)

        toolbar = ttk.LabelFrame(container, text="Kayit Filtreleri", padding=12)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.columnconfigure(3, weight=1)

        ttk.Label(toolbar, text="Tarih").grid(row=0, column=0, padx=(0, 8), pady=4, sticky="w")
        ttk.Entry(toolbar, textvariable=self.log_date_filter_var, width=16).grid(row=0, column=1, pady=4, sticky="w")
        ttk.Button(toolbar, text="Bugun", command=self._set_logs_today_filter).grid(row=0, column=2, padx=(12, 8), pady=4)
        ttk.Button(toolbar, text="Yenile", command=self.refresh_logs_view).grid(row=0, column=3, pady=4, sticky="w")
        ttk.Label(toolbar, textvariable=self.log_summary_var).grid(row=0, column=4, pady=4, sticky="e")

        info = ttk.LabelFrame(container, text="Bilgi", padding=12)
        info.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        ttk.Label(
            info,
            text="Tarih filtresi `YYYY-MM-DD` formatindadir. Bos birakilirsa tum kayitlar listelenir.",
        ).grid(row=0, column=0, sticky="w")

        table_frame = ttk.LabelFrame(container, text="Kayitlar", padding=12)
        table_frame.grid(row=2, column=0, sticky="nsew", pady=(12, 0))
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        columns = ("saved_at", "party_no", "barcode", "meter", "kg", "error_code", "operator", "trigger")
        self.logs_tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=22)
        self.logs_tree.heading("saved_at", text="Kayit Zamani")
        self.logs_tree.heading("party_no", text="Parti No")
        self.logs_tree.heading("barcode", text="Barkod")
        self.logs_tree.heading("meter", text="Metre")
        self.logs_tree.heading("kg", text="Kg")
        self.logs_tree.heading("error_code", text="Hata")
        self.logs_tree.heading("operator", text="Operator")
        self.logs_tree.heading("trigger", text="Tetik")
        self.logs_tree.column("saved_at", width=170, anchor="w")
        self.logs_tree.column("party_no", width=150, anchor="w")
        self.logs_tree.column("barcode", width=170, anchor="w")
        self.logs_tree.column("meter", width=100, anchor="e")
        self.logs_tree.column("kg", width=100, anchor="e")
        self.logs_tree.column("error_code", width=130, anchor="w")
        self.logs_tree.column("operator", width=170, anchor="w")
        self.logs_tree.column("trigger", width=130, anchor="w")
        self.logs_tree.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.logs_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.logs_tree.configure(yscrollcommand=scrollbar.set)

    def _build_settings_page(self, container: ttk.Frame) -> None:
        """
        Ayarlar sayfasını inşa eder: seri bağlantı (port/baud/birim),
        web servis URL'leri (login, barkod, kayıt, health) ve yazıcı seçimi.
        Sadece admin rolü erişebilir.
        """
        container.columnconfigure(0, weight=1)
        container.rowconfigure(2, weight=1)

        serial_frame = ttk.LabelFrame(container, text="Seri Baglanti", padding=16)
        serial_frame.grid(row=0, column=0, sticky="ew")
        serial_frame.columnconfigure(1, weight=1)
        serial_frame.columnconfigure(3, weight=1)

        ttk.Label(serial_frame, text="Port").grid(row=0, column=0, padx=(0, 8), pady=6, sticky="w")
        self.settings_port_combo = ttk.Combobox(serial_frame, textvariable=self.port_var, width=24)
        self.settings_port_combo.grid(row=0, column=1, padx=(0, 16), pady=6, sticky="ew")
        ttk.Label(serial_frame, text="Baud").grid(row=0, column=2, padx=(0, 8), pady=6, sticky="w")
        ttk.Combobox(
            serial_frame,
            textvariable=self.baud_var,
            values=["9600", "19200", "38400", "57600", "115200"],
            state="readonly",
            width=14,
        ).grid(row=0, column=3, pady=6, sticky="w")

        ttk.Label(serial_frame, text="Varsayilan Birim").grid(row=1, column=0, padx=(0, 8), pady=6, sticky="w")
        ttk.Combobox(
            serial_frame,
            textvariable=self.default_unit_var,
            values=["kg", "m"],
            state="readonly",
            width=14,
        ).grid(row=1, column=1, pady=6, sticky="w")
        ttk.Button(serial_frame, text="Portlari Yenile", command=self.refresh_ports).grid(row=1, column=2, padx=(0, 8), pady=6)
        ttk.Label(serial_frame, textvariable=self.status_var).grid(row=1, column=3, pady=6, sticky="e")

        service_frame = ttk.LabelFrame(container, text="Web Servis ve Yazici", padding=16)
        service_frame.grid(row=1, column=0, sticky="ew", pady=(16, 0))
        service_frame.columnconfigure(1, weight=1)

        ttk.Label(service_frame, text="Login URL").grid(row=0, column=0, padx=(0, 8), pady=6, sticky="w")
        ttk.Entry(service_frame, textvariable=self.login_url_var).grid(row=0, column=1, pady=6, sticky="ew")
        ttk.Label(service_frame, text="Barkod Servisi").grid(row=1, column=0, padx=(0, 8), pady=6, sticky="w")
        ttk.Entry(service_frame, textvariable=self.barcode_lookup_url_var).grid(row=1, column=1, pady=6, sticky="ew")
        ttk.Label(service_frame, text="Kayit Servisi").grid(row=2, column=0, padx=(0, 8), pady=6, sticky="w")
        ttk.Entry(service_frame, textvariable=self.save_measurement_url_var).grid(row=2, column=1, pady=6, sticky="ew")
        ttk.Label(service_frame, text="Health URL").grid(row=3, column=0, padx=(0, 8), pady=6, sticky="w")
        ttk.Entry(service_frame, textvariable=self.health_url_var).grid(row=3, column=1, pady=6, sticky="ew")
        ttk.Label(service_frame, text="Yazici").grid(row=4, column=0, padx=(0, 8), pady=6, sticky="w")
        self.settings_printer_combo = ttk.Combobox(service_frame, textvariable=self.printer_name_var, width=38)
        self.settings_printer_combo.grid(row=4, column=1, pady=6, sticky="ew")
        ttk.Checkbutton(service_frame, text="Kayit sonrasi otomatik bas", variable=self.auto_print_var).grid(
            row=5, column=1, pady=6, sticky="w"
        )

        advanced_frame = ttk.LabelFrame(container, text="Makine Ayarlari", padding=16)
        advanced_frame.grid(row=2, column=0, sticky="nsew", pady=(16, 0))
        advanced_frame.columnconfigure(1, weight=1)

        ttk.Label(advanced_frame, text="Sifir Toleransi").grid(row=0, column=0, padx=(0, 8), pady=(0, 10), sticky="w")
        ttk.Entry(advanced_frame, textvariable=self.machine_zero_tolerance_var, width=14).grid(
            row=0, column=1, pady=(0, 10), sticky="w"
        )
        ttk.Label(
            advanced_frame,
            text="Makinenin sifir kabul edecegi alt esik degeri burada tanimlanir.",
            wraplength=720,
            justify="left",
        ).grid(row=0, column=2, padx=(12, 0), pady=(0, 10), sticky="w")

        info_frame = ttk.LabelFrame(container, text="Bilgi", padding=16)
        info_frame.grid(row=3, column=0, sticky="nsew", pady=(16, 0))
        info_frame.columnconfigure(0, weight=1)
        ttk.Label(
            info_frame,
            text="Ayarlar penceresi admin kullanicilar icindir. Seri baglanti, varsayilan birim, yazici, servis adresleri ve sifir toleransi buradan yonetilir. Hata kodlari ayri yonetim ekranindadir.",
            wraplength=900,
            justify="left",
        ).grid(row=0, column=0, sticky="w")

    def _build_error_code_admin_page(self, container: ttk.Frame) -> None:
        container.columnconfigure(0, weight=2)
        container.columnconfigure(1, weight=3)
        container.rowconfigure(1, weight=1)

        header = ttk.LabelFrame(container, text="Hata Kodu Yonetimi", padding=12)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(
            header,
            text="Hata kodlarini grup bazli olarak ekleyin, guncelleyin veya silin. Degisiklikler ayri dosyada saklanir.",
            wraplength=900,
            justify="left",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.error_code_admin_status_var, font=("Segoe UI", 12, "bold")).grid(
            row=0, column=1, sticky="e"
        )

        form_frame = ttk.LabelFrame(container, text="Form", padding=16)
        form_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 12), pady=(12, 0))
        form_frame.columnconfigure(1, weight=1)

        ttk.Label(form_frame, text="Grup").grid(row=0, column=0, padx=(0, 8), pady=6, sticky="w")
        self.error_code_group_combo = ttk.Combobox(form_frame, textvariable=self.error_code_group_var)
        self.error_code_group_combo.grid(row=0, column=1, pady=6, sticky="ew")
        ttk.Label(form_frame, text="Kod").grid(row=1, column=0, padx=(0, 8), pady=6, sticky="w")
        ttk.Entry(form_frame, textvariable=self.error_code_value_var).grid(row=1, column=1, pady=6, sticky="ew")
        ttk.Label(form_frame, text="Aciklama").grid(row=2, column=0, padx=(0, 8), pady=6, sticky="w")
        ttk.Entry(form_frame, textvariable=self.error_code_description_var).grid(row=2, column=1, pady=6, sticky="ew")

        action_row = ttk.Frame(form_frame)
        action_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        ttk.Button(action_row, text="Yeni", command=self.reset_error_code_form).pack(side="left")
        ttk.Button(action_row, text="Ekle / Guncelle", command=self.upsert_error_code_entry, style="OperatorSuccess.TButton").pack(side="left", padx=(8, 0))
        ttk.Button(action_row, text="Secileni Sil", command=self.delete_selected_error_code_entry, style="OperatorDanger.TButton").pack(side="left", padx=(8, 0))
        ttk.Button(action_row, text="Kaydet", command=self.save_error_code_groups, style="OperatorInfo.TButton").pack(side="left", padx=(8, 0))
        ttk.Button(action_row, text="Varsayilanlari Yukle", command=self.restore_default_error_code_groups).pack(side="left", padx=(8, 0))

        group_row = ttk.Frame(form_frame)
        group_row.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        ttk.Button(group_row, text="Grubu Sil", command=self.delete_current_error_code_group).pack(side="left")

        table_frame = ttk.LabelFrame(container, text="Kod Listesi", padding=12)
        table_frame.grid(row=1, column=1, sticky="nsew", pady=(12, 0))
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        columns = ("group", "code", "description")
        self.error_code_admin_tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=24)
        self.error_code_admin_tree.heading("group", text="Grup")
        self.error_code_admin_tree.heading("code", text="Kod")
        self.error_code_admin_tree.heading("description", text="Aciklama")
        self.error_code_admin_tree.column("group", width=260, anchor="w")
        self.error_code_admin_tree.column("code", width=100, anchor="center")
        self.error_code_admin_tree.column("description", width=420, anchor="w")
        self.error_code_admin_tree.grid(row=0, column=0, sticky="nsew")
        self.error_code_admin_tree.bind("<<TreeviewSelect>>", self._on_error_code_admin_select)

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.error_code_admin_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.error_code_admin_tree.configure(yscrollcommand=scrollbar.set)

        self.refresh_error_code_admin_view()

    @staticmethod
    def _normalize_error_code_groups(data: object) -> dict[str, list[tuple[str, str]]]:
        if not isinstance(data, dict):
            raise ValueError("Hata kodlari JSON nesnesi olmali")
        normalized: dict[str, list[tuple[str, str]]] = {}
        for group_name, items in data.items():
            if not isinstance(group_name, str) or not group_name.strip():
                raise ValueError("Grup adlari bos olmayan metin olmali")
            if not isinstance(items, list):
                raise ValueError(f"{group_name} icin deger liste olmali")
            normalized_items: list[tuple[str, str]] = []
            for item in items:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    raise ValueError(f"{group_name} icindeki her satir [kod, aciklama] formatinda olmali")
                code, description = item
                normalized_items.append((str(code), str(description)))
            normalized[group_name] = normalized_items
        return normalized

    def refresh_error_code_admin_view(self) -> None:
        if hasattr(self, "error_code_group_combo"):
            self.error_code_group_combo["values"] = list(self.error_code_groups.keys())
        if not hasattr(self, "error_code_admin_tree"):
            return
        for item_id in self.error_code_admin_tree.get_children():
            self.error_code_admin_tree.delete(item_id)
        for group_name, items in self.error_code_groups.items():
            for code, description in items:
                self.error_code_admin_tree.insert("", "end", values=(group_name, code, description))
        total = sum(len(items) for items in self.error_code_groups.values())
        self.error_code_admin_status_var.set(f"{len(self.error_code_groups)} grup / {total} kod")

    def reset_error_code_form(self) -> None:
        self.error_code_value_var.set("")
        self.error_code_description_var.set("")
        if not self.error_code_group_var.get().strip() and self.error_code_groups:
            self.error_code_group_var.set(next(iter(self.error_code_groups)))

    def _on_error_code_admin_select(self, _event: tk.Event | None = None) -> None:
        if not hasattr(self, "error_code_admin_tree"):
            return
        selection = self.error_code_admin_tree.selection()
        if not selection:
            return
        values = self.error_code_admin_tree.item(selection[0], "values")
        if len(values) != 3:
            return
        self.error_code_group_var.set(str(values[0]))
        self.error_code_value_var.set(str(values[1]))
        self.error_code_description_var.set(str(values[2]))

    def upsert_error_code_entry(self) -> None:
        group_name = self.error_code_group_var.get().strip()
        code = self.error_code_value_var.get().strip()
        description = self.error_code_description_var.get().strip()
        if not group_name or not code or not description:
            messagebox.showwarning("Hata Kodlari", "Grup, kod ve aciklama alanlari zorunludur.")
            return
        items = list(self.error_code_groups.get(group_name, []))
        updated = False
        for index, (existing_code, _existing_description) in enumerate(items):
            if existing_code == code:
                items[index] = (code, description)
                updated = True
                break
        if not updated:
            items.append((code, description))
            items.sort(key=lambda item: item[0])
        self.error_code_groups[group_name] = items
        self.refresh_error_code_admin_view()
        self.error_code_admin_status_var.set("Hata kodu guncellendi, kaydetmeyi unutmayin")

    def delete_selected_error_code_entry(self) -> None:
        group_name = self.error_code_group_var.get().strip()
        code = self.error_code_value_var.get().strip()
        if not group_name or not code or group_name not in self.error_code_groups:
            messagebox.showwarning("Hata Kodlari", "Silmek icin once tablodan bir kayit secin.")
            return
        remaining = [(item_code, item_description) for item_code, item_description in self.error_code_groups[group_name] if item_code != code]
        if len(remaining) == len(self.error_code_groups[group_name]):
            messagebox.showwarning("Hata Kodlari", "Secilen kod bulunamadi.")
            return
        if remaining:
            self.error_code_groups[group_name] = remaining
        else:
            del self.error_code_groups[group_name]
        self.refresh_error_code_admin_view()
        self.reset_error_code_form()
        self.error_code_admin_status_var.set("Kod silindi, kaydetmeyi unutmayin")

    def delete_current_error_code_group(self) -> None:
        group_name = self.error_code_group_var.get().strip()
        if not group_name or group_name not in self.error_code_groups:
            messagebox.showwarning("Hata Kodlari", "Silmek icin gecerli bir grup secin.")
            return
        if not messagebox.askyesno("Hata Kodlari", f"{group_name} grubundaki tum kodlar silinsin mi?"):
            return
        del self.error_code_groups[group_name]
        self.refresh_error_code_admin_view()
        self.reset_error_code_form()
        self.error_code_admin_status_var.set("Grup silindi, kaydetmeyi unutmayin")

    def restore_default_error_code_groups(self) -> None:
        self.error_code_groups = copy.deepcopy(ERROR_CODE_GROUPS)
        self.refresh_error_code_admin_view()
        self.reset_error_code_form()
        self.error_code_admin_status_var.set("Varsayilan kodlar yuklendi, kaydetmeyi unutmayin")

    def save_error_code_groups(self) -> None:
        try:
            ERROR_CODE_SETTINGS_PATH.write_text(
                json.dumps(self.error_code_groups, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            messagebox.showerror("Hata Kodlari", f"Kaydetme basarisiz: {exc}")
            return
        self.error_code_admin_status_var.set(f"Hata kodlari kaydedildi: {ERROR_CODE_SETTINGS_PATH.name}")
        if self.error_code_window is not None and self.error_code_window.winfo_exists():
            self._close_error_code_window()

    def _load_error_code_groups(self) -> None:
        loaded_data: object = self.error_code_groups
        if ERROR_CODE_SETTINGS_PATH.exists():
            try:
                loaded_data = json.loads(ERROR_CODE_SETTINGS_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded_data = copy.deepcopy(ERROR_CODE_GROUPS)
        elif SETTINGS_PATH.exists():
            try:
                legacy_data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                if isinstance(legacy_data, dict) and "error_code_groups" in legacy_data:
                    loaded_data = legacy_data["error_code_groups"]
            except (OSError, json.JSONDecodeError):
                loaded_data = copy.deepcopy(ERROR_CODE_GROUPS)
        try:
            self.error_code_groups = self._normalize_error_code_groups(loaded_data)
        except ValueError:
            self.error_code_groups = copy.deepcopy(ERROR_CODE_GROUPS)
        self.refresh_error_code_admin_view()
        self.reset_error_code_form()

    def _build_login_page(self, container: ttk.Frame) -> None:
        """
        Şifre giriş ekranını inşa eder.
        Enter tuşuyla veya 'Giris Yap' butonuyla login() tetiklenir.
        Login URL yoksa TEST_USER_PASSWORD / TEST_ADMIN_PASSWORD test şifresiyle girilir.
        """
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)

        shell = ttk.Frame(container)
        shell.grid(row=0, column=0, sticky="nsew")
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(0, weight=1)

        card = ttk.LabelFrame(shell, text="Operator Girisi", padding=28)
        card.grid(row=0, column=0)
        card.columnconfigure(0, weight=1)

        ttk.Label(card, text="Kalite Operator Sistemi", font=("Segoe UI", 30, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        ttk.Label(
            card,
            text="Operatora ozel sifre ile giris yapin.",
            font=("Segoe UI", 16),
        ).grid(row=1, column=0, sticky="w", pady=(0, 24))

        form = ttk.Frame(card)
        form.grid(row=2, column=0, sticky="ew")
        form.columnconfigure(0, weight=1)

        ttk.Label(form, text="Sifre", font=("Segoe UI", 16, "bold")).grid(row=0, column=0, sticky="w")
        self.password_entry = ttk.Entry(form, textvariable=self.password_var, show="*", width=26)
        self.password_entry.grid(row=1, column=0, sticky="ew", pady=(8, 24))
        self.password_entry.bind("<Return>", lambda _event: self.login())

        ttk.Button(card, text="Giris Yap", command=self.login).grid(row=3, column=0, sticky="ew")
        ttk.Label(
            card,
            textvariable=self.login_status_var,
            font=("Segoe UI", 14, "bold"),
            foreground="#0c4a6e",
        ).grid(row=4, column=0, sticky="w", pady=(18, 0))

        self.root.after(50, self.password_entry.focus_set)

    def _build_serial_page(self, container: ttk.Frame) -> None:
        """
        Admin debug/test ekranını inşa eder.
        - Port/baud seçimi ve bağlantı kontrolleri
        - Demo başlat/durdur, diagnostics toggle
        - Ölçüm geçmişi treeview ve ham veri log metin kutusu
        - Manuel satır gönderme, oturum temizleme, CSV aktarma
        """
        container.columnconfigure(0, weight=1)
        container.rowconfigure(2, weight=1)

        header = ttk.LabelFrame(container, text="Debug Ozeti", padding=12)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(
            header,
            text="Bu ekran sadece admin icin debug, ham veri takibi ve manuel test amaciyla kullanilir.",
        ).grid(row=0, column=0, sticky="w")

        controls = ttk.LabelFrame(container, text="Canli Veri Baglantisi", padding=12)
        controls.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        controls.columnconfigure(5, weight=1)

        ttk.Label(controls, text="Port").grid(row=0, column=0, padx=(0, 8), pady=4, sticky="w")
        self.port_combo = ttk.Combobox(controls, textvariable=self.port_var, width=22)
        self.port_combo.grid(row=0, column=1, padx=(0, 12), pady=4, sticky="w")

        ttk.Label(controls, text="Baud").grid(row=0, column=2, padx=(0, 8), pady=4, sticky="w")
        baud_combo = ttk.Combobox(
            controls,
            textvariable=self.baud_var,
            values=["9600", "19200", "38400", "57600", "115200"],
            state="readonly",
            width=12,
        )
        baud_combo.grid(row=0, column=3, padx=(0, 12), pady=4, sticky="w")

        ttk.Button(controls, text="Refresh", command=self.refresh_ports).grid(row=0, column=4, padx=(0, 8), pady=4)
        ttk.Button(controls, text="Connect", command=self.connect).grid(row=0, column=5, padx=(0, 8), pady=4, sticky="w")
        ttk.Button(controls, text="Disconnect", command=self.disconnect).grid(row=0, column=6, pady=4, sticky="w")

        ttk.Label(controls, text="COM4 veya socket://127.0.0.1:7001 gibi ozel adresler desteklenir.").grid(
            row=1, column=0, columnspan=4, padx=(0, 8), pady=4, sticky="w"
        )

        ttk.Label(controls, textvariable=self.status_var).grid(row=1, column=4, columnspan=3, pady=4, sticky="e")

        ttk.Button(controls, text="Demo Baslat", command=self.start_demo).grid(row=2, column=4, padx=(0, 8), pady=4, sticky="w")
        ttk.Button(controls, text="Demo Durdur", command=self.stop_demo).grid(row=2, column=5, padx=(0, 8), pady=4, sticky="w")
        ttk.Checkbutton(controls, text="Diagnostics", variable=self.diagnostics_enabled_var).grid(
            row=2, column=6, pady=4, sticky="e"
        )

        summary = ttk.Frame(container, padding=(0, 16, 0, 16))
        summary.grid(row=2, column=0, sticky="ew")
        summary.columnconfigure((0, 1, 2, 3), weight=1)

        self._create_stat_card(summary, 0, "Last Value", self.last_value_var)
        self._create_stat_card(summary, 1, "Meter Total", self.meter_total_var)
        self._create_stat_card(summary, 2, "Kg Total", self.kg_total_var)
        self._create_stat_card(summary, 3, "Source", self.source_var)

        content = ttk.Panedwindow(container, orient="horizontal")
        content.grid(row=3, column=0, sticky="nsew")

        measurement_frame = ttk.LabelFrame(content, text="Olcum Gecmisi", padding=12)
        log_frame = ttk.LabelFrame(content, text="Ham Veri Akisi", padding=12)
        content.add(measurement_frame, weight=3)
        content.add(log_frame, weight=2)

        self.tree = ttk.Treeview(measurement_frame, columns=("time", "unit", "value", "raw"), show="headings", height=18)
        self.tree.heading("time", text="Time")
        self.tree.heading("unit", text="Unit")
        self.tree.heading("value", text="Value")
        self.tree.heading("raw", text="Raw Data")
        self.tree.column("time", width=160, anchor="w")
        self.tree.column("unit", width=80, anchor="center")
        self.tree.column("value", width=100, anchor="e")
        self.tree.column("raw", width=320, anchor="w")
        self.tree.pack(fill="both", expand=True)

        self.log_text = tk.Text(log_frame, wrap="word", state="disabled", height=18)
        self.log_text.pack(fill="both", expand=True)

        actions = ttk.Frame(container, padding=(0, 16, 0, 0))
        actions.grid(row=4, column=0, sticky="ew")
        ttk.Button(actions, text="Oturumu Temizle", command=self.clear_session).pack(side="left")

        manual_frame = ttk.Frame(actions)
        manual_frame.pack(side="left", padx=(16, 0))
        ttk.Label(manual_frame, text="Manuel Test Satiri").pack(side="left", padx=(0, 8))
        ttk.Entry(manual_frame, textvariable=self.manual_line_var, width=36).pack(side="left", padx=(0, 8))
        ttk.Button(manual_frame, text="Test Gonder", command=self.send_manual_line).pack(side="left")

        ttk.Button(actions, text="CSV Aktar", command=self.export_csv).pack(side="right")

        diagnostics = ttk.LabelFrame(container, text="Debug Durumu", padding=12)
        diagnostics.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        diagnostics.columnconfigure(1, weight=1)
        diagnostics.columnconfigure(3, weight=1)
        ttk.Label(diagnostics, text="Son Parse Sonucu").grid(row=0, column=0, padx=(0, 8), pady=4, sticky="w")
        ttk.Label(diagnostics, textvariable=self.parse_status_var).grid(row=0, column=1, padx=(0, 16), pady=4, sticky="w")
        ttk.Label(diagnostics, text="Parsed Satir").grid(row=0, column=2, padx=(0, 8), pady=4, sticky="w")
        ttk.Label(diagnostics, textvariable=self.parsed_count_var).grid(row=0, column=3, padx=(0, 16), pady=4, sticky="w")
        ttk.Label(diagnostics, text="Yok Sayilan Satir").grid(row=0, column=4, padx=(0, 8), pady=4, sticky="w")
        ttk.Label(diagnostics, textvariable=self.ignored_count_var).grid(row=0, column=5, pady=4, sticky="w")

    def _build_operator_page(self, container: ttk.Frame) -> None:
        """
        Operatör çalışma ekranını inşa eder:
        - Üst bar: kullanıcı adı, Ayarlar/Debug/Çıkış butonları
        - Barkod satırı: Yeni Parti, Barkodu Getir, Hata Kodları, Kaydet/Yazdır
        - Parti bilgisi kartları (müşteri, parti, renk, kalite talimati vb.)
        - Anlık değerler (metre/kg) ve yazıcı seçimi
        - Alt durum çubuğu: COM/WEB/YAZICI bağlantı durumları
        """
        container.columnconfigure(0, weight=1)
        container.rowconfigure(3, weight=1)

        header = ttk.Frame(container)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="KALITE OPERATOR PANELI", font=("Segoe UI", 28, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        actions = ttk.Frame(header)
        actions.grid(row=0, column=1, sticky="e")
        ttk.Label(actions, textvariable=self.logged_user_var, font=("Segoe UI", 14, "bold")).pack(side="left", padx=(0, 12))
        self.settings_button = ttk.Button(actions, text="Ayarlar", command=self.show_settings_window, style="OperatorNeutral.TButton")
        self.error_code_admin_button = ttk.Button(
            actions,
            text="Hata Kod Yonetimi",
            command=self.show_error_code_admin_window,
            style="OperatorNeutral.TButton",
        )
        self.logs_button = ttk.Button(actions, text="Kayit Loglari", command=self.show_logs_window, style="OperatorNeutral.TButton")
        self.service_button = ttk.Button(actions, text="Debug/Test", command=self.show_service_window, style="OperatorNeutral.TButton")
        ttk.Button(actions, text="Cikis", command=self.logout, style="OperatorDanger.TButton").pack(side="left")

        scan_frame = ttk.LabelFrame(container, text="Barkod ve Kayit", padding=20)
        scan_frame.grid(row=1, column=0, sticky="ew")
        scan_frame.columnconfigure(1, weight=1)

        ttk.Label(scan_frame, text="Barkod", font=("Segoe UI", 18, "bold")).grid(
            row=0, column=0, padx=(0, 12), pady=6, sticky="w"
        )
        self.barcode_entry = ttk.Entry(scan_frame, textvariable=self.barcode_var, width=34)
        self.barcode_entry.grid(row=0, column=1, padx=(0, 12), pady=6, sticky="ew")
        self.barcode_entry.bind("<Return>", lambda _event: self.lookup_barcode())
        ttk.Button(scan_frame, text="Yeni Parti", command=self.start_new_party, style="OperatorDanger.TButton").grid(
            row=0, column=2, padx=(0, 12), pady=6
        )
        ttk.Button(scan_frame, text="Barkodu Getir", command=self.lookup_barcode, style="OperatorInfo.TButton").grid(
            row=0, column=3, padx=(0, 12), pady=6
        )
        ttk.Button(scan_frame, text="Hata Kodlari", command=self.open_error_code_window, style="OperatorWarn.TButton").grid(
            row=0, column=4, padx=(0, 12), pady=6
        )
        ttk.Button(
            scan_frame,
            text="Kaydet / Yazdir",
            command=lambda: self.save_operator_record(print_label=True),
            style="OperatorSuccess.TButton",
        ).grid(
            row=0, column=5, pady=6
        )

        ttk.Label(
            scan_frame,
            textvariable=self.operator_status_var,
            font=("Segoe UI", 16, "bold"),
            foreground="#0f5f35",
        ).grid(row=1, column=0, columnspan=5, pady=(12, 0), sticky="w")

        content = ttk.Frame(container)
        content.grid(row=2, column=0, sticky="nsew", pady=(18, 0))
        content.columnconfigure(0, weight=3)
        content.columnconfigure(1, weight=2)
        content.rowconfigure(0, weight=1)

        details_frame = ttk.LabelFrame(content, text="Parti Bilgisi", padding=20)
        details_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        details_frame.columnconfigure(0, weight=1)
        details_frame.columnconfigure(1, weight=1)

        self._create_info_tile(details_frame, 0, 0, "Musteri", self.customer_var, frame_style="InfoBlue.TLabelframe", label_style="CardValueBlue.TLabel")
        self._create_info_tile(details_frame, 0, 1, "Parti No", self.party_no_var, frame_style="InfoWarm.TLabelframe", label_style="CardValueWarm.TLabel")
        self._create_info_tile(details_frame, 1, 0, "Parti ID", self.party_id_var, frame_style="InfoBlue.TLabelframe", label_style="CardValueBlue.TLabel")
        self._create_info_tile(details_frame, 1, 1, "Sarj No", self.sarj_no_var, frame_style="InfoWarm.TLabelframe", label_style="CardValueWarm.TLabel")
        self._create_info_tile(details_frame, 2, 0, "Kalite", self.kalite_var, frame_style="InfoBlue.TLabelframe", label_style="CardValueBlue.TLabel")
        self._create_info_tile(details_frame, 2, 1, "Renk", self.renk_var, frame_style="InfoWarm.TLabelframe", label_style="CardValueWarm.TLabel")

        notes_frame = ttk.Frame(details_frame)
        notes_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        notes_frame.columnconfigure(1, weight=1)
        ttk.Label(notes_frame, text="Kalite Talimati", font=("Segoe UI", 16, "bold")).grid(
            row=0, column=0, padx=(0, 12), sticky="nw"
        )
        ttk.Label(
            notes_frame,
            textvariable=self.kalite_talimati_var,
            font=("Segoe UI", 14),
            justify="left",
            wraplength=720,
        ).grid(row=0, column=1, sticky="ew")
        ttk.Label(notes_frame, text="Operator Notu", font=("Segoe UI", 16, "bold")).grid(
            row=1, column=0, padx=(0, 12), pady=(14, 0), sticky="w"
        )
        ttk.Entry(notes_frame, textvariable=self.operator_notes_var).grid(row=1, column=1, pady=(14, 0), sticky="ew")
        ttk.Label(notes_frame, text="Hata Kodu", font=("Segoe UI", 16, "bold")).grid(
            row=2, column=0, padx=(0, 12), pady=(14, 0), sticky="w"
        )
        ttk.Label(notes_frame, textvariable=self.selected_error_code_var, font=("Segoe UI", 15, "bold")).grid(
            row=2, column=1, pady=(14, 0), sticky="w"
        )
        error_actions = ttk.Frame(notes_frame)
        error_actions.grid(row=3, column=0, columnspan=2, sticky="w", pady=(14, 0))
        ttk.Button(error_actions, text="Hata Kodunu Temizle", command=self.clear_error_code_selection).pack(side="left")

        sidebar = ttk.Frame(content)
        sidebar.grid(row=0, column=1, sticky="nsew")
        sidebar.columnconfigure(0, weight=1)

        totals_frame = ttk.LabelFrame(sidebar, text="Anlik Degerler", padding=20)
        totals_frame.grid(row=0, column=0, sticky="ew")
        totals_frame.columnconfigure(0, weight=1)
        totals_frame.columnconfigure(1, weight=1)
        self._create_total_tile(totals_frame, 0, "METRE", self.last_meter_var, frame_style="InfoBlue.TLabelframe", label_style="CardValueBlue.TLabel")
        self._create_total_tile(totals_frame, 1, "KG", self.last_kg_var, frame_style="InfoGreen.TLabelframe", label_style="CardValueGreen.TLabel")

        printer_frame = ttk.LabelFrame(sidebar, text="Etiket Yazici", padding=20)
        printer_frame.grid(row=1, column=0, sticky="ew", pady=(16, 0))
        printer_frame.columnconfigure(0, weight=1)
        ttk.Label(printer_frame, text="Yazici", font=("Segoe UI", 16, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        try:
            import win32print as _wp  # type: ignore[import-untyped]
            _pnames = [p[2] for p in _wp.EnumPrinters(_wp.PRINTER_ENUM_LOCAL | _wp.PRINTER_ENUM_CONNECTIONS)]
        except Exception:
            _pnames = []
        self.printer_name_combo = ttk.Combobox(
            printer_frame, textvariable=self.printer_name_var, values=_pnames, width=30
        )
        self.printer_name_combo.grid(row=1, column=0, pady=(0, 12), sticky="ew")
        ttk.Checkbutton(
            printer_frame,
            text="Kayit sonrasi otomatik bas",
            variable=self.auto_print_var,
        ).grid(row=2, column=0, sticky="w")
        ttk.Button(printer_frame, text="Test Etiketi", command=self._print_test_label).grid(
            row=3, column=0, pady=(12, 0), sticky="ew"
        )

        footer = ttk.Frame(container)
        footer.grid(row=3, column=0, sticky="ew", pady=(18, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(
            footer,
            text="Tam ekran cikis: ESC   |   Tam ekran geri al: F11   |   Servis penceresi: Ctrl+Shift+D",
            font=("Segoe UI", 12),
            foreground="#556371",
        ).grid(row=0, column=0, sticky="w")
        status_bar = tk.Frame(footer, background="#f2f4f7")
        status_bar.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.serial_status_label = tk.Label(
            status_bar,
            textvariable=self.serial_connection_var,
            font=("Segoe UI", 12, "bold"),
            fg="#0f5f35",
            bg="#f2f4f7",
        )
        self.serial_status_label.pack(side="left", padx=(0, 18))
        self.webservice_status_label = tk.Label(
            status_bar,
            textvariable=self.webservice_connection_var,
            font=("Segoe UI", 12, "bold"),
            fg="#9a6700",
            bg="#f2f4f7",
        )
        self.webservice_status_label.pack(side="left", padx=(0, 18))
        self.printer_status_label = tk.Label(
            status_bar,
            textvariable=self.printer_connection_var,
            font=("Segoe UI", 12, "bold"),
            fg="#9a6700",
            bg="#f2f4f7",
        )
        self.printer_status_label.pack(side="left")

    def _build_payload_preview(self, container: ttk.Frame, row: int) -> None:
        """
        Debug ekranında 'Kaydet' işlemi öncesinde gönderilecek JSON payload'u
        gösteren salt-okunur metin kutusu oluşturur.
        """
        payload_frame = ttk.LabelFrame(container, text="Save Payload Preview", padding=12)
        payload_frame.grid(row=row, column=0, sticky="nsew", pady=(12, 0))
        payload_frame.columnconfigure(0, weight=1)
        payload_frame.rowconfigure(0, weight=1)

        self.payload_preview = tk.Text(payload_frame, wrap="word", state="disabled", height=16)
        self.payload_preview.grid(row=0, column=0, sticky="nsew")
        self.refresh_payload_preview()

    def _create_info_tile(
        self,
        parent: ttk.Frame,
        row: int,
        column: int,
        title: str,
        variable: tk.StringVar,
        frame_style: str = "TLabelframe",
        label_style: str = "TLabel",
    ) -> None:
        """
        Parti bilgisi bölümünde tek bir bilgi kartı (LabelFrame + Label) oluşturur.
        row/column konumuna yerleştirilir; stil parçaları renk temasini belirler.
        """
        tile = ttk.LabelFrame(parent, text=title, padding=16, style=frame_style)
        tile.grid(row=row, column=column, padx=8, pady=8, sticky="nsew")
        parent.rowconfigure(row, weight=1)
        parent.columnconfigure(column, weight=1)
        ttk.Label(tile, textvariable=variable, style=label_style).pack(anchor="w")

    def _create_total_tile(
        self,
        parent: ttk.Frame,
        column: int,
        title: str,
        variable: tk.StringVar,
        frame_style: str = "TLabelframe",
        label_style: str = "TLabel",
    ) -> None:
        """
        Anlık değerler bölümünde metre veya kg özet kartı oluşturur.
        _create_info_tile'dan farkı: sadece column konumlandırması kullanır.
        """
        tile = ttk.LabelFrame(parent, text=title, padding=18, style=frame_style)
        tile.grid(row=0, column=column, padx=8, pady=8, sticky="ew")
        ttk.Label(tile, textvariable=variable, style=label_style).pack(anchor="center")

    def open_error_code_window(self) -> None:
        """
        Hata kodu seçim penceresini açar.
        Pencere zaten açıksa öne getirir. ERROR_CODE_GROUPS sözlüğündeki
        gruplar sütun halinde listelenir; seçim set_error_code() ile işlenir.
        """
        if self.error_code_window is not None and self.error_code_window.winfo_exists():
            self.error_code_window.lift()
            self.error_code_window.focus_force()
            return

        self.error_code_window = tk.Toplevel(self.root)
        self.error_code_window.title("Hata Kodlari")
        self.error_code_window.geometry("1120x760+80+80")
        self.error_code_window.configure(background="#f2f4f7")
        self.error_code_window.transient(self.root)
        self.error_code_window.grab_set()
        self.error_code_window.protocol("WM_DELETE_WINDOW", self._close_error_code_window)

        shell = ttk.Frame(self.error_code_window, padding=20)
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(0, weight=1)
        shell.columnconfigure(1, weight=1)

        for column, (group_name, codes) in enumerate(self.error_code_groups.items()):
            group_frame = ttk.LabelFrame(shell, text=group_name, padding=16)
            group_frame.grid(row=0, column=column, padx=10, sticky="nsew")
            group_frame.columnconfigure(0, weight=1)
            for row, (code, description) in enumerate(codes):
                ttk.Button(
                    group_frame,
                    text=f"{code} - {description}",
                    command=lambda g=group_name, c=code, d=description: self.set_error_code(g, c, d),
                ).grid(row=row, column=0, sticky="ew", pady=6)

        footer = ttk.Frame(shell)
        footer.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        ttk.Button(footer, text="Hata Yok", command=self.clear_error_code_selection).pack(side="left")
        ttk.Button(footer, text="Kapat", command=self._close_error_code_window).pack(side="right")

    def _close_error_code_window(self) -> None:
        """Hata kodu penceresini güvenli şekilde kapatır (grab release + destroy)."""
        if self.error_code_window is None:
            return
        if self.error_code_window.winfo_exists():
            self.error_code_window.grab_release()
            self.error_code_window.destroy()
        self.error_code_window = None

    def set_error_code(self, category: str, code: str, description: str) -> None:
        """
        Seçilen hata kodunu current_error_code'a kaydeder, operatör ekranında
        gösterir ve payload önizlemesini günceller. Ardından pencereyi kapatır.
        """
        self.current_error_code = {
            "category": category,
            "code": code,
            "description": description,
        }
        self.selected_error_code_var.set(f"{code} - {description}")
        self.operator_status_var.set(f"Hata kodu secildi: {code} - {description}")
        self.refresh_payload_preview()
        self._close_error_code_window()

    def clear_error_code_selection(self) -> None:
        """
        Seçili hata kodunu sıfırlar, UI göstergelerini 'Hata kodu secilmedi' yapar
        ve pencere açıksa kapatır.
        """
        self.current_error_code = {}
        self.selected_error_code_var.set("Hata kodu secilmedi")
        self.refresh_payload_preview()
        if self.error_code_window is not None and self.error_code_window.winfo_exists():
            self._close_error_code_window()

    def show_service_window(self) -> None:
        """
        Admin debug/test penceresini gösterir.
        Giriş yapılmamışsa veya kullanıcı admin değilse erişim reddedilir.
        """
        if not self.current_user_data:
            self.login_status_var.set("Servis ekranina erismek icin once giris yapin")
            return
        if not self._is_admin():
            self.operator_status_var.set("Bu kullanici servis/test ekranina erisemez")
            return
        if self.service_window is None:
            return
        self.service_window.deiconify()
        self.service_window.lift()
        self.service_window.focus_force()

    def show_settings_window(self) -> None:
        """
        Ayarlar penceresini gösterir.
        Giriş yapılmamışsa veya kullanıcı admin değilse erişim reddedilir.
        """
        if not self.current_user_data:
            self.login_status_var.set("Ayarlar ekranina erismek icin once giris yapin")
            return
        if not self._is_admin():
            self.operator_status_var.set("Bu kullanici ayarlar ekranina erisemez")
            return
        if self.settings_window is None:
            return
        self.settings_window.deiconify()
        self.settings_window.lift()
        self.settings_window.focus_force()

    def show_logs_window(self) -> None:
        """
        Kayıt logları penceresini gösterir; açılmadan önce tabloyu günceller.
        Giriş yapılmamışsa veya kullanıcı admin değilse erişim reddedilir.
        """
        if not self.current_user_data:
            self.login_status_var.set("Kayit loglari ekranina erismek icin once giris yapin")
            return
        if not self._is_admin():
            self.operator_status_var.set("Bu kullanici kayit loglari ekranina erisemez")
            return
        if self.logs_window is None:
            return
        self.refresh_logs_view()
        self.logs_window.deiconify()
        self.logs_window.lift()
        self.logs_window.focus_force()

    def show_error_code_admin_window(self) -> None:
        if not self.current_user_data:
            self.login_status_var.set("Hata kodu yonetimine erismek icin once giris yapin")
            return
        if not self._is_admin():
            self.operator_status_var.set("Bu kullanici hata kodu yonetimine erisemez")
            return
        if self.error_code_admin_window is None:
            return
        self.refresh_error_code_admin_view()
        self.error_code_admin_window.deiconify()
        self.error_code_admin_window.lift()
        self.error_code_admin_window.focus_force()

    def _toggle_service_window(self, _event: tk.Event | None = None) -> str | None:
        """
        Ctrl+Shift+D kısayoluyla debug penceresini açıp kapatir (toggle).
        Admin değilse 'break' döndürerek olayı yürtmez.
        """
        if self.service_window is None:
            return None
        if not self._is_admin():
            return "break"
        if self.service_window.state() == "withdrawn":
            self.show_service_window()
        else:
            self.service_window.withdraw()
        return "break"

    def _exit_fullscreen(self, _event: tk.Event | None = None) -> str:
        """ESC tuşuyla tam ekrandan çıkar."""
        self.root.attributes("-fullscreen", False)
        return "break"

    def _enter_fullscreen(self, _event: tk.Event | None = None) -> str:
        """F11 tuşuyla tam ekrana geri döner."""
        self.root.attributes("-fullscreen", True)
        return "break"

    def login(self) -> None:
        """
        Şifre alanından şifreyi alıp validasyonu yapar.
        Boş değilse arka plan thread'inde _login_request()'i çalıştırır.
        """
        password = self.password_var.get().strip()
        if not password:
            self.login_status_var.set("Sifre zorunludur")
            return

        self.login_status_var.set("Giris kontrol ediliyor...")
        threading.Thread(target=self._login_request, args=(password,), daemon=True).start()

    def _login_request(self, password: str) -> None:
        """
        Login işlemini arka planda gerçekleştirir.
        - login_url ayarlanmışsa: JSON body ile HTTP POST atar, yanıtı doğrular.
        - URL yoksa: TEST_USER_PASSWORD ve TEST_ADMIN_PASSWORD ile offline test modu.
        Başarılı yanıtta _apply_login_success() ana thread'de çağrılır.
        """
        login_url = self.login_url_var.get().strip()
        if not login_url:
            if password == TEST_USER_PASSWORD:
                mock_data = dict(DEFAULT_LOGIN_RESPONSE)
                self.root.after(0, lambda: self._apply_login_success(mock_data, "Test user girisi yapildi"))
                return
            if password == TEST_ADMIN_PASSWORD:
                mock_data = dict(DEFAULT_LOGIN_RESPONSE)
                mock_data.update(
                    {
                        "name": "Test",
                        "surname": "Admin",
                        "userid": "admin-test",
                        "userrole": "admin",
                    }
                )
                self.root.after(0, lambda: self._apply_login_success(mock_data, "Test admin girisi yapildi"))
                return
            self.root.after(
                0,
                lambda: self.login_status_var.set("Giris basarisiz: test sifresi gecersiz"),
            )
            return

        try:
            body = json.dumps({"password": password}).encode("utf-8")
            request = urllib.request.Request(
                login_url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = response.read().decode("utf-8")
            data = json.loads(payload)
            if not isinstance(data, dict):
                raise ValueError("Beklenen JSON nesnesi donmedi")
            for key in ("name", "surname", "userid", "userrole"):
                if key not in data or data[key] in (None, ""):
                    raise ValueError(f"Eksik alan: {key}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            self.root.after(0, lambda e=exc: self.login_status_var.set(f"Giris basarisiz: {e}"))
            return

        self.root.after(0, lambda: self._apply_login_success(data, "Giris basarili"))

    def _apply_login_success(self, data: dict[str, object], status_message: str) -> None:
        """
        Giriş başarılı olduğunda kullanıcı bilgilerini kaydeder,
        rol bazında butonları güncelleir ve operatör sayfasına geçer.
        """
        self.current_user_data = {
            "name": str(data.get("name", "")),
            "surname": str(data.get("surname", "")),
            "userid": str(data.get("userid", "")),
            "userrole": str(data.get("userrole", "")),
        }
        full_name = f"{self.current_user_data['name']} {self.current_user_data['surname']}".strip()
        role = self.current_user_data["userrole"]
        self.logged_user_var.set(f"{full_name} | {role} | ID: {self.current_user_data['userid']}")
        self._apply_role_access()
        self.login_status_var.set(status_message)
        self.operator_status_var.set(f"Hos geldiniz {full_name}")
        self.login_page.pack_forget()
        self.operator_page.pack(fill="both", expand=True)
        self.root.after(50, self.barcode_entry.focus_set)

    def logout(self) -> None:
        """
        Oturumu kapatır: seri bağlantıyı keser, pop-up pencereleri gizler,
        kullanıcı verisini temizler ve login sayfasına döner.
        Oturum ölçümleri ve parti bağlamı da sıfırlanır.
        """
        self.disconnect()
        if self.service_window is not None:
            self.service_window.withdraw()
        if self.settings_window is not None:
            self.settings_window.withdraw()
        if self.error_code_admin_window is not None:
            self.error_code_admin_window.withdraw()
        if self.logs_window is not None:
            self.logs_window.withdraw()
        self.current_user_data = {}
        self.password_var.set("")
        self.logged_user_var.set("Giris yapilmadi")
        self.login_status_var.set("Oturum kapatildi")
        self.operator_status_var.set("Servis hazir degil")
        self._apply_role_access()
        self.operator_page.pack_forget()
        self.login_page.pack(fill="both", expand=True)
        self.clear_session()
        self._clear_party_context()
        self.root.after(50, self.password_entry.focus_set)

    def _is_admin(self) -> bool:
        """Oturumdaki kullanıcının rolünün 'admin' olup olmadığını kontrol eder."""
        return self.current_user_data.get("userrole", "").strip().lower() == "admin"

    def _apply_role_access(self) -> None:
        """
        Kullanıcı rolüne göre üst menü butonlarını gösterir/gizler.
        Admin ise Ayarlar, Kayıt Logları ve Debug/Test butonları görünür olur.
        """
        if (
            not hasattr(self, "service_button")
            or not hasattr(self, "settings_button")
            or not hasattr(self, "logs_button")
            or not hasattr(self, "error_code_admin_button")
        ):
            return
        self.service_button.pack_forget()
        self.settings_button.pack_forget()
        self.error_code_admin_button.pack_forget()
        self.logs_button.pack_forget()
        if self._is_admin():
            self.settings_button.pack(side="left", padx=(0, 8))
            self.error_code_admin_button.pack(side="left", padx=(0, 8))
            self.logs_button.pack(side="left", padx=(0, 8))
            self.service_button.pack(side="left", padx=(0, 8))

    def _set_logs_today_filter(self) -> None:
        """Log tarih filtresini bugüne ayarlar ve tabloyu yeniler."""
        self.log_date_filter_var.set(datetime.now().strftime("%Y-%m-%d"))
        self.refresh_logs_view()

    def refresh_logs_view(self) -> None:
        """
        Kayıt logları tablosunu operator_records.json'dan okuyarak günceller.
        log_date_filter_var'daki tarihe göre süzgü uygular ('YYYY-MM-DD' formatı).
        Boş bırakılırsa tüm kayıtlar listelenir.
        """
        if not hasattr(self, "logs_tree"):
            return

        for item_id in self.logs_tree.get_children():
            self.logs_tree.delete(item_id)

        records = self._load_saved_records()
        date_filter = self.log_date_filter_var.get().strip()
        filtered_records: list[dict[str, object]] = []
        for record in reversed(records):
            saved_at = str(record.get("saved_at", ""))
            if date_filter and not saved_at.startswith(date_filter):
                continue
            filtered_records.append(record)

        for record in filtered_records:
            operator_name = " ".join(
                part for part in [str(record.get("operator_name", "")), str(record.get("operator_surname", ""))] if part
            ).strip()
            if not operator_name:
                operator_name = str(record.get("operator_userid", ""))
            error_text = str(record.get("error_code", ""))
            if record.get("error_description"):
                error_text = f"{error_text} - {record.get('error_description', '')}".strip(" -")

            self.logs_tree.insert(
                "",
                "end",
                values=(
                    str(record.get("saved_at", "")),
                    str(record.get("party_no", "")),
                    str(record.get("barcode", "")),
                    f"{float(record.get('meter', 0) or 0):.2f}",
                    f"{float(record.get('kg', 0) or 0):.2f}",
                    error_text,
                    operator_name,
                    str(record.get("trigger", "")),
                ),
            )

        self.log_summary_var.set(f"{len(filtered_records)} kayit listelendi")

    def _load_saved_records(self) -> list[dict[str, object]]:
        """
        LOCAL_SAVE_PATH (operator_records.json) dosyasından tüm kayıtları okur.
        Dosya yoksa, bozuksa veya liste değilse boş liste döndürür.
        """
        if not LOCAL_SAVE_PATH.exists():
            return []
        try:
            data = json.loads(LOCAL_SAVE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def _create_stat_card(self, parent: ttk.Frame, column: int, title: str, variable: tk.StringVar) -> None:
        """Debug ekranında üst satırda gösterilen küçük istatistik kartı oluşturur."""
        card = ttk.LabelFrame(parent, text=title, padding=12)
        card.grid(row=0, column=column, padx=6, sticky="ew")
        ttk.Label(card, textvariable=variable, font=("Segoe UI", 18, "bold")).pack(anchor="center")

    def refresh_ports(self) -> None:
        """
        Sistemdeki COM portlarını tarar, combobox listelerini günceller.
        Uygun port yoksa ilkini seçer. Yazıcı listesini ve seri durum göstergesini de yeniler.
        """
        ports = [port.device for port in list_ports.comports()]
        self.port_combo["values"] = ports
        if hasattr(self, "settings_port_combo"):
            self.settings_port_combo["values"] = ports
        if ports and not self.port_var.get().strip():
            self.port_var.set(ports[0])
        elif not ports and not self.port_var.get().strip():
            self.port_var.set("")
        self._refresh_printers()
        self._update_serial_connection_status()
        self.status_var.set(f"{len(ports)} port(s) found")

    def _setup_setting_traces(self) -> None:
        """
        Tüm ayar değişkenlerine (port, baud, URL'ler, yazıcı vb.) 'write' trace ekler.
        Değişen her ayar otomatik olarak _on_setting_changed() ile kaydedilir.
        """
        variables = [
            self.port_var,
            self.baud_var,
            self.default_unit_var,
            self.machine_zero_tolerance_var,
            self.login_url_var,
            self.barcode_lookup_url_var,
            self.save_measurement_url_var,
            self.health_url_var,
            self.printer_name_var,
            self.auto_print_var,
        ]
        for variable in variables:
            variable.trace_add("write", self._on_setting_changed)

    def _on_setting_changed(self, *_args: object) -> None:
        """
        Herhangi bir ayar değiştiğinde tetiklenir:
        - Ayarları JSON'a kaydeder
        - Seri ve yazıcı durum göstergelerini günceller
        - Web servis sağlık kontrolu başlatır
        """
        self._save_settings()
        self._update_serial_connection_status()
        self._update_printer_connection_status()
        self._request_webservice_health_check()

    def _load_settings(self) -> None:
        """
        app_settings.json dosyasından ayarları okuyup ilgili StringVar/BooleanVar'lara yazar.
        Dosya yoksa veya geçersizse mevcut varsayılan değerler korunur.
        """
        if not SETTINGS_PATH.exists():
            return
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return

        self.port_var.set(str(data.get("port", self.port_var.get())))
        self.baud_var.set(str(data.get("baud", self.baud_var.get())))
        self.default_unit_var.set(str(data.get("default_unit", self.default_unit_var.get())))
        self.machine_zero_tolerance_var.set(str(data.get("machine_zero_tolerance", self.machine_zero_tolerance_var.get())))
        self.login_url_var.set(str(data.get("login_url", self.login_url_var.get())))
        self.barcode_lookup_url_var.set(str(data.get("barcode_lookup_url", self.barcode_lookup_url_var.get())))
        self.save_measurement_url_var.set(str(data.get("save_measurement_url", self.save_measurement_url_var.get())))
        self.health_url_var.set(str(data.get("health_url", self.health_url_var.get())))
        self.printer_name_var.set(str(data.get("printer_name", self.printer_name_var.get())))
        self.auto_print_var.set(bool(data.get("auto_print", self.auto_print_var.get())))

    def _save_settings(self) -> None:
        """
        Mevcut ayar değişkenlerini app_settings.json dosyasına JSON formatında kaydeder.
        Yazım hatası oluşursa sessizce göz ardı edilir.
        """
        settings_payload = {
            "port": self.port_var.get().strip(),
            "baud": self.baud_var.get().strip(),
            "default_unit": self.default_unit_var.get().strip(),
            "machine_zero_tolerance": self.machine_zero_tolerance_var.get().strip(),
            "login_url": self.login_url_var.get().strip(),
            "barcode_lookup_url": self.barcode_lookup_url_var.get().strip(),
            "save_measurement_url": self.save_measurement_url_var.get().strip(),
            "health_url": self.health_url_var.get().strip(),
            "printer_name": self.printer_name_var.get().strip(),
            "auto_print": self.auto_print_var.get(),
        }
        try:
            SETTINGS_PATH.write_text(json.dumps(settings_payload, ensure_ascii=True, indent=2), encoding="utf-8")
        except OSError:
            return

    def _auto_connect_saved_port(self) -> None:
        """
        Uygulama başlatılırken kaydetilen port adı varsa ve bağlanılı değilse
        otomatik olarak seri porta bağlanmayı dener.
        """
        port_name = self.port_var.get().strip()
        if not port_name:
            return
        if self.reader and self.reader.is_alive():
            return
        self.connect()

    def _schedule_connectivity_checks(self) -> None:
        """
        Her 15 saniyede bir seri port, yazıcı ve web servis bağlantı durumlarını
        kontrol eden periyodik döngüyü yönetir.
        """
        self._update_serial_connection_status()
        self._update_printer_connection_status()
        self._request_webservice_health_check()
        self.root.after(15000, self._schedule_connectivity_checks)

    def _set_status_label(self, label: tk.Label | None, variable: tk.StringVar, text: str, color: str) -> None:
        """Verilen StringVar'a metin, Label widget'a renk atar. Kod tekrarını azaltan yardımcı."""
        variable.set(text)
        if label is not None:
            label.configure(fg=color)

    def _update_serial_connection_status(self) -> None:
        """
        Alt durum çubuğundaki COM göstergesini günceller:
        BAGLI (yeşil) / HAZIR (sarı) / BULUNAMADI (kırmızı) / SECiLMEDi (kırmızı).
        """
        port_name = self.port_var.get().strip()
        available_ports = list(self.port_combo.cget("values")) if hasattr(self, "port_combo") else []
        if self.reader and self.reader.is_alive():
            self._set_status_label(self.serial_status_label, self.serial_connection_var, f"COM: BAGLI ({port_name})", "#0f5f35")
            return
        if port_name and port_name in available_ports:
            self._set_status_label(self.serial_status_label, self.serial_connection_var, f"COM: HAZIR ({port_name})", "#9a6700")
            return
        if port_name:
            self._set_status_label(self.serial_status_label, self.serial_connection_var, f"COM: BULUNAMADI ({port_name})", "#b42318")
            return
        self._set_status_label(self.serial_status_label, self.serial_connection_var, "COM: SECILMEDI", "#b42318")

    def _update_printer_connection_status(self) -> None:
        """
        Alt durum çubuğundaki YAZICI göstergesini günceller:
        HAZIR (yeşil) / BULUNAMADI (kırmızı) / SECiLMEDi (kırmızı).
        """
        printer_name = self.printer_name_var.get().strip()
        if printer_name and printer_name in self.available_printers:
            self._set_status_label(self.printer_status_label, self.printer_connection_var, f"YAZICI: HAZIR ({printer_name})", "#0f5f35")
            return
        if printer_name:
            self._set_status_label(self.printer_status_label, self.printer_connection_var, f"YAZICI: BULUNAMADI ({printer_name})", "#b42318")
            return
        self._set_status_label(self.printer_status_label, self.printer_connection_var, "YAZICI: SECILMEDI", "#b42318")

    def _resolve_health_url(self) -> str:
        """
        Web servis sağlık kontrol URL'sini belirler.
        Explicit olarak girilmişse onu kullanır; yoksa kayıt/barkod/login URL'lerinden
        '/health' yolu otomatik oluşturulur.
        """
        explicit_url = self.health_url_var.get().strip()
        if explicit_url:
            return explicit_url

        for candidate in (
            self.save_measurement_url_var.get().strip(),
            self.barcode_lookup_url_var.get().strip(),
            self.login_url_var.get().strip(),
        ):
            if not candidate:
                continue
            parsed = urllib.parse.urlsplit(candidate)
            path = parsed.path.rstrip("/")
            if "/" in path:
                path = path.rsplit("/", 1)[0]
            health_path = f"{path}/health" if path else "/health"
            return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, health_path, "", ""))
        return ""

    def _request_webservice_health_check(self) -> None:
        """
        Sağlık kontrol URL'si mevcutsa ve uçuca bir istek yoksa arka plan
        thread'inde _check_webservice_health()'i başlatır.
        """
        health_url = self._resolve_health_url()
        if not health_url:
            self._set_status_label(self.webservice_status_label, self.webservice_connection_var, "WEB: AYARLANMADI", "#9a6700")
            return
        if self.web_health_check_inflight:
            return
        self.web_health_check_inflight = True
        threading.Thread(target=self._check_webservice_health, args=(health_url,), daemon=True).start()

    def _check_webservice_health(self, health_url: str) -> None:
        """
        Health endpoint'e HTTP GET atar. 2xx ise WEB: BAGLI (yeşil),
        hata kodunda WEB: HATA, bağlanamıyorsa WEB: ULAŞILAMIYOR gösterir.
        web_health_check_inflight bayrağını sonunda sıfırlar.
        """
        try:
            request = urllib.request.Request(health_url, method="GET")
            with urllib.request.urlopen(request, timeout=5) as response:
                status_code = getattr(response, "status", 200)
            if 200 <= status_code < 300:
                self.root.after(
                    0,
                    lambda: self._set_status_label(
                        self.webservice_status_label,
                        self.webservice_connection_var,
                        f"WEB: BAGLI ({health_url})",
                        "#0f5f35",
                    ),
                )
            else:
                self.root.after(
                    0,
                    lambda: self._set_status_label(
                        self.webservice_status_label,
                        self.webservice_connection_var,
                        f"WEB: HATA ({status_code})",
                        "#b42318",
                    ),
                )
        except (urllib.error.URLError, TimeoutError):
            self.root.after(
                0,
                lambda: self._set_status_label(
                    self.webservice_status_label,
                    self.webservice_connection_var,
                    "WEB: ULASILAMIYOR",
                    "#b42318",
                ),
            )
        finally:
            self.web_health_check_inflight = False

    def _refresh_printers(self) -> None:
        """
        win32print ile sistemdeki yazıcıları listeler, available_printers'a kaydeder
        ve ilgili combobox'ları günceller. win32print bulunamazsa liste boş kalır.
        """
        try:
            import win32print as _wp  # type: ignore[import-untyped]
            printer_names = [p[2] for p in _wp.EnumPrinters(_wp.PRINTER_ENUM_LOCAL | _wp.PRINTER_ENUM_CONNECTIONS)]
        except Exception:
            printer_names = []

        self.available_printers = printer_names

        if hasattr(self, "printer_name_combo"):
            self.printer_name_combo["values"] = printer_names
        if hasattr(self, "settings_printer_combo"):
            self.settings_printer_combo["values"] = printer_names
        self._update_printer_connection_status()

    def connect(self) -> None:
        """
        Seçili COM portuna seri bağlantı kurar.
        SerialReader thread başlatılır; önceden demo çalışıyorsa durdurulur.
        """
        if self.reader and self.reader.is_alive():
            messagebox.showinfo("Connection", "Serial port is already connected.")
            return

        self.stop_demo()

        port_name = self.port_var.get().strip()
        if not port_name:
            messagebox.showwarning("Connection", "Select a serial port first.")
            return

        try:
            baud_rate = int(self.baud_var.get())
        except ValueError:
            messagebox.showwarning("Connection", "Baud rate must be numeric.")
            return

        self.reader = SerialReader(port_name, baud_rate, self.serial_queue)
        self.reader.start()
        self.status_var.set("Connecting...")
        self.source_var.set(port_name)
        self._update_serial_connection_status()

    def disconnect(self) -> None:
        """Seri bağlantıyı ve varsa demo akışını durdurur; kaynak durumunu 'Idle' yapar."""
        if self.reader:
            self.reader.stop()
            self.reader = None
        self.stop_demo()
        self.source_var.set("Idle")
        self.status_var.set("Disconnected")
        self._update_serial_connection_status()

    def start_demo(self) -> None:
        """
        Dahili demo veri akışını başlatır.
        Seri bağlantı varsa izin vermez. demo_samples listesindeki örnekleri
        900ms aralıklarla handle_serial_line()'a gönderir.
        """
        if self.reader and self.reader.is_alive():
            messagebox.showinfo("Demo", "Disconnect the serial connection before starting demo data.")
            return
        if self.demo_job is not None:
            return
        self.status_var.set("Demo stream running")
        self.source_var.set("Built-in demo")
        self.demo_index = 0
        self.schedule_demo_line()

    def schedule_demo_line(self) -> None:
        """
        demo_samples listesinden sıradaki satırı handle_serial_line()'a gönderir
        ve kendini 900ms sonra tekrar çağırır (after loop).
        """
        line = self.demo_samples[self.demo_index % len(self.demo_samples)]
        self.demo_index += 1
        self.handle_serial_line(line)
        self.demo_job = self.root.after(900, self.schedule_demo_line)

    def stop_demo(self) -> None:
        """Demo after loop'unu iptal eder. Seri bağlantı da yoksa durum mesajını günceller."""
        if self.demo_job is not None:
            self.root.after_cancel(self.demo_job)
            self.demo_job = None
            if not (self.reader and self.reader.is_alive()):
                self.status_var.set("Demo stream stopped")

    def send_manual_line(self) -> None:
        """
        Debug ekranındaki manuel satır giriş kutusundaki değeri
        doğrudan handle_serial_line()'a gönderir (test amaçlı).
        """
        line = self.manual_line_var.get().strip()
        if not line:
            messagebox.showwarning("Manual Test", "Enter a line to test first.")
            return
        self.source_var.set("Manual test")
        self.handle_serial_line(line)

    def process_serial_queue(self) -> None:
        """
        Her 100ms'de Tkinter after ile çağrılır.
        serial_queue'daki mesajları tüketir:
        - 'DATA|...' → handle_serial_line()
        - 'ERROR|...' → hata mesaj kutusu
        - Diğer → durum çubuğu + log
        """
        while not self.serial_queue.empty():
            message = self.serial_queue.get_nowait()
            prefix, payload = message.split("|", 1)
            if prefix == "DATA":
                self.handle_serial_line(payload)
            elif prefix == "ERROR":
                self.status_var.set("Connection error")
                messagebox.showerror("Serial Error", payload)
                self.append_log(f"ERROR: {payload}")
            else:
                self.status_var.set(payload)
                self.append_log(payload)
        self.root.after(100, self.process_serial_queue)

    def handle_serial_line(self, line: str) -> None:
        """
        Gelen ham satırı işler:
        - Loga yazar
        - parse_measurement() ile ayrıştırır
        - Parse edilemezse ignore sayıcısını artırır
        - Başarılıysa totalleri, UI değişkenlerini ve treeview'i günceller
        - Top sonu algılanırsa operatörü uyarır
        """
        self.append_log(line)
        measurement = self.parse_measurement(line)
        if measurement is None:
            self.ignored_count += 1
            self.ignored_count_var.set(str(self.ignored_count))
            self.parse_status_var.set(f"Ignored: {line}")
            if self.diagnostics_enabled_var.get():
                self.append_log(f"IGNORED: {line}")
            return

        self.measurements.append(measurement)
        self.parsed_count += 1
        self.parsed_count_var.set(str(self.parsed_count))
        self._apply_measurement(measurement)
        self._update_last_captured_values(measurement)
        self.meter_total_var.set(f"{self.totals['m']:.2f} m")
        self.kg_total_var.set(f"{self.totals['kg']:.2f} kg")
        self.refresh_payload_preview()
        self.tree.insert(
            "",
            0,
            values=(
                measurement.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                measurement.unit,
                f"{measurement.value:.2f}",
                measurement.raw_line,
            ),
        )
        if self._is_top_end_measurement(measurement):
            self.operator_status_var.set("Top sonu algilandi. Kaydet / Yazdir butonuna basin.")

    def parse_measurement(self, line: str) -> Measurement | None:
        """
        Ham metin satırından sayısal ölçümü ayrıştırır ve Measurement nesnesi döndürür.
        - Satırda sayı yoksa None döner (göz ardı edilir).
        - 'KG' anahtar kelimesi → kg birimi
        - 'metre'/'meter'/tek 'm' → metre birimi (cm birimiyle geliyorsa 100'e bölür)
        - İki sayı + birim yoksa → ilki metre(cm), ikincisi kg ("pair" modu)
        - Hiçbir birim ipucu yoksa default_unit_var kullanılır.
        """
        lowered = line.lower()
        numbers = NUMBER_PATTERN.findall(lowered)
        if not numbers:
            return None

        if len(numbers) >= 2 and "kg" not in lowered and "metre" not in lowered and "meter" not in lowered:
            meter_value = centimeters_to_meters(float(numbers[0].replace(",", ".")))
            kg_value = float(numbers[1].replace(",", "."))
            return Measurement(
                raw_line=line,
                value=meter_value,
                unit="pair",
                timestamp=datetime.now(),
                meter_value=meter_value,
                kg_value=kg_value,
            )

        value = float(numbers[0].replace(",", "."))
        if "kg" in lowered:
            unit = "kg"
        elif "metre" in lowered or "meter" in lowered or re.search(r"(?<!k)\bm\b", lowered):
            unit = "m"
        else:
            unit = self.default_unit_var.get()

        return Measurement(
            raw_line=line,
            value=value,
            unit=unit,
            timestamp=datetime.now(),
            meter_value=value if unit == "m" else None,
            kg_value=value if unit == "kg" else None,
        )

    def append_log(self, message: str) -> None:
        """Debug ekranındaki ham veri metin kutusuna zaman damgalı mesaj ekler."""
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{datetime.now():%H:%M:%S} {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def clear_session(self) -> None:
        """
        Mevcut oturumun tüm ölçüm verilerini, toplamları ve UI göstergelerini sıfırlar.
        Seri bağlantı ve parti bilgisi korunur; sadece ölçüm geçmişi temizlenir.
        """
        self.measurements.clear()
        self.totals = {"m": 0.0, "kg": 0.0}
        self.parsed_count = 0
        self.ignored_count = 0
        self.current_meter_raw = 0.0
        self.current_kg_raw = 0.0
        self.meter_offset = 0.0
        self.last_meter_value = 0.0
        self.last_kg_value = 0.0
        self.auto_save_armed = False
        self.meter_cycle_active = False
        self.last_auto_save_signature = ""
        self.last_value_var.set("-")
        self.meter_total_var.set("0.00 m")
        self.kg_total_var.set("0.00 kg")
        self.last_meter_var.set("0.00 m")
        self.last_kg_var.set("0.00 kg")
        self.parsed_count_var.set("0")
        self.ignored_count_var.set("0")
        self.parse_status_var.set("No data yet")
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.status_var.set("Session cleared")
        self.refresh_payload_preview()

    def start_new_party(self) -> None:
        """
        Yeni partiye geçiş işlemini gerçekleştirir.
        Önce makine sıfırda olup olmadığını kontrol eder (MACHINE_ZERO_TOLERANCE);
        sıfırda değilse uyarı gösterir. Başarılıysa oturumu ve parti bağlamını temizler.
        """
        if not self._machine_is_zero():
            self.operator_status_var.set("Makine sifirda degil. Once makine reset butonuna basin.")
            messagebox.showwarning(
                "Yeni Parti",
                "Makine sifirda degil. Yeni partiye gecmeden once makine uzerindeki reset butonuna basin.",
            )
            return

        self.clear_session()
        self._clear_party_context()
        self.operator_status_var.set("Yeni parti hazir. Refakat barkodunu okutun.")
        self.root.after(50, self.barcode_entry.focus_set)

    def _machine_is_zero(self) -> bool:
        """
        Makine ham değerlerinin (metre, kg ve toplam) tümünün
        MACHINE_ZERO_TOLERANCE (0.01) sınırı içinde sıfır sayılıp sayılamayacağını kontrol eder.
        """
        values = (
            self.current_meter_raw,
            self.current_kg_raw,
            self.totals["m"],
            self.totals["kg"],
        )
        try:
            tolerance = float(self.machine_zero_tolerance_var.get().strip())
        except ValueError:
            tolerance = MACHINE_ZERO_TOLERANCE
        return all(abs(value) <= tolerance for value in values)

    def _clear_party_context(self) -> None:
        """
        Barkod, müşteri, parti no, renk, kalite talimati gibi tüm parti
        bilgilerini ve hata kodu seçimini sıfırlar; payload önizlemesini günceller.
        """
        self.current_party_data = {}
        self.barcode_var.set("")
        self.customer_var.set("-")
        self.party_no_var.set("-")
        self.party_id_var.set("-")
        self.sarj_no_var.set("-")
        self.kalite_var.set("-")
        self.kalite_talimati_var.set("-")
        self.renk_var.set("-")
        self.operator_notes_var.set("")
        self.clear_error_code_selection()
        self.refresh_payload_preview()

    def export_csv(self) -> None:
        """
        Mevcut oturumdaki ölçümleri kullanıcının seçtiği yola CSV formatında aktarır.
        Ölçüm yoksa uyarı gösterir.
        """
        if not self.measurements:
            messagebox.showinfo("Export", "No measurements to export.")
            return

        default_name = f"fabric_measurements_{datetime.now():%Y%m%d_%H%M%S}.csv"
        target = filedialog.asksaveasfilename(
            title="Export measurements",
            defaultextension=".csv",
            initialfile=default_name,
            filetypes=[("CSV Files", "*.csv")],
        )
        if not target:
            return

        path = Path(target)
        with path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(["timestamp", "unit", "value", "meter_value", "kg_value", "raw_line"])
            for measurement in self.measurements:
                writer.writerow(
                    [
                        measurement.timestamp.isoformat(timespec="seconds"),
                        measurement.unit,
                        f"{measurement.value:.2f}",
                        "" if measurement.meter_value is None else f"{measurement.meter_value:.2f}",
                        "" if measurement.kg_value is None else f"{measurement.kg_value:.2f}",
                        measurement.raw_line,
                    ]
                )
        self.status_var.set(f"Exported to {path.name}")

    def lookup_barcode(self) -> None:
        """
        Barkod alanındaki değeri sorgular.
        - barcode_lookup_url ayarlıysa arka plan thread'inde HTTP GET atar.
        - URL yoksa DEFAULT_PARTY_DATA ile test verisi yükler.
        """
        barcode = self.barcode_var.get().strip()
        if not barcode:
            messagebox.showwarning("Barcode", "Scan or enter a barcode first.")
            return
        barcode_lookup_url = self.barcode_lookup_url_var.get().strip()
        if not barcode_lookup_url:
            self.load_party_data({"barcode": barcode, **DEFAULT_PARTY_DATA}, status_message="Default test party loaded")
            return
        self.operator_status_var.set("Fetching barcode...")
        threading.Thread(target=self._lookup_barcode_request, args=(barcode,), daemon=True).start()

    def _lookup_barcode_request(self, barcode: str) -> None:
        """
        Barkod servisi URL'sine ?barcode=... parametresiyle GET isteği atar.
        Başarılı yanıtta load_party_data() ana thread'de çağrılır.
        """
        try:
            barcode_lookup_url = self.barcode_lookup_url_var.get().strip()
            url = f"{barcode_lookup_url.rstrip('/')}?barcode={urllib.parse.quote(barcode)}"
            request = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read().decode("utf-8")
            data = json.loads(body)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            self.root.after(0, lambda: self.operator_status_var.set(f"Lookup failed: {exc}"))
            return
        self.root.after(0, lambda: self.load_party_data(data))

    def load_party_data(self, data: dict, status_message: str = "Barcode loaded") -> None:
        """
        API veya test verisinden gelen parti bilgilerini (_first_value ile esnek alan
        okuması yaparak) current_party_data'ya kaydeder ve UI kartlarını günceller.
        Birden fazla olası alan adı (Türkçe/İngilizce) desteklenir.
        """
        customer = self._first_value(data, ["customer", "musteri", "customer_name", "cari_unvan"])
        party_no = self._first_value(data, ["party_no", "parti_no", "batch_no", "lot_no"])
        party_id = self._first_value(data, ["party_id", "parti_id", "batch_id", "id"])
        sarj_no = self._first_value(data, ["sarj_no", "sarj", "charge_no", "seri_no"])
        kalite = self._first_value(data, ["kalite", "quality", "quality_name", "kalite_adi"])
        kalite_talimati = self._first_value(
            data,
            ["kalite_talimati", "quality_instruction", "quality_instructions", "quality_note", "talimat", "instruction"],
        )
        renk = self._first_value(data, ["renk", "color", "colour", "color_name", "renk_adi"])
        self.current_party_data = {
            "barcode": self.barcode_var.get().strip(),
            "customer": customer,
            "party_no": party_no,
            "party_id": party_id,
            "sarj_no": sarj_no,
            "kalite": kalite,
            "kalite_talimati": kalite_talimati,
            "renk": renk,
        }
        self.customer_var.set(customer or "-")
        self.party_no_var.set(party_no or "-")
        self.party_id_var.set(party_id or "-")
        self.sarj_no_var.set(sarj_no or "-")
        self.kalite_var.set(kalite or "-")
        self.kalite_talimati_var.set(kalite_talimati or "-")
        self.renk_var.set(renk or "-")
        self.operator_status_var.set(status_message)
        self.refresh_payload_preview()

    def save_operator_record(self, print_label: bool = False) -> None:
        """
        Mevcut kayıtı işler:
        1. Parti yüklenmemişse uyarı gösterir.
        2. _persist_record() ile operator_records.json'a yazar.
        3. _soft_reset_after_save() ile metreyi sıfırlar.
        4. save_measurement_url ayarlıysa HTTP POST atar (arka plan thread).
        5. print_label=True veya auto_print açıksa etiketi yazdırır.
        """
        payload = self.build_save_payload()
        if not payload["party_id"] and not payload["party_no"]:
            messagebox.showwarning("Save", "Load party information before saving.")
            return
        self._persist_record(payload, trigger="manual")
        self._soft_reset_after_save()
        should_print = print_label or self.auto_print_var.get()
        save_measurement_url = self.save_measurement_url_var.get().strip()
        if should_print and not save_measurement_url:
            self._print_label_background(payload)
        if save_measurement_url:
            self.operator_status_var.set("Saving...")
            threading.Thread(target=self._save_operator_record_request, args=(payload, should_print), daemon=True).start()
        else:
            if should_print:
                self.operator_status_var.set(f"Kaydedildi ve yazdirildi: {LOCAL_SAVE_PATH.name}")
            else:
                self.operator_status_var.set(f"Saved to {LOCAL_SAVE_PATH.name} and reset to zero")

    def _save_operator_record_request(self, payload: dict[str, object], print_label: bool) -> None:
        """
        Kayıt verisini uzak sunucuya HTTP POST ile gönderir.
        Sunucu ek barkod döndürebilir; bu durumda etikette yeni barkod kullanılır.
        """
        try:
            body = json.dumps(payload).encode("utf-8")
            save_measurement_url = self.save_measurement_url_var.get().strip()
            request = urllib.request.Request(
                save_measurement_url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                response_body = response.read().decode("utf-8")
            response_payload = json.loads(response_body) if response_body else {}
            if not isinstance(response_payload, dict):
                response_payload = {}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            self.root.after(0, lambda: self.operator_status_var.set(f"Save failed: {exc}"))
            return
        final_payload = dict(payload)
        returned_barcode = self._first_value(response_payload, ["barcode", "label_barcode", "etiket_barkod", "generated_barcode"])
        if returned_barcode:
            final_payload["barcode"] = returned_barcode

        def on_success() -> None:
            if print_label:
                self._print_label_background(final_payload)
                self.operator_status_var.set("Kaydedildi ve yazdirma baslatildi")
            else:
                self.operator_status_var.set("Saved successfully")

        self.root.after(0, on_success)

    def build_save_payload(self) -> dict[str, object]:
        """
        Kayıt için gönderilecek tüm alanları tek bir dict'te toplar:
        parti bilgisi, operatör bilgisi, ölçüm değerleri, hata kodu, not ve zaman damgası.
        """
        payload = {
            "barcode": self.current_party_data.get("barcode") or self.barcode_var.get().strip(),
            "customer": self.current_party_data.get("customer", ""),
            "party_no": self.current_party_data.get("party_no", ""),
            "party_id": self.current_party_data.get("party_id", ""),
            "sarj_no": self.current_party_data.get("sarj_no", ""),
            "kalite": self.current_party_data.get("kalite", ""),
            "kalite_talimati": self.current_party_data.get("kalite_talimati", ""),
            "renk": self.current_party_data.get("renk", ""),
            "error_category": self.current_error_code.get("category", ""),
            "error_code": self.current_error_code.get("code", ""),
            "error_description": self.current_error_code.get("description", ""),
            "operator_name": self.current_user_data.get("name", ""),
            "operator_surname": self.current_user_data.get("surname", ""),
            "operator_userid": self.current_user_data.get("userid", ""),
            "operator_role": self.current_user_data.get("userrole", ""),
            "meter": round(self.last_meter_value, 2),
            "kg": round(self.last_kg_value, 2),
            "notes": self.operator_notes_var.get().strip(),
            "last_measurement": self.last_value_var.get(),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        return payload

    def refresh_payload_preview(self) -> None:
        """
        Debug ekranındaki JSON önizleme kutusunu günceller.
        Bu pencere açık değilse (payload_preview widget'i yoksa) sessizce döner.
        """
        if not hasattr(self, "payload_preview"):
            return
        payload = self.build_save_payload()
        self.payload_preview.configure(state="normal")
        self.payload_preview.delete("1.0", "end")
        self.payload_preview.insert("end", json.dumps(payload, ensure_ascii=True, indent=2))
        self.payload_preview.configure(state="disabled")

    def _update_last_captured_values(self, measurement: Measurement) -> None:
        """
        Yeni bir ölçüm geldiğinde son geçerli metre/kg değerlerini kaydeder
        (toplam > 0 olduğunda güncellenir; top sonu etiketi için kullanılır).
        """
        if measurement.meter_value is not None and self.totals["m"] > 0:
            self.last_meter_value = self.totals["m"]
            self.last_meter_var.set(f"{self.totals['m']:.2f} m")
        if measurement.kg_value is not None and self.totals["kg"] > 0:
            self.last_kg_value = self.totals["kg"]
            self.last_kg_var.set(f"{self.totals['kg']:.2f} kg")

    def _is_top_end_measurement(self, measurement: Measurement) -> bool:
        """
        Metre değerinin sıfıra düştüğünü ve önceki toplamda metre/kg veri olduğunu
        kontrol eder. Bu durum 'top sonu' sinyalü sayılır.
        """
        meter_zero_detected = measurement.meter_value == 0 or (measurement.unit == "m" and measurement.value == 0)
        return meter_zero_detected and (self.last_meter_value > 0 or self.last_kg_value > 0)

    def _auto_save_on_zero(self, measurement: Measurement) -> None:
        """
        Metre sıfıra düştüğünde, auto_save_armed ve meter_cycle_active bayrakları
        doğruysa otomatik kayıt yapar. Aynı verinin tekrar kaydedilmemesi için
        imza (signature) karşılaştırması yapar.
        NOT: Bu metot şu an handle_serial_line içinden çağrılmıyor; ilerideki kullanım için hazır.
        """
        meter_zero_detected = measurement.meter_value == 0 or (measurement.unit == "m" and measurement.value == 0)
        if not meter_zero_detected or not self.auto_save_armed or not self.meter_cycle_active:
            return
        payload = self.build_save_payload()
        if payload["meter"] == 0 and payload["kg"] == 0:
            return
        signature = json.dumps(
            {
                "barcode": payload["barcode"],
                "party_id": payload["party_id"],
                "party_no": payload["party_no"],
                "meter": payload["meter"],
                "kg": payload["kg"],
            },
            sort_keys=True,
        )
        if signature == self.last_auto_save_signature:
            return
        self._persist_record(payload, trigger=f"auto_zero_{measurement.unit}")
        self.last_auto_save_signature = signature
        self._soft_reset_after_save()
        self.auto_save_armed = False
        self.meter_cycle_active = False
        self.operator_status_var.set(f"Auto saved to {LOCAL_SAVE_PATH.name} and reset to zero")

    def _apply_measurement(self, measurement: Measurement) -> None:
        """
        Gelen ölçümü totallere uygular:
        - Metre geldiğinde: raw değeri kaydeder, offset'i hesaplar, totals['m'] = raw - offset
        - kg geldiğinde: totals['kg'] = raw (negatif değerleri sıfırlar)
        - 'pair' modunda her ikisini birden günceller.
        parse_status_var ve last_value_var UI göstergelerini günceller.
        """
        if measurement.meter_value is not None:
            self.current_meter_raw = measurement.meter_value
            if self.current_meter_raw < self.meter_offset:
                self.meter_offset = 0.0
            self.totals["m"] = max(self.current_meter_raw - self.meter_offset, 0.0)
        if measurement.kg_value is not None:
            self.current_kg_raw = measurement.kg_value
            self.totals["kg"] = max(self.current_kg_raw, 0.0)

        if measurement.unit == "pair":
            self.parse_status_var.set(
                f"Parsed {self.totals['m']:.2f} m and {self.totals['kg']:.2f} kg from '{measurement.raw_line}'"
            )
            self.last_value_var.set(f"{self.totals['m']:.2f} m / {self.totals['kg']:.2f} kg")
            return

        if measurement.unit == "m":
            display_value = self.totals["m"]
        elif measurement.unit == "kg":
            display_value = self.totals["kg"]
        else:
            display_value = measurement.value
        self.parse_status_var.set(f"Parsed {display_value:.2f} {measurement.unit} from '{measurement.raw_line}'")
        self.last_value_var.set(f"{display_value:.2f} {measurement.unit}")

    def _print_label_background(self, payload: dict[str, object]) -> None:
        """Start a background thread to send the label to the printer."""
        printer_name = self.printer_name_var.get().strip()
        if not printer_name:
            self.operator_status_var.set("Yazici secilmedi \u2013 etiket basilmadi.")
            return
        threading.Thread(
            target=self._do_print_label,
            args=(dict(payload), printer_name),
            daemon=True,
        ).start()

    def _do_print_label(self, payload: dict, printer_name: str) -> None:
        """
        label_printer.print_label_win() çağrısını yapıp etiketi basma işlemini gerçekleştirir.
        Arka plan thread'inde çalışır. Hata oluşursa operatör durum çubuğuna mesaj yazar.
        """
        try:
            label_printer.print_label_win(
                printer_name=printer_name,
                parti=str(payload.get("party_no", "")),
                sarj=str(payload.get("sarj_no", "")),
                kalite=str(payload.get("kalite", "")),
                renk=str(payload.get("renk", "")),
                mt=float(payload.get("meter", 0)),
                kg=float(payload.get("kg", 0)),
                barcode=str(payload.get("barcode", "")),
            )
            self.root.after(0, lambda: self.operator_status_var.set("Etiket yazdirildi."))
        except Exception as exc:  # noqa: BLE001
            self.root.after(0, lambda e=exc: self.operator_status_var.set(f"Yazici hatasi: {e}"))

    def _print_test_label(self) -> None:
        """Print a test label using the current payload values."""
        self._print_label_background(self.build_save_payload())

    def _soft_reset_after_save(self) -> None:
        """
        Kayıt sonrası kısmi sıfırlama yapar:
        - Metre toplamı sıfırlanır (metre_offset = mevcut ham değer olarak ayarlanır)
        - kg toplamı mevcut ham değerle korunur (topa devam ediliyor olabilir)
        - Hata kodu seçimi temizlenir, payload önizlemesi güncellenir.
        """
        self.meter_offset = self.current_meter_raw
        self.totals["m"] = 0.0
        self.totals["kg"] = max(self.current_kg_raw, 0.0)
        self.last_meter_value = 0.0
        self.last_kg_value = max(self.current_kg_raw, 0.0)
        self.meter_cycle_active = False
        self.last_meter_var.set("0.00 m")
        self.last_kg_var.set(f"{self.last_kg_value:.2f} kg")
        self.meter_total_var.set("0.00 m")
        self.kg_total_var.set(f"{self.totals['kg']:.2f} kg")
        self.last_value_var.set(f"0.00 m / {self.totals['kg']:.2f} kg")
        self.clear_error_code_selection()
        self.refresh_payload_preview()

    def _persist_record(self, payload: dict[str, object], trigger: str) -> None:
        """
        Kayıtı operator_records.json dosyasına ekler (append).
        trigger alanı kaydın 'manual' veya 'auto_zero_m' gibi çagriş kaynağını belirtir.
        Dosya bozuksa yeni liste oluşturulur. Kayıttan sonra log tablosu yenilenir.
        """
        record = dict(payload)
        record["trigger"] = trigger
        records: list[dict[str, object]] = []
        if LOCAL_SAVE_PATH.exists():
            try:
                existing = json.loads(LOCAL_SAVE_PATH.read_text(encoding="utf-8"))
                if isinstance(existing, list):
                    records = existing
            except json.JSONDecodeError:
                records = []
        records.append(record)
        LOCAL_SAVE_PATH.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        if hasattr(self, "logs_tree"):
            self.refresh_logs_view()

    @staticmethod
    def _first_value(data: dict, keys: list[str]) -> str:
        """
        Verilen dict'te keys listesindeki ilk dolu değeri döndürür.
        Tüm anahtarlara karşılık gelen değer yoksa veya boşsa boş string döner.
        API cevaplarında Türkçe/İngilizce alan adlarını birlikte desteklemek için kullanılır.
        """
        for key in keys:
            value = data.get(key)
            if value is not None and value != "":
                return str(value)
        return ""


def main() -> None:
    """Uygulamayı başlatır: Tkinter penceresi oluşturur, FabricCounterApp'i yükler ve event döngüsünü çalıştırır."""
    root = tk.Tk()
    app = FabricCounterApp(root)
    root.protocol("WM_DELETE_WINDOW", lambda: on_close(root, app))
    root.mainloop()


def on_close(root: tk.Tk, app: FabricCounterApp) -> None:
    """Pencere kapatılırken seri bağlantıyı temiz şekilde keser, ardından pencereyi yıkar."""
    app.disconnect()
    root.destroy()


if __name__ == "__main__":
    main()