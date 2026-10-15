"""Makine entegrasyonu: makinelerden ve başka sistemlerden gelen alarm olaylarını arızaya çevirir.

Bir kaynak (PLC, CNC, bağlayıcı, MES) iki tür olay gönderir: "alarm başladı" ve "alarm bitti".
Hata Asistanı bunlardan arıza kaydı açar ve kapatır; arıza tablosundaki tetikleyici sayesinde
Telegram bildirimi anında gider, kayıt raporlara ve bakım planına girer.

Kurallar:
- Aynı olay (kaynak, olay_id) ikinci kez gelirse yeniden işlenmez, ilk sonucu döner. Bağlayıcı
  emin olamadığı her olayı yeniden gönderebilir.
- Alarm başlayınca: makinede bu kaynaktan açık bir arıza varsa alarm ona eklenir (bir duruş,
  birkaç alarm). Yoksa son KISA_ARA içinde kapanmış bir arıza yeniden açılır: alarm kapanıp
  hemen geri geliyorsa her seferinde yeni arıza ve yeni bildirim olmasın. Yeni alarm hattı
  durduruyor ama o arıza durdurmuyorduysa yeniden açılmaz, yeni arıza açılır (bildirim gitsin).
  Yoksa yeni arıza açılır. Hattı durduran bir alarm eklenince arıza "hat durdu"ya, önemi en
  yükseğe çıkar.
- Alarm bitince: arızanın süren başka alarmı yoksa arıza kapanır.
- Bitiş başlangıçtan önce gelirse (ağda sıra karıştı) saklanır; başlangıç gelince eşleşir.
- Tip, önem ve hattı durdurup durdurmadığı: önce olayın kendisi, sonra kaynağın kataloğu (tam
  kod, sonra en uzun önek), sonra kaynağın varsayılanları. Eşlenmemiş alarm da kaydedilir.
- Gelecek zamanlı olay alınma anına çekilir (makinenin saati ileri); ESKI_OLAY'dan eski olay
  reddedilir.
- Aynı makinenin olayları sırayla işlenir (makine başına kilit): eşzamanlı istekler aynı duruş
  için iki arıza açamaz.
"""

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg
from psycopg.rows import dict_row

from app.db import TZ

KISA_ARA = timedelta(seconds=60)
GEC_BITIS_PENCERESI = timedelta(days=7)
ESKI_OLAY = timedelta(days=30)
ONEM_SIRASI = {"dusuk": 0, "orta": 1, "yuksek": 2}
KILIT_ANAHTARI = 0x48415F45  # pg_advisory_xact_lock'un ilk anahtarı ("HA_E"); ikincisi makine id
ANAHTAR_ONEKI = "ha_"
EN_FAZLA_OLAY = 500  # bir istekteki olay
ACIKLAMA_SINIRI = 400  # eklenen alarmlar arıza açıklamasını bundan uzun yapmaz

# Pres hattı operatör panelinin alarm kodları (PRES-BK-01 s. 2 listesi, her kodun bölümü).
# (desen, anlam, tip, önem, hattı durdurur, kılavuz sayfası)
PRES_PANELI = [
    ("A101", "Hidrolik basınç düşük", "hidrolik", "yuksek", True, 5),
    ("A102", "Yağ sıcaklığı yüksek", "hidrolik", "orta", True, 6),
    ("A103", "Yağ seviyesi düşük", "hidrolik", "yuksek", True, 6),
    ("A104", "Filtre tıkanık", "hidrolik", "orta", False, 6),
    ("A201", "Işık bariyeri ihlali", "sensor", "dusuk", True, 8),
    ("A202", "Kalıp emniyet pimi takılı değil", "mekanik", "orta", True, 2),
    ("A301", "Motor sürücü hatası", "elektrik", "yuksek", True, 7),
    ("A302", "Acil stop devresi açık", "elektrik", "yuksek", True, 7),
    ("A401", "Besleme hatası", "mekanik", "orta", True, 9),
    ("A501", "Transfer robotu pozisyon hatası", "pnomatik", "orta", True, 9),
]
HAZIR_KATALOGLAR = {"pres-paneli": ("PRES-BK-01", PRES_PANELI)}


@dataclass(frozen=True)
class Kaynak:
    ad: str
    katalog: str | None
    varsayilan_tip: str
    varsayilan_onem: str
    varsayilan_hat_durdurur: bool
    yetki: str = "olay"  # olay ya da okuma (servis hesabı)


@dataclass(frozen=True)
class Olay:
    olay_id: str
    tur: str  # "alarm_basladi" ya da "alarm_bitti"
    makine_kodu: str
    alarm_kodu: str
    zaman: datetime
    mesaj: str | None = None
    ariza_tipi: str | None = None
    onem: str | None = None
    hat_durdurur: bool | None = None


@dataclass(frozen=True)
class Sonuc:
    olay_id: str
    durum: str  # islendi, tekrar, hata
    sonuc: str | None = None
    ariza_id: int | None = None
    eslesme: str | None = None  # katalog ya da varsayilan (yalnızca başlangıçta)
    hata: str | None = None


@dataclass(frozen=True)
class Ozellik:
    tip: str
    onem: str
    hat_durdurur: bool
    anlam: str | None
    eslesme: str


class OlayHatasi(ValueError):
    """Olay kalıcı olarak işlenemez (bilinmeyen makine, çok eski olay); yeniden denenmemeli."""


# --- Anahtarlar ve kaynaklar -----------------------------------------------------------------


def anahtar_uret() -> str:
    return ANAHTAR_ONEKI + secrets.token_urlsafe(32)


def anahtar_ozeti(anahtar: str) -> str:
    return hashlib.sha256(anahtar.encode()).hexdigest()


def kaynak_bul(conn: psycopg.Connection, anahtar: str | None) -> Kaynak | None:
    """Anahtarın ait olduğu etkin kaynak; anahtar yoksa, yanlışsa ya da kaynak kapalıysa None.
    Karşılaştırma özet üzerinden veritabanında yapılır (anahtarın kendisi saklanmaz)."""
    if not anahtar or not anahtar.startswith(ANAHTAR_ONEKI) or len(anahtar) > 200:
        return None
    satir = conn.execute(
        "SELECT ad, katalog, varsayilan_tip, varsayilan_onem, varsayilan_hat_durdurur, yetki "
        "FROM entegrasyon_kaynaklari WHERE anahtar_ozeti = %s AND aktif",
        [anahtar_ozeti(anahtar)],
    ).fetchone()
    return Kaynak(*satir) if satir else None


def kaynak_ekle(
    conn: psycopg.Connection,
    ad: str,
    aciklama: str,
    *,
    katalog: str | None = None,
    varsayilan_tip: str = "mekanik",
    varsayilan_onem: str = "orta",
    varsayilan_hat_durdurur: bool = True,
    anahtar: str | None = None,
    yetki: str = "olay",
) -> str:
    """Kaynağı ekler ve anahtarını döner (anahtar yalnızca bu an görülür, saklanmaz).
    yetki="okuma": servis hesabı, yalnızca okur."""
    anahtar = anahtar or anahtar_uret()
    conn.execute(
        "INSERT INTO entegrasyon_kaynaklari (ad, aciklama, katalog, anahtar_ozeti, varsayilan_tip, "
        "varsayilan_onem, varsayilan_hat_durdurur, yetki) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        [ad, aciklama, katalog, anahtar_ozeti(anahtar), varsayilan_tip, varsayilan_onem,
         varsayilan_hat_durdurur, yetki],
    )  # fmt: skip
    return anahtar


def anahtar_yenile(conn: psycopg.Connection, ad: str, anahtar: str | None = None) -> str | None:
    """Kaynağın anahtarını değiştirir (eskisi hemen geçersiz olur); kaynak yoksa None."""
    anahtar = anahtar or anahtar_uret()
    sonuc = conn.execute(
        "UPDATE entegrasyon_kaynaklari SET anahtar_ozeti = %s WHERE ad = %s",
        [anahtar_ozeti(anahtar), ad],
    )
    return anahtar if sonuc.rowcount else None


def demo_kaynagi_hazirla(conn: psycopg.Connection, anahtar: str) -> None:
    """Simülatör kaynağını .env'deki anahtarla hazırlar: yoksa ekler, anahtar değiştiyse
    günceller."""
    if not anahtar.startswith(ANAHTAR_ONEKI) or len(anahtar) < 20:
        raise ValueError(
            f"Simülatör anahtarı '{ANAHTAR_ONEKI}' ile başlamalı ve en az 20 karakter olmalı."
        )
    if anahtar_yenile(conn, "simulator", anahtar) is None:
        kaynak_ekle(
            conn,
            "simulator",
            "Pres 3 hattının PLC simülatörü (OPC UA, entegrasyon/simulator.py)",
            katalog="pres-paneli",
            anahtar=anahtar,
        )


def katalogu_yukle(conn: psycopg.Connection) -> None:
    """Hazır katalogları ekler; elle değiştirilmiş satırlara dokunmaz."""
    with conn.cursor() as cur:
        for katalog, (dokuman, alarmlar) in HAZIR_KATALOGLAR.items():
            cur.executemany(
                "INSERT INTO alarm_katalogu (katalog, desen, anlam, ariza_tipi, onem, "
                "hat_durdurur, dokuman_kodu, sayfa) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                [(katalog, desen, anlam, tip, onem, durdurur, dokuman, sayfa)
                 for desen, anlam, tip, onem, durdurur, sayfa in alarmlar],
            )  # fmt: skip


def eslesme_bul(conn: psycopg.Connection, katalog: str | None, alarm_kodu: str) -> dict | None:
    """Katalogda alarm koduna uyan satır: tam eşleşme, yoksa en uzun önek ("SV*")."""
    if katalog is None:
        return None
    with conn.cursor(row_factory=dict_row) as cur:
        # LIKE değil left(): desende "_" olabilir, LIKE onu joker sanardı.
        cur.execute(
            """
            SELECT desen, anlam, ariza_tipi, onem, hat_durdurur, dokuman_kodu, sayfa
            FROM alarm_katalogu
            WHERE katalog = %(katalog)s
              AND (desen = %(kod)s
                   OR (right(desen, 1) = '*'
                       AND left(%(kod)s, length(desen) - 1) = left(desen, length(desen) - 1)))
            ORDER BY desen = %(kod)s DESC, length(desen) DESC
            LIMIT 1
            """,
            {"katalog": katalog, "kod": alarm_kodu},
        )
        return cur.fetchone()


def ozellik_belirle(kaynak: Kaynak, eslesme: dict | None, olay: Olay) -> Ozellik:
    """Her alan için: olayın kendisi > katalog > kaynağın varsayılanı."""

    def sec(olaydaki, katalog_alani, varsayilan):
        if olaydaki is not None:
            return olaydaki
        return eslesme[katalog_alani] if eslesme else varsayilan

    return Ozellik(
        tip=sec(olay.ariza_tipi, "ariza_tipi", kaynak.varsayilan_tip),
        onem=sec(olay.onem, "onem", kaynak.varsayilan_onem),
        hat_durdurur=sec(olay.hat_durdurur, "hat_durdurur", kaynak.varsayilan_hat_durdurur),
        anlam=eslesme["anlam"] if eslesme else None,
        eslesme="katalog" if eslesme else "varsayilan",
    )


def aciklama_metni(olay: Olay, ozellik: Ozellik) -> str:
    """Arızanın açıklaması: kod ve anlamı (kılavuz araması da bunu kullanır)."""
    metin = f"{olay.alarm_kodu} {ozellik.anlam or olay.mesaj or 'alarmı'}"
    if ozellik.anlam and olay.mesaj and olay.mesaj != ozellik.anlam:
        metin += f" ({olay.mesaj})"
    if ozellik.eslesme == "varsayilan":
        metin += " [eşlenmemiş alarm kodu]"
    return metin[:ACIKLAMA_SINIRI]


# --- Olay işleme -----------------------------------------------------------------------------


def olay_isle(conn: psycopg.Connection, kaynak: Kaynak, olay: Olay, simdi: datetime) -> Sonuc:
    """Tek olayı kendi işleminde (transaction) işler. Bağlantı autocommit olmalı."""
    try:
        with conn.transaction():
            return _isle(conn, kaynak, olay, simdi)
    except OlayHatasi as hata:
        return Sonuc(olay.olay_id, "hata", hata=str(hata))
    except psycopg.errors.UniqueViolation:
        # Aynı olay_id eşzamanlı ve başka bir makine koduyla geldi: ilk kayıt esastır.
        onceki = _onceki_sonuc(conn, kaynak, olay.olay_id)
        if onceki is None:
            raise
        return onceki


def _onceki_sonuc(conn, kaynak: Kaynak, olay_id: str) -> Sonuc | None:
    satir = conn.execute(
        "SELECT sonuc, ariza_id FROM entegrasyon_olaylari WHERE kaynak = %s AND olay_id = %s",
        [kaynak.ad, olay_id],
    ).fetchone()
    return Sonuc(olay_id, "tekrar", satir[0], satir[1]) if satir else None


def _isle(conn, kaynak: Kaynak, olay: Olay, simdi: datetime) -> Sonuc:
    makine = conn.execute(
        "SELECT id FROM makineler WHERE kod = upper(%s)", [olay.makine_kodu.strip()]
    ).fetchone()
    if makine is None:
        raise OlayHatasi(f"'{olay.makine_kodu}' kodlu makine yok.")
    makine_id = makine[0]
    # Aynı makinenin olayları sırayla: eşzamanlı iki başlangıç iki arıza açmasın.
    conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", [KILIT_ANAHTARI, makine_id])

    if onceki := _onceki_sonuc(conn, kaynak, olay.olay_id):
        return onceki
    if olay.zaman < simdi - ESKI_OLAY:
        raise OlayHatasi(f"Olay {ESKI_OLAY.days} günden eski; işlenmedi.")
    zaman = min(olay.zaman, simdi)  # makinenin saati ileriyse

    eslesme = None
    if olay.tur == "alarm_basladi":
        sonuc, ariza_id, eslesme = _baslat(conn, kaynak, makine_id, olay, zaman)
    else:
        sonuc, ariza_id = _bitir(conn, kaynak, makine_id, olay.alarm_kodu, zaman)
    conn.execute(
        "INSERT INTO entegrasyon_olaylari (kaynak, olay_id, tur, makine_id, alarm_kodu, mesaj, "
        "zaman, alinma, sonuc, ariza_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        [kaynak.ad, olay.olay_id, olay.tur, makine_id, olay.alarm_kodu, olay.mesaj, zaman, simdi,
         sonuc, ariza_id],
    )  # fmt: skip
    return Sonuc(olay.olay_id, "islendi", sonuc, ariza_id, eslesme)


def _baslat(conn, kaynak: Kaynak, makine_id: int, olay: Olay, zaman: datetime):
    ozellik = ozellik_belirle(kaynak, eslesme_bul(conn, kaynak.katalog, olay.alarm_kodu), olay)
    anahtar = {"kaynak": kaynak.ad, "makine": makine_id, "kod": olay.alarm_kodu}

    aktif = conn.execute(
        "SELECT a.ariza_id, r.bitis IS NULL FROM aktif_alarmlar a "
        "JOIN ariza_kayitlari r ON r.id = a.ariza_id "
        "WHERE a.kaynak = %(kaynak)s AND a.makine_id = %(makine)s AND a.alarm_kodu = %(kod)s",
        anahtar,
    ).fetchone()
    if aktif and aktif[1]:
        return "zaten_aktif", aktif[0], ozellik.eslesme
    if aktif:  # arıza elle kapatılmış ama alarm satırı kalmış
        _alarm_satirini_sil(conn, anahtar)

    # Bu alarmın daha önce gelmiş, eşleşmemiş bitişi (ağda sıra karıştı).
    gec_bitis = conn.execute(
        "SELECT id, zaman FROM entegrasyon_olaylari "
        "WHERE kaynak = %(kaynak)s AND makine_id = %(makine)s AND alarm_kodu = %(kod)s "
        "AND tur = 'alarm_bitti' AND sonuc = 'aktif_alarm_yok' "
        "AND zaman >= %(zaman)s AND zaman <= %(sinir)s ORDER BY zaman, id LIMIT 1",
        {**anahtar, "zaman": zaman, "sinir": zaman + GEC_BITIS_PENCERESI},
    ).fetchone()

    acik = conn.execute(
        "SELECT r.id FROM aktif_alarmlar a JOIN ariza_kayitlari r ON r.id = a.ariza_id "
        "WHERE a.kaynak = %(kaynak)s AND a.makine_id = %(makine)s AND r.bitis IS NULL "
        "ORDER BY r.id LIMIT 1",
        anahtar,
    ).fetchone()
    if acik:
        ariza_id, sonuc = acik[0], "ariza_eklendi"
        _yukselt(conn, ariza_id, olay, ozellik)
    elif (ariza_id := _yeniden_acilacak(conn, kaynak, makine_id, zaman, ozellik)) is not None:
        conn.execute("UPDATE ariza_kayitlari SET bitis = NULL WHERE id = %s", [ariza_id])
        _yukselt(conn, ariza_id, olay, ozellik)
        sonuc = "ariza_yeniden_acildi"
    else:
        ariza_id = conn.execute(
            "INSERT INTO ariza_kayitlari (makine_id, baslangic, ariza_tipi, onem, hat_durdu, "
            "aciklama) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            [makine_id, zaman, ozellik.tip, ozellik.onem, ozellik.hat_durdurur,
             aciklama_metni(olay, ozellik)],
        ).fetchone()[0]  # fmt: skip
        sonuc = "ariza_acildi"

    conn.execute(
        "INSERT INTO aktif_alarmlar (kaynak, makine_id, alarm_kodu, ariza_id, baslangic) "
        "VALUES (%(kaynak)s, %(makine)s, %(kod)s, %(ariza)s, %(zaman)s)",
        {**anahtar, "ariza": ariza_id, "zaman": zaman},
    )
    if gec_bitis:
        _alarmi_kapat(conn, anahtar, ariza_id, gec_bitis[1])
        conn.execute(
            "UPDATE entegrasyon_olaylari SET sonuc = 'gec_eslesti', ariza_id = %s WHERE id = %s",
            [ariza_id, gec_bitis[0]],
        )
    return sonuc, ariza_id, ozellik.eslesme


def _yeniden_acilacak(conn, kaynak: Kaynak, makine_id: int, zaman: datetime, ozellik: Ozellik):
    """Bu kaynağın bu makinede son KISA_ARA içinde kapanmış arızası. Yeni alarm hattı durduruyor
    ama o arıza durdurmuyorduysa yeniden açılmaz: yeni duruş, yeni bildirim."""
    satir = conn.execute(
        "SELECT r.id, r.hat_durdu FROM ariza_kayitlari r "
        "WHERE r.makine_id = %(makine)s AND r.bitis IS NOT NULL "
        "AND r.bitis >= %(alt)s AND r.baslangic <= %(zaman)s "
        "AND EXISTS (SELECT 1 FROM entegrasyon_olaylari e "
        "            WHERE e.kaynak = %(kaynak)s AND e.ariza_id = r.id) "
        "ORDER BY r.bitis DESC, r.id DESC LIMIT 1",
        {"makine": makine_id, "alt": zaman - KISA_ARA, "zaman": zaman, "kaynak": kaynak.ad},
    ).fetchone()
    if satir is None or (ozellik.hat_durdurur and not satir[1]):
        return None
    return satir[0]


def _yukselt(conn, ariza_id: int, olay: Olay, ozellik: Ozellik) -> None:
    """Eklenen alarm arızayı ağırlaştırabilir: hattı durdurur, önemi yükseltir; kodu açıklamaya
    eklenir. hat_durdu yalnızca değişecekse yazılır (tetikleyici botu boşuna uyandırmasın)."""
    if ozellik.hat_durdurur:
        conn.execute(
            "UPDATE ariza_kayitlari SET hat_durdu = true WHERE id = %s AND NOT hat_durdu",
            [ariza_id],
        )
    ek = f" · {olay.alarm_kodu} {ozellik.anlam or olay.mesaj or ''}".rstrip()
    conn.execute(
        """
        UPDATE ariza_kayitlari SET
            onem = CASE WHEN %(sira)s > (CASE onem WHEN 'dusuk' THEN 0
                                                   WHEN 'orta' THEN 1 ELSE 2 END)
                        THEN %(onem)s ELSE onem END,
            aciklama = CASE WHEN strpos(aciklama, %(kod)s) = 0
                             AND length(aciklama) + length(%(ek)s) <= %(sinir)s
                            THEN aciklama || %(ek)s ELSE aciklama END
        WHERE id = %(id)s
        """,
        {"sira": ONEM_SIRASI[ozellik.onem], "onem": ozellik.onem, "kod": olay.alarm_kodu,
         "ek": ek, "sinir": ACIKLAMA_SINIRI, "id": ariza_id},
    )  # fmt: skip


def _bitir(conn, kaynak: Kaynak, makine_id: int, alarm_kodu: str, zaman: datetime):
    anahtar = {"kaynak": kaynak.ad, "makine": makine_id, "kod": alarm_kodu}
    aktif = conn.execute(
        "SELECT ariza_id FROM aktif_alarmlar "
        "WHERE kaynak = %(kaynak)s AND makine_id = %(makine)s AND alarm_kodu = %(kod)s",
        anahtar,
    ).fetchone()
    if aktif is None:
        return "aktif_alarm_yok", None  # saklanır; başlangıç sonradan gelirse eşleşir
    return _alarmi_kapat(conn, anahtar, aktif[0], zaman), aktif[0]


def _alarm_satirini_sil(conn, anahtar: dict) -> None:
    conn.execute(
        "DELETE FROM aktif_alarmlar "
        "WHERE kaynak = %(kaynak)s AND makine_id = %(makine)s AND alarm_kodu = %(kod)s",
        anahtar,
    )


def _alarmi_kapat(conn, anahtar: dict, ariza_id: int, zaman: datetime) -> str:
    """Alarmı siler; arızanın süren başka alarmı yoksa arızayı kapatır. Bitiş başlangıçtan önce
    olamaz (makine saatleri tutarsızsa): en az bir saniye sonra kapanır."""
    _alarm_satirini_sil(conn, anahtar)
    if conn.execute(
        "SELECT EXISTS (SELECT 1 FROM aktif_alarmlar WHERE ariza_id = %s)", [ariza_id]
    ).fetchone()[0]:
        return "alarm_kapandi"
    kapanan = conn.execute(
        "UPDATE ariza_kayitlari SET bitis = greatest(%s, baslangic + interval '1 second') "
        "WHERE id = %s AND bitis IS NULL",
        [zaman, ariza_id],
    ).rowcount
    return "ariza_kapandi" if kapanan else "alarm_kapandi"


def olaylari_isle(
    conn: psycopg.Connection, kaynak: Kaynak, olaylar: list[Olay], simdi: datetime
) -> list[Sonuc]:
    """Olayları verildiği sırayla, her birini kendi işleminde işler; kaynağın son olay anını
    günceller."""
    sonuclar = [olay_isle(conn, kaynak, olay, simdi) for olay in olaylar]
    conn.execute(
        "UPDATE entegrasyon_kaynaklari SET son_olay = %s WHERE ad = %s", [simdi, kaynak.ad]
    )
    return sonuclar


def aktif_alarmlar(conn: psycopg.Connection, kaynak: str | None = None) -> list[dict]:
    """Süren alarmlar (bağlayıcı yeniden bağlanınca bununla uzlaşır; arayüz de gösterir)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT a.kaynak, m.kod AS makine_kodu, a.alarm_kodu, a.baslangic, a.ariza_id, "
            "r.aciklama AS ariza_aciklamasi FROM aktif_alarmlar a "
            "JOIN makineler m ON m.id = a.makine_id JOIN ariza_kayitlari r ON r.id = a.ariza_id "
            "WHERE %(kaynak)s::text IS NULL OR a.kaynak = %(kaynak)s "
            "ORDER BY a.baslangic, m.kod, a.alarm_kodu",
            {"kaynak": kaynak},
        )
        return cur.fetchall()


def durum(conn: psycopg.Connection, olay_sayisi: int = 50) -> dict:
    """Bakım ekibi için: kaynaklar, süren alarmlar ve son olaylar (anahtarlar yok)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT k.ad, k.aciklama, k.katalog, k.aktif, k.yetki, k.son_olay, "
            "(SELECT count(*) FROM aktif_alarmlar a WHERE a.kaynak = k.ad)::int AS aktif_alarm "
            "FROM entegrasyon_kaynaklari k ORDER BY k.ad"
        )
        kaynaklar = cur.fetchall()
        cur.execute(
            "SELECT e.kaynak, e.olay_id, e.tur, m.kod AS makine_kodu, e.alarm_kodu, e.mesaj, "
            "e.zaman, e.alinma, e.sonuc, e.ariza_id FROM entegrasyon_olaylari e "
            "JOIN makineler m ON m.id = e.makine_id ORDER BY e.id DESC LIMIT %s",
            [olay_sayisi],
        )
        olaylar = cur.fetchall()
    return {"kaynaklar": kaynaklar, "aktif_alarmlar": aktif_alarmlar(conn), "son_olaylar": olaylar}


# --- Hız sınırı --------------------------------------------------------------------------------


class HizSiniri:
    """Kaynak başına jeton kovası: saniyede `hiz` olay, en fazla `kova` olay birden. Bir makine
    alarm fırtınasına girerse API'yi ve botu boğmasın. Süreç içinde tutulur (tek API süreci)."""

    def __init__(self, hiz: float = 200, kova: float = 1000, saat=time.monotonic):
        self.hiz, self.kova, self.saat = hiz, kova, saat
        self._kovalar: dict[str, tuple[float, float]] = {}
        self._kilit = threading.Lock()

    def al(self, kaynak: str, adet: int) -> float:
        """Jeton varsa düşer ve 0 döner; yoksa kaç saniye sonra yeniden denenebileceğini döner
        (jeton düşülmez)."""
        with self._kilit:
            simdi = self.saat()
            jeton, son = self._kovalar.get(kaynak, (self.kova, simdi))
            jeton = min(self.kova, jeton + (simdi - son) * self.hiz)
            if adet > jeton:
                self._kovalar[kaynak] = (jeton, simdi)
                return (adet - jeton) / self.hiz
            self._kovalar[kaynak] = (jeton - adet, simdi)
            return 0.0


def zaman_duzelt(zaman: datetime) -> datetime:
    """Saat dilimi verilmemiş zaman fabrikanın saatidir (PLC'lerin çoğu yerel saat gönderir)."""
    return zaman.replace(tzinfo=TZ) if zaman.tzinfo is None else zaman
