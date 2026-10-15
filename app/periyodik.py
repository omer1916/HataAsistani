"""Periyodik bakım takibi: her planın son bakımı, sıradaki tarihi ve durumu.

Plan (periyodik_bakimlar tablosu) bir makine, bir aralık ve kılavuzdaki kaynaktır, örneğin
"P3-HP, Yıllık bakım, 365 günde bir, PRES-BK-01 s. 3". Son bakım, plana bağlı en son kapanan
iş emrinin kapanış günüdür; plana bağlı iş emri yoksa planın baz tarihi (sisteme geçmeden
önceki son bakım). Sıradaki bakım = son bakım + aralık. Durum bugüne göre:

- gecikti: sıradaki tarih geçti
- yaklasiyor: sıradaki tarih bugün ya da önümüzdeki YAKLASMA_GUN gün içinde
- zamaninda: daha ileride

"Yapıldı" kaydı plana bağlı, kapanmış bir periyodik iş emri açar; sıradaki tarih kendiliğinden
kayar. Web arayüzü, agent aracı, Telegram botu ve bakım planı bu modülü kullanır.
"""

from datetime import date, datetime, time, timedelta
from typing import Literal

import psycopg
from psycopg.rows import dict_row

from app.db import TZ

YAKLASMA_GUN = 7
# Geçmiş bir güne yazılan bakımın kapanış saati (vardiya ortası); bugüne yazılan "şimdi" kapanır.
GECMIS_KAPANIS_SAATI = time(16, 0)

Durum = Literal["gecikti", "yaklasiyor", "zamaninda"]
DURUM_ADLARI: dict[str, str] = {
    "gecikti": "gecikti",
    "yaklasiyor": "yaklaşıyor",
    "zamaninda": "zamanında",
}


class BakimKaydiHatasi(ValueError):
    """Yapıldı kaydı geçersiz (ör. ileri tarih); mesaj kullanıcıya gösterilir."""


def durum_hesapla(siradaki: date, bugun: date) -> tuple[int, Durum]:
    """(sıradaki bakıma kalan gün, durum). Kalan gün eksiyse bakım o kadar gün gecikmiştir."""
    kalan = (siradaki - bugun).days
    if kalan < 0:
        return kalan, "gecikti"
    if kalan <= YAKLASMA_GUN:
        return kalan, "yaklasiyor"
    return kalan, "zamaninda"


def kalan_metni(kalan: int) -> str:
    if kalan < 0:
        return f"{-kalan} gün gecikti"
    if kalan == 0:
        return "bugün"
    if kalan == 1:
        return "yarın"
    return f"{kalan} gün sonra"


def bakimlari_getir(
    conn: psycopg.Connection,
    bugun: date,
    *,
    makine_kodu: str | None = None,
    hat: str | None = None,
    plan_id: int | None = None,
) -> list[dict]:
    """Planların durumu; en çok geciken (en az gün kalan) önce. Filtreler isteğe bağlı; hat adı
    veritabanındaki yazımıyla verilmeli (bkz. sorgular.hat_adini_bul)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT p.id AS plan_id, m.kod AS makine_kodu, m.ad AS makine_adi, h.ad AS hat,
                   p.ad AS bakim, p.aralik_gun, p.sure_saat, p.yapilacaklar,
                   p.dokuman_kodu, p.sayfa, p.bolum, p.baz_tarih,
                   son.kapanis AS son_kapanis, son.atanan AS son_yapan,
                   EXISTS (SELECT 1 FROM is_emirleri e
                           WHERE e.plan_id = p.id AND e.kapanis IS NULL) AS acik_is_emri
            FROM periyodik_bakimlar p
            JOIN makineler m ON m.id = p.makine_id
            JOIN hatlar h    ON h.id = m.hat_id
            LEFT JOIN LATERAL (
                SELECT e.kapanis, e.atanan FROM is_emirleri e
                WHERE e.plan_id = p.id AND e.kapanis IS NOT NULL
                ORDER BY e.kapanis DESC LIMIT 1
            ) son ON true
            WHERE (%(makine)s::text IS NULL OR m.kod = upper(%(makine)s::text))
              AND (%(hat)s::text IS NULL OR h.ad = %(hat)s::text)
              AND (%(plan)s::int IS NULL OR p.id = %(plan)s::int)
            """,
            {
                "makine": makine_kodu.strip() if makine_kodu else None,
                "hat": hat,
                "plan": plan_id,
            },
        )
        satirlar = cur.fetchall()

    bakimlar = []
    for s in satirlar:
        kapanis = s.pop("son_kapanis")
        s["kayitli"] = kapanis is not None  # False: son bakım sistem öncesi baz tarih
        s["son_bakim"] = kapanis.astimezone(TZ).date() if kapanis else s["baz_tarih"]
        del s["baz_tarih"]
        s["siradaki"] = s["son_bakim"] + timedelta(days=s["aralik_gun"])
        s["kalan_gun"], s["durum"] = durum_hesapla(s["siradaki"], bugun)
        bakimlar.append(s)
    bakimlar.sort(key=lambda b: (b["kalan_gun"], b["makine_kodu"], b["aralik_gun"]))
    return bakimlar


def ozet(bakimlar: list[dict]) -> dict[str, int]:
    return {
        "gecikmis": sum(b["durum"] == "gecikti" for b in bakimlar),
        "yaklasan": sum(b["durum"] == "yaklasiyor" for b in bakimlar),
    }


def yapildi_kaydet(
    conn: psycopg.Connection,
    plan_id: int,
    *,
    yapan: str,
    simdi: datetime,
    tarih: date | None = None,
    sure_saat: float | None = None,
    aciklama: str | None = None,
) -> dict | None:
    """Planın bakımının yapıldığını kaydeder: plana bağlı, kapanmış bir periyodik iş emri açar.
    Planın güncel durumunu döner; plan yoksa None. Süre verilmezse planın tahmini süresi."""
    bugun = simdi.astimezone(TZ).date()
    tarih = tarih or bugun
    if tarih > bugun:
        raise BakimKaydiHatasi("Bakım tarihi ileri bir gün olamaz.")
    plan = conn.execute(
        "SELECT makine_id, ad, sure_saat FROM periyodik_bakimlar WHERE id = %s", [plan_id]
    ).fetchone()
    if plan is None:
        return None
    makine_id, ad, plan_suresi = plan
    kapanis = simdi if tarih == bugun else datetime.combine(tarih, GECMIS_KAPANIS_SAATI, TZ)
    acilis = kapanis - timedelta(hours=sure_saat or plan_suresi)
    conn.execute(
        """
        INSERT INTO is_emirleri (makine_id, tip, aciklama, durum, atanan, acilis, kapanis, plan_id)
        VALUES (%s, 'periyodik', %s, 'kapali', %s, %s, %s, %s)
        """,
        [makine_id, f"{ad}: {aciklama}" if aciklama else ad, yapan, acilis, kapanis, plan_id],
    )
    return bakimlari_getir(conn, bugun, plan_id=plan_id)[0]


def gecikmis_bakimlar(conn: psycopg.Connection, bugun: date) -> dict[str, list[dict]]:
    """Makine koduna göre geciken planlar (bakım planı bunları öne alır)."""
    sonuc: dict[str, list[dict]] = {}
    for b in bakimlari_getir(conn, bugun):
        if b["durum"] == "gecikti":
            sonuc.setdefault(b["makine_kodu"], []).append(b)
    return sonuc
