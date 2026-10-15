"""Bağlayıcı uçtan uca: gerçek OPC UA sunucusu (PLC simülatörü) -> bağlayıcı -> API -> veritabanı.

Her testte simülatör boş bir portta açılır, bağlayıcı ona bağlanır ve olayları test
veritabanına bağlı API'ye gönderir (httpx ASGITransport; ağ yok). Arızalar veritabanından
okunur. Ayrıca göndericinin her HTTP cevabındaki davranışı ve diskteki kuyruk.

Yük testleri ortam değişkeniyle büyür: HA_YOGUN=1
"""

import asyncio
import os
import random
import socket
import threading
import time
from pathlib import Path

import httpx
import psycopg
import pytest

from app import entegrasyon as e
from entegrasyon.baglayici import Baglayici
from entegrasyon.gonderici import Gonderici
from entegrasyon.kuyruk import Kuyruk
from entegrasyon.opcua import olay_kimligi
from entegrasyon.simulator import Simulator

YOGUN = os.environ.get("HA_YOGUN") == "1"
HIZLI = 0.02  # testlerde bekleme ölçeği (gerçekte 1 sn)


def bos_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def kaynak(test_veritabani):
    with psycopg.connect(test_veritabani, autocommit=True) as conn:
        son = conn.execute("SELECT max(id) FROM ariza_kayitlari").fetchone()[0]
        ad = f"test-bag-{bos_port()}"
        anahtar = e.kaynak_ekle(conn, ad, "bağlayıcı testi", katalog="pres-paneli")
        yield ad, anahtar
        conn.execute("DELETE FROM entegrasyon_kaynaklari WHERE ad = %s", [ad])
        conn.execute("DELETE FROM ariza_kayitlari WHERE id > %s", [son])


class Ortam:
    """Simülatör + bağlayıcı + veritabanı sorguları."""

    def __init__(self, istemci, url, kaynak, kuyruk=None, transport=None, adres=None):
        self.istemci, self.url = istemci, url
        self.ad, self.anahtar = kaynak
        self.adres = adres or f"opc.tcp://127.0.0.1:{bos_port()}/test/"
        self.kuyruk = Kuyruk() if kuyruk is None else kuyruk
        self.transport = transport or httpx.ASGITransport(app=istemci.app)
        self.db = psycopg.connect(url, autocommit=True)
        self.sim: Simulator | None = None
        self.baglayici: Baglayici | None = None

    async def simulator_ac(self):
        self.sim = await Simulator(self.adres).baslat()
        return self.sim

    async def baglayici_ac(self, kuyruk=None):
        self.dur = asyncio.Event()
        http = httpx.AsyncClient(transport=self.transport, base_url="http://test")
        kuyruk = self.kuyruk if kuyruk is None else kuyruk
        self.baglayici = Baglayici(
            self.adres, "http://test", self.anahtar, kuyruk, http, ilk_bekleme=HIZLI
        )
        self.gorev = asyncio.create_task(self.baglayici.calis(self.dur))
        await kosul(lambda: self.baglayici.izleyici.bagli.is_set(), "bağlayıcı bağlanmadı")
        await kosul(lambda: self.baglayici.izleyici.uzlasildi, "uzlaşma bitmedi")

    async def baglayici_kapat(self):
        self.dur.set()
        await asyncio.wait_for(self.gorev, 10)
        await self.baglayici.http.aclose()

    async def kapat(self):
        if self.baglayici and not self.gorev.done():
            await self.baglayici_kapat()
        if self.sim:
            await self.sim.durdur()
        self.db.close()

    def acik_arizalar(self) -> list[tuple]:
        return self.db.execute(
            "SELECT r.id, m.kod, r.aciklama FROM ariza_kayitlari r "
            "JOIN makineler m ON m.id = r.makine_id WHERE r.bitis IS NULL AND EXISTS "
            "(SELECT 1 FROM entegrasyon_olaylari o WHERE o.kaynak = %s AND o.ariza_id = r.id) "
            "ORDER BY r.id",
            [self.ad],
        ).fetchall()

    def aktifler(self) -> set[tuple[str, str]]:
        return {
            (m, k)
            for m, k in self.db.execute(
                "SELECT m.kod, a.alarm_kodu FROM aktif_alarmlar a "
                "JOIN makineler m ON m.id = a.makine_id WHERE a.kaynak = %s",
                [self.ad],
            )
        }

    def olaylar(self) -> list[tuple]:
        return self.db.execute(
            "SELECT tur, alarm_kodu, sonuc FROM entegrasyon_olaylari WHERE kaynak = %s ORDER BY id",
            [self.ad],
        ).fetchall()


async def kosul(fonksiyon, mesaj="koşul sağlanmadı", sure=15.0):
    son = time.monotonic() + sure
    while not fonksiyon():
        if time.monotonic() > son:
            raise AssertionError(mesaj)
        await asyncio.sleep(0.02)


def calistir(istemci, url, kaynak, senaryo, **ayar):
    async def ana():
        ortam = Ortam(istemci, url, kaynak, **ayar)
        try:
            await senaryo(ortam)
        finally:
            await ortam.kapat()

    asyncio.run(ana())


# --- Temel akış --------------------------------------------------------------------------------


def test_alarm_ariza_acar_kapatir_ve_hizli(istemci, test_veritabani, kaynak):
    async def senaryo(o: Ortam):
        sim = await o.simulator_ac()
        await o.baglayici_ac()
        gecikmeler = []
        for _ in range(5):
            basla = time.monotonic()
            await sim.ayarla("P3-HP", "A101", True)
            await kosul(lambda: o.aktifler() == {("P3-HP", "A101")}, "alarm API'ye ulaşmadı")
            gecikmeler.append(time.monotonic() - basla)
            assert len(o.acik_arizalar()) == 1
            await sim.ayarla("P3-HP", "A101", False)
            await kosul(lambda: not o.acik_arizalar(), "arıza kapanmadı")
        # Alarm her seferinde 60 sn içinde geri geldiği için aynı arıza yeniden açıldı.
        sonuclar = [s for _, _, s in o.olaylar()]
        assert sonuclar == ["ariza_acildi"] + ["ariza_kapandi", "ariza_yeniden_acildi"] * 4 + [
            "ariza_kapandi"
        ]
        assert max(gecikmeler) < 2.0, gecikmeler  # PLC'deki değişimden veritabanına
        aciklama = o.db.execute(
            "SELECT aciklama FROM ariza_kayitlari r WHERE EXISTS (SELECT 1 FROM "
            "entegrasyon_olaylari x WHERE x.kaynak = %s AND x.ariza_id = r.id)",
            [o.ad],
        ).fetchone()[0]
        assert aciklama == "A101 Hidrolik basınç düşük"

    calistir(istemci, test_veritabani, kaynak, senaryo)


def test_birden_cok_makine_ve_alarm(istemci, test_veritabani, kaynak):
    async def senaryo(o: Ortam):
        sim = await o.simulator_ac()
        await o.baglayici_ac()
        for makine, kod in [("P3-HP", "A104"), ("P3-HP", "A302"), ("P3-RB", "A401")]:
            await sim.ayarla(makine, kod, True)
        await kosul(lambda: len(o.aktifler()) == 3)
        arizalar = o.acik_arizalar()
        assert sorted(m for _, m, _ in arizalar) == [
            "P3-HP",
            "P3-RB",
        ]  # HP'nin iki alarmı tek arıza
        hp = next(a for a in arizalar if a[1] == "P3-HP")
        assert "A104" in hp[2] and "A302" in hp[2]
        for makine, kod in [("P3-HP", "A104"), ("P3-HP", "A302"), ("P3-RB", "A401")]:
            await sim.ayarla(makine, kod, False)
        await kosul(lambda: not o.acik_arizalar() and not o.aktifler())

    calistir(istemci, test_veritabani, kaynak, senaryo)


# --- Bağlantı ve yeniden başlatma --------------------------------------------------------------


def test_baglaninca_suren_alarm_bildirilir_yeniden_baglaninca_tekrar_acilmaz(
    istemci, test_veritabani, kaynak
):
    async def senaryo(o: Ortam):
        sim = await o.simulator_ac()
        await sim.ayarla("P3-TR", "A501", True)  # bağlayıcı yokken başlayan alarm
        await o.baglayici_ac()
        await kosul(lambda: o.aktifler() == {("P3-TR", "A501")})
        await o.baglayici_kapat()
        await o.baglayici_ac(kuyruk=Kuyruk())  # yeniden başlatma: aynı olay kimliği, "tekrar"
        await o.baglayici.gonderici.bosalt(5)
        assert len(o.acik_arizalar()) == 1 and len(o.olaylar()) == 1

    calistir(istemci, test_veritabani, kaynak, senaryo)


def test_baglanti_yokken_biten_alarm_uzlasmayla_kapanir(istemci, test_veritabani, kaynak):
    async def senaryo(o: Ortam):
        sim = await o.simulator_ac()
        await o.baglayici_ac()
        await sim.ayarla("P3-HP", "A301", True)
        await kosul(lambda: o.aktifler() == {("P3-HP", "A301")})
        await o.baglayici_kapat()
        await sim.ayarla("P3-HP", "A301", False)  # bağlayıcı yokken bitti
        await o.baglayici_ac(kuyruk=Kuyruk())
        await kosul(lambda: not o.aktifler() and not o.acik_arizalar(), "uzlaşılmadı")
        assert o.olaylar()[-1] == ("alarm_bitti", "A301", "ariza_kapandi")

    calistir(istemci, test_veritabani, kaynak, senaryo)


def test_plc_kapanip_acilirsa_yeniden_baglanir(istemci, test_veritabani, kaynak):
    async def senaryo(o: Ortam):
        await o.simulator_ac()
        await o.baglayici_ac()
        ilk = o.baglayici.izleyici.baglanti_sayisi
        await o.sim.durdur()
        await kosul(lambda: not o.baglayici.izleyici.bagli.is_set(), "kopma fark edilmedi")
        sim = await o.simulator_ac()  # aynı adreste yeniden
        await kosul(lambda: o.baglayici.izleyici.baglanti_sayisi > ilk, "yeniden bağlanmadı")
        await kosul(lambda: o.baglayici.izleyici.bagli.is_set())
        await sim.ayarla("P3-HP", "A102", True)
        await kosul(lambda: o.aktifler() == {("P3-HP", "A102")})

    calistir(istemci, test_veritabani, kaynak, senaryo)


def test_plc_hic_yokken_bekler_gelince_baglanir(istemci, test_veritabani, kaynak):
    async def senaryo(o: Ortam):
        o.dur = asyncio.Event()
        http = httpx.AsyncClient(transport=o.transport, base_url="http://test")
        o.baglayici = Baglayici(o.adres, "http://test", o.anahtar, o.kuyruk, http,
                                ilk_bekleme=HIZLI)  # fmt: skip
        o.gorev = asyncio.create_task(o.baglayici.calis(o.dur))
        await asyncio.sleep(0.5)  # sunucu yok: hata vermeden yeniden denemeli
        assert not o.gorev.done()
        sim = await o.simulator_ac()
        await kosul(lambda: o.baglayici.izleyici.bagli.is_set(), "geç gelen sunucuya bağlanmadı")
        await sim.ayarla("P3-RB", "A401", True)
        await kosul(lambda: o.aktifler() == {("P3-RB", "A401")})

    calistir(istemci, test_veritabani, kaynak, senaryo)


# --- API kesintisi ve kuyruk -----------------------------------------------------------------


class KesintiliTransport(httpx.AsyncBaseTransport):
    """`acik` False iken her isteğe bağlantı hatası (ya da verilen durum kodu) döner."""

    def __init__(self, ic, kod: int | None = None):
        self.ic, self.kod, self.acik, self.istekler = ic, kod, False, 0

    async def handle_async_request(self, istek):
        self.istekler += 1
        if self.acik:
            return await self.ic.handle_async_request(istek)
        if self.kod:
            return httpx.Response(self.kod, json={"detail": "bakımda"})
        raise httpx.ConnectError("API kapalı", request=istek)


@pytest.mark.parametrize("kesinti", [None, 503, 500], ids=["ag-yok", "503", "500"])
def test_api_kapaliyken_olaylar_bekler_acilinca_sirayla_bir_kez_gider(
    istemci, test_veritabani, kaynak, kesinti
):
    transport = KesintiliTransport(httpx.ASGITransport(app=istemci.app), kesinti)

    async def senaryo(o: Ortam):
        transport.acik = True
        sim = await o.simulator_ac()
        await o.baglayici_ac()
        transport.acik = False
        for kod in ["A101", "A102", "A103"]:
            await sim.ayarla("P3-HP", kod, True)
            await asyncio.sleep(0.15)
        await sim.ayarla("P3-HP", "A101", False)
        await kosul(lambda: len(o.kuyruk) == 4, "olaylar kuyruğa girmedi")
        await asyncio.sleep(0.3)  # birkaç başarısız deneme
        assert len(o.kuyruk) == 4 and not o.olaylar()
        transport.acik = True
        await kosul(lambda: len(o.kuyruk) == 0, "kuyruk boşalmadı")
        assert [(t, k) for t, k, _ in o.olaylar()] == [
            ("alarm_basladi", "A101"), ("alarm_basladi", "A102"), ("alarm_basladi", "A103"),
            ("alarm_bitti", "A101"),
        ]  # fmt: skip
        assert o.aktifler() == {("P3-HP", "A102"), ("P3-HP", "A103")}
        assert len(o.acik_arizalar()) == 1

    calistir(istemci, test_veritabani, kaynak, senaryo, transport=transport)


def test_baglayici_yeniden_baslasa_da_kuyruk_kaybolmaz(istemci, test_veritabani, kaynak, tmp_path):
    dosya = tmp_path / "kuyruk.sqlite"
    transport = KesintiliTransport(httpx.ASGITransport(app=istemci.app))

    async def senaryo(o: Ortam):
        transport.acik = True
        sim = await o.simulator_ac()
        await o.baglayici_ac(kuyruk=Kuyruk(dosya))
        transport.acik = False
        await sim.ayarla("P3-HP", "A201", True)
        await kosul(lambda: len(Kuyruk(dosya)) == 1, "olay diske yazılmadı")
        await o.baglayici_kapat()  # API'ye ulaşamadan kapandı
        transport.acik = True
        await o.baglayici_ac(kuyruk=Kuyruk(dosya))  # yeni süreç, aynı dosya
        await kosul(lambda: o.aktifler() == {("P3-HP", "A201")}, "kuyruktaki olay gitmedi")
        assert len(o.olaylar()) == 1  # bağlanınca gelen ilk değer de aynı kimlik: tekrar

    calistir(istemci, test_veritabani, kaynak, senaryo, transport=transport)


# --- Yük ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("tohum", range(6 if YOGUN else 2))
def test_hizli_rastgele_degisimlerden_sonra_durum_ayni(istemci, test_veritabani, kaynak, tohum):
    """Yüzlerce alarm değişimi milisaniyeler arayla. OPC UA ara değerleri birleştirebilir (100 ms
    içinde açılıp kapanan alarm hiç görünmeyebilir), ama sonunda veritabanındaki süren alarmlar
    PLC'dekiyle aynı olmalı ve her açık arızanın süren bir alarmı olmalı."""
    adet = 3000 if YOGUN else 400
    rng = random.Random(tohum)

    async def senaryo(o: Ortam):
        sim = await o.simulator_ac()
        await o.baglayici_ac()
        alarmlar = sorted(sim.dugumler)
        for _ in range(adet):
            makine, kod = rng.choice(alarmlar)
            await sim.ayarla(makine, kod, rng.random() < 0.5)
            await asyncio.sleep(rng.choice([0, 0, 0.001, 0.01, 0.05]))
        beklenen = await sim.aktifler()
        await kosul(
            lambda: o.aktifler() == beklenen and len(o.kuyruk) == 0, "durum yakınsamadı", 30
        )
        acik = o.acik_arizalar()
        assert sorted({m for _, m, _ in acik}) == sorted({m for m, _ in beklenen})
        assert o.baglayici.gonderici.atilan == 0
        # Bağlayıcı yeniden başlasa da (aynı durum) hiçbir şey değişmez.
        await o.baglayici_kapat()
        await o.baglayici_ac(kuyruk=Kuyruk())
        await o.baglayici.gonderici.bosalt(10)
        assert o.aktifler() == beklenen and o.acik_arizalar() == acik

    calistir(istemci, test_veritabani, kaynak, senaryo)


# --- Gönderici: her HTTP cevabı ------------------------------------------------------------------


def _gonderici(cevaplar, kuyruk=None):
    """Sıradaki cevabı veren sahte API; istekleri kaydeder."""
    istekler = []

    def isle(istek):
        istekler.append(istek)
        cevap = cevaplar.pop(0)
        return cevap(istek) if callable(cevap) else cevap

    kuyruk = Kuyruk() if kuyruk is None else kuyruk
    http = httpx.AsyncClient(transport=httpx.MockTransport(isle), base_url="http://test")
    return Gonderici(kuyruk, "http://test", "ha_x", http, parti=4, ilk_bekleme=HIZLI), istekler


def _kabul(istek):
    import json

    olaylar = json.loads(istek.content)["olaylar"]
    return httpx.Response(200, json={"kaynak": "x", "sonuclar": [
        {"sira": i, "olay_id": o["olay_id"], "durum": "islendi"} for i, o in enumerate(olaylar)
    ]})  # fmt: skip


def _doldur(g, n):
    for i in range(n):
        g.kuyruk.ekle({"olay_id": f"o{i}"})


def test_gonderici_sira_ve_paketler():
    async def senaryo():
        g, istekler = _gonderici([_kabul] * 3)
        _doldur(g, 10)
        for _ in range(3):
            assert await g.bir_tur() == 0
        assert await g.bir_tur() is None and len(g.kuyruk) == 0
        import json

        gonderilen = [o["olay_id"] for r in istekler for o in json.loads(r.content)["olaylar"]]
        assert gonderilen == [f"o{i}" for i in range(10)]
        assert all(r.headers["x-api-anahtari"] == "ha_x" for r in istekler)

    asyncio.run(senaryo())


@pytest.mark.parametrize("kod", [500, 502, 503, 504, 404, 418])
def test_gonderici_sunucu_hatasinda_bekler_silmez(kod):
    async def senaryo():
        g, _ = _gonderici([httpx.Response(kod)] * 4 + [_kabul])
        _doldur(g, 2)
        sureler = [await g.bir_tur() for _ in range(4)]
        assert all(s > 0 for s in sureler) and sureler[-1] > sureler[0]  # artan bekleme
        assert len(g.kuyruk) == 2
        assert await g.bir_tur() == 0 and len(g.kuyruk) == 0

    asyncio.run(senaryo())


def test_gonderici_429_401_ve_bozuk_cevap():
    async def senaryo():
        g, _ = _gonderici([
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(429, headers={"Retry-After": "sacma"}),
            httpx.Response(401),
            httpx.Response(200, content=b"json degil"),
            httpx.Response(200, json={"sonuclar": [{"durum": "islendi"}]}),  # eksik sonuç
            _kabul,
        ])  # fmt: skip
        _doldur(g, 2)
        assert await g.bir_tur() == 7
        assert await g.bir_tur() == HIZLI
        assert await g.bir_tur() == 30 * HIZLI
        assert await g.bir_tur() > 0 and await g.bir_tur() > 0
        assert len(g.kuyruk) == 2  # hiçbiri silinmedi
        assert await g.bir_tur() == 0 and len(g.kuyruk) == 0

    asyncio.run(senaryo())


def test_gonderici_ag_hatalari():
    def kopar(tur):
        def f(istek):
            raise tur("x", request=istek)

        return f

    async def senaryo():
        g, _ = _gonderici([kopar(httpx.ConnectError), kopar(httpx.ReadTimeout),
                           kopar(httpx.RemoteProtocolError), _kabul])  # fmt: skip
        _doldur(g, 1)
        for _ in range(3):
            assert await g.bir_tur() > 0
        assert await g.bir_tur() == 0 and len(g.kuyruk) == 0

    asyncio.run(senaryo())


@pytest.mark.parametrize("kod", [413, 422])
def test_gonderici_buyuk_paketi_boler_tek_olayi_atar(kod):
    async def senaryo():
        r = httpx.Response(kod)
        g, istekler = _gonderici([r, r, _kabul, r, r, _kabul, _kabul])
        _doldur(g, 4)
        for _ in range(7):
            assert await g.bir_tur() == 0
        # 4 reddedildi -> 2 -> 1 (o0 kabul) -> paket 2'ye büyür (o1, o2 reddedildi) -> 1 (o1
        # tek başına da reddedildi: atıldı) -> o2 -> o3
        import json

        sayilar = [len(json.loads(r.content)["olaylar"]) for r in istekler]
        assert sayilar == [4, 2, 1, 2, 1, 1, 1]
        assert (g.atilan, g.kabul, len(g.kuyruk)) == (1, 3, 0)

    asyncio.run(senaryo())


def test_gonderici_kalici_reddedilen_olayi_atar():
    async def senaryo():
        def kismen(istek):
            return httpx.Response(200, json={"sonuclar": [
                {"durum": "islendi"}, {"durum": "gecersiz", "hata": "x"}, {"durum": "hata"},
                {"durum": "tekrar"},
            ]})  # fmt: skip

        g, _ = _gonderici([kismen])
        _doldur(g, 4)
        assert await g.bir_tur() == 0
        assert (g.kabul, g.atilan, len(g.kuyruk)) == (2, 2, 0)

    asyncio.run(senaryo())


def test_gonderici_calis_yeni_olayda_hemen_uyanir():
    async def senaryo():
        g, istekler = _gonderici([_kabul] * 5)
        dur = asyncio.Event()
        gorev = asyncio.create_task(g.calis(dur))
        await asyncio.sleep(0.05)
        basla = time.monotonic()
        g.kuyruk.ekle({"olay_id": "acil"})
        g.haber_ver()
        await kosul(lambda: len(g.kuyruk) == 0)
        assert time.monotonic() - basla < 0.5  # 1 sn'lik yoklamayı beklemedi
        dur.set()
        await asyncio.wait_for(gorev, 5)
        assert len(istekler) == 1

    asyncio.run(senaryo())


# --- Kuyruk -------------------------------------------------------------------------------------


def test_kuyruk_sirali_ve_kalici(tmp_path: Path):
    dosya = tmp_path / "k.sqlite"
    k = Kuyruk(dosya)
    for i in range(5):
        k.ekle({"no": i, "metin": "çşğüöı İ ✓"})
    alinan = k.al(3)
    assert [o["no"] for _, o in alinan] == [0, 1, 2]
    k.sil([alinan[0][0], alinan[2][0]])
    k.kapat()
    k2 = Kuyruk(dosya)  # süreç yeniden başladı
    assert [o["no"] for _, o in k2.al(10)] == [1, 3, 4]
    assert k2.al(10)[0][1]["metin"] == "çşğüöı İ ✓"
    assert len(k2) == 3
    k2.kapat()


def test_kuyruk_es_zamanli_ekleme():
    k = Kuyruk()
    adet, is_parcacigi = (5000, 10) if YOGUN else (1000, 8)

    def ekle(n):
        for i in range(adet // is_parcacigi):
            k.ekle({"p": n, "i": i})

    parcaciklar = [threading.Thread(target=ekle, args=(n,)) for n in range(is_parcacigi)]
    for p in parcaciklar:
        p.start()
    for p in parcaciklar:
        p.join()
    olaylar = [o for _, o in k.al(adet + 1)]
    assert len(olaylar) == adet
    for n in range(is_parcacigi):  # her iş parçacığının kendi sırası korunur
        assert [o["i"] for o in olaylar if o["p"] == n] == list(range(adet // is_parcacigi))


def test_olay_kimligi_kararli():
    from datetime import UTC, datetime

    z = datetime(2026, 10, 15, 8, 0, 0, 123456, tzinfo=UTC)
    assert olay_kimligi("P3-HP", "A101", True, z) == olay_kimligi("P3-HP", "A101", True, z)
    assert olay_kimligi("P3-HP", "A101", True, z) != olay_kimligi("P3-HP", "A101", False, z)
    assert len(olay_kimligi("P3-HP", "A101", True, z)) <= 128
