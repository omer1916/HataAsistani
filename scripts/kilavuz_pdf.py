"""data/kilavuzlar/kaynak/*.md dosyalarından kılavuz PDF'lerini üretir.

Kullanım:
    python scripts/kilavuz_pdf.py

Kılavuzlar bu proje için yazılmış kurgusal dokümanlardır. Metin git'te okunabilir kalsın diye
Markdown olarak tutulur; PDF'ler bu betikle üretilip commit'lenir. Uygulama (ingest) yalnızca
PDF'leri okur, tıpkı gerçek bir fabrikanın doküman arşivinde olduğu gibi.

Desteklenen yazım: "## " bölüm, "### " alt bölüm, "- " madde, "1) " numaralı adım,
boş satır paragraf sonu ve "<!-- sayfa -->" sayfa sonu. Kaynaktaki her sayfa PDF'te tek
sayfaya sığmalıdır; taşarsa betik hata verir. Böylece sayfa numaraları kaynak metinle aynı
kalır ve ölçüm setindeki "doğru sayfa" etiketleri kaymaz.
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from app.db import TZ

KILAVUZ_KLASORU = Path(__file__).resolve().parent.parent / "data" / "kilavuzlar"
KAYNAK_KLASORU = KILAVUZ_KLASORU / "kaynak"
SAYFA_SONU = "<!-- sayfa -->"
ERISIM_ADLARI = {"operasyon": "Operasyon", "bakim": "Bakım"}

# Türkçe karakterler (ğ, ş, ı, İ) için Unicode bir TTF font gerekir; ilk bulunan kullanılır.
FONT_ADAYLARI = [
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ),
    (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ),
]


def font_bul() -> tuple[str, str]:
    for normal, kalin in FONT_ADAYLARI:
        if Path(normal).exists() and Path(kalin).exists():
            return normal, kalin
    sys.exit("Türkçe karakter destekleyen bir TTF font bulunamadı (Arial veya DejaVu Sans).")


class KilavuzPDF(FPDF):
    def __init__(self, kod: str, baslik: str, fontlar: tuple[str, str]):
        super().__init__(format="A4")
        self.kod, self.baslik = kod, baslik
        self.add_font("govde", "", fontlar[0])
        self.add_font("govde", "B", fontlar[1])
        self.set_margins(20, 16, 20)
        self.set_auto_page_break(True, margin=18)
        self.set_title(baslik)
        self.set_author("Hata Asistanı projesi (kurgusal doküman)")
        # Sabit tarih: PDF'ler her üretimde aynı çıksın, git'te boş yere değişmesin.
        self.set_creation_date(datetime(2026, 9, 1, tzinfo=TZ))

    def header(self):
        self.set_font("govde", "", 8)
        self.set_text_color(120)
        self.cell(0, 6, f"{self.kod} · {self.baslik}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_text_color(0)
        self.ln(4)

    def footer(self):
        self.set_y(-14)
        self.set_font("govde", "", 8)
        self.set_text_color(120)
        self.cell(0, 6, f"Sayfa {self.page_no()} / {{nb}}", align="C")
        self.set_text_color(0)


def _bloklar(metin: str) -> list[tuple[str, str]]:
    """Bir sayfanın kaynak metnini (tür, içerik) bloklarına ayırır."""
    bloklar: list[tuple[str, str]] = []
    paragraf: list[str] = []

    def paragrafi_kapat():
        if paragraf:
            bloklar.append(("paragraf", " ".join(paragraf)))
            paragraf.clear()

    for satir in metin.splitlines():
        satir = satir.strip()
        if not satir:
            paragrafi_kapat()
        elif satir.startswith("### "):
            paragrafi_kapat()
            bloklar.append(("alt_baslik", satir[4:]))
        elif satir.startswith("## "):
            paragrafi_kapat()
            bloklar.append(("baslik", satir[3:]))
        elif satir.startswith("- "):
            paragrafi_kapat()
            bloklar.append(("madde", "•  " + satir[2:]))
        elif re.match(r"\d+\) ", satir):
            paragrafi_kapat()
            bloklar.append(("madde", satir))
        else:
            paragraf.append(satir)
    paragrafi_kapat()
    return bloklar


def _kapak(pdf: KilavuzPDF, kilavuz: dict) -> None:
    pdf.set_font("govde", "B", 18)
    pdf.multi_cell(0, 9, kilavuz["baslik"], new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(1)
    pdf.set_font("govde", "", 9)
    pdf.set_text_color(90)
    erisim = ERISIM_ADLARI[kilavuz["erisim"]]
    pdf.multi_cell(
        0,
        5,
        f"Doküman kodu: {kilavuz['kod']}    Sürüm: 1.0    Erişim: {erisim}",
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )
    pdf.multi_cell(
        0,
        5,
        "Bu doküman Hata Asistanı portföy projesi için yazılmış kurgusal bir kılavuzdur; "
        "gerçek bir ekipmana veya firmaya ait değildir.",
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
    )
    pdf.set_text_color(0)
    pdf.ln(5)


def _sayfa_yaz(pdf: KilavuzPDF, metin: str) -> None:
    for tur, icerik in _bloklar(metin):
        if tur == "baslik":
            pdf.ln(2)
            pdf.set_font("govde", "B", 12.5)
            pdf.multi_cell(0, 7, icerik, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(1.5)
        elif tur == "alt_baslik":
            pdf.ln(1)
            pdf.set_font("govde", "B", 10.5)
            pdf.multi_cell(0, 6, icerik, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(0.5)
        elif tur == "madde":
            pdf.set_font("govde", "", 10)
            pdf.set_x(pdf.l_margin + 4)
            pdf.multi_cell(pdf.epw - 4, 5, icerik, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(1)
        else:
            pdf.set_font("govde", "", 10)
            pdf.multi_cell(0, 5, icerik, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(2)


def kilavuz_uret(kilavuz: dict, fontlar: tuple[str, str]) -> int:
    """Kılavuzun PDF'ini üretir ve sayfa sayısını döner."""
    kaynak = (KAYNAK_KLASORU / f"{kilavuz['kod']}.md").read_text(encoding="utf-8")
    pdf = KilavuzPDF(kilavuz["kod"], kilavuz["baslik"], fontlar)
    for sira, sayfa_metni in enumerate(kaynak.split(SAYFA_SONU), start=1):
        pdf.add_page()
        if sira == 1:
            _kapak(pdf, kilavuz)
        _sayfa_yaz(pdf, sayfa_metni)
        if pdf.page_no() != sira:
            sys.exit(f"{kilavuz['kod']}: kaynak metnin {sira}. sayfası tek sayfaya sığmadı.")
    pdf.output(str(KILAVUZ_KLASORU / f"{kilavuz['kod']}.pdf"))
    return pdf.page_no()


def main() -> None:
    fontlar = font_bul()
    kilavuzlar = json.loads((KILAVUZ_KLASORU / "kilavuzlar.json").read_text(encoding="utf-8"))
    for kilavuz in kilavuzlar:
        sayfa_sayisi = kilavuz_uret(kilavuz, fontlar)
        print(f"{kilavuz['kod']:<11} {sayfa_sayisi:>2} sayfa  {kilavuz['baslik']}")


if __name__ == "__main__":
    main()
