"""GDI label printing for Argox X-1000VL - 76x76mm square label, 90deg CW rotation."""
from __future__ import annotations

_TR = str.maketrans("sScCgGuUoOiI", "sScCgGuUoOiI")
_TR = str.maketrans(
    "\u015f\u015e\u00e7\u00c7\u011f\u011e\u00fc\u00dc\u00f6\u00d6\u0131\u0130",
    "sScCgGuUoOiI",
)


def _a(text: str) -> str:
    return str(text).translate(_TR)


def print_label_win(
    printer_name: str,
    parti: str,
    sarj: str,
    kalite: str,
    renk: str,
    mt: float,
    kg: float,
    barcode: str,
) -> None:
    """Print a 76x76mm fabric-roll label via Windows GDI (Argox X-1000VL)."""
    import win32ui
    import win32con
    import io
    import ctypes
    import struct

    mt_str = f"{mt:.2f}".replace(".", ",")
    kg_str = f"{kg:.2f}".replace(".", ",")
    safe_bc = "".join(c for c in str(barcode) if c.isalnum() or c in "-. $/+%")

    hDC = win32ui.CreateDC()
    hDC.CreatePrinterDC(printer_name)

    dpi_x = hDC.GetDeviceCaps(win32con.LOGPIXELSX)
    dpi_y = hDC.GetDeviceCaps(win32con.LOGPIXELSY)

    def mmx(v): return int(v * dpi_x / 25.4)
    def mmy(v): return int(v * dpi_y / 25.4)

    # 90 derece saat yonunde (CW) donme - label 76x76mm kare
    label_h = mmy(76)

    class XFORM(ctypes.Structure):
        _fields_ = [
            ("eM11", ctypes.c_float), ("eM12", ctypes.c_float),
            ("eM21", ctypes.c_float), ("eM22", ctypes.c_float),
            ("eDx",  ctypes.c_float), ("eDy",  ctypes.c_float),
        ]

    gdi32 = ctypes.windll.gdi32
    gdi32.SetGraphicsMode(hDC.GetSafeHdc(), 2)  # GM_ADVANCED
    gdi32.SetWorldTransform(
        hDC.GetSafeHdc(),
        ctypes.byref(XFORM(0.0, 1.0, -1.0, 0.0, float(label_h), 0.0))
    )

    # Kullanilabilir alan: y=0..51mm (76-25mm mavi on baski alani)
    font_hdr = win32ui.CreateFont({"name": "Arial", "height": -mmy(3.8), "weight": 400, "charset": 0})
    font_big = win32ui.CreateFont({"name": "Arial", "height": -mmy(6.0), "weight": 700, "charset": 0})

    hDC.StartDoc(f"Etiket {parti or 'label'}")
    hDC.StartPage()

    hDC.SelectObject(font_hdr)
    hDC.TextOut(mmx(2), mmy(1.5),  _a(f"Parti  : {parti}"))
    hDC.TextOut(mmx(2), mmy(7.0),  _a(f"Sarj   : {sarj}"))
    hDC.TextOut(mmx(2), mmy(12.5), _a(f"Kalite : {kalite}"))
    hDC.TextOut(mmx(2), mmy(18.0), _a(f"Renk   : {renk}"))

    hDC.SelectObject(font_big)
    hDC.TextOut(mmx(2),  mmy(26.0), _a(f"Mt :{mt_str}"))
    hDC.TextOut(mmx(40), mmy(26.0), _a(f"Kg :{kg_str}"))

    hDC.MoveTo(mmx(1), mmy(33.5))
    hDC.LineTo(mmx(74), mmy(33.5))

    if safe_bc:
        try:
            import barcode as bc_lib
            from barcode.writer import ImageWriter
            from PIL import Image as PILImage

            buf = io.BytesIO()
            bc_lib.get("code128", safe_bc, writer=ImageWriter()).write(buf, options={
                "module_height": 12.0, "module_width": 0.35, "quiet_zone": 0.5,
                "font_size": 6, "text_distance": 1.2,
                "background": "white", "foreground": "black", "write_text": True,
            })
            buf.seek(0)
            img = PILImage.open(buf).convert("RGB")
            w_dest = mmx(70)
            h_dest = mmy(11)
            img = img.resize((w_dest, h_dest), PILImage.LANCZOS)

            bmp_buf = io.BytesIO()
            img.save(bmp_buf, "BMP")
            bmp_bytes = bmp_buf.getvalue()
            px_off  = struct.unpack_from("<I", bmp_bytes, 10)[0]
            bmp_w   = struct.unpack_from("<i", bmp_bytes, 18)[0]
            bmp_h   = struct.unpack_from("<i", bmp_bytes, 22)[0]
            c_info   = ctypes.create_string_buffer(bytes(bmp_bytes[14:px_off]))
            c_pixels = ctypes.create_string_buffer(bytes(bmp_bytes[px_off:]))

            gdi32.StretchDIBits(
                hDC.GetSafeHdc(),
                mmx(2), mmy(35), w_dest, h_dest,
                0, 0, bmp_w, abs(bmp_h),
                c_pixels, c_info, 0, 0x00CC0020,
            )
        except Exception:
            from win32ui import CreateFont
            font_bc = CreateFont({"name": "Arial", "height": -mmy(3.2), "weight": 400, "charset": 0})
            hDC.SelectObject(font_bc)
            hDC.TextOut(mmx(2), mmy(37), safe_bc)

    hDC.EndPage()
    hDC.EndDoc()
    hDC.DeleteDC()


if __name__ == "__main__":
    print_label_win(
        printer_name="Argox X-1000VL series PPLA",
        parti="183-6-S51",
        sarj="611652",
        kalite="FW 30/1 OE",
        renk="BEYAZ",
        mt=245.50,
        kg=18.30,
        barcode="183-6-S51",
    )
    print("Etiket gonderildi!")
