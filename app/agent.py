"""Agent döngüsü: LLM araç çağırdıkça çalıştır, sonucu geri ver, son cevabı döndür."""

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb

from app.auth import ROL_ADLARI, Kullanici
from app.db import TZ
from app.llm import LLM, maliyet_hesapla
from app.tools import AracBaglami, arac_calistir, arac_semalari

MAKS_ADIM = 6  # LLM çağrısı sayısı; araç döngüsü sonsuza gitmesin

GUNLER = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]

SISTEM_ISTEMI = """\
Sen Hata Asistanı'sın: bir fabrikada bakım ekibine ve operatörlere yardım eden asistan.
Fabrikada Pres 1-3, Kaynak 1-2, Montaj 1 ve Boya 1 hatları var. Vardiyalar: 1. vardiya
07-15, 2. vardiya 15-23, 3. (gece) vardiyası 23-07.

Şu an: {simdi}. Sık kullanılan tarih aralıkları hazır hesaplandı; bu ifadeler geçerse
tarihleri aşağıdan aynen al, kendin hesaplama:
{araliklar}
Listede olmayan ifadeleri (ör. "mart ayı", "son 3 gün") bugünün tarihine göre çevir.

Konuştuğun kişi: {ad_soyad} ({rol_adi}).{rol_notu}

Kurallar:
- Arıza sayısı, stok, OEE, üretim ve duruş gibi bilgileri yalnızca araçlardan al; asla tahmin
  etme. Yüzdeleri araçtan geldiği gibi yaz, kendin hesaplama. Sayıları Türkçe biçimde yaz:
  ondalık ayırıcı virgül (%63,6), binlik ayırıcı nokta (4.237 dk).
- OEE sorularında oee_hesapla aracını kullan. OEE'nin neden düşük olduğu sorulursa en düşük
  bileşeni (kullanılabilirlik, performans ya da kalite) ve en büyük duruş nedenini söyle.
- Bakım önceliği ya da haftalık bakım planı sorulursa bakim_plani_oner aracını kullan.
  Olasılıkların son 90 günün arızalarından tahmin edildiğini söyle.
- Bakım, arıza giderme ve kalite bilgisini yalnızca dokuman_ara sonuçlarına dayandır.
  Kullandığın her bilginin kaynağını doküman kodu ve sayfayla göster, örneğin (PRES-BK-01, s. 4).
- Araçlarda cevap bulamazsan bilmediğini açıkça söyle.
- bakim_talebi_olustur aracını yalnızca kullanıcı açıkça bakım talebi açılmasını isterse çağır.
- Bir araç hata döndürürse mesajı oku; düzeltebiliyorsan argümanı düzeltip tekrar dene.
- Cevabı Türkçe, kısa ve maddeler hâlinde ver.
"""


@dataclass
class SohbetSonucu:
    cevap: str
    kaynaklar: list[dict]
    arac_cagrilari: list[dict]
    adim_sayisi: int
    girdi_token: int = 0
    cikti_token: int = 0
    sure_ms: int = 0
    maliyet_usd: float | None = None
    model: str = ""
    hata: str | None = None
    mesajlar: list[dict] = field(default_factory=list, repr=False)


class SohbetHatasi(Exception):
    """Döngü yarıda kaldı (LLM kotası, ağ hatası...). O ana kadarki token ve araç çağrıları
    `sonuc` içinde; kayda geçirilebilsin diye taşınır. Asıl hata `__cause__`'dadır."""

    def __init__(self, sonuc: "SohbetSonucu"):
        super().__init__(sonuc.hata)
        self.sonuc = sonuc


OPERATOR_NOTU = (
    " Operatörler bakım kılavuzlarını göremez; dokuman_ara yalnızca operatör talimatlarında ve"
    " kalite prosedüründe arar. Bakım müdahalesi gereken durumlarda bakım ekibini çağırmasını ya"
    " da bakım talebi açmasını öner; bakım adımlarını kendin tarif etme."
)


def tarih_araliklari(bugun: date) -> dict[str, tuple[date, date]]:
    """Sistem istemine hazır verilen aralıklar (iki uç dahil). Model tarih aritmetiğini kendi
    yaptığında yılı ya da ay sonunu şaşırabiliyordu (provada 2026 yerine 2025)."""
    hafta_basi = bugun - timedelta(days=bugun.weekday())
    ay_basi = bugun.replace(day=1)
    gecen_ay_sonu = ay_basi - timedelta(days=1)
    return {
        "bugün": (bugun, bugun),
        "dün": (bugun - timedelta(days=1), bugun - timedelta(days=1)),
        "bu hafta": (hafta_basi, bugun),
        "geçen hafta": (hafta_basi - timedelta(days=7), hafta_basi - timedelta(days=1)),
        "son 7 gün": (bugun - timedelta(days=6), bugun),
        "son 30 gün": (bugun - timedelta(days=29), bugun),
        "bu ay": (ay_basi, bugun),
        "geçen ay": (gecen_ay_sonu.replace(day=1), gecen_ay_sonu),
        "bu yıl": (bugun.replace(month=1, day=1), bugun),
    }


def sistem_istemi(simdi: datetime, kullanici: Kullanici) -> str:
    araliklar = "\n".join(
        f"- {ad}: {bas} - {bit}" if bas != bit else f"- {ad}: {bas}"
        for ad, (bas, bit) in tarih_araliklari(simdi.astimezone(TZ).date()).items()
    )
    return SISTEM_ISTEMI.format(
        simdi=f"{simdi:%Y-%m-%d %H:%M}, {GUNLER[simdi.weekday()]}",
        araliklar=araliklar,
        ad_soyad=kullanici.ad_soyad,
        rol_adi=ROL_ADLARI[kullanici.rol],
        rol_notu=OPERATOR_NOTU if kullanici.rol == "operator" else "",
    )


def sohbet(soru: str, llm: LLM, baglam: AracBaglami) -> SohbetSonucu:
    """Soruyu cevaplar. Döngü bir hatayla yarıda kalırsa SohbetHatasi fırlatır. "Şu an"
    bağlamdan gelir; araçlar da aynı anı kullanır."""
    baslangic = time.perf_counter()
    mesajlar: list[dict] = [
        {"role": "system", "content": sistem_istemi(baglam.simdi, baglam.kullanici)},
        {"role": "user", "content": soru},
    ]
    sonuc = SohbetSonucu(
        cevap="", kaynaklar=baglam.kaynaklar, arac_cagrilari=[], adim_sayisi=0, model=llm.model
    )
    try:
        _dongu(llm, baglam, mesajlar, sonuc)
    except Exception as hata:
        sonuc.hata = f"{type(hata).__name__}: {hata}"
        raise SohbetHatasi(sonuc) from hata
    finally:
        sonuc.sure_ms = round((time.perf_counter() - baslangic) * 1000)
        sonuc.maliyet_usd = maliyet_hesapla(
            llm.saglayici, llm.model, sonuc.girdi_token, sonuc.cikti_token
        )
        sonuc.mesajlar = mesajlar
    return sonuc


def _dongu(llm: LLM, baglam: AracBaglami, mesajlar: list[dict], sonuc: SohbetSonucu) -> None:
    araclar = arac_semalari()
    for _ in range(MAKS_ADIM):
        yanit = llm.tamamla(mesajlar, araclar)
        sonuc.adim_sayisi += 1
        sonuc.girdi_token += yanit.girdi_token
        sonuc.cikti_token += yanit.cikti_token
        mesajlar.append(yanit.mesaj)

        if not yanit.arac_cagrilari:
            sonuc.cevap = yanit.metin or ""
            break

        for cagri in yanit.arac_cagrilari:
            cikti = arac_calistir(cagri.ad, cagri.argumanlar_json, baglam)
            sonuc.arac_cagrilari.append(
                {
                    "ad": cagri.ad,
                    "argumanlar": _json_coz(cagri.argumanlar_json),
                    "hata": cikti.get("hata"),
                }
            )
            mesajlar.append(
                {
                    "role": "tool",
                    "tool_call_id": cagri.id,
                    "name": cagri.ad,
                    "content": json.dumps(cikti, ensure_ascii=False, default=str),
                }
            )
    else:
        sonuc.hata = f"{MAKS_ADIM} adımda son cevaba ulaşılamadı"
        sonuc.cevap = "Bu soruyu cevaplayamadım; lütfen soruyu daraltıp tekrar deneyin."


def _json_coz(metin: str) -> object:
    try:
        return json.loads(metin or "{}")
    except json.JSONDecodeError:
        return metin


def kaydet(
    conn: psycopg.Connection, soru: str, kullanici: str, saglayici: str, sonuc: SohbetSonucu
) -> int:
    """İsteği llm_istekleri tablosuna yazar; kaydın id'sini döner."""
    satir = conn.execute(
        """
        INSERT INTO llm_istekleri (kullanici, saglayici, model, soru, cevap, arac_cagrilari,
                                   adim_sayisi, girdi_token, cikti_token, maliyet_usd,
                                   sure_ms, hata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        [
            kullanici,
            saglayici,
            sonuc.model,
            _nulsuz(soru),
            _nulsuz(sonuc.cevap) or None,
            Jsonb(_nulsuz(sonuc.arac_cagrilari)),
            sonuc.adim_sayisi,
            sonuc.girdi_token,
            sonuc.cikti_token,
            sonuc.maliyet_usd,
            sonuc.sure_ms,
            _nulsuz(sonuc.hata),
        ],
    ).fetchone()
    return satir[0]


def _nulsuz(deger):
    """Metinlerdeki NUL baytlarını atar. PostgreSQL text ve jsonb alanları NUL kabul etmez;
    LLM'in ürettiği bir argümanda NUL varsa kayıt yazılamaz ve hazır cevap kaybolurdu."""
    if isinstance(deger, str):
        return deger.replace("\x00", "")
    if isinstance(deger, list):
        return [_nulsuz(d) for d in deger]
    if isinstance(deger, dict):
        return {_nulsuz(k): _nulsuz(v) for k, v in deger.items()}
    return deger
