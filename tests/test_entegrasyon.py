"""Makine entegrasyonu: alarm olaylarının arızaya çevrilmesi, kataloğu, API'si ve güvenliği.

Senaryolar her kuralı tek tek dener; test_entegrasyon_durum.py aynı kuralları rastgele binlerce
olay dizisinde bağımsız bir modelle karşılaştırır.
"""

import asyncio
import itertools
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from app import entegrasyon as e
from app import telegram_bot as tb
from app.db import TZ
from tests.conftest import BAKIM, OPERATOR, SABIT_AN, SahteEmbedder, yetki

KOK = Path(__file__).resolve().parent.parent
SIMDI = SABIT_AN
T0 = SIMDI - timedelta(hours=2)
_sayac = itertools.count()


@pytest.fixture
def conn(test_veritabani):
    with psycopg.connect(test_veritabani, autocommit=True) as baglanti:
        yield baglanti


def temizle(conn, son_ariza: int) -> None:
    """Testin yazdığı her şeyi siler (diğer testler veriyi seed'le karşılaştırıyor)."""
    conn.execute("DELETE FROM aktif_alarmlar")
    conn.execute("DELETE FROM entegrasyon_olaylari")
    conn.execute("DELETE FROM ariza_kayitlari WHERE id > %s", [son_ariza])
    conn.execute("DELETE FROM entegrasyon_kaynaklari WHERE ad LIKE 'test-%%'")
    conn.execute("DELETE FROM alarm_katalogu WHERE katalog LIKE 'test-%%'")


@pytest.fixture
def temiz(conn):
    son = conn.execute("SELECT max(id) FROM ariza_kayitlari").fetchone()[0]
    yield conn
    temizle(conn, son)


def kaynak_olustur(conn, ad="test-pres", katalog="pres-paneli", **ayar) -> tuple[e.Kaynak, str]:
    anahtar = e.kaynak_ekle(conn, ad, "test kaynağı", katalog=katalog, **ayar)
    return e.kaynak_bul(conn, anahtar), anahtar


@pytest.fixture
def kaynak(temiz):
    return kaynak_olustur(temiz)[0]


def olay(tur, kod="A101", saniye=0, makine="P3-HP", olay_id=None, **alan) -> e.Olay:
    return e.Olay(
        olay_id=olay_id or f"o{next(_sayac)}",
        tur="alarm_basladi" if tur == "b" else "alarm_bitti",
        makine_kodu=makine,
        alarm_kodu=kod,
        zaman=T0 + timedelta(seconds=saniye),
        **alan,
    )


def isle(conn, kaynak, *olaylar, simdi=SIMDI):
    sonuclar = [e.olay_isle(conn, kaynak, o, simdi) for o in olaylar]
    return sonuclar[0] if len(sonuclar) == 1 else sonuclar


def ariza(conn, ariza_id) -> dict:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("SELECT * FROM ariza_kayitlari WHERE id = %s", [ariza_id])
        return cur.fetchone()


def aktifler(conn) -> set[tuple[str, str]]:
    return {
        (k, kod)
        for k, kod in conn.execute(
            "SELECT m.kod, a.alarm_kodu FROM aktif_alarmlar a "
            "JOIN makineler m ON m.id = a.makine_id"
        )
    }


# --- Başla, bitir, tekrar ---------------------------------------------------------------------


def test_alarm_ariza_acar_ve_kapatir(temiz, kaynak):
    s = isle(temiz, kaynak, olay("b", "A101", 0))
    assert (s.durum, s.sonuc, s.eslesme) == ("islendi", "ariza_acildi", "katalog")
    a = ariza(temiz, s.ariza_id)
    assert (a["ariza_tipi"], a["onem"], a["hat_durdu"]) == ("hidrolik", "yuksek", True)
    assert a["aciklama"] == "A101 Hidrolik basınç düşük"
    assert a["baslangic"] == T0 and a["bitis"] is None
    assert aktifler(temiz) == {("P3-HP", "A101")}

    s2 = isle(temiz, kaynak, olay("s", "A101", 300))
    assert (s2.sonuc, s2.ariza_id) == ("ariza_kapandi", s.ariza_id)
    assert ariza(temiz, s.ariza_id)["bitis"] == T0 + timedelta(seconds=300)
    assert aktifler(temiz) == set()


def test_ayni_olay_ikinci_kez_islenmez(temiz, kaynak):
    o = olay("b", olay_id="ayni-olay")
    ilk, ikinci = isle(temiz, kaynak, o, o)
    assert ilk.durum == "islendi" and ikinci.durum == "tekrar"
    assert (ikinci.sonuc, ikinci.ariza_id) == (ilk.sonuc, ilk.ariza_id)
    # Aynı kimlik başka bir makine koduyla gelse de ilk kayıt esas.
    ucuncu = isle(temiz, kaynak, olay("b", makine="P1-HP", olay_id="ayni-olay"))
    assert (ucuncu.durum, ucuncu.ariza_id) == ("tekrar", ilk.ariza_id)
    sayi = temiz.execute("SELECT count(*) FROM entegrasyon_olaylari").fetchone()[0]
    assert sayi == 1


def test_suren_alarm_ikinci_kez_baslarsa_yeni_ariza_acilmaz(temiz, kaynak):
    ilk, ikinci = isle(temiz, kaynak, olay("b", "A101", 0), olay("b", "A101", 30))
    assert ikinci.sonuc == "zaten_aktif" and ikinci.ariza_id == ilk.ariza_id


def test_aktif_alarm_yokken_bitis(temiz, kaynak):
    s = isle(temiz, kaynak, olay("s", "A101", 0))
    assert (s.sonuc, s.ariza_id) == ("aktif_alarm_yok", None)
    assert temiz.execute("SELECT count(*) FROM aktif_alarmlar").fetchone()[0] == 0


# --- Bir duruş, birkaç alarm -------------------------------------------------------------------


def test_ayni_makinedeki_alarmlar_tek_arizada_toplanir_ve_agirlastirir(temiz, kaynak):
    filtre, basinc = isle(temiz, kaynak, olay("b", "A104", 0), olay("b", "A101", 20))
    assert filtre.sonuc == "ariza_acildi" and basinc.sonuc == "ariza_eklendi"
    assert basinc.ariza_id == filtre.ariza_id
    a = ariza(temiz, filtre.ariza_id)
    # A104 hattı durdurmaz, orta; A101 eklenince hat durdu, önem yüksek.
    assert (a["hat_durdu"], a["onem"], a["ariza_tipi"]) == (True, "yuksek", "hidrolik")
    assert a["aciklama"] == "A104 Filtre tıkanık · A101 Hidrolik basınç düşük"

    ilk_biten = isle(temiz, kaynak, olay("s", "A104", 60))
    assert ilk_biten.sonuc == "alarm_kapandi" and ariza(temiz, filtre.ariza_id)["bitis"] is None
    son_biten = isle(temiz, kaynak, olay("s", "A101", 90))
    assert son_biten.sonuc == "ariza_kapandi"
    assert ariza(temiz, filtre.ariza_id)["bitis"] == T0 + timedelta(seconds=90)


def test_hafif_alarm_agir_arizayi_hafifletmez(temiz, kaynak):
    agir, hafif = isle(temiz, kaynak, olay("b", "A101", 0), olay("b", "A201", 5))
    a = ariza(temiz, agir.ariza_id)
    assert (a["onem"], a["hat_durdu"]) == ("yuksek", True)


def test_farkli_makineler_ve_kaynaklar_ayri_ariza(temiz, kaynak):
    diger, _ = kaynak_olustur(temiz, "test-diger")
    s1, s2 = isle(temiz, kaynak, olay("b", "A101", 0), olay("b", "A101", 0, makine="P1-HP"))
    s3 = isle(temiz, diger, olay("b", "A101", 0))
    assert len({s1.ariza_id, s2.ariza_id, s3.ariza_id}) == 3


# --- Kapanıp hemen geri gelen alarm ------------------------------------------------------------


def test_kisa_arada_geri_gelen_alarm_ayni_arizayi_yeniden_acar(temiz, kaynak):
    ilk, _, geri = isle(
        temiz, kaynak, olay("b", "A101", 0), olay("s", "A101", 10), olay("b", "A101", 69)
    )
    assert geri.sonuc == "ariza_yeniden_acildi" and geri.ariza_id == ilk.ariza_id
    assert ariza(temiz, ilk.ariza_id)["bitis"] is None
    isle(temiz, kaynak, olay("s", "A101", 100))
    assert ariza(temiz, ilk.ariza_id)["bitis"] == T0 + timedelta(seconds=100)


def test_uzun_aradan_sonra_yeni_ariza(temiz, kaynak):
    ilk, _, sonra = isle(
        temiz, kaynak, olay("b", "A101", 0), olay("s", "A101", 10), olay("b", "A101", 71)
    )
    assert sonra.sonuc == "ariza_acildi" and sonra.ariza_id != ilk.ariza_id


def test_durdurmayan_ariza_durduran_alarmla_yeniden_acilmaz(temiz, kaynak):
    # A104 hattı durdurmuyordu; ardından gelen A101 yeni bir duruştur, bildirimi gitmeli.
    ilk, _, sonra = isle(
        temiz, kaynak, olay("b", "A104", 0), olay("s", "A104", 10), olay("b", "A101", 20)
    )
    assert sonra.sonuc == "ariza_acildi" and sonra.ariza_id != ilk.ariza_id


def test_elle_kapatilan_ariza(temiz, kaynak):
    s = isle(temiz, kaynak, olay("b", "A101", 0))
    temiz.execute(
        "UPDATE ariza_kayitlari SET bitis = %s WHERE id = %s",
        [T0 + timedelta(seconds=100), s.ariza_id],
    )
    # Alarm sürüyor görünse de arıza kapalı: yeni başlangıç yeni arıza açar (eski satır temizlenir).
    yeni = isle(temiz, kaynak, olay("b", "A101", 600))
    assert yeni.sonuc == "ariza_acildi" and yeni.ariza_id != s.ariza_id
    assert aktifler(temiz) == {("P3-HP", "A101")}
    # Kapalı arızanın bitişine dokunulmaz.
    assert ariza(temiz, s.ariza_id)["bitis"] == T0 + timedelta(seconds=100)


# --- Sıra ve zaman -----------------------------------------------------------------------------


def test_bitis_baslangictan_once_gelirse_eslesir(temiz, kaynak):
    erken = isle(temiz, kaynak, olay("s", "A101", 300))
    assert erken.sonuc == "aktif_alarm_yok"
    gec = isle(temiz, kaynak, olay("b", "A101", 0))
    assert gec.sonuc == "ariza_acildi"
    a = ariza(temiz, gec.ariza_id)
    assert (a["baslangic"], a["bitis"]) == (T0, T0 + timedelta(seconds=300))
    assert aktifler(temiz) == set()
    sonuc = temiz.execute(
        "SELECT sonuc, ariza_id FROM entegrasyon_olaylari WHERE olay_id = %s", [erken.olay_id]
    ).fetchone()
    assert sonuc == ("gec_eslesti", gec.ariza_id)


def test_onceki_bitis_sonraki_baslangicla_eslesmez(temiz, kaynak):
    isle(temiz, kaynak, olay("s", "A101", 0))
    sonra = isle(temiz, kaynak, olay("b", "A101", 60))
    assert ariza(temiz, sonra.ariza_id)["bitis"] is None  # eski bitiş bu alarmın değil


def test_bitis_saati_baslangictan_onceyse_bir_saniye_sonra_kapanir(temiz, kaynak):
    s = isle(temiz, kaynak, olay("b", "A101", 100))
    isle(temiz, kaynak, olay("s", "A101", 40))  # makine saatleri tutarsız
    a = ariza(temiz, s.ariza_id)
    assert a["bitis"] == a["baslangic"] + timedelta(seconds=1)


def test_gelecek_zaman_simdiye_cekilir_eski_olay_reddedilir(temiz, kaynak):
    ileri = olay("b", "A101", 0)
    ileri = e.Olay(**{**ileri.__dict__, "zaman": SIMDI + timedelta(days=3)})
    s = isle(temiz, kaynak, ileri)
    assert ariza(temiz, s.ariza_id)["baslangic"] == SIMDI
    eski = e.Olay(**{**olay("b", "A102").__dict__, "zaman": SIMDI - timedelta(days=31)})
    r = isle(temiz, kaynak, eski)
    assert r.durum == "hata" and "30 günden eski" in r.hata
    assert temiz.execute(
        "SELECT count(*) FROM entegrasyon_olaylari WHERE olay_id = %s", [eski.olay_id]
    ).fetchone() == (0,)


def test_bilinmeyen_makine_kaydedilmez(temiz, kaynak):
    s = isle(temiz, kaynak, olay("b", makine="X9-YOK"))
    assert s.durum == "hata" and "X9-YOK" in s.hata
    assert temiz.execute("SELECT count(*) FROM entegrasyon_olaylari").fetchone() == (0,)
    # Makine kodu büyük/küçük harf ve boşluk fark etmez.
    assert isle(temiz, kaynak, olay("b", makine=" p3-hp ")).sonuc == "ariza_acildi"


# --- Katalog ve eşleme -------------------------------------------------------------------------


def test_katalog_kilavuzla_ayni():
    """Hazır katalogdaki her kod kılavuzun alarm listesinde (s. 2) anlamıyla geçiyor; sayfası,
    kodun bölümünün olduğu sayfa (bölümü olmayan A202 için liste sayfası)."""
    sayfalar = (KOK / "data/kilavuzlar/kaynak/PRES-BK-01.md").read_text(encoding="utf-8")
    sayfalar = sayfalar.split("<!-- sayfa -->")
    for desen, anlam, _, _, _, sayfa in e.PRES_PANELI:
        assert f"- {desen} {anlam}" in sayfalar[1], desen
        if sayfa != 2:
            assert f"({desen})" in sayfalar[sayfa - 1], (desen, sayfa)


def test_eslesme_sirasi(temiz):
    temiz.cursor().executemany(
        "INSERT INTO alarm_katalogu (katalog, desen, anlam, ariza_tipi, onem, hat_durdurur) "
        "VALUES ('test-k', %s, %s, %s, 'orta', true)",
        [("SV*", "servo", "elektrik"), ("SV04*", "servo hazır değil", "elektrik"),
         ("SV0401", "V-READY", "elektrik"), ("SP_1*", "iş mili", "mekanik")],
    )  # fmt: skip
    bul = lambda kod: (e.eslesme_bul(temiz, "test-k", kod) or {}).get("anlam")  # noqa: E731
    assert bul("SV0401") == "V-READY"  # tam eşleşme önce
    assert bul("SV0402") == "servo hazır değil"  # uzun önek
    assert bul("SV9") == "servo"
    assert bul("SP_19") == "iş mili"
    assert bul("SPX19") is None  # "_" joker değil
    assert bul("S") is None and bul("") is None
    assert e.eslesme_bul(temiz, None, "SV0401") is None


def test_olaydaki_alanlar_katalogu_ve_varsayilani_ezer(temiz):
    k, _ = kaynak_olustur(temiz, "test-cnc", katalog=None, varsayilan_tip="elektrik",
                          varsayilan_onem="dusuk", varsayilan_hat_durdurur=False)  # fmt: skip
    s = isle(temiz, k, olay("b", "EX1234", 0, mesaj="Spindle overload"))
    a = ariza(temiz, s.ariza_id)
    assert s.eslesme == "varsayilan"
    assert (a["ariza_tipi"], a["onem"], a["hat_durdu"]) == ("elektrik", "dusuk", False)
    assert a["aciklama"] == "EX1234 Spindle overload [eşlenmemiş alarm kodu]"
    s2 = isle(temiz, k, olay("b", "EX9", 0, makine="P1-HP", ariza_tipi="mekanik", onem="yuksek",
                             hat_durdurur=True))  # fmt: skip
    a2 = ariza(temiz, s2.ariza_id)
    assert (a2["ariza_tipi"], a2["onem"], a2["hat_durdu"]) == ("mekanik", "yuksek", True)
    # Katalogdaki kod da olayın verdiği alanla ezilir.
    p, _ = kaynak_olustur(temiz, "test-pres2")
    s3 = isle(temiz, p, olay("b", "A101", 0, makine="P2-HP", onem="dusuk"))
    assert ariza(temiz, s3.ariza_id)["onem"] == "dusuk"


def test_aciklama_uzunlugu_sinirli(temiz, kaynak):
    s = isle(temiz, kaynak, olay("b", "B1", 0, mesaj="x" * 500))
    assert len(ariza(temiz, s.ariza_id)["aciklama"]) <= e.ACIKLAMA_SINIRI
    for i in range(40):  # her eklenen alarm açıklamayı uzatır ama sınırı aşmaz
        isle(temiz, kaynak, olay("b", f"C{i:03d}", i + 1, mesaj="y" * 50))
    assert len(ariza(temiz, s.ariza_id)["aciklama"]) <= e.ACIKLAMA_SINIRI


# --- Anahtar ve kaynak -------------------------------------------------------------------------


def test_anahtar(temiz):
    k, anahtar = kaynak_olustur(temiz)
    assert anahtar.startswith("ha_") and len(anahtar) >= 40
    assert e.kaynak_bul(temiz, anahtar) == k
    saklanan = temiz.execute(
        "SELECT anahtar_ozeti FROM entegrasyon_kaynaklari WHERE ad = 'test-pres'"
    ).fetchone()[0]
    assert anahtar not in saklanan and saklanan == e.anahtar_ozeti(anahtar)
    for yanlis in (None, "", anahtar[:-1], anahtar + "x", "ha_" + "a" * 300, anahtar.upper()):
        assert e.kaynak_bul(temiz, yanlis) is None
    yeni = e.anahtar_yenile(temiz, "test-pres")
    assert e.kaynak_bul(temiz, anahtar) is None and e.kaynak_bul(temiz, yeni) == k
    temiz.execute("UPDATE entegrasyon_kaynaklari SET aktif = false WHERE ad = 'test-pres'")
    assert e.kaynak_bul(temiz, yeni) is None
    assert e.anahtar_yenile(temiz, "test-yok") is None


def test_demo_kaynagi(temiz):
    anahtar = "ha_" + "d" * 40
    try:
        e.demo_kaynagi_hazirla(temiz, anahtar)
        k = e.kaynak_bul(temiz, anahtar)
        assert (k.ad, k.katalog) == ("simulator", "pres-paneli")
        e.demo_kaynagi_hazirla(temiz, anahtar[:-1] + "e")  # anahtar değişti
        assert e.kaynak_bul(temiz, anahtar) is None
        with pytest.raises(ValueError):
            e.demo_kaynagi_hazirla(temiz, "kisa")
    finally:
        temiz.execute("DELETE FROM entegrasyon_kaynaklari WHERE ad = 'simulator'")


def test_hiz_siniri():
    saat = [0.0]
    h = e.HizSiniri(hiz=10, kova=20, saat=lambda: saat[0])
    assert h.al("a", 20) == 0 and h.al("a", 1) == pytest.approx(0.1)
    assert h.al("b", 5) == 0  # kaynaklar birbirini etkilemez
    saat[0] = 1.0
    assert h.al("a", 10) == 0 and h.al("a", 1) > 0
    saat[0] = 100.0
    assert h.al("a", 20) == 0 and h.al("a", 21 - 20) > 0  # kova taşmaz
    assert h.al("c", 21) == pytest.approx(0.1)  # kovadan büyük istek: bekleme süresi


# --- Tetikleyici ve bot ------------------------------------------------------------------------


def test_acilan_ariza_botu_aninda_uyandirir(temiz, kaynak, test_veritabani):
    with psycopg.connect(test_veritabani, autocommit=True) as dinleyici:
        dinleyici.execute(sql.SQL("LISTEN {}").format(sql.Identifier("ariza_kayitlari")))
        isle(temiz, kaynak, olay("b", "A101", 0))
        assert sum(1 for _ in dinleyici.notifies(timeout=1)) == 1


def test_bot_entegrasyon_arizasini_bildirir(temiz, kaynak):
    class Telegram:
        giden: list = []

        def mesaj_gonder(self, chat_id, metin):
            self.giden.append((chat_id, metin))

    temiz.execute("DELETE FROM telegram_baglantilari; DELETE FROM telegram_kodlari")
    api = Telegram()
    bot = tb.Bot(api, temiz, SahteEmbedder(), lambda: None, simdi=lambda: SIMDI)
    temiz.execute(
        "INSERT INTO telegram_baglantilari (kullanici_adi, chat_id) VALUES ('bakim', 4242)"
    )
    try:
        assert bot.bildirimleri_gonder() == 0  # başlangıç noktası
        isle(temiz, kaynak, olay("b", "A104", 0))  # hattı durdurmaz: bildirim yok, izlenir
        assert bot.bildirimleri_gonder() == 0
        isle(temiz, kaynak, olay("b", "A101", 10))  # eklenen alarm hattı durdurdu
        assert bot.bildirimleri_gonder() == 1
        metin = api.giden[-1][1]
        assert "Pres 3 durdu" in metin and "A104 Filtre tıkanık" in metin
        isle(temiz, kaynak, olay("b", "A302", 20))  # aynı duruşa üçüncü alarm: yeni bildirim yok
        assert bot.bildirimleri_gonder() == 0
    finally:
        temiz.execute("DELETE FROM telegram_baglantilari; DELETE FROM telegram_kodlari")


# --- API ------------------------------------------------------------------------------------------


@pytest.fixture
def api_kaynagi(istemci, temiz):
    _, anahtar = kaynak_olustur(temiz, "test-api")
    return {"X-API-Anahtari": anahtar}


def _govde(*olaylar):
    return {"olaylar": list(olaylar)}


def _o(tur="alarm_basladi", kod="A101", makine="P3-HP", olay_id=None, **alan):
    return {
        "olay_id": olay_id or f"api-{next(_sayac)}",
        "tur": tur,
        "makine_kodu": makine,
        "alarm_kodu": kod,
        "zaman": datetime.now(TZ).isoformat(),
        **alan,
    }


def test_api_karisik_toplu_istek(istemci, api_kaynagi, temiz):
    cevap = istemci.post(
        "/entegrasyon/olaylar",
        json=_govde(
            _o(kod="A101", olay_id="b1"),
            _o(makine="BOŞLUK VAR"),  # desen dışı
            _o(makine="X9-YOK"),  # bilinmeyen makine
            "olay değil",
            {"olay_id": "eksik"},
            _o(tur="alarm_bitti", kod="A101"),
            _o(kod="A101", olay_id="b1"),  # tekrar
        ),
        headers=api_kaynagi,
    )
    assert cevap.status_code == 200, cevap.text
    govde = cevap.json()
    assert govde["kaynak"] == "test-api"
    durumlar = [(s["sira"], s["durum"], s["sonuc"]) for s in govde["sonuclar"]]
    assert durumlar == [
        (0, "islendi", "ariza_acildi"),
        (1, "gecersiz", None),
        (2, "hata", None),
        (3, "gecersiz", None),
        (4, "gecersiz", None),
        (5, "islendi", "ariza_kapandi"),
        (6, "tekrar", "ariza_acildi"),
    ]
    assert govde["sonuclar"][4]["olay_id"] == "eksik"
    assert "makine_kodu" in govde["sonuclar"][1]["hata"]
    son_olay = temiz.execute(
        "SELECT son_olay FROM entegrasyon_kaynaklari WHERE ad = 'test-api'"
    ).fetchone()[0]
    assert son_olay is not None


def test_api_saat_dilimsiz_zaman_turkiye_saati(istemci, api_kaynagi, temiz):
    yerel = datetime.now(TZ).replace(tzinfo=None, microsecond=0) - timedelta(minutes=5)
    s = istemci.post(
        "/entegrasyon/olaylar",
        json=_govde(_o(zaman=yerel.isoformat())),
        headers=api_kaynagi,
    ).json()["sonuclar"][0]
    assert ariza(temiz, s["ariza_id"])["baslangic"] == yerel.replace(tzinfo=TZ)


@pytest.mark.parametrize(
    "govde",
    [{}, {"olaylar": []}, {"olaylar": [_o()] * 501}, {"olaylar": [_o()], "x": 1}, [_o()], "x"],
    ids=["bos", "bos-liste", "501-olay", "fazla-alan", "liste", "metin"],
)
def test_api_govde_dogrulama(istemci, api_kaynagi, govde):
    assert istemci.post("/entegrasyon/olaylar", json=govde, headers=api_kaynagi).status_code == 422


@pytest.mark.parametrize(
    "baslik",
    [
        {},
        {"X-API-Anahtari": ""},
        {"X-API-Anahtari": "ha_yanlis-anahtar-yanlis-anahtar-yanlis"},
        {"X-API-Anahtari": "yanlis"},
        {"Authorization": "Bearer x"},
    ],
    ids=["yok", "bos", "yanlis", "oneksiz", "bearer"],
)
def test_api_anahtarsiz_istek_reddedilir(istemci, baslik):
    cevap = istemci.post(
        "/entegrasyon/olaylar", json=_govde(_o()), headers={"Authorization": "", **baslik}
    )
    assert cevap.status_code == 401
    assert cevap.headers["www-authenticate"].startswith("ApiKey")


def test_api_kullanici_tokeni_ve_kapali_kaynak(istemci, api_kaynagi, temiz):
    # Kullanıcı (JWT) olay gönderemez; kaynak anahtarı kullanıcı uç noktalarını açmaz.
    assert (
        istemci.post("/entegrasyon/olaylar", json=_govde(_o()), headers=yetki(BAKIM)).status_code
        == 401
    )
    assert istemci.get("/hatlar", headers={"Authorization": "", **api_kaynagi}).status_code == 401
    temiz.execute("UPDATE entegrasyon_kaynaklari SET aktif = false WHERE ad = 'test-api'")
    assert (
        istemci.post("/entegrasyon/olaylar", json=_govde(_o()), headers=api_kaynagi).status_code
        == 401
    )


def test_api_hiz_siniri(istemci, api_kaynagi):
    eski = istemci.app.state.hiz_siniri
    istemci.app.state.hiz_siniri = e.HizSiniri(hiz=1, kova=2)
    try:
        assert (
            istemci.post(
                "/entegrasyon/olaylar", json=_govde(_o(), _o()), headers=api_kaynagi
            ).status_code
            == 200
        )
        cevap = istemci.post("/entegrasyon/olaylar", json=_govde(_o()), headers=api_kaynagi)
        assert cevap.status_code == 429 and int(cevap.headers["retry-after"]) >= 1
    finally:
        istemci.app.state.hiz_siniri = eski


def test_api_buyuk_govde_uygulamaya_ulasmaz(istemci, api_kaynagi):
    govde = json.dumps(_govde(_o(mesaj="x" * 400))).encode() + b" " * (1024 * 1024)
    cevap = istemci.post(
        "/entegrasyon/olaylar",
        content=govde,
        headers={**api_kaynagi, "Content-Type": "application/json"},
    )
    assert cevap.status_code == 413


def test_govde_siniri_content_length_olmadan_da_sayar():
    asyncio.run(_govde_siniri_senaryosu())


async def _govde_siniri_senaryosu():
    from app.guvenlik_basliklari import GovdeSiniri

    cagrildi = []

    async def uygulama(scope, receive, send):
        cagrildi.append(await receive())

    gonderilen = []
    parcalar = [{"type": "http.request", "body": b"x" * 600, "more_body": True}] * 3

    async def receive():
        return parcalar.pop(0)

    async def send(mesaj):
        gonderilen.append(mesaj)

    ara = GovdeSiniri(uygulama, sinir=1000)
    await ara({"type": "http", "path": "/entegrasyon/olaylar", "headers": []}, receive, send)
    assert not cagrildi and gonderilen[0]["status"] == 413
    # Sınırın altındaki parçalı gövde birleşip uygulamaya tek parça gider.
    parcalar[:] = [
        {"type": "http.request", "body": b"ab", "more_body": True},
        {"type": "http.request", "body": b"cd", "more_body": False},
    ]
    await ara({"type": "http", "path": "/entegrasyon/olaylar", "headers": []}, receive, send)
    assert cagrildi == [{"type": "http.request", "body": b"abcd", "more_body": False}]
    # Başka yollara dokunulmaz.
    cagrildi.clear()
    parcalar[:] = [{"type": "http.request", "body": b"z" * 5000, "more_body": False}]
    await ara({"type": "http", "path": "/chat", "headers": []}, receive, send)
    assert cagrildi[0]["body"] == b"z" * 5000


def test_api_aktif_alarmlar_yalnizca_kendi_kaynagi(istemci, api_kaynagi, temiz):
    diger, _ = kaynak_olustur(temiz, "test-baska")
    isle(temiz, diger, olay("b", "A101", 0))
    istemci.post("/entegrasyon/olaylar", json=_govde(_o(kod="A102")), headers=api_kaynagi)
    cevap = istemci.get("/entegrasyon/aktif-alarmlar", headers={"Authorization": "", **api_kaynagi})
    assert cevap.status_code == 200
    assert [(a["kaynak"], a["alarm_kodu"]) for a in cevap.json()] == [("test-api", "A102")]


def test_api_durum_bakima_acik_anahtar_gostermez(istemci, api_kaynagi, temiz):
    istemci.post("/entegrasyon/olaylar", json=_govde(_o()), headers=api_kaynagi)
    cevap = istemci.get("/entegrasyon/durum")
    assert cevap.status_code == 200
    govde = cevap.json()
    assert any(k["ad"] == "test-api" and k["aktif_alarm"] == 1 for k in govde["kaynaklar"])
    assert govde["son_olaylar"][0]["kaynak"] == "test-api"
    metin = cevap.text
    assert api_kaynagi["X-API-Anahtari"] not in metin and "anahtar" not in metin
    assert istemci.get("/entegrasyon/durum", headers=yetki(OPERATOR)).status_code == 403


# --- Komut satırı (scripts/entegrasyon.py) ------------------------------------------------------


def test_komut_satiri_kaynak_yonetimi(temiz):
    from scripts.entegrasyon import calistir

    cikti = calistir(["kaynak-ekle", "test-cli", "CLI testi", "--katalog", "pres-paneli",
                      "--varsayilan-tip", "elektrik", "--hat-durdurmaz"], temiz)  # fmt: skip
    anahtar = cikti.splitlines()[-1]
    k = e.kaynak_bul(temiz, anahtar)
    assert (k.ad, k.katalog, k.varsayilan_tip, k.varsayilan_hat_durdurur) == (
        "test-cli", "pres-paneli", "elektrik", False,
    )  # fmt: skip
    with pytest.raises(SystemExit, match="zaten var"):
        calistir(["kaynak-ekle", "test-cli", "x"], temiz)
    with pytest.raises(SystemExit, match="küçük harf"):
        calistir(["kaynak-ekle", "Test Büyük", "x"], temiz)
    assert "test-cli" in calistir(["kaynaklar"], temiz)
    assert anahtar not in calistir(["kaynaklar"], temiz)

    yeni = calistir(["anahtar-yenile", "test-cli"], temiz).splitlines()[-1]
    assert e.kaynak_bul(temiz, anahtar) is None and e.kaynak_bul(temiz, yeni).ad == "test-cli"
    assert "kapatıldı" in calistir(["kapat", "test-cli"], temiz)
    assert e.kaynak_bul(temiz, yeni) is None
    assert "KAPALI" in calistir(["kaynaklar"], temiz)
    calistir(["ac", "test-cli"], temiz)
    assert e.kaynak_bul(temiz, yeni) is not None
    for komut in (["anahtar-yenile", "test-yok"], ["kapat", "test-yok"]):
        with pytest.raises(SystemExit, match="kaynak yok"):
            calistir(komut, temiz)


def test_komut_satiri_katalog(temiz):
    from scripts.entegrasyon import calistir

    calistir(["katalog-ekle", "test-fanuc", "SV*", "Servo alarmı", "elektrik", "yuksek"], temiz)
    calistir(["katalog-ekle", "test-fanuc", "SV*", "Servo", "elektrik", "orta",
              "--hat-durdurmaz"], temiz)  # fmt: skip  # aynı desen: güncellenir
    assert e.eslesme_bul(temiz, "test-fanuc", "SV0401") == {
        "desen": "SV*", "anlam": "Servo", "ariza_tipi": "elektrik", "onem": "orta",
        "hat_durdurur": False, "dokuman_kodu": None, "sayfa": None,
    }  # fmt: skip
    assert "SV*" in calistir(["katalog", "test-fanuc"], temiz)
    assert "A101" in calistir(["katalog"], temiz)  # hazır katalog
    with pytest.raises(SystemExit, match="birlikte"):
        calistir(["katalog-ekle", "test-fanuc", "X1", "x", "mekanik", "orta", "--sayfa", "3"],
                 temiz)  # fmt: skip
    for desen in ("S V", "SV**", "*SV", "x" * 65):
        with pytest.raises(SystemExit, match="Desen"):
            calistir(["katalog-ekle", "test-fanuc", desen, "x", "mekanik", "orta"], temiz)
    with pytest.raises(SystemExit):  # argparse: geçersiz tip
        calistir(["katalog-ekle", "test-fanuc", "X1", "x", "nukleer", "orta"], temiz)


# --- Arayüz (Node) -----------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="Node kurulu değil")
def test_arayuz_makine_ekrani_metinleri():
    modul = (KOK / "app" / "static" / "makine.js").as_uri()
    simdi = "Date.parse('2026-10-15T12:00:00Z')"
    zamanlar = ["11:59:50", "11:55:00", "09:00:00", "-13T12:00:00", "12:00:30"]
    cagrilar = ", ".join(
        f"m.onceMetni('2026-10{z}Z', {simdi})" if z.startswith("-") else
        f"m.onceMetni('2026-10-15T{z}Z', {simdi})"
        for z in zamanlar
    )  # fmt: skip
    betik = (
        f"import * as m from {json.dumps(modul)};"
        f"process.stdout.write(JSON.stringify([{cagrilar}, Object.keys(m.SONUC_ADLARI)]));"
    )
    cikti = subprocess.run(
        ["node", "--input-type=module", "-e", betik],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=True,
    ).stdout  # fmt: skip
    sure1, sure2, sure3, sure4, ileri, sonuclar = json.loads(cikti)
    assert (sure1, sure2, sure3, sure4) == ("10 sn önce", "5 dk önce", "3 sa önce", "2 gün önce")
    assert ileri == "0 sn önce"  # saat farkı: gelecek zaman eksi olmaz
    # Arayüz, API'nin üretebileceği her sonucu Türkçe gösterir.
    kaynak = (KOK / "app" / "entegrasyon.py").read_text(encoding="utf-8")
    uretilen = set(
        re.findall(
            r'"(ariza_[a-z_]+|alarm_kapandi|zaten_aktif|aktif_alarm_yok|gec_eslesti)"', kaynak
        )
    )
    uretilen -= {"ariza_tipi", "ariza_id", "ariza_kayitlari"}
    assert uretilen <= set(sonuclar), uretilen - set(sonuclar)
