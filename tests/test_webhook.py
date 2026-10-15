"""Webhook'lar: tetikleyiciler giden kutusuna doğru bildirimi yazar, gönderici imzalar, sırayı
korur, yeniden dener, vazgeçer; iki gönderici aynı bildirimi iki kez göndermez; uçtan uca
gerçek bir HTTP alıcısına.

Yük ve rastgele dizi sayıları HA_YOGUN=1 ile büyür.
"""

import json
import os
import random
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import psycopg
import pytest
from psycopg import sql

from app import periyodik, webhook
from scripts import webhook_gonderici
from scripts.entegrasyon import calistir as cli
from scripts.seed import veritabanina_yaz
from tests.conftest import TEST_DB_URL

YOGUN = os.environ.get("HA_YOGUN") == "1"
SIMDI = datetime.now(UTC)


@pytest.fixture
def db(test_veritabani):
    conn = psycopg.connect(test_veritabani, autocommit=True)
    son_ariza = conn.execute("SELECT max(id) FROM ariza_kayitlari").fetchone()[0]
    son_is = conn.execute("SELECT max(id) FROM is_emirleri").fetchone()[0]
    conn.execute("DELETE FROM webhooklar")
    yield conn
    conn.execute("DELETE FROM webhooklar")
    conn.execute("DELETE FROM is_emirleri WHERE id > %s", [son_is])
    conn.execute("DELETE FROM ariza_kayitlari WHERE id > %s", [son_ariza])
    conn.close()


def makine_id(conn, kod="P3-HP") -> int:
    return conn.execute("SELECT id FROM makineler WHERE kod = %s", [kod]).fetchone()[0]


def ariza_ekle(conn, kod="P3-HP", kapali=False, hat_durdu=False, aciklama="test arızası") -> int:
    return conn.execute(
        "INSERT INTO ariza_kayitlari (makine_id, baslangic, bitis, ariza_tipi, onem, hat_durdu, "
        "aciklama) VALUES (%s, now() - interval '10 minutes', "
        "CASE WHEN %s THEN now() END, 'hidrolik', 'yuksek', %s, %s) RETURNING id",
        [makine_id(conn, kod), kapali, hat_durdu, aciklama],
    ).fetchone()[0]


def bildirimler(conn, webhook_adi=None) -> list[tuple]:
    return conn.execute(
        "SELECT webhook, olay, icerik, durum, deneme FROM giden_bildirimler "
        "WHERE %(w)s::text IS NULL OR webhook = %(w)s ORDER BY id",
        {"w": webhook_adi},
    ).fetchall()


def olaylar(conn, webhook_adi=None) -> list[str]:
    return [o for _, o, _, _, _ in bildirimler(conn, webhook_adi)]


def zamani_gelsin(conn) -> None:
    """Geri çekilen bildirimlerin zamanını şimdiye çeker (beklemeden sınamak için)."""
    conn.execute("UPDATE giden_bildirimler SET sonraki = now() WHERE durum = 'bekliyor'")


class Alici:
    """Sahte alıcı: cevabı `kod` (ya da kodlar listesi sırayla) belirler; istekleri kaydeder."""

    def __init__(self, kod=200):
        self.kod, self.istekler, self.kilit = kod, [], threading.Lock()

    def __call__(self, istek: httpx.Request) -> httpx.Response:
        with self.kilit:
            self.istekler.append(istek)
            kod = self.kod.pop(0) if isinstance(self.kod, list) else self.kod
        if isinstance(kod, Exception):
            raise kod
        return httpx.Response(
            kod,
            text="tamam" if kod < 300 else "olmadı",
            headers={"Location": "http://baska/"} if kod in (301, 302) else {},
        )

    def http(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self), follow_redirects=False)

    def teslimatlar(self, adres_parcasi="") -> list[int]:
        return [int(r.headers["X-Hata-Asistani-Teslimat"]) for r in self.istekler
                if adres_parcasi in str(r.url)]  # fmt: skip


def bosalt(conn, http, en_fazla=100) -> int:
    toplam = 0
    for _ in range(en_fazla):
        n = webhook.bir_tur(conn, http)
        if not n:
            return toplam
        toplam += n
    raise AssertionError("giden kutusu boşalmadı")


# --- Tetikleyiciler ---------------------------------------------------------------------------


def test_ariza_olaylari_ve_icerik(db):
    webhook.webhook_ekle(db, "erp", "https://erp.test/hook")
    ariza = ariza_ekle(db, aciklama="A101 Hidrolik basınç düşük")
    db.execute("UPDATE ariza_kayitlari SET aciklama = aciklama || ' ek' WHERE id = %s", [ariza])
    db.execute("UPDATE ariza_kayitlari SET hat_durdu = true WHERE id = %s", [ariza])
    db.execute("UPDATE ariza_kayitlari SET hat_durdu = true WHERE id = %s", [ariza])  # değişmedi
    db.execute("UPDATE ariza_kayitlari SET bitis = now() WHERE id = %s", [ariza])
    db.execute("UPDATE ariza_kayitlari SET bitis = NULL WHERE id = %s", [ariza])  # yeniden açıldı
    db.execute("UPDATE ariza_kayitlari SET bitis = now() WHERE id = %s", [ariza])
    gecmis = ariza_ekle(db, "P3-RB", kapali=True)  # kapalı eklenen (geçmiş) kayıt
    satirlar = bildirimler(db)
    assert [o for _, o, _, _, _ in satirlar] == [
        "ariza_acildi", "hat_durdu", "ariza_kapandi", "ariza_acildi", "ariza_kapandi",
        "ariza_kapandi",
    ]  # fmt: skip
    ilk = satirlar[0][2]
    assert ilk["olay"] == "ariza_acildi" and datetime.fromisoformat(ilk["zaman"])
    a = ilk["ariza"]
    assert (a["id"], a["makine_kodu"], a["hat"], a["onem"], a["ariza_tipi"]) == (
        ariza, "P3-HP", "Pres 3", "yuksek", "hidrolik")  # fmt: skip
    assert a["aciklama"] == "A101 Hidrolik basınç düşük" and a["bitis"] is None
    assert a["yeniden_acildi"] is False and a["sure_dk"] is None and a["hat_durdu"] is False
    assert satirlar[1][2]["ariza"]["hat_durdu"] is True
    kapandi = satirlar[2][2]["ariza"]
    assert kapandi["bitis"] is not None and 9 <= kapandi["sure_dk"] <= 11
    assert satirlar[3][2]["ariza"]["yeniden_acildi"] is True
    assert satirlar[5][2]["ariza"]["id"] == gecmis
    assert {d for _, _, _, d, _ in satirlar} == {"bekliyor"}


def test_kapanan_arizada_hat_durdu_ayrica_bildirilmez(db):
    webhook.webhook_ekle(db, "erp", "https://erp.test/hook")
    ariza = ariza_ekle(db)
    db.execute("UPDATE ariza_kayitlari SET hat_durdu = true, bitis = now() WHERE id = %s", [ariza])
    assert olaylar(db) == ["ariza_acildi", "ariza_kapandi"]


def test_periyodik_bakim_yapildi(db):
    webhook.webhook_ekle(db, "erp", "https://erp.test/hook", ["periyodik_bakim_yapildi"])
    plan = db.execute("SELECT id, ad FROM periyodik_bakimlar ORDER BY id LIMIT 1").fetchone()
    periyodik.yapildi_kaydet(db, plan[0], yapan="Demo Bakım Mühendisi", simdi=datetime.now(UTC),
                             aciklama="filtre değişti")  # fmt: skip
    ariza_ekle(db)  # dinlemediği olay
    # Açık açılıp sonra kapanan periyodik iş emri de bildirilir; arıza iş emri bildirilmez.
    is_emri = db.execute(
        "INSERT INTO is_emirleri (makine_id, tip, aciklama, durum, atanan, acilis, plan_id) "
        "SELECT makine_id, 'periyodik', ad, 'acik', 'Ali', now() - interval '2 hours', id "
        "FROM periyodik_bakimlar WHERE id = %s RETURNING id",
        [plan[0]],
    ).fetchone()[0]
    assert len(olaylar(db)) == 1
    db.execute("UPDATE is_emirleri SET durum = 'kapali', kapanis = now() WHERE id = %s", [is_emri])
    db.execute("UPDATE is_emirleri SET aciklama = 'x' WHERE id = %s", [is_emri])
    satirlar = bildirimler(db)
    assert [o for _, o, _, _, _ in satirlar] == ["periyodik_bakim_yapildi"] * 2
    b = satirlar[0][2]["bakim"]
    assert b["plan_id"] == plan[0] and b["ad"] == plan[1] and b["yapan"] == "Demo Bakım Mühendisi"
    assert b["aciklama"] == f"{plan[1]}: filtre değişti" and b["makine_kodu"]
    assert satirlar[1][2]["bakim"]["is_emri_id"] == is_emri


def test_olay_suzgeci_ve_kapali_webhook(db):
    webhook.webhook_ekle(db, "hepsi", "https://a.test/")
    webhook.webhook_ekle(db, "durus", "https://b.test/", ["hat_durdu"])
    webhook.webhook_ekle(db, "kapali", "https://c.test/")
    db.execute("UPDATE webhooklar SET aktif = false WHERE ad = 'kapali'")
    ariza = ariza_ekle(db)
    db.execute("UPDATE ariza_kayitlari SET hat_durdu = true WHERE id = %s", [ariza])
    assert olaylar(db, "hepsi") == ["ariza_acildi", "hat_durdu"]
    assert olaylar(db, "durus") == ["hat_durdu"]
    assert olaylar(db, "kapali") == []


def test_webhook_yokken_hicbir_sey_yazilmaz_islem_geri_alinirsa_bildirim_yok(db):
    ariza_ekle(db)
    assert bildirimler(db) == []
    webhook.webhook_ekle(db, "erp", "https://erp.test/hook")
    with pytest.raises(RuntimeError), db.transaction():
        ariza_ekle(db)
        assert len(bildirimler(db)) == 1  # işlemin içinde görünür
        raise RuntimeError("geri al")
    assert bildirimler(db) == []


def test_seed_yeniden_calisinca_bildirim_yagdirmaz(test_verisi):
    """Webhook tanımlıyken seed binlerce geçmiş arıza yazar; hiçbiri bildirim olmamalı ve
    webhook tanımı korunmalı."""
    from tests.conftest import _veritabanini_olustur

    url = TEST_DB_URL.rsplit("/", 1)[0] + "/andon_test_seed"
    try:
        _veritabanini_olustur(url)
    except psycopg.OperationalError as hata:
        pytest.skip(f"veritabanı oluşturulamadı: {hata}")
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            veritabanina_yaz(conn, test_verisi)
            webhook.webhook_ekle(conn, "erp", "https://erp.test/hook")
            veritabanina_yaz(conn, test_verisi)
            assert conn.execute("SELECT count(*) FROM ariza_kayitlari").fetchone()[0] > 100
            assert conn.execute("SELECT count(*) FROM giden_bildirimler").fetchone()[0] == 0
            assert [w["ad"] for w in webhook.webhooklar(conn)] == ["erp"]
            ariza_ekle(conn)  # seed'den sonra tetikleyici yerinde
            assert conn.execute("SELECT count(*) FROM giden_bildirimler").fetchone()[0] == 1
    finally:
        with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier("andon_test_seed")))  # fmt: skip


IŞLEMLER = ["ekle", "ekle_kapali", "kapat", "ac", "durdur", "aciklama", "sil_webhook_olay"]


@pytest.mark.parametrize("tohum", range(40 if YOGUN else 8))
def test_rastgele_ariza_dizisi_modelle_ayni(db, tohum):
    """Rastgele arıza işlemleri: tetikleyicinin yazdığı bildirimler bağımsız bir modelle aynı."""
    rng = random.Random(tohum)
    secili = rng.sample(webhook.OLAYLAR[:3], rng.randint(1, 3))
    webhook.webhook_ekle(db, "model", "https://m.test/", secili)
    arizalar: dict[int, dict] = {}
    beklenen = []
    for _ in range(60):
        islem = rng.choice(IŞLEMLER[:-1])
        if islem.startswith("ekle") or not arizalar:
            kapali = islem == "ekle_kapali"
            durdu = rng.random() < 0.3
            ariza = ariza_ekle(db, rng.choice(["P3-HP", "P3-RB", "P3-TR"]), kapali, durdu)
            arizalar[ariza] = {"acik": not kapali, "durdu": durdu}
            beklenen.append("ariza_kapandi" if kapali else "ariza_acildi")
            continue
        ariza = rng.choice(sorted(arizalar))
        d = arizalar[ariza]
        if islem == "kapat" and d["acik"]:
            db.execute("UPDATE ariza_kayitlari SET bitis = now() WHERE id = %s", [ariza])
            d["acik"] = False
            beklenen.append("ariza_kapandi")
        elif islem == "ac" and not d["acik"]:
            db.execute("UPDATE ariza_kayitlari SET bitis = NULL WHERE id = %s", [ariza])
            d["acik"] = True
            beklenen.append("ariza_acildi")
        elif islem == "durdur":
            db.execute("UPDATE ariza_kayitlari SET hat_durdu = true WHERE id = %s", [ariza])
            if d["acik"] and not d["durdu"]:
                beklenen.append("hat_durdu")
            d["durdu"] = True
        elif islem == "aciklama":
            db.execute("UPDATE ariza_kayitlari SET aciklama = 'değişti' WHERE id = %s", [ariza])
    assert olaylar(db) == [o for o in beklenen if o in secili]


# --- İmza ve gövde ----------------------------------------------------------------------------


def test_imza_dogrulanir_oynanmis_ya_da_eski_istek_reddedilir():
    gizli = webhook.gizli_uret()
    govde = b'{"olay":"ariza_acildi"}'
    b = webhook.basliklar("json", gizli, "ariza_acildi", 7, govde, zaman=1_700_000_000)
    assert webhook.imza_dogrula(gizli, b, govde, simdi=1_700_000_100)
    assert not webhook.imza_dogrula(gizli, b, govde + b" ", simdi=1_700_000_100)
    assert not webhook.imza_dogrula("whsec_baska" + "x" * 30, b, govde, simdi=1_700_000_100)
    assert not webhook.imza_dogrula(gizli, b, govde, simdi=1_700_000_000 + 301)  # tekrar oynatma
    assert not webhook.imza_dogrula(gizli, {**b, "X-Hata-Asistani-Zaman": "1700000001"}, govde,
                                    simdi=1_700_000_100)  # fmt: skip
    assert not webhook.imza_dogrula(gizli, {}, govde)
    assert not webhook.imza_dogrula(gizli, {**b, "X-Hata-Asistani-Zaman": "dün"}, govde)
    assert b["X-Hata-Asistani-Teslimat"] == "7" and b["X-Hata-Asistani-Olay"] == "ariza_acildi"
    assert "X-Hata-Asistani-Imza" not in webhook.basliklar("teams", gizli, "x", 1, govde)
    assert len(gizli) > 40 and gizli != webhook.gizli_uret()


ICERIK = {"olay": "ariza_acildi", "zaman": "2026-10-15T08:00:00+03:00", "ariza": {
    "id": 5, "makine_kodu": "P3-HP", "makine": "Hidrolik Pres", "hat": "Pres 3", "onem": "yuksek",
    "hat_durdu": True, "aciklama": "A101 Hidrolik basınç düşük", "yeniden_acildi": False,
    "sure_dk": None}}  # fmt: skip


def test_teams_ve_slack_bicimi():
    slack = json.loads(webhook.govde("slack", "ariza_acildi", ICERIK))
    assert slack == {"text": "Arıza açıldı: P3-HP Hidrolik Pres (Pres 3) · önem yüksek · hat durdu"
                     " · A101 Hidrolik basınç düşük"}  # fmt: skip
    teams = json.loads(webhook.govde("teams", "ariza_acildi", ICERIK))
    kart = teams["attachments"][0]
    assert teams["type"] == "message"
    assert kart["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert kart["content"]["body"][0]["text"] == slack["text"]
    kapandi = {**ICERIK, "ariza": {**ICERIK["ariza"], "sure_dk": 12.5}}
    assert "12.5 dk sürdü" in json.loads(webhook.govde("slack", "ariza_kapandi", kapandi))["text"]
    assert json.loads(webhook.govde("json", "ariza_acildi", ICERIK)) == ICERIK
    assert "denemesi" in json.loads(webhook.govde("slack", "deneme", {}))["text"]
    bakim = {"bakim": {"makine_kodu": "P3-HP", "makine": "Pres", "hat": "Pres 3", "ad": "Yıllık",
                       "yapan": "Ali"}}  # fmt: skip
    assert "Periyodik bakım yapıldı" in webhook._metin("periyodik_bakim_yapildi", bakim)
    for olay in (*webhook.OLAYLAR, "deneme", "bilinmeyen"):  # eksik alanla da çökmez
        for bicim in webhook.BICIMLER:
            json.loads(webhook.govde(bicim, olay, {}))


# --- Gönderim ---------------------------------------------------------------------------------


def test_gonderim_imzali_ve_kaydedilir(db):
    gizli = webhook.webhook_ekle(db, "erp", "https://erp.test/hook?kaynak=ha")
    ariza = ariza_ekle(db)
    alici = Alici(200)
    assert bosalt(db, alici.http()) == 1
    (istek,) = alici.istekler
    assert str(istek.url) == "https://erp.test/hook?kaynak=ha" and istek.method == "POST"
    assert webhook.imza_dogrula(gizli, istek.headers, istek.content)
    govde = json.loads(istek.content)
    assert govde["olay"] == "ariza_acildi" and govde["ariza"]["id"] == ariza
    assert istek.headers["content-type"].startswith("application/json")
    satir = db.execute("SELECT id, durum, deneme, gonderim, son_hata FROM giden_bildirimler"
                       ).fetchone()  # fmt: skip
    assert satir[1:3] == ("gonderildi", 1) and satir[3] is not None and satir[4] is None
    assert int(istek.headers["X-Hata-Asistani-Teslimat"]) == satir[0]
    assert webhook.bir_tur(db, alici.http()) == 0  # bir daha gönderilmez


@pytest.mark.parametrize(
    "hata",
    [500, 502, 503, 404, 400, 401, 302, 429, httpx.ConnectError("yok"), httpx.ReadTimeout("geç")],
    ids=str,
)
def test_basarisiz_gonderim_bekler_sira_bozulmaz(db, hata):
    webhook.webhook_ekle(db, "erp", "https://erp.test/hook")
    ilk, ikinci = ariza_ekle(db), ariza_ekle(db, "P3-RB")
    alici = Alici([hata, hata, 200, 200])
    assert webhook.bir_tur(db, alici.http()) == 1
    satirlar = db.execute("SELECT durum, deneme, sonraki > now(), son_hata FROM giden_bildirimler "
                          "ORDER BY id").fetchall()  # fmt: skip
    assert satirlar[0][:3] == ("bekliyor", 1, True) and satirlar[0][3]
    assert satirlar[1][:2] == ("bekliyor", 0)
    assert webhook.bir_tur(db, alici.http()) == 0  # ilki geri çekildi; ikincisi onu bekler
    sonraki_1 = db.execute("SELECT sonraki FROM giden_bildirimler ORDER BY id LIMIT 1").fetchone()
    zamani_gelsin(db)
    assert webhook.bir_tur(db, alici.http()) == 1
    sonraki_2 = db.execute("SELECT sonraki - now() FROM giden_bildirimler ORDER BY id LIMIT 1"
                           ).fetchone()[0]  # fmt: skip
    assert sonraki_2 > timedelta(seconds=6)  # ikinci bekleme daha uzun (5 sn -> 10 sn)
    assert sonraki_1[0] is not None
    zamani_gelsin(db)
    assert bosalt(db, alici.http()) == 2
    gonderilen = [json.loads(r.content)["ariza"]["id"] for r in alici.istekler]
    assert gonderilen == [ilk, ilk, ilk, ikinci]
    assert [d for d, *_ in db.execute("SELECT durum FROM giden_bildirimler ORDER BY id")] == [
        "gonderildi", "gonderildi"]  # fmt: skip


def test_bir_alici_cevap_vermezse_digerleri_beklemez(db):
    webhook.webhook_ekle(db, "bozuk", "https://bozuk.test/")
    webhook.webhook_ekle(db, "saglam", "https://saglam.test/")

    def alici(istek):
        return httpx.Response(503 if "bozuk" in str(istek.url) else 200)

    http = httpx.Client(transport=httpx.MockTransport(alici))
    for _ in range(5):
        ariza_ekle(db)
    for _ in range(10):
        webhook.bir_tur(db, http)
    durumlar = db.execute("SELECT webhook, durum, count(*) FROM giden_bildirimler "
                          "GROUP BY 1, 2 ORDER BY 1, 2").fetchall()  # fmt: skip
    assert durumlar == [("bozuk", "bekliyor", 5), ("saglam", "gonderildi", 5)]


def test_en_fazla_denemeden_sonra_vazgecer_sira_ilerler(db):
    webhook.webhook_ekle(db, "erp", "https://erp.test/hook")
    ariza_ekle(db)
    ariza_ekle(db)
    alici = Alici([500] * webhook.EN_FAZLA_DENEME + [200])
    for _ in range(webhook.EN_FAZLA_DENEME):
        zamani_gelsin(db)
        assert webhook.bir_tur(db, alici.http()) == 1
    satirlar = db.execute("SELECT durum, deneme, son_hata FROM giden_bildirimler ORDER BY id"
                          ).fetchall()  # fmt: skip
    assert satirlar[0][:2] == ("vazgecildi", webhook.EN_FAZLA_DENEME)
    assert satirlar[0][2].startswith("HTTP 500")
    assert bosalt(db, alici.http()) == 1  # sıradaki gider
    assert webhook.webhooklar(db)[0]["vazgecilen"] == 1


def test_bekleme_suresi_artar_ve_siniri_asmaz():
    rng = random.Random(1)
    sureler = [webhook.bekleme(n, rng) for n in range(1, 20)]
    assert 4 <= sureler[0] <= 6 and 8 <= sureler[1] <= 12
    assert all(s <= webhook.EN_UZUN_BEKLEME * 1.2 for s in sureler)
    toplam = sum(webhook.bekleme(n, random.Random(n)) for n in range(1, webhook.EN_FAZLA_DENEME))
    assert 3 * 3600 < toplam < 6 * 3600  # birkaç saatlik kesintiyi karşılar


def test_kapali_webhook_gondermez_acilinca_kaldigi_yerden(db):
    webhook.webhook_ekle(db, "erp", "https://erp.test/hook")
    ariza_ekle(db)
    db.execute("UPDATE webhooklar SET aktif = false")
    ariza_ekle(db)  # kapalıyken olan bildirim yazılmaz
    alici = Alici(200)
    assert webhook.bir_tur(db, alici.http()) == 0
    assert webhook.sonraki_bekleme(db, 30) == 30
    db.execute("UPDATE webhooklar SET aktif = true")
    assert bosalt(db, alici.http()) == 1


def test_deneme_bildirimi_tek_denemelik(db):
    webhook.webhook_ekle(db, "erp", "https://erp.test/hook")
    assert webhook.deneme_ekle(db, "erp") and not webhook.deneme_ekle(db, "yok")
    assert webhook.bir_tur(db, Alici(500).http()) == 1
    assert db.execute("SELECT olay, durum FROM giden_bildirimler").fetchone() == (
        "deneme", "vazgecildi")  # fmt: skip


def test_sonraki_bekleme_engellenen_satirda_bosa_donmez(db):
    """Geri çekilmiş bir bildirimin arkasındaki satırın zamanı gelmiş olsa da gönderici onu
    bekleyemez: sıradaki zaman baştaki satırınkidir (boşa dönen döngü olmasın)."""
    webhook.webhook_ekle(db, "erp", "https://erp.test/hook")
    ariza_ekle(db)
    ariza_ekle(db)
    webhook.bir_tur(db, Alici(500).http())
    assert 3 < webhook.sonraki_bekleme(db, 30) <= 6.1


def test_iki_gonderici_ayni_bildirimi_iki_kez_gondermez_sira_bozulmaz(db, test_veritabani):
    adet = 400 if YOGUN else 80
    for ad in ("alici-a", "alici-b", "alici-c"):
        webhook.webhook_ekle(db, ad, f"https://{ad}.test/")
    for i in range(adet):
        ariza_ekle(db, ["P3-HP", "P3-RB", "P3-TR"][i % 3])
    alici = Alici(200)
    hatalar = []

    def gonderici():
        try:
            with psycopg.connect(test_veritabani, autocommit=True) as conn:
                http = alici.http()
                bos = 0
                while bos < 3:
                    bos = 0 if webhook.bir_tur(conn, http) else bos + 1
        except Exception as hata:  # noqa: BLE001
            hatalar.append(hata)

    parcaciklar = [threading.Thread(target=gonderici) for _ in range(4)]
    for p in parcaciklar:
        p.start()
    for p in parcaciklar:
        p.join(120)
    assert not hatalar
    for ad in ("alici-a", "alici-b", "alici-c"):
        teslim = alici.teslimatlar(f"{ad}.test")
        assert len(teslim) == adet and len(set(teslim)) == adet, ad  # her biri bir kez
        assert teslim == sorted(teslim), ad  # webhook'un kendi sırası
    assert db.execute("SELECT count(*) FROM giden_bildirimler WHERE durum <> 'gonderildi'"
                      ).fetchone()[0] == 0  # fmt: skip


# --- Komut satırı -----------------------------------------------------------------------------


def test_komut_satiri(db):
    cikti = cli(["webhook-ekle", "erp", "https://erp.test/hook"], db)
    gizli = cikti.splitlines()[2]
    assert gizli.startswith("whsec_")
    assert gizli == db.execute("SELECT gizli FROM webhooklar WHERE ad = 'erp'").fetchone()[0]
    cikti = cli(["webhook-ekle", "kanal", "https://x.webhook.office.com/a", "--bicim", "teams",
                 "--olay", "ariza_acildi", "--olay", "hat_durdu"], db)  # fmt: skip
    assert "whsec_" not in cikti  # Teams imza doğrulamaz; anahtar gösterilmez
    liste = cli(["webhooklar"], db)
    assert "erp" in liste and "kanal" in liste and "ariza_acildi, hat_durdu" in liste
    assert gizli not in liste
    ariza_ekle(db)
    assert "bekleyen=1" in cli(["webhooklar"], db).split("kanal")[0]
    assert "sıraya kondu" in cli(["webhook-dene", "erp"], db)
    assert "kapatıldı" in cli(["webhook-kapat", "erp"], db)
    with pytest.raises(SystemExit, match="kapalı"):
        cli(["webhook-dene", "erp"], db)
    assert "açıldı" in cli(["webhook-ac", "erp"], db)
    webhook.bir_tur(db, Alici([500, 200]).http())
    assert "son hata: HTTP 500" in cli(["webhooklar"], db)
    assert "silindi" in cli(["webhook-sil", "erp"], db)
    assert olaylar(db, "erp") == []  # bekleyenler de silindi
    for komut in ("webhook-sil", "webhook-ac", "webhook-kapat", "webhook-dene"):
        with pytest.raises(SystemExit, match="webhook yok"):
            cli([komut, "erp"], db)


@pytest.mark.parametrize(
    "argumanlar, hata",
    [
        (["webhook-ekle", "kanal", "https://b.test/"], "zaten var"),
        (["webhook-ekle", "Büyük", "https://b.test/"], "küçük harf"),
        (["webhook-ekle", "x", "https://b.test/"], "küçük harf"),
        (["webhook-ekle", "ftp", "ftp://b.test/"], "http"),
        (["webhook-ekle", "bosluk", "https://b.test/a b"], "boşluk"),
        (["webhook-ekle", "uzun", "https://b.test/" + "a" * 500], "500"),
    ],
)
def test_komut_satiri_hatalari(db, argumanlar, hata):
    cli(["webhook-ekle", "kanal", "https://a.test/"], db)
    with pytest.raises(SystemExit, match=hata):
        cli(argumanlar, db)
    with pytest.raises(SystemExit):  # argparse: bilinmeyen olay
        cli(["webhook-ekle", "yeni", "https://a.test/", "--olay", "her_sey"], db)


# --- Uçtan uca: gerçek HTTP alıcısı -------------------------------------------------------------


class GercekAlici(BaseHTTPRequestHandler):
    gelenler: list = []
    kod = 200
    gecikme = 0.0

    def do_POST(self):  # noqa: N802
        govde = self.rfile.read(int(self.headers["Content-Length"]))
        time.sleep(self.gecikme)
        GercekAlici.gelenler.append((dict(self.headers), govde, time.monotonic()))
        self.send_response(self.kod)
        self.end_headers()

    def log_message(self, *_):
        pass


@pytest.fixture
def gercek_alici():
    GercekAlici.gelenler, GercekAlici.kod, GercekAlici.gecikme = [], 200, 0.0
    sunucu = ThreadingHTTPServer(("127.0.0.1", 0), GercekAlici)
    threading.Thread(target=sunucu.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{sunucu.server_address[1]}/hook"
    sunucu.shutdown()


@pytest.fixture
def gonderici(test_veritabani):
    dur = threading.Event()

    def baslat(http=None):
        p = threading.Thread(target=webhook_gonderici.dongu, kwargs={
            "baglanti_ac": lambda: psycopg.connect(test_veritabani), "dur": dur,
            "http": http or httpx.Client(timeout=2, follow_redirects=False), "yedek_aralik": 30,
        }, daemon=True)  # fmt: skip
        p.start()
        time.sleep(0.3)  # LISTEN kurulsun
        return p

    yield baslat
    dur.set()


def bekle(kosul, sure=10.0):
    son = time.monotonic() + sure
    while not kosul():
        assert time.monotonic() < son, "koşul sağlanmadı"
        time.sleep(0.02)


def test_uctan_uca_makine_alarmindan_erp_ye(istemci, db, kaynak, gercek_alici, gonderici):
    """PLC alarmı API'ye gelir, arıza açılır, gönderici sinyalle hemen uyanır ve ERP'ye imzalı
    bildirim gider; alarm bitince kapanış da."""
    gizli = webhook.webhook_ekle(db, "erp", gercek_alici)
    gonderici()
    ad, anahtar = kaynak
    zaman = datetime.now(UTC)
    basla = time.monotonic()
    cevap = istemci.post("/entegrasyon/olaylar", headers={"X-API-Anahtari": anahtar}, json={
        "olaylar": [{"olay_id": "e2e-1", "tur": "alarm_basladi", "makine_kodu": "P3-HP",
                     "alarm_kodu": "A101", "zaman": zaman.isoformat()}]})  # fmt: skip
    assert cevap.status_code == 200, cevap.text
    bekle(lambda: len(GercekAlici.gelenler) == 1)
    basliklar, govde, geldi = GercekAlici.gelenler[0]
    assert geldi - basla < 2.0  # 30 sn'lik yedek yoklamayı beklemedi: NOTIFY ile uyandı
    assert webhook.imza_dogrula(gizli, basliklar, govde)
    veri = json.loads(govde)
    assert veri["olay"] == "ariza_acildi" and veri["ariza"]["makine_kodu"] == "P3-HP"
    assert veri["ariza"]["aciklama"].startswith("A101")
    istemci.post("/entegrasyon/olaylar", headers={"X-API-Anahtari": anahtar}, json={
        "olaylar": [{"olay_id": "e2e-2", "tur": "alarm_bitti", "makine_kodu": "P3-HP",
                     "alarm_kodu": "A101", "zaman": (zaman + timedelta(seconds=30)).isoformat()}]
    })  # fmt: skip
    bekle(lambda: len(GercekAlici.gelenler) == 2)
    assert json.loads(GercekAlici.gelenler[1][1])["olay"] == "ariza_kapandi"


def test_uctan_uca_alici_kapaliyken_bekler_donunce_gonderir(db, gercek_alici, gonderici):
    webhook.webhook_ekle(db, "erp", gercek_alici)
    GercekAlici.kod = 503
    gonderici()
    ariza_ekle(db)
    bekle(lambda: len(GercekAlici.gelenler) == 1)
    GercekAlici.kod = 200
    zamani_gelsin(db)
    db.execute("SELECT pg_notify('giden_bildirimler', '')")  # bekleyeni uyandır
    bekle(lambda: len(GercekAlici.gelenler) == 2)
    bekle(lambda: olaylar(db) and bildirimler(db)[0][3] == "gonderildi")


def test_yavas_alici_zaman_asimina_ugrar(db, gercek_alici):
    webhook.webhook_ekle(db, "erp", gercek_alici)
    GercekAlici.gecikme = 1.0
    ariza_ekle(db)
    with httpx.Client(timeout=0.2) as http:
        assert webhook.bir_tur(db, http) == 1
    durum, hata = db.execute("SELECT durum, son_hata FROM giden_bildirimler").fetchone()
    assert durum == "bekliyor" and "Timeout" in hata


def test_gonderici_veritabani_kopsa_da_olmez(db, gercek_alici):
    """Bağlantı açılamıyorsa gönderici 5 sn bekleyip yeniden dener; döngü ölmez."""
    denemeler = []

    def baglanti_ac():
        denemeler.append(time.monotonic())
        if len(denemeler) < 2:
            raise psycopg.OperationalError("veritabanı yok")
        return psycopg.connect(TEST_DB_URL)

    webhook.webhook_ekle(db, "erp", gercek_alici)
    dur = threading.Event()
    orijinal = dur.wait
    dur.wait = lambda sure=None: orijinal(0.05)  # 5 sn yerine hızlı
    p = threading.Thread(target=webhook_gonderici.dongu, kwargs={
        "baglanti_ac": baglanti_ac, "dur": dur, "http": httpx.Client(timeout=2),
        "yedek_aralik": 0.2}, daemon=True)  # fmt: skip
    p.start()
    try:
        bekle(lambda: len(denemeler) >= 2)
        ariza_ekle(db)
        bekle(lambda: len(GercekAlici.gelenler) == 1)
    finally:
        dur.set()
        p.join(5)
    assert not p.is_alive()
