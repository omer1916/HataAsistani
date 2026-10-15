"""Webhook'lar: arıza ve bakım olaylarını dış sistemlere (ERP, MES, Teams, Slack) iletir.

Bildirimler sql/webhook.sql'deki tetikleyicilerle giden kutusuna (giden_bildirimler) yazılır;
burada sırayla gönderilir. Her webhook'un bildirimleri kendi sırasıyla gider; biri cevap vermezse
diğerleri beklemez. Ulaşılamayan alıcıya artan aralıklarla (5 sn'den 1 saate) yeniden denenir,
EN_FAZLA_DENEME'den sonra bırakılır.

json biçiminde her istek imzalıdır (alıcı doğrular, bkz. README):
    X-Hata-Asistani-Imza: sha256=<HMAC-SHA256(gizli, "<zaman>.<gövde>") onaltılık>
    X-Hata-Asistani-Zaman: <unix saniye>   (eski isteği tekrar oynatmaya karşı)
    X-Hata-Asistani-Teslimat: <bildirim no> (en az bir kez teslim: alıcı tekrarı bununla ayıklar)
    X-Hata-Asistani-Olay: ariza_acildi | ariza_kapandi | hat_durdu | periyodik_bakim_yapildi
"""

import hashlib
import hmac
import json
import random
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta

import httpx
import psycopg

OLAYLAR = ("ariza_acildi", "ariza_kapandi", "hat_durdu", "periyodik_bakim_yapildi")
BICIMLER = ("json", "teams", "slack")
EN_FAZLA_DENEME = 15  # aralar 5 sn, 10 sn ... en fazla 1 saat: toplam ~4,5 saat
ILK_BEKLEME = 5.0
EN_UZUN_BEKLEME = 3600.0
PARTI = 20  # bir turda en fazla bu kadar webhook'a aynı anda
ZAMAN_ASIMI = httpx.Timeout(10.0)
KANAL = "giden_bildirimler"
HATA_SINIRI = 300
ONEM_ADI = {"dusuk": "düşük", "orta": "orta", "yuksek": "yüksek"}


class WebhookHatasi(ValueError):
    pass


def gizli_uret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def imza(gizli: str, zaman: int, govde: bytes) -> str:
    ozet = hmac.new(gizli.encode(), f"{zaman}.".encode() + govde, hashlib.sha256)
    return f"sha256={ozet.hexdigest()}"


def imza_dogrula(gizli: str, basliklar, govde: bytes, simdi: float | None = None,
                 tolerans: int = 300) -> bool:  # fmt: skip
    """Alıcı tarafı için örnek (ve testler): imza doğru ve istek en fazla `tolerans` sn eski mi."""
    try:
        zaman = int(basliklar["X-Hata-Asistani-Zaman"])
        gelen = basliklar["X-Hata-Asistani-Imza"]
    except (KeyError, ValueError):
        return False
    if abs((time.time() if simdi is None else simdi) - zaman) > tolerans:
        return False
    return hmac.compare_digest(gelen, imza(gizli, zaman, govde))


# --- Tanımlar ----------------------------------------------------------------------------------


def webhook_ekle(conn: psycopg.Connection, ad: str, adres: str, olaylar: list[str] | None = None,
                 bicim: str = "json") -> str:  # fmt: skip
    """Webhook'u ekler, gizli anahtarını döner (yalnızca bu an görülür)."""
    olaylar = list(dict.fromkeys(olaylar or OLAYLAR))
    if bilinmeyen := set(olaylar) - set(OLAYLAR):
        raise WebhookHatasi(f"Bilinmeyen olay: {', '.join(sorted(bilinmeyen))}")
    if bicim not in BICIMLER:
        raise WebhookHatasi(f"Biçim {', '.join(BICIMLER)} olmalı.")
    gizli = gizli_uret()
    try:
        conn.execute(
            "INSERT INTO webhooklar (ad, adres, bicim, gizli, olaylar) VALUES (%s, %s, %s, %s, %s)",
            [ad, adres, bicim, gizli, olaylar],
        )
    except psycopg.errors.UniqueViolation:
        raise WebhookHatasi(f"'{ad}' adında bir webhook zaten var.") from None
    except psycopg.errors.CheckViolation:
        raise WebhookHatasi(
            "Ad küçük harf, rakam, - ve _ olmalı (2-40 karakter); adres http(s):// ile "
            "başlamalı, boşluk içermemeli (en fazla 500 karakter)."
        ) from None
    return gizli


def webhooklar(conn: psycopg.Connection) -> list[dict]:
    """Tanımlar ve gönderim sayıları (gizli anahtar hariç)."""
    satirlar = conn.execute(
        """
        SELECT w.ad, w.adres, w.bicim, w.olaylar, w.aktif,
               count(*) FILTER (WHERE g.durum = 'bekliyor') AS bekleyen,
               count(*) FILTER (WHERE g.durum = 'gonderildi') AS gonderilen,
               count(*) FILTER (WHERE g.durum = 'vazgecildi') AS vazgecilen,
               max(g.gonderim) AS son_gonderim,
               (SELECT son_hata FROM giden_bildirimler s WHERE s.webhook = w.ad
                AND s.son_hata IS NOT NULL ORDER BY s.id DESC LIMIT 1) AS son_hata
        FROM webhooklar w LEFT JOIN giden_bildirimler g ON g.webhook = w.ad
        GROUP BY w.ad ORDER BY w.ad
        """
    ).fetchall()
    adlar = ["ad", "adres", "bicim", "olaylar", "aktif", "bekleyen", "gonderilen", "vazgecilen",
             "son_gonderim", "son_hata"]  # fmt: skip
    return [dict(zip(adlar, s, strict=True)) for s in satirlar]


def deneme_ekle(conn: psycopg.Connection, ad: str) -> bool:
    """Webhook'un sırasının sonuna deneme bildirimi koyar; webhook yoksa False."""
    sonuc = conn.execute(
        "INSERT INTO giden_bildirimler (webhook, olay, icerik) SELECT ad, 'deneme', "
        "jsonb_build_object('olay', 'deneme', 'zaman', now(), 'mesaj', %s::text) "
        "FROM webhooklar WHERE ad = %s",
        ["Hata Asistanı webhook denemesi", ad],
    )
    return bool(sonuc.rowcount)


# --- Gövde -------------------------------------------------------------------------------------


def _metin(olay: str, icerik: dict) -> str:
    """Teams ve Slack için tek satırlık özet."""
    if olay == "deneme":
        return "Hata Asistanı: webhook denemesi başarılı."
    if olay == "periyodik_bakim_yapildi":
        b = icerik.get("bakim", {})
        return (f"Periyodik bakım yapıldı: {b.get('makine_kodu')} {b.get('makine')} "
                f"({b.get('hat')}) · {b.get('ad')} · yapan {b.get('yapan')}")  # fmt: skip
    a = icerik.get("ariza", {})
    baslik = {
        "ariza_acildi": "Arıza yeniden açıldı" if a.get("yeniden_acildi") else "Arıza açıldı",
        "ariza_kapandi": "Arıza kapandı",
        "hat_durdu": "Hat durdu",
    }.get(olay, olay)
    parcalar = [
        f"{baslik}: {a.get('makine_kodu')} {a.get('makine')} ({a.get('hat')})",
        f"önem {ONEM_ADI.get(a.get('onem'), a.get('onem'))}",
    ]
    if a.get("hat_durdu") and olay != "ariza_kapandi":
        parcalar.append("hat durdu")
    if olay == "ariza_kapandi" and a.get("sure_dk") is not None:
        parcalar.append(f"{a['sure_dk']:g} dk sürdü")
    parcalar.append(str(a.get("aciklama", ""))[:300])
    return " · ".join(parcalar)


def govde(bicim: str, olay: str, icerik: dict) -> bytes:
    if bicim == "teams":
        # Teams "Workflows" gelen webhook'u: Adaptive Card'lı mesaj.
        veri = {"type": "message", "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {"type": "AdaptiveCard", "version": "1.4", "body": [
                {"type": "TextBlock", "text": _metin(olay, icerik), "wrap": True}]},
        }]}  # fmt: skip
    elif bicim == "slack":
        veri = {"text": _metin(olay, icerik)}
    else:
        veri = icerik
    return json.dumps(veri, ensure_ascii=False, separators=(",", ":")).encode()


def basliklar(bicim: str, gizli: str, olay: str, bildirim_id: int, govde_: bytes,
              zaman: int | None = None) -> dict[str, str]:  # fmt: skip
    zaman = int(time.time()) if zaman is None else zaman
    b = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "HataAsistani-Webhook/1.0",
        "X-Hata-Asistani-Olay": olay,
        "X-Hata-Asistani-Teslimat": str(bildirim_id),
        "X-Hata-Asistani-Zaman": str(zaman),
    }
    if bicim == "json":
        b["X-Hata-Asistani-Imza"] = imza(gizli, zaman, govde_)
    return b


# --- Gönderim ----------------------------------------------------------------------------------

# Her açık webhook'un en eski bekleyen bildirimi: yalnızca o gönderilebilir (sıra korunur).
SIRADAKILER = """
    FROM giden_bildirimler g JOIN webhooklar w ON w.ad = g.webhook
    WHERE g.durum = 'bekliyor' AND w.aktif
      AND NOT EXISTS (SELECT 1 FROM giden_bildirimler o WHERE o.webhook = g.webhook
                      AND o.durum = 'bekliyor' AND o.id < g.id)
"""


@dataclass
class Bildirim:
    id: int
    webhook: str
    olay: str
    icerik: dict
    deneme: int
    adres: str
    gizli: str
    bicim: str


def bekleme(deneme: int, rng: random.Random | None = None) -> float:
    """deneme. başarısızlıktan sonra beklenecek süre (±%20 dağıtılmış: alıcı dönünce bütün
    göndericiler aynı anda yüklenmesin)."""
    carpan = (rng or random).uniform(0.8, 1.2)
    return min(EN_UZUN_BEKLEME, ILK_BEKLEME * 2 ** (deneme - 1)) * carpan


def gonder(http: httpx.Client, b: Bildirim) -> str | None:
    """İsteği gönderir; başarılıysa None, değilse kısa hata metni. Yönlendirme izlenmez (adres
    yöneticinin verdiğidir; başka yere gitmesin)."""
    icerik = govde(b.bicim, b.olay, b.icerik)
    try:
        cevap = http.post(b.adres, content=icerik,
                          headers=basliklar(b.bicim, b.gizli, b.olay, b.id, icerik))  # fmt: skip
    except httpx.HTTPError as hata:
        return f"{type(hata).__name__}: {hata}"[:HATA_SINIRI]
    if 200 <= cevap.status_code < 300:
        return None
    return f"HTTP {cevap.status_code}: {cevap.text[:200]}"[:HATA_SINIRI]


def bir_tur(conn: psycopg.Connection, http: httpx.Client, rng: random.Random | None = None) -> int:
    """Gönderilme zamanı gelmiş bildirimleri (her webhook'un en eskisi) aynı anda gönderir ve
    sonuçları yazar. Gönderilen (başarılı ya da başarısız) sayısını döner.

    Satırlar işlem boyunca kilitlidir (FOR UPDATE SKIP LOCKED): iki gönderici çalışsa da aynı
    bildirim iki kez gönderilmez ve sıra bozulmaz. Bağlantı autocommit olmalı."""
    rng = rng or random.Random()
    with conn.transaction():
        satirlar = conn.execute(
            "SELECT g.id, g.webhook, g.olay, g.icerik, g.deneme, w.adres, w.gizli, w.bicim"
            + SIRADAKILER
            + "AND g.sonraki <= now() ORDER BY g.id LIMIT %s FOR UPDATE OF g SKIP LOCKED",
            [PARTI],
        ).fetchall()
        if not satirlar:
            return 0
        bildirimler = [Bildirim(*s) for s in satirlar]
        with ThreadPoolExecutor(max_workers=len(bildirimler)) as havuz:
            hatalar = list(havuz.map(lambda b: gonder(http, b), bildirimler))
        for b, hata in zip(bildirimler, hatalar, strict=True):
            if hata is None:
                conn.execute(
                    "UPDATE giden_bildirimler SET durum = 'gonderildi', gonderim = now(), "
                    "deneme = deneme + 1, son_hata = NULL WHERE id = %s",
                    [b.id],
                )
            elif b.deneme + 1 >= EN_FAZLA_DENEME or b.olay == "deneme":
                conn.execute(
                    "UPDATE giden_bildirimler SET durum = 'vazgecildi', deneme = deneme + 1, "
                    "son_hata = %s WHERE id = %s",
                    [hata, b.id],
                )
            else:
                conn.execute(
                    "UPDATE giden_bildirimler SET deneme = deneme + 1, son_hata = %s, "
                    "sonraki = now() + %s WHERE id = %s",
                    [hata, timedelta(seconds=bekleme(b.deneme + 1, rng)), b.id],
                )
    return len(bildirimler)


def sonraki_bekleme(conn: psycopg.Connection, en_fazla: float) -> float:
    """Sıradaki bildirimin zamanı gelene kadar saniye (yoksa `en_fazla`)."""
    kalan = conn.execute(
        "SELECT extract(epoch FROM min(g.sonraki) - now())" + SIRADAKILER
    ).fetchone()[0]
    return en_fazla if kalan is None else max(0.0, min(float(kalan), en_fazla))
