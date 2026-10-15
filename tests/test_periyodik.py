"""Periyodik bakım takibi: seed'deki planlar ve kılavuz kaynakları, son/sıradaki bakım ve durum,
"Yapıldı" kaydı, API, agent aracı ve Telegram (/bakimlar, sabah özeti)."""

import json
import shutil
import subprocess
from datetime import date, datetime, time, timedelta
from pathlib import Path

import psycopg
import pytest
from hypothesis import given
from hypothesis import strategies as st

from app import periyodik
from app import telegram_bot as tb
from app.db import TZ
from app.tools import AracBaglami, arac_calistir
from scripts.seed import GENEL_AYLIK_BAKIM, PERIYODIK_PLANLAR, PERIYODIK_SENARYO
from tests.conftest import BAKIM, OPERATOR, SABIT_AN, SahteEmbedder, yetki
from tests.test_telegram import BAKIM_SOHBETI, OPERATOR_SOHBETI, guncelleme

KOK = Path(__file__).resolve().parent.parent
BUGUN = SABIT_AN.date()


@pytest.fixture
def conn(test_veritabani):
    with psycopg.connect(test_veritabani, autocommit=True) as baglanti:
        yield baglanti


@pytest.fixture
def temiz(conn):
    """Testin açtığı iş emirleri ve özet işaretleri sonda silinir (diğer testler seed'le
    karşılaştırıyor)."""
    son_id = conn.execute("SELECT max(id) FROM is_emirleri").fetchone()[0]
    yield conn
    conn.execute("DELETE FROM is_emirleri WHERE id > %s", [son_id])
    conn.execute("DELETE FROM telegram_ozetleri")


def _bul(bakimlar, makine, bakim):
    return next(b for b in bakimlar if b["makine_kodu"] == makine and b["bakim"] == bakim)


# --- Seed: planlar ve kılavuz kaynakları ----------------------------------------------------


def _kilavuz_sayfalari(kod: str) -> list[str]:
    metin = (KOK / "data" / "kilavuzlar" / "kaynak" / f"{kod}.md").read_text(encoding="utf-8")
    return metin.split("<!-- sayfa -->")


@pytest.mark.parametrize(
    "plan",
    [p for planlar in PERIYODIK_PLANLAR.values() for p in planlar],
    ids=lambda p: f"{p[4][0]}-{p[0]}",
)
def test_plan_kaynagi_kilavuzdaki_sayfayi_gosteriyor(plan):
    ad, aralik, _, yapilacaklar, (kod, sayfa, bolum) = plan
    sayfa_metni = _kilavuz_sayfalari(kod)[sayfa - 1]
    numara = bolum.split()[0]
    assert f"### {numara}" in sayfa_metni, f"{kod} s. {sayfa}'da {bolum} yok"
    # Aralık bölümün adından gelir: aylık 30, 6 aylık 182, yıllık 365 gün.
    assert {"Aylık bakım": 30, "6 aylık bakım": 182, "Yıllık bakım": 365}[ad] == aralik
    # Yapılacaklardaki stok kodları kılavuzun o sayfasında geçiyor (uydurma kod yok).
    for kelime in yapilacaklar.replace("(", " ").replace(")", " ").split():
        if kelime[:3] in ("HYD", "MEK") and "-" in kelime:
            assert kelime in sayfa_metni


def test_her_makinenin_aylik_plani_var_kilavuzu_olanlarin_uzun_araliklari_da(test_verisi):
    planlar = test_verisi["periyodik_bakimlar"]
    makineler = {m["id"]: m["kod"] for m in test_verisi["makineler"]}
    aylik = {makineler[p["makine_id"]] for p in planlar if p["ad"] == "Aylık bakım"}
    assert aylik == set(makineler.values())
    yillik = sorted(makineler[p["makine_id"]] for p in planlar if p["ad"] == "Yıllık bakım")
    assert yillik == ["K1-KR1", "K1-KR2", "K2-KR1", "K2-KR2", "P1-HP", "P2-HP", "P3-HP"]
    genel = [p for p in planlar if p["dokuman_kodu"] is None]
    assert all(p["yapilacaklar"] == GENEL_AYLIK_BAKIM[3] for p in genel)


def test_seed_aylik_bakim_is_emirlerini_plana_baglar(test_verisi):
    planlar = test_verisi["periyodik_bakimlar"]
    aylik = {p["makine_id"]: p["id"] for p in planlar if p["aralik_gun"] == 30}
    for emir in test_verisi["is_emirleri"]:
        if emir["tip"] == "periyodik":
            assert emir["plan_id"] == aylik[emir["makine_id"]]
        else:
            assert emir["plan_id"] is None


# --- Durum hesabı --------------------------------------------------------------------------


@given(st.integers(-400, 400))
def test_durum_sinirlari(kalan):
    hesap, durum = periyodik.durum_hesapla(BUGUN + timedelta(days=kalan), BUGUN)
    assert hesap == kalan
    if kalan < 0:
        assert durum == "gecikti"
    elif kalan <= periyodik.YAKLASMA_GUN:
        assert durum == "yaklasiyor"
    else:
        assert durum == "zamaninda"


@pytest.mark.parametrize(
    ("kalan", "metin"),
    [
        (-12, "12 gün gecikti"),
        (-1, "1 gün gecikti"),
        (0, "bugün"),
        (1, "yarın"),
        (9, "9 gün sonra"),
    ],
)
def test_kalan_metni(kalan, metin):
    assert periyodik.kalan_metni(kalan) == metin


def test_senaryo_ve_siralama(conn):
    bakimlar = periyodik.bakimlari_getir(conn, BUGUN)
    assert len(bakimlar) == 21 + 7 * 2  # her makineye aylık; 3 pres + 4 robota 6 aylık ve yıllık
    for (makine, ad), kalan in PERIYODIK_SENARYO.items():
        b = _bul(bakimlar, makine, ad)
        assert b["kalan_gun"] == kalan and not b["kayitli"]  # sistem öncesi baz tarihten
    p3 = _bul(bakimlar, "P3-HP", "Yıllık bakım")
    assert p3["durum"] == "gecikti" and p3["siradaki"] == BUGUN - timedelta(days=12)
    assert (p3["dokuman_kodu"], p3["sayfa"]) == ("PRES-BK-01", 3)
    assert _bul(bakimlar, "P1-HP", "6 aylık bakım")["durum"] == "yaklasiyor"
    assert [b["kalan_gun"] for b in bakimlar] == sorted(b["kalan_gun"] for b in bakimlar)
    assert bakimlar[0] is p3  # en çok geciken önce
    assert periyodik.ozet(bakimlar)["gecikmis"] == 1  # seed'den hemen sonra yalnızca senaryo


def test_aylik_son_bakim_seedin_son_is_emri(conn, test_verisi):
    makine = next(m for m in test_verisi["makineler"] if m["kod"] == "K2-KR1")
    son = max(
        e["kapanis"]
        for e in test_verisi["is_emirleri"]
        if e["makine_id"] == makine["id"] and e["tip"] == "periyodik" and e["kapanis"]
    )
    b = _bul(periyodik.bakimlari_getir(conn, BUGUN), "K2-KR1", "Aylık bakım")
    assert b["kayitli"] and b["son_bakim"] == son.astimezone(TZ).date()
    assert b["siradaki"] == b["son_bakim"] + timedelta(days=30)
    assert b["son_yapan"]  # seed'deki teknisyen


def test_filtreler(conn):
    assert {b["makine_kodu"] for b in periyodik.bakimlari_getir(conn, BUGUN, hat="Pres 3")} == {
        "P3-HP",
        "P3-RB",
        "P3-TR",
    }
    tek = periyodik.bakimlari_getir(conn, BUGUN, makine_kodu=" p3-hp ")
    assert {b["bakim"] for b in tek} == {"Aylık bakım", "6 aylık bakım", "Yıllık bakım"}
    assert periyodik.bakimlari_getir(conn, BUGUN, makine_kodu="YOK-01") == []


# --- "Yapıldı" kaydı -----------------------------------------------------------------------


def test_yapildi_is_emri_acar_ve_tarihi_kaydirir(temiz):
    plan_id = _bul(periyodik.bakimlari_getir(temiz, BUGUN), "P3-HP", "Yıllık bakım")["plan_id"]
    sonuc = periyodik.yapildi_kaydet(
        temiz, plan_id, yapan="Demo Bakım Mühendisi", simdi=SABIT_AN, aciklama="Conta seti değişti"
    )
    assert sonuc["durum"] == "zamaninda" and sonuc["kayitli"]
    assert sonuc["son_bakim"] == BUGUN and sonuc["siradaki"] == BUGUN + timedelta(days=365)
    assert sonuc["son_yapan"] == "Demo Bakım Mühendisi"
    emir = temiz.execute(
        "SELECT tip, durum, aciklama, acilis, kapanis FROM is_emirleri WHERE plan_id = %s "
        "ORDER BY id DESC LIMIT 1",
        [plan_id],
    ).fetchone()
    assert emir[:3] == ("periyodik", "kapali", "Yıllık bakım: Conta seti değişti")
    assert emir[4] - emir[3] == timedelta(hours=6)  # planın tahmini süresi
    # Artık gecikmiş bakım yok; bakım planı da P3-HP'yi zorunlu saymaz.
    assert periyodik.gecikmis_bakimlar(temiz, BUGUN) == {}


def test_gecmis_gune_yazilan_bakim(temiz):
    plan_id = _bul(periyodik.bakimlari_getir(temiz, BUGUN), "P3-HP", "Yıllık bakım")["plan_id"]
    dun = BUGUN - timedelta(days=1)
    sonuc = periyodik.yapildi_kaydet(
        temiz, plan_id, yapan="x", simdi=SABIT_AN, tarih=dun, sure_saat=2.5
    )
    assert sonuc["son_bakim"] == dun and sonuc["kalan_gun"] == 364
    acilis, kapanis = temiz.execute(
        "SELECT acilis, kapanis FROM is_emirleri WHERE plan_id = %s ORDER BY id DESC LIMIT 1",
        [plan_id],
    ).fetchone()
    assert kapanis == datetime.combine(dun, periyodik.GECMIS_KAPANIS_SAATI, TZ)
    assert kapanis - acilis == timedelta(hours=2.5)


def test_eski_tarihli_kayit_son_bakimi_geri_almaz(temiz):
    b = _bul(periyodik.bakimlari_getir(temiz, BUGUN), "K2-KR1", "Aylık bakım")
    eski = b["son_bakim"] - timedelta(days=40)
    sonuc = periyodik.yapildi_kaydet(temiz, b["plan_id"], yapan="x", simdi=SABIT_AN, tarih=eski)
    assert sonuc["son_bakim"] == b["son_bakim"]  # en son kapanan iş emri esas


def test_ileri_tarih_ve_olmayan_plan(temiz):
    with pytest.raises(periyodik.BakimKaydiHatasi, match="ileri"):
        periyodik.yapildi_kaydet(
            temiz, 1, yapan="x", simdi=SABIT_AN, tarih=BUGUN + timedelta(days=1)
        )
    assert periyodik.yapildi_kaydet(temiz, 10**6, yapan="x", simdi=SABIT_AN) is None


# --- API ------------------------------------------------------------------------------------


def test_api_listesi(istemci):
    cevap = istemci.get("/periyodik-bakim")
    assert cevap.status_code == 200
    govde = cevap.json()
    assert govde["yaklasma_gun"] == periyodik.YAKLASMA_GUN
    assert len(govde["bakimlar"]) == 35
    durumlar = [b["durum"] for b in govde["bakimlar"]]
    assert govde["gecikmis"] == durumlar.count("gecikti")
    assert govde["yaklasan"] == durumlar.count("yaklasiyor")
    kalanlar = [b["kalan_gun"] for b in govde["bakimlar"]]
    assert kalanlar == sorted(kalanlar)
    hat = istemci.get("/periyodik-bakim", params={"hat": "pres 3"}).json()["bakimlar"]
    assert {b["hat"] for b in hat} == {"Pres 3"}
    assert istemci.get("/periyodik-bakim", params={"hat": "Pres 9"}).status_code == 404
    assert istemci.get("/periyodik-bakim", params={"x": "1"}).status_code == 422
    assert istemci.get("/periyodik-bakim", headers=yetki(OPERATOR)).status_code == 403


def test_api_yapildi(istemci, temiz):
    plan_id = _bul(periyodik.bakimlari_getir(temiz, BUGUN), "P2-HP", "Yıllık bakım")["plan_id"]
    yol = f"/periyodik-bakim/{plan_id}/yapildi"
    bugun = datetime.now(TZ).date()
    cevap = istemci.post(yol, json={"aciklama": "  Emniyet valfi 230 bar  "})
    assert cevap.status_code == 201
    govde = cevap.json()
    assert govde["son_bakim"] == bugun.isoformat() and govde["durum"] == "zamaninda"
    assert govde["son_yapan"] == BAKIM.ad_soyad
    aciklama = temiz.execute(
        "SELECT aciklama FROM is_emirleri WHERE plan_id = %s ORDER BY id DESC LIMIT 1", [plan_id]
    ).fetchone()[0]
    assert aciklama == "Yıllık bakım: Emniyet valfi 230 bar"

    yarin = (bugun + timedelta(days=1)).isoformat()
    assert istemci.post(yol, json={"tarih": yarin}).status_code == 422
    for gecersiz in ({"sure_saat": 0}, {"sure_saat": 25}, {"aciklama": "a\x00b"}, {"x": 1}):
        assert istemci.post(yol, json=gecersiz).status_code == 422, gecersiz
    assert istemci.post(yol, json={"aciklama": "x" * 501}).status_code == 422
    assert istemci.post("/periyodik-bakim/999999/yapildi", json={}).status_code == 404
    assert istemci.post("/periyodik-bakim/0/yapildi", json={}).status_code == 422
    assert istemci.post(yol, json={}, headers=yetki(OPERATOR)).status_code == 403


# --- Agent aracı ---------------------------------------------------------------------------


def _arac(conn, rol="bakim", **argumanlar):
    kullanici = BAKIM if rol == "bakim" else OPERATOR
    baglam = AracBaglami(conn, SahteEmbedder(), kullanici, simdi=SABIT_AN)
    return arac_calistir("periyodik_bakim_durumu", json.dumps(argumanlar), baglam)


def test_arac_geciken_bakimlar(conn):
    sonuc = _arac(conn, durum="gecikti")["sonuc"]
    assert sonuc["gecikmis"] == 1
    (satir,) = sonuc["bakimlar"]
    assert satir["makine"] == "P3-HP" and satir["bakim"] == "Yıllık bakım"
    assert satir["kalan"] == "12 gün gecikti" and satir["kaynak"] == "PRES-BK-01, s. 3"
    assert satir["not"] == "son bakım sisteme geçmeden önceki kayıttan"
    assert "yapilacaklar" not in satir  # liste sorusunda kısa tutulur


def test_arac_tek_makine_ve_hatalar(conn):
    satirlar = _arac(conn, makine_kodu="p3-hp")["sonuc"]["bakimlar"]
    assert {s["bakim"] for s in satirlar} == {"Aylık bakım", "6 aylık bakım", "Yıllık bakım"}
    assert all("HYD-" in s["yapilacaklar"] or "MEK-" in s["yapilacaklar"] for s in satirlar)
    assert "Geçerli kodlar" in _arac(conn, makine_kodu="X-1")["hata"]
    assert "Geçerli hatlar" in _arac(conn, hat="Pres 9")["hata"]
    assert "yalnızca bakım" in _arac(conn, rol="operator")["hata"]


# --- Telegram -------------------------------------------------------------------------------


class Telegram:
    def __init__(self):
        self.giden: list[tuple[int, str]] = []

    def mesaj_gonder(self, chat_id, metin):
        self.giden.append((chat_id, metin))

    def yaziyor(self, chat_id):
        pass


@pytest.fixture
def bot(temiz):
    temiz.execute("DELETE FROM telegram_baglantilari; DELETE FROM telegram_kodlari")
    api = Telegram()
    saat = {"an": SABIT_AN}
    b = tb.Bot(api, temiz, SahteEmbedder(), lambda: None, simdi=lambda: saat["an"])
    b.saat_ayarla = lambda an: saat.update(an=an)
    for kullanici, chat_id in (("bakim", BAKIM_SOHBETI), ("operator", OPERATOR_SOHBETI)):
        kod = tb.kod_uret(temiz, kullanici, simdi=SABIT_AN)
        b.guncellemeyi_isle(guncelleme(chat_id, f"/baglan {kod}"))
    api.giden.clear()
    yield b
    temiz.execute("DELETE FROM telegram_baglantilari")


def test_bakimlar_komutu(bot):
    bot.guncellemeyi_isle(guncelleme(BAKIM_SOHBETI, "/bakimlar"))
    bot.guncellemeyi_isle(guncelleme(OPERATOR_SOHBETI, "/bakimlar"))
    (bakim_sohbeti, bakim), (_, operator) = bot.api.giden
    assert bakim_sohbeti == BAKIM_SOHBETI
    assert "Geciken (1)" in bakim and "<b>P3-HP</b>" in bakim and "12 gün gecikti" in bakim
    assert "6 aylık bakım, 4 gün sonra" in bakim  # P1-HP, yaklaşan
    assert "yalnızca bakım" in operator
    assert "/bakimlar" in tb.yardim_metni(BAKIM) and "/bakimlar" not in tb.yardim_metni(OPERATOR)


def test_sabah_ozeti_gunde_bir_kez_yalnizca_bakima(bot):
    sabah = datetime.combine(BUGUN, time(7, 30), TZ)
    bot.saat_ayarla(sabah - timedelta(minutes=1))
    assert bot.sabah_ozetini_gonder(time(7, 30)) == 0  # saati gelmedi
    bot.saat_ayarla(sabah)
    assert bot.sabah_ozetini_gonder(time(7, 30)) == 1
    ((chat_id, metin),) = bot.api.giden
    assert chat_id == BAKIM_SOHBETI and "Bugünün periyodik bakımları" in metin
    assert "P3-HP" in metin and "Bu hafta" in metin
    bot.saat_ayarla(sabah + timedelta(hours=3))
    assert bot.sabah_ozetini_gonder(time(7, 30)) == 0  # aynı gün ikinci kez gitmez
    # Ertesi gün akşam açılan bot "günaydın" göndermez.
    bot.saat_ayarla(datetime.combine(BUGUN + timedelta(days=1), time(21), TZ))
    assert bot.sabah_ozetini_gonder(time(7, 30)) == 0


def test_sabah_ozeti_metni():
    bugun = date(2026, 10, 1)

    def b(kalan, kod="P3-HP"):
        _, durum = periyodik.durum_hesapla(bugun + timedelta(days=kalan), bugun)
        return {
            "makine_kodu": kod,
            "makine_adi": "Hidrolik <Pres>",
            "hat": "Pres 3",
            "bakim": "Aylık bakım",
            "kalan_gun": kalan,
            "durum": durum,
            "acik_is_emri": kalan == 0,
        }

    assert tb.sabah_ozeti([b(30)], bugun) is None  # gönderilecek bir şey yok
    yalniz_hafta = tb.sabah_ozeti([b(3)], bugun)
    assert "Bugün vadesi gelen ya da geciken bakım yok" in yalniz_hafta
    assert "Bu hafta 1 bakım daha var" in yalniz_hafta
    metin = tb.sabah_ozeti([b(-2), b(0, "P1-HP"), b(5)], bugun)
    assert "Geciken (1)" in metin and "Bugün (1)" in metin and "Bu hafta 1" in metin
    assert "2 gün gecikti" in metin and "iş emri açık" in metin
    assert "&lt;Pres&gt;" in metin and "<Pres>" not in metin  # HTML kaçışlı
    assert "01.10.2026" in metin
    assert "Geciken ya da önümüzdeki" in tb.periyodik_listesi([b(30)], bugun)


# --- Arayüz (Node) --------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="Node kurulu değil")
def test_arayuz_tarih_ve_kalan_metni():
    modul = (KOK / "app" / "static" / "periyodik.js").as_uri()
    betik = (
        f"import * as m from {json.dumps(modul)};"
        "process.stdout.write(JSON.stringify(["
        "m.tarihMetni('2026-10-01'), m.kalanMetni(-3), m.kalanMetni(0), m.kalanMetni(1),"
        "m.kalanMetni(12), Object.keys(m.DURUMLAR)]));"
    )
    cikti = subprocess.run(
        ["node", "--input-type=module", "-e", betik],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    ).stdout
    assert json.loads(cikti) == [
        "01.10.2026",
        "3 gün gecikti",
        "bugün",
        "yarın",
        "12 gün sonra",
        ["gecikti", "yaklasiyor", "zamaninda"],
    ]
    # Python ve arayüz aynı metni üretir (Telegram ile web aynı dili konuşsun).
    for kalan in (-3, 0, 1, 12):
        assert periyodik.kalan_metni(kalan) in cikti


def test_sabah_ozeti_hatasi_botu_dusurmez_veritabani_kopmasi_yukari_cikar(capsys):
    from scripts import telegram_bot as calistirici

    class Bot:
        def __init__(self, hata):
            self.hata = hata

        def sabah_ozetini_gonder(self, saat):
            raise self.hata

    calistirici.sabah_ozeti_turu(Bot(ValueError("GIZLI-ICERIK")), time(7, 30))
    cikti = capsys.readouterr().out
    assert "periyodik bakım özeti gönderilemedi: ValueError" in cikti
    assert "GIZLI-ICERIK" not in cikti
    with pytest.raises(psycopg.OperationalError):  # ana döngü yeniden bağlansın
        calistirici.sabah_ozeti_turu(Bot(psycopg.OperationalError("x")), time(7, 30))
