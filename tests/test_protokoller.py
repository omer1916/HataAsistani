"""Protokol ayrıntıları: adres ve harita doğrulama, mesaj ve XML çözümleme (fuzz dahil),
simülatör sunucularının bozuk girdiye dayanıklılığı, her protokolün kendine özgü kopma
durumları ve dört protokolün aynı anda aynı PLC'yi izlemesi.

Fuzz ve yük sayıları HA_YOGUN=1 ile büyür.
"""

import asyncio
import json
import random
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from baglayici_ortami import (
    HIZLI,
    PROTOKOLLER,
    YOGUN,
    Ortam,
    baglayici_adresi,
    bekci,
    bos_port,
    calistir,
    deneme_haritasi,
    kosul,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from entegrasyon import baglayici, modbus, mqtt, mtconnect
from entegrasyon.izleyici import Izleyici, gizle
from entegrasyon.kuyruk import Kuyruk
from entegrasyon.simulator import Simulator, modbus_haritasi

FUZZ = 3000 if YOGUN else 300
AYAR = settings(max_examples=FUZZ, deadline=None, suppress_health_check=list(HealthCheck))
T0 = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)


def dongude(senaryo):
    return asyncio.run(senaryo())


def olaylar(kuyruk: Kuyruk) -> list[tuple[str, str, str]]:
    return [(o["tur"], o["makine_kodu"], o["alarm_kodu"]) for _, o in kuyruk.al(10_000)]


# --- Protokol seçimi ve adresler --------------------------------------------------------------


@pytest.mark.parametrize(
    "adres, sinif",
    [
        ("opc.tcp://plc:4840/x/", "OpcUaIzleyici"),
        ("OPC.TCP://plc:4840", "OpcUaIzleyici"),
        ("modbus://plc:502", "ModbusIzleyici"),
        ("modbus+tcp://plc", "ModbusIzleyici"),
        ("mqtt://broker/fabrika", "MqttIzleyici"),
        ("mqtts://ali:gizli@broker:8883", "MqttIzleyici"),
        ("http://ajan:5000", "MTConnectIzleyici"),
        ("https://ajan/mtc/?cihaz=VMC:P3-HP", "MTConnectIzleyici"),
    ],
)
def test_protokol_adresten_secilir(adres, sinif):
    b = baglayici.Baglayici(
        adres, "http://test", "ha_x", Kuyruk(), httpx.AsyncClient(),
        modbus_haritasi=deneme_haritasi(),
    )  # fmt: skip
    assert type(b.izleyici).__name__ == sinif
    assert b.izleyici.uzlas == b._uzlas and b.izleyici.haber_ver == b.gonderici.haber_ver


@pytest.mark.parametrize(
    "adres", ["ftp://x", "plc:4840", "", "opc.udp://x", "tcp://x:502", "mqtt:/x", "://x"]
)
def test_bilinmeyen_adres_reddedilir(adres):
    with pytest.raises(ValueError, match="Desteklenmeyen"):
        baglayici.izleyici_sec(adres, deneme_haritasi())


def test_modbus_haritasiz_reddedilir():
    with pytest.raises(ValueError, match="MODBUS_HARITASI"):
        baglayici.izleyici_sec("modbus://plc")


@pytest.mark.parametrize(
    "adres, beklenen",
    [
        ("mqtt://ali:gizli@h:1883/x", "mqtt://ali:***@h:1883/x"),
        ("mqtt://:gizli@h", "mqtt://:***@h"),
        ("mqtts://ali:g%40zli@h/x", "mqtts://ali:***@h/x"),
        ("https://u:p@ajan:5000?cihaz=a:B", "https://u:***@ajan:5000?cihaz=a:B"),
        ("mqtt://h:1883/x", "mqtt://h:1883/x"),
        ("http://ajan:5000?cihaz=a:B", "http://ajan:5000?cihaz=a:B"),
        ("opc.tcp://plc:4840/x/", "opc.tcp://plc:4840/x/"),
    ],
)
def test_gunluge_parola_yazilmaz(adres, beklenen):
    assert gizle(adres) == beklenen


def test_baglanamayan_izleyicinin_gunlugunde_parola_yok(caplog):
    port = bos_port()  # dinleyen yok

    async def senaryo():
        dur = asyncio.Event()
        i = mqtt.MqttIzleyici(f"mqtt://ali:CokGizli@127.0.0.1:{port}/x", Kuyruk(),
                              ilk_bekleme=HIZLI)  # fmt: skip
        gorev = asyncio.create_task(i.calis(dur))
        await asyncio.sleep(0.3)
        dur.set()
        await asyncio.wait_for(gorev, 5)

    with caplog.at_level("INFO"):
        dongude(senaryo)
    assert "MQTT bağlantısı yok" in caplog.text
    assert "CokGizli" not in caplog.text


@pytest.mark.parametrize(
    "ortam, hata",
    [
        ({}, "KAYNAK_ADRESI"),
        ({"ENTEGRASYON_ANAHTARI": "ha_x"}, "KAYNAK_ADRESI"),
        ({"KAYNAK_ADRESI": "ftp://x"}, "Desteklenmeyen"),
        ({"KAYNAK_ADRESI": "modbus://plc"}, "MODBUS_HARITASI"),
        ({"KAYNAK_ADRESI": "modbus://plc", "MODBUS_HARITASI": "yok.json"}, "okunamadı"),
        ({"KAYNAK_ADRESI": "http://ajan?aralik=0"}, "aralik"),
        ({"KAYNAK_ADRESI": "http://ajan?cihaz=VMC"}, "cihaz"),
        ({"KAYNAK_ADRESI": "mqtt://b/+"}, "joker"),
    ],
)
def test_baglayici_hatali_ayarla_anlasilir_mesajla_cikar(monkeypatch, tmp_path, ortam, hata):
    for ad in ("ENTEGRASYON_ANAHTARI", "SIMULATOR_ANAHTARI", "KAYNAK_ADRESI", "OPCUA_ADRESI",
               "MODBUS_HARITASI"):  # fmt: skip
        monkeypatch.delenv(ad, raising=False)
    if ortam:
        monkeypatch.setenv("ENTEGRASYON_ANAHTARI", "ha_x")
    for ad, deger in ortam.items():
        monkeypatch.setenv(ad, deger)
    monkeypatch.setenv("KUYRUK_DOSYASI", str(tmp_path / "k.sqlite"))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as cikis:
        asyncio.run(baglayici._ana())
    assert hata in str(cikis.value)


def test_baglayici_eski_opcua_adresi_degiskeniyle_de_calisir(monkeypatch, tmp_path):
    calisan = []

    async def calis(self, dur):
        calisan.append(type(self.izleyici).__name__)

    monkeypatch.setattr(baglayici.Baglayici, "calis", calis)
    for ad in ("KAYNAK_ADRESI", "ENTEGRASYON_ANAHTARI"):
        monkeypatch.delenv(ad, raising=False)
    monkeypatch.setenv("SIMULATOR_ANAHTARI", "ha_x")
    monkeypatch.setenv("OPCUA_ADRESI", "opc.tcp://plc:4840/x/")
    monkeypatch.setenv("KUYRUK_DOSYASI", str(tmp_path / "k.sqlite"))
    asyncio.run(baglayici._ana())
    assert calisan == ["OpcUaIzleyici"]


# --- Ortak izleyici: ilk değer ve değişim kuralları -------------------------------------------


def test_ilk_deger_ve_degisim_kurallari():
    k = Kuyruk()
    i = Izleyici("x://y", k)
    s = [T0 + timedelta(seconds=n) for n in range(10)]
    i._izlemeye_basla(None)
    i.degisti("M", "A", False, s[0])  # ilk değer, alarm yok: olay yok
    i.degisti("M", "B", True, s[0])  # ilk değer, süren alarm: başladı
    i.degisti("M", "B", True, s[1])  # değişmedi
    i.degisti("M", "B", False, s[2])
    i.degisti("M", "A", True, s[3])
    assert olaylar(k) == [("alarm_basladi", "M", "B"), ("alarm_bitti", "M", "B"),
                          ("alarm_basladi", "M", "A")]  # fmt: skip
    i.degisti("M", "D", True, s[3])
    k.sil([n for n, _ in k.al(100)])
    i._izlemeye_basla(None)  # yeniden bağlandı
    i.degisti("M", "A", False, s[4])  # önceki bağlantıda sürüyordu, kopukken bitti
    i.degisti("M", "B", False, s[4])  # zaten bitmişti
    i.degisti("M", "D", True, s[5])  # hâlâ sürüyor: yeniden gönderilmez
    i.degisti("M", "C", True, s[4])
    assert olaylar(k) == [("alarm_bitti", "M", "A"), ("alarm_basladi", "M", "C")]
    kimlikler = [o["olay_id"] for _, o in k.al(100)]
    assert len(set(kimlikler)) == 2


def test_beklenen_degerler_gelince_hazir():
    i = Izleyici("x://y", Kuyruk())
    i._izlemeye_basla(2)
    i.degisti("M", "A", False, T0)
    assert not i._hepsi_geldi.is_set()
    i.degisti("M", "A", True, T0)  # aynı anahtar ikinci kez sayılmaz
    assert not i._hepsi_geldi.is_set()
    i.degisti("M", "B", False, T0)
    assert i._hepsi_geldi.is_set()
    i._izlemeye_basla(0)
    assert i._hepsi_geldi.is_set()


def test_uzlasma_kurallari():
    async def senaryo():
        k = Kuyruk()
        cevaplar = []

        async def uzlas():
            cevap = cevaplar.pop(0)
            if isinstance(cevap, Exception):
                raise cevap
            return cevap

        i = Izleyici("x://y", k, uzlas=uzlas)
        i._izlemeye_basla(None)
        i.degisti("M", "BITTI", False, T0)
        i.degisti("M", "SURUYOR", True, T0)
        k.sil([n for n, _ in k.al(10)])
        sunucuda = [{"makine_kodu": "M", "alarm_kodu": a, "baslangic": T0.isoformat()}
                    for a in ("BITTI", "SURUYOR", "BILINMIYOR")]  # fmt: skip
        k.ekle({"olay_id": "bekleyen"})
        await i._uzlas()  # kuyruk boş değil: API'nin durumu eski, uzlaşma ertelenir
        assert not i.uzlasildi and cevaplar == []
        k.sil([n for n, _ in k.al(10)])
        cevaplar.append(httpx.ConnectError("API yok"))
        await i._uzlas()  # API kapalı: sonra yeniden
        assert not i.uzlasildi
        cevaplar.append(sunucuda)
        await i._uzlas()
        assert i.uzlasildi
        gonderilen = [o for _, o in k.al(10)]
        assert [(o["tur"], o["alarm_kodu"]) for o in gonderilen] == [("alarm_bitti", "BITTI")]
        assert gonderilen[0]["olay_id"].startswith("uzlasma:M:BITTI:")

    asyncio.run(senaryo())


# --- Modbus: adres, harita ------------------------------------------------------------------


@pytest.mark.parametrize(
    "adres, beklenen",
    [("modbus://plc", ("plc", 502)), ("modbus://10.0.0.5:5020", ("10.0.0.5", 5020)),
     ("modbus+tcp://plc:1502", ("plc", 1502))],
)  # fmt: skip
def test_modbus_adresi(adres, beklenen):
    assert modbus.adres_coz(adres) == beklenen


@pytest.mark.parametrize("adres", ["http://plc", "modbus://", "modbus://:502", "modbus://h:99999"])
def test_modbus_adresi_hatali(adres):
    with pytest.raises(ValueError):
        modbus.adres_coz(adres)


GECERLI_HARITA = {"makineler": {"P2-HP": {"A101": 0, "A102": 1}, "P2-RB": {"A401": 16}}}


def test_modbus_haritasi_dogrulanir():
    h = modbus.harita_dogrula(GECERLI_HARITA)
    assert h == {
        "birim": 1, "tablo": "coil", "aralik": 0.2,
        "adresler": {0: ("P2-HP", "A101"), 1: ("P2-HP", "A102"), 16: ("P2-RB", "A401")},
    }  # fmt: skip
    h = modbus.harita_dogrula({**GECERLI_HARITA, "birim": 0, "tablo": "discrete", "aralik": 5})
    assert (h["birim"], h["tablo"], h["aralik"]) == (0, "discrete", 5.0)


def _harita(**ek):
    return {**GECERLI_HARITA, **ek}


def _alarm(deger):
    return {"makineler": {"P2-HP": {"A1": deger}}}


@pytest.mark.parametrize(
    "harita, hata",
    [
        (None, "makineler"), ([], "makineler"), ("x", "makineler"), ({}, "makineler"),
        ({"makineler": []}, "makineler"), ({"makineler": {}}, "en az bir"),
        (_harita(tablo="holding"), "tablo"), (_harita(tablo=None), "tablo"),
        (_harita(birim=248), "birim"), (_harita(birim=-1), "birim"),
        (_harita(birim=True), "birim"), (_harita(birim="1"), "birim"),
        (_harita(birim=1.0), "birim"),
        (_harita(aralik=0.01), "aralik"), (_harita(aralik=61), "aralik"),
        (_harita(aralik=True), "aralik"), (_harita(aralik=float("nan")), "aralik"),
        (_harita(aralik=float("inf")), "aralik"), (_harita(aralik="1"), "aralik"),
        ({"makineler": {"P2-HP": {}}}, "boş"), ({"makineler": {"P2-HP": [0]}}, "hatalı"),
        ({"makineler": {"P2 HP": {"A1": 0}}}, "makine kodu"),
        ({"makineler": {"": {"A1": 0}}}, "makine kodu"),
        ({"makineler": {"M" * 21: {"A1": 0}}}, "makine kodu"),
        ({"makineler": {"P2-HP": {"A 1": 0}}}, "alarm kodu"),
        ({"makineler": {"P2-HP": {"": 0}}}, "alarm kodu"),
        (_alarm(-1), "adres"), (_alarm(65536), "adres"), (_alarm(True), "adres"),
        (_alarm(1.0), "adres"), (_alarm("0"), "adres"), (_alarm(None), "adres"),
        ({"makineler": {"P2-HP": {"A1": 0}, "P2-RB": {"A2": 0}}}, "için de"),
    ],
)  # fmt: skip
def test_modbus_haritasi_hatali(harita, hata):
    with pytest.raises(ValueError, match=hata):
        modbus.harita_dogrula(harita)


def test_modbus_haritasi_dosyadan_bom_ile(tmp_path):
    dosya = tmp_path / "harita.json"
    dosya.write_text(json.dumps(GECERLI_HARITA), encoding="utf-8-sig")  # Not Defteri
    assert len(modbus.harita_oku(dosya)["adresler"]) == 3
    dosya.write_text("{bozuk", encoding="utf-8")
    with pytest.raises(ValueError):
        modbus.harita_oku(dosya)
    with pytest.raises(OSError):
        modbus.harita_oku(tmp_path / "yok.json")


JSON = st.recursive(
    st.none() | st.booleans() | st.integers(-10, 70_000) | st.floats() | st.text(max_size=30),
    lambda ic: st.lists(ic, max_size=4) | st.dictionaries(st.text(max_size=25), ic, max_size=4),
    max_leaves=20,
)
HARITA_BENZERI = st.fixed_dictionaries(
    {"makineler": st.dictionaries(
        st.text(max_size=22) | st.from_regex(r"\A[A-Z0-9-]{1,20}\Z"),
        st.dictionaries(st.text(max_size=66) | st.from_regex(r"\A[A-Z0-9]{1,8}\Z"),
                        st.integers(-3, 70_000) | JSON, max_size=6),
        max_size=4)},
    optional={"birim": JSON | st.integers(-1, 250), "tablo": st.sampled_from(["coil", "discrete",
              "x"]) | JSON, "aralik": JSON | st.floats(0, 100)},
)  # fmt: skip


@AYAR
@given(harita=JSON | HARITA_BENZERI)
def test_modbus_haritasi_fuzz(harita):
    """Ne verilirse verilsin ya geçerli bir harita döner ya da anlaşılır ValueError."""
    try:
        h = modbus.harita_dogrula(harita)
    except ValueError:
        return
    assert h["tablo"] in ("coil", "discrete") and 0 <= h["birim"] <= 247
    assert all(0 <= a <= 65535 for a in h["adresler"])
    assert len(set(h["adresler"].values())) == len(h["adresler"])
    for makine, kod in h["adresler"].values():
        assert modbus.MAKINE.match(makine) and modbus.KOD.match(kod)


def test_simulator_modbus_haritasi_gecerli_ve_komut_satirindan_alinir():
    h = modbus.harita_dogrula(modbus_haritasi())
    assert len(h["adresler"]) == 10 and h["adresler"][0] == ("P3-HP", "A101")
    assert h["adresler"][9] == ("P3-TR", "A501")
    cikti = subprocess.run(
        [sys.executable, "-m", "entegrasyon.simulator", "modbus-haritasi"],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout  # fmt: skip
    assert modbus.harita_dogrula(json.loads(cikti)) == h
    # Docker'daki bağlayıcının kullandığı dosya simülatörle aynı kalmalı.
    kok = Path(__file__).resolve().parent.parent
    assert modbus.harita_oku(kok / "entegrasyon" / "pres3-modbus-haritasi.json") == h


# --- Modbus: simülatör sunucusu, bozuk çerçeveler -------------------------------------------


def mbap(pdu: bytes, islem=1, protokol=0, birim=1, uzunluk=None) -> bytes:
    uzunluk = len(pdu) + 1 if uzunluk is None else uzunluk
    return struct.pack(">HHHB", islem, protokol, uzunluk, birim) + pdu


async def ham(port: int, veri: bytes) -> list[tuple[int, int, bytes]]:
    """Ham baytları gönderir, yazmayı kapatır; sunucunun döndüğü çerçeveler (işlem, birim, pdu).
    Her çerçeve geçerli MBAP olmalı."""
    okuyucu, yazici = await asyncio.open_connection("127.0.0.1", port)
    yazici.write(veri)
    await yazici.drain()
    yazici.write_eof()
    gelen = await asyncio.wait_for(okuyucu.read(), 5)
    yazici.close()
    cerceveler = []
    while gelen:
        islem, protokol, uzunluk, birim = struct.unpack(">HHHB", gelen[:7])
        assert protokol == 0 and 2 <= uzunluk <= 254 and len(gelen) >= 6 + uzunluk
        cerceveler.append((islem, birim, gelen[7 : 6 + uzunluk]))
        gelen = gelen[6 + uzunluk :]
    return cerceveler


@pytest.mark.parametrize(
    "istek, cevap",
    [
        (b"\x01\x00\x00\x00\x0a", b"\x01\x02\x09\x02"),  # bobin 0, 3, 9
        (b"\x02\x00\x00\x00\x0a", b"\x02\x02\x09\x02"),  # ayrık giriş: aynı bitler
        (b"\x01\x00\x03\x00\x01", b"\x01\x01\x01"),
        (b"\x01\xff\xff\x00\x01", b"\x01\x01\x01"),  # son adres
        (b"\x01\x00\x00\x07\xd0", b"\x01\xfa" + b"\x09\x02" + bytes(248)),  # 2000 bit
        (b"\x03\x00\x00\x00\x01", b"\x83\x01"),  # tutma yazmacı: desteklenmiyor
        (b"\x2b\x0e\x01\x00", b"\xab\x01"),  # cihaz kimliği: desteklenmiyor
        (b"\x01\x00\x00\x00\x00", b"\x81\x03"),  # 0 bit
        (b"\x01\x00\x00\x07\xd1", b"\x81\x03"),  # 2001 bit
        (b"\x01\xff\xff\x00\x02", b"\x81\x02"),  # adres alanı dışı
        (b"\x01\x00\x00\x00", b"\x81\x03"),  # eksik
        (b"\x01\x00\x00\x00\x01\x00", b"\x81\x03"),  # fazla
        (b"\x05\x00\x07\x12\x34", b"\x85\x03"),  # tek bobin: değer FF00 ya da 0000 olmalı
        (b"\x0f\x00\x00\x00\x0a\x01\xff", b"\x8f\x03"),  # bayt sayısı tutmuyor
        (b"\x0f\x00\x00\x07\xb1\xf7" + bytes(247), b"\x8f\x03"),  # 1969 bobin
        (b"\x0f\xff\xff\x00\x02\x01\x03", b"\x8f\x02"),
        (b"\x0f\x00\x00", b"\x8f\x03"),
    ],
)
def test_modbus_sunucusu_cevaplari(istek, cevap):
    async def senaryo():
        bitler = {0: True, 3: True, 9: True, 65535: True}
        s = await modbus.ModbusSunucusu(bitler, adres=("127.0.0.1", 0)).baslat()
        try:
            assert await ham(s.port, mbap(istek, islem=0xBEEF, birim=7)) == [(0xBEEF, 7, cevap)]
        finally:
            await s.durdur()

    asyncio.run(senaryo())


def test_modbus_sunucusu_yazma_ve_sirali_istekler():
    async def senaryo():
        yazilan = []
        bitler: dict[int, bool] = {}
        s = await modbus.ModbusSunucusu(bitler, lambda a, d: yazilan.append((a, d)),
                                        ("127.0.0.1", 0)).baslat()  # fmt: skip
        try:
            istekler = [
                b"\x05\x00\x07\xff\x00", b"\x0f\x00\x00\x00\x0a\x02\xff\x02",
                b"\x05\x00\x07\x00\x00", b"\x01\x00\x00\x00\x0a",
            ]  # fmt: skip
            cevaplar = await ham(s.port, b"".join(mbap(p, islem=n) for n, p in enumerate(istekler)))
            assert [n for n, _, _ in cevaplar] == [0, 1, 2, 3]  # sıra ve işlem numarası korunur
            assert cevaplar[0][2] == istekler[0] and cevaplar[1][2] == istekler[1][:5]
            assert cevaplar[3][2] == b"\x01\x02\x7f\x02"  # 0-9 açık, 7 kapandı
            assert yazilan[0] == (7, True) and yazilan[-1] == (7, False) and len(yazilan) == 12
        finally:
            await s.durdur()

    asyncio.run(senaryo())


@pytest.mark.parametrize(
    "veri",
    [mbap(b"\x01\x00\x00\x00\x01", protokol=1), mbap(b"", uzunluk=1),
     mbap(b"\x01" * 300, uzunluk=300), b"GET / HTTP/1.1\r\n\r\n", b"\x00\x01\x00", b""],
)  # fmt: skip
def test_modbus_sunucusu_bozuk_cercevede_baglantiyi_kapatir(veri):
    async def senaryo():
        s = await modbus.ModbusSunucusu({}, adres=("127.0.0.1", 0)).baslat()
        try:
            assert await ham(s.port, veri) == []
            # Sunucu ayakta: sonraki bağlantı normal cevap alır.
            assert await ham(s.port, mbap(b"\x01\x00\x00\x00\x01")) == [(1, 1, b"\x01\x01\x00")]
        finally:
            await s.durdur()

    asyncio.run(senaryo())


def test_modbus_sunucusu_fuzz():
    """Rastgele baytlar (bir kısmı geçerli başlıkla) binlerce bağlantıda: sunucu hiç hata
    fırlatmaz, her cevabı geçerli MBAP'tır ve sonunda hâlâ doğru cevap verir."""
    rng = random.Random(1916)
    hatalar = []

    async def senaryo():
        asyncio.get_running_loop().set_exception_handler(lambda _, b: hatalar.append(b))
        s = await modbus.ModbusSunucusu({}, adres=("127.0.0.1", 0)).baslat()
        try:
            for _ in range(FUZZ):
                parcalar = []
                for _ in range(rng.randint(0, 4)):
                    pdu = bytes([rng.choice([1, 2, 5, 15, 3, 0, 0x80, 255])]) + rng.randbytes(
                        rng.randint(0, 260)
                    )
                    if rng.random() < 0.7:
                        parcalar.append(mbap(pdu[:253]))
                    else:
                        parcalar.append(rng.randbytes(rng.randint(0, 40)))
                await ham(s.port, b"".join(parcalar))
            assert await ham(s.port, mbap(b"\x01\x00\x00\x00\x01")) != []
        finally:
            await s.durdur()

    asyncio.run(senaryo())
    assert not hatalar, hatalar[:3]


def test_modbus_sunucusu_bagli_istemci_varken_hemen_kapanir():
    """Python 3.12'de wait_closed açık bağlantıları bekler; durdur() takılmamalı."""

    async def senaryo():
        s = await modbus.ModbusSunucusu({}, adres=("127.0.0.1", 0)).baslat()
        _, yazici = await asyncio.open_connection("127.0.0.1", s.port)
        await asyncio.wait_for(s.durdur(), 3)
        yazici.close()

    asyncio.run(senaryo())


# --- Modbus izleyici: dağınık adresler, okuma hatası, cevapsız PLC -------------------------


class HataliModbus(modbus.ModbusSunucusu):
    """`kip`: None normal, "hata" her okumaya istisna cevabı, "sessiz" hiç cevap yok."""

    kip = None

    def _isle(self, pdu):
        if self.kip == "hata":
            return self._hata(pdu[0], 0x04)
        return super()._isle(pdu)

    async def _baglanti(self, okuyucu, yazici):
        self._baglantilar.add(yazici)
        try:
            while True:
                islem, _, uzunluk, birim = struct.unpack(">HHHB", await okuyucu.readexactly(7))
                pdu = await okuyucu.readexactly(uzunluk - 1)
                if self.kip == "sessiz":
                    continue  # okur ama hiç cevap vermez
                cevap = self._isle(pdu)
                yazici.write(struct.pack(">HHHB", islem, 0, len(cevap) + 1, birim) + cevap)
                await yazici.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._baglantilar.discard(yazici)
            yazici.close()


def test_modbus_izleyici_daginik_adresler_parca_parca_okunur():
    async def senaryo():
        bitler = {1999: True, 4500: True, 65535: True}
        s = await modbus.ModbusSunucusu(bitler, adres=("127.0.0.1", 0)).baslat()
        harita = {"aralik": 0.05, "makineler": {
            "M-1": {"A0": 0, "A1": 1999, "A2": 2000},
            "M-2": {"A3": 4500, "A4": 65535}}}  # fmt: skip
        k, dur = Kuyruk(), asyncio.Event()
        i = modbus.ModbusIzleyici(f"modbus://127.0.0.1:{s.port}", k, harita=harita,
                                  ilk_bekleme=HIZLI)  # fmt: skip
        gorev = asyncio.create_task(i.calis(dur))
        try:
            await kosul(lambda: len(i.durum) == 5, "ilk değerler okunmadı")
            assert i.durum == {("M-1", "A0"): False, ("M-1", "A1"): True, ("M-1", "A2"): False,
                               ("M-2", "A3"): True, ("M-2", "A4"): True}  # fmt: skip
            ilk = s.istek_sayisi
            await asyncio.sleep(0.3)
            assert (s.istek_sayisi - ilk) % 4 == 0 or s.istek_sayisi - ilk > 8  # 4 okuma/tur
            bitler[0], bitler[65535] = True, False
            await kosul(lambda: i.durum[("M-1", "A0")] and not i.durum[("M-2", "A4")])
            assert olaylar(k) == [
                ("alarm_basladi", "M-1", "A1"), ("alarm_basladi", "M-2", "A3"),
                ("alarm_basladi", "M-2", "A4"), ("alarm_basladi", "M-1", "A0"),
                ("alarm_bitti", "M-2", "A4"),
            ]  # fmt: skip
            assert i.baglanti_sayisi == 1
        finally:
            dur.set()
            await asyncio.wait_for(gorev, 5)
            await s.durdur()

    asyncio.run(senaryo())


@pytest.mark.parametrize("kip", ["hata", "sessiz"])
def test_modbus_izleyici_okuma_hatasinda_yeniden_baglanir_yanlis_olay_uretmez(kip):
    async def senaryo():
        bitler = {0: True}
        s = await HataliModbus(bitler, adres=("127.0.0.1", 0)).baslat()
        k, dur = Kuyruk(), asyncio.Event()
        i = modbus.ModbusIzleyici(
            f"modbus://127.0.0.1:{s.port}", k, harita=deneme_haritasi(), zaman_asimi=0.3,
            ilk_bekleme=HIZLI,
        )  # fmt: skip
        gorev = asyncio.create_task(i.calis(dur))
        try:
            await kosul(lambda: i.bagli.is_set())
            assert olaylar(k) == [("alarm_basladi", "P3-HP", "A101")]
            HataliModbus.kip = kip
            ilk = i.baglanti_sayisi
            await kosul(lambda: i.baglanti_sayisi >= ilk + 2, "yeniden bağlanmayı denemedi")
            assert len(k) == 1  # PLC cevap vermiyor diye alarm "bitti" sayılmadı
            HataliModbus.kip = None
            await kosul(lambda: i.bagli.is_set(), "düzelen PLC'ye bağlanmadı")
            bitler[0] = False
            await kosul(lambda: len(k) == 2)
            assert olaylar(k)[-1] == ("alarm_bitti", "P3-HP", "A101")
        finally:
            HataliModbus.kip = None
            dur.set()
            await asyncio.wait_for(gorev, 5)
            await s.durdur()

    asyncio.run(senaryo())


# --- MQTT: adres ve mesaj ------------------------------------------------------------------


def test_mqtt_adresi():
    a = mqtt.adres_coz("mqtt://broker")
    assert (a["host"], a["port"], a["tls"], a["onek"], a["kullanici"]) == (
        "broker", 1883, False, "hata-asistani", None)  # fmt: skip
    a = mqtt.adres_coz("mqtts://broker")
    assert (a["port"], a["tls"]) == (8883, True)
    a = mqtt.adres_coz("mqtt://ali:p%40ss@broker:1884/fabrika/hat3/")
    assert (a["kullanici"], a["parola"], a["port"], a["onek"]) == (
        "ali", "p@ss", 1884, "fabrika/hat3")  # fmt: skip


@pytest.mark.parametrize("adres", ["mqtt:///x", "http://b", "mqtt://b/+/x", "mqtt://b/a/#",
                                   "mqtt://b:99999"])  # fmt: skip
def test_mqtt_adresi_hatali(adres):
    with pytest.raises(ValueError):
        mqtt.adres_coz(adres)


ONEK = "hata-asistani"
KONU = f"{ONEK}/P3-HP/alarm/A101"


@pytest.mark.parametrize(
    "icerik, aktif, zaman",
    [
        (b"1", True, None), (b"0", False, None), (b"true", True, None), (b"FALSE", False, None),
        (b" on \n", True, None), (b"off", False, None), (b"aktif", True, None),
        (b"pasif", False, None), (b'{"aktif": false}', False, None),
        (b'{"aktif": true, "zaman": "2026-10-15T11:00:00+03:00"}', True, T0),
        (b'{"aktif": true, "zaman": "2026-10-15T08:00:00Z"}', True, T0),
        (b'{"aktif": true, "zaman": "2026-10-15T08:00:00"}', True, T0),  # saat dilimsiz: UTC
        (b'{"aktif": true, "zaman": null, "ek": [1, 2]}', True, None),
    ],
)  # fmt: skip
def test_mqtt_mesaji_cozulur(icerik, aktif, zaman):
    makine, kod, a, z = mqtt.mesaj_coz(ONEK, KONU, icerik)
    assert (makine, kod, a, z) == ("P3-HP", "A101", aktif, zaman)
    assert z is None or z.tzinfo is not None


def test_mqtt_cok_seviyeli_onek():
    assert mqtt.mesaj_coz("fab/hat3", "fab/hat3/P3-HP/alarm/A1", b"1")[:3] == ("P3-HP", "A1", True)
    with pytest.raises(ValueError):
        mqtt.mesaj_coz("fab/hat3", "fab/hat4/P3-HP/alarm/A1", b"1")


@pytest.mark.parametrize(
    "konu, icerik",
    [
        ("baska/P3-HP/alarm/A101", b"1"), (f"{ONEK}/P3-HP/alarm", b"1"),
        (f"{ONEK}/P3-HP/uyari/A101", b"1"), (f"{ONEK}/P3-HP/alarm/A101/x", b"1"),
        (f"{ONEK}//alarm/A101", b"1"), (f"{ONEK}/P3 HP/alarm/A101", b"1"),
        (f"{ONEK}/{'M' * 21}/alarm/A101", b"1"), (f"{ONEK}/P3-HP/alarm/A 101", b"1"),
        (f"{ONEK}/P3-HP/alarm/", b"1"), (f"{ONEK}/P3-HP/alarm/{'A' * 65}", b"1"),
        (KONU, b"2"), (KONU, b""), (KONU, b"evet"), (KONU, b"\xff\xfe"), (KONU, b"1" * 1025),
        (KONU, b'{"durum": true}'), (KONU, b'{"aktif": "true"}'), (KONU, b'{"aktif": 1}'),
        (KONU, b'{"aktif": null}'), (KONU, b"[true]"), (KONU, b"{bozuk"),
        (KONU, b'{"aktif": true, "zaman": "dun"}'), (KONU, b'{"aktif": true, "zaman": 5}'),
        (KONU, b"{" + b'"a":[' * 150 + b"1" + b"]" * 150 + b"}"),
    ],
)  # fmt: skip
def test_mqtt_bozuk_mesaj_reddedilir(konu, icerik):
    with pytest.raises((ValueError, UnicodeDecodeError)):
        mqtt.mesaj_coz(ONEK, konu, icerik)


KONU_PARCASI = st.sampled_from(["hata-asistani", "P3-HP", "alarm", "A101", "", "+", "#", "x y"])


@AYAR
@given(
    konu=st.text(max_size=80)
    | st.lists(KONU_PARCASI | st.text(max_size=12), max_size=6).map("/".join),
    icerik=st.binary(max_size=1100) | JSON.map(lambda j: json.dumps(j).encode())
    | st.fixed_dictionaries({"aktif": JSON, "zaman": JSON | st.datetimes().map(str)}).map(
        lambda j: json.dumps(j).encode()),
)  # fmt: skip
def test_mqtt_mesaji_fuzz(konu, icerik):
    try:
        makine, kod, aktif, zaman = mqtt.mesaj_coz(ONEK, konu, icerik)
    except (ValueError, UnicodeDecodeError):
        izleyici = mqtt.MqttIzleyici("mqtt://b", Kuyruk())
        izleyici._mesaj(konu, icerik)  # bağlantıyı koparmaz, sayılır
        assert izleyici.yok_sayilan == 1
        return
    assert mqtt.MAKINE.match(makine) and mqtt.KOD.match(kod) and isinstance(aktif, bool)
    assert zaman is None or zaman.tzinfo is not None


# --- MTConnect: adres ve XML ------------------------------------------------------------------


def test_mtconnect_adresi():
    a = mtconnect.adres_coz("http://ajan:5000")
    assert a == {"temel": "http://ajan:5000", "cihazlar": {}, "seviyeler": {"Fault"},
                 "aralik": 1.0}  # fmt: skip
    a = mtconnect.adres_coz("https://ajan/mtc/?cihaz=VMC-3Axis:P3-HP,Mazak:P1-HP&cihaz=Haas:P2-HP"
                            "&seviye=Fault,Warning&aralik=0.5")  # fmt: skip
    assert a["temel"] == "https://ajan/mtc"
    assert a["cihazlar"] == {"VMC-3Axis": "P3-HP", "Mazak": "P1-HP", "Haas": "P2-HP"}
    assert a["seviyeler"] == {"Fault", "Warning"} and a["aralik"] == 0.5


@pytest.mark.parametrize(
    "sorgu",
    ["?cihaz=VMC", "?cihaz=:P3-HP", "?cihaz=VMC:P3 HP", "?cihaz=VMC:", "?seviye=Error",
     "?seviye=Fault,Normal", "?aralik=0", "?aralik=61", "?aralik=abc", "?aralik=nan",
     "?aralik=inf"],
)  # fmt: skip
def test_mtconnect_adresi_hatali(sorgu):
    with pytest.raises(ValueError):
        mtconnect.adres_coz(f"http://ajan:5000{sorgu}")
    with pytest.raises(ValueError):
        mtconnect.adres_coz("ftp://ajan")


ORNEK_20 = b"""<?xml version="1.0" encoding="UTF-8"?>
<MTConnectStreams xmlns="urn:mtconnect.org:MTConnectStreams:2.0">
  <Header creationTime="2026-10-15T08:00:00Z" sender="ajan" instanceId="1" version="2.0"/>
  <Streams>
    <DeviceStream name="VMC-3Axis" uuid="vmc">
      <ComponentStream component="Controller" componentId="c1">
        <Condition>
          <Fault dataItemId="sistem" timestamp="2026-10-15T08:00:00Z" nativeCode="E 12/3"
                 type="SYSTEM">Sicaklik</Fault>
          <Warning dataItemId="mantik" timestamp="2026-10-15T08:00:01Z" nativeCode="W7"/>
        </Condition>
        <Events><Execution dataItemId="ex" timestamp="2026-10-15T08:00:00Z">ACTIVE</Execution>
        </Events>
      </ComponentStream>
      <ComponentStream component="Linear" componentId="x">
        <Condition>
          <Fault dataItemId="x_servo" timestamp="dun"/>
          <Normal dataItemId="x_sicaklik" timestamp="2026-10-15T08:00:02Z"/>
        </Condition>
      </ComponentStream>
    </DeviceStream>
    <DeviceStream name="Okuma" uuid="ok">
      <ComponentStream component="Controller" componentId="c2">
        <Condition><Unavailable dataItemId="sistem" timestamp="2026-10-15T08:00:00Z"/></Condition>
      </ComponentStream>
    </DeviceStream>
    <DeviceStream name="Haas VF-2" uuid="haas"><ComponentStream component="Controller"
      componentId="c3"><Condition><Fault dataItemId="a" nativeCode="1"/></Condition>
      </ComponentStream></DeviceStream>
  </Streams>
</MTConnectStreams>"""


def test_mtconnect_cevabi_cozulur():
    makineler, aktif, normal = mtconnect.cevap_coz(ORNEK_20, {}, {"Fault"})
    # Okuma: bütün koşullar Unavailable (makineye ulaşılamıyor) -> bilinmiyor. "Haas VF-2"
    # makine kodu olamaz -> atlandı.
    assert makineler == {"VMC-3Axis"}
    assert set(aktif) == {("VMC-3Axis", "E_12_3"), ("VMC-3Axis", "x_servo")}
    assert aktif[("VMC-3Axis", "E_12_3")] == T0
    assert set(normal) == {("VMC-3Axis", "x_sicaklik")}
    _, aktif, _ = mtconnect.cevap_coz(ORNEK_20, {}, {"Fault", "Warning"})
    assert ("VMC-3Axis", "W7") in aktif
    makineler, aktif, _ = mtconnect.cevap_coz(ORNEK_20, {"Haas VF-2": "P2-HP"}, {"Fault"})
    assert makineler == {"P2-HP"} and set(aktif) == {("P2-HP", "1")}  # eşlemeyle yalnızca o


def test_mtconnect_simulator_xml_gidip_gelir():
    xml = mtconnect.akis_xml({"P3-HP": {"A101": T0, "A<&>": T0}, "P3-RB": {}})
    makineler, aktif, normal = mtconnect.cevap_coz(xml, {}, {"Fault"})
    assert makineler == {"P3-HP", "P3-RB"}
    assert aktif == {("P3-HP", "A101"): T0, ("P3-HP", "A___"): T0}
    assert set(normal) == {("P3-RB", "alarm")}


@pytest.mark.parametrize(
    "xml, hata",
    [
        (b"", "XML"), (b"<bozuk", "XML"), (b"json degil", "XML"),
        (b'<?xml version="1.0" encoding="bogus"?><a/>', "XML"),
        (b'<MTConnectError xmlns="urn:mtconnect.org:MTConnectError:1.3"><Errors><Error '
         b'errorCode="NO_DEVICE">Could not find the device</Error></Errors></MTConnectError>',
         "Could not find"),
        (b'<MTConnectDevices xmlns="urn:mtconnect.org:MTConnectDevices:1.3"/>', "kök"),
        (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///c:/windows/win.ini">]>'
         b"<MTConnectStreams>&e;</MTConnectStreams>", "XML"),
        (b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa">'
         + b"".join(b'<!ENTITY %c "%s">' % (98 + n, b"&%c;" % (97 + n) * 10) for n in range(9))
         + b"]><MTConnectStreams>&j;</MTConnectStreams>", "XML"),  # milyar kahkaha
    ],
)  # fmt: skip
def test_mtconnect_bozuk_ya_da_zararli_cevap(xml, hata):
    with pytest.raises(ValueError, match=hata):
        mtconnect.cevap_coz(xml, {}, {"Fault"})


def test_mtconnect_cok_derin_xml():
    derin = b"<MTConnectStreams>" + b"<a>" * 100_000 + b"</a>" * 100_000 + b"</MTConnectStreams>"
    assert mtconnect.cevap_coz(derin, {}, {"Fault"}) == (set(), {}, {})


ETIKETLER = ["MTConnectStreams", "Streams", "DeviceStream", "ComponentStream", "Condition",
             "Fault", "Warning", "Normal", "Unavailable", "Events", "Samples", "x"]  # fmt: skip
OZNITELIK = st.dictionaries(
    st.sampled_from(["name", "nativeCode", "dataItemId", "timestamp", "uuid"]),
    st.text(max_size=30) | st.sampled_from(["P3-HP", "A101", "2026-10-15T08:00:00Z", ""]),
    max_size=4,
)
OGE = st.recursive(
    st.tuples(st.sampled_from(ETIKETLER), OZNITELIK, st.just([])),
    lambda ic: st.tuples(st.sampled_from(ETIKETLER), OZNITELIK, st.lists(ic, max_size=4)),
    max_leaves=25,
)


def _xml(oge, ad_alani: str) -> ET.Element:
    etiket, oznitelik, cocuklar = oge
    e = ET.Element(f"{{{ad_alani}}}{etiket}" if ad_alani else etiket)
    for ad, deger in oznitelik.items():
        e.set(ad, deger)
    e.extend(_xml(c, ad_alani) for c in cocuklar)
    return e


@AYAR
@given(
    oge=OGE.map(lambda o: ("MTConnectStreams", o[1], o[2])) | OGE,
    ad_alani=st.sampled_from(["", "urn:mtconnect.org:MTConnectStreams:1.3",
                              "urn:mtconnect.org:MTConnectStreams:2.0"]),
    cihazlar=st.sampled_from([{}, {"P3-HP": "P3-HP"}, {"x": "M-1"}]),
    seviyeler=st.sampled_from([{"Fault"}, {"Fault", "Warning"}]),
    bozma=st.binary(max_size=8),
)  # fmt: skip
def test_mtconnect_cevabi_fuzz(oge, ad_alani, cihazlar, seviyeler, bozma):
    try:
        xml = ET.tostring(_xml(oge, ad_alani))
    except ValueError:
        return
    for girdi in (xml, xml[: len(xml) // 2] + bozma):
        try:
            makineler, aktif, normal = mtconnect.cevap_coz(girdi, cihazlar, seviyeler)
        except ValueError:
            continue
        for makine, kod in [*aktif, *normal]:
            assert makine in makineler and mtconnect.MAKINE.match(makine)
            assert 1 <= len(kod) <= 64 and not mtconnect.KOD_DISI.search(kod)
        assert all(z.tzinfo is not None for z in [*aktif.values(), *normal.values()])


@AYAR
@given(veri=st.binary(max_size=400))
def test_mtconnect_rastgele_bayt_fuzz(veri):
    try:
        mtconnect.cevap_coz(veri, {}, {"Fault"})
    except ValueError:
        pass


# --- MTConnect izleyici: ajanın her türlü kötü cevabı --------------------------------------


def _akis(alarmlar: dict[str, dict[str, datetime]]) -> bytes:
    return mtconnect.akis_xml(alarmlar)


UNAVAILABLE = (b'<MTConnectStreams xmlns="urn:mtconnect.org:MTConnectStreams:1.3"><Streams>'
               b'<DeviceStream name="P3-HP"><ComponentStream component="Controller"><Condition>'
               b'<Unavailable dataItemId="alarm"/></Condition></ComponentStream></DeviceStream>'
               b"</Streams></MTConnectStreams>")  # fmt: skip


def test_mtconnect_izleyici_kotu_cevaplarda_alarm_bitmis_sayilmaz():
    durum = {"cevap": httpx.Response(200, content=_akis({"P3-HP": {"A101": T0}}))}
    istekler = []

    def isle(istek):
        istekler.append(istek)
        return durum["cevap"]

    async def senaryo():
        k, dur = Kuyruk(), asyncio.Event()
        http = httpx.AsyncClient(transport=httpx.MockTransport(isle))
        i = mtconnect.MTConnectIzleyici("http://ajan:5000/?aralik=1", k, http=http,
                                        ilk_bekleme=HIZLI)  # fmt: skip
        gorev = asyncio.create_task(i.calis(dur))
        try:
            await kosul(lambda: i.bagli.is_set())
            assert olaylar(k) == [("alarm_basladi", "P3-HP", "A101")]
            assert str(istekler[0].url) == "http://ajan:5000/current"
            kotuler = [
                httpx.Response(500), httpx.Response(404), httpx.Response(302, headers={
                    "Location": "http://baska/"}), httpx.Response(200, content=b"<bozuk"),
                httpx.Response(200, content=b'<MTConnectError><Errors><Error errorCode="X">'
                               b"ajan bozuk</Error></Errors></MTConnectError>"),
                httpx.Response(200, content=b"<MTConnectStreams>" + b" " * (6 * 1024 * 1024)),
            ]  # fmt: skip
            for kotu in kotuler:
                ilk = i.baglanti_sayisi
                durum["cevap"] = kotu
                await kosul(lambda ilk=ilk: i.baglanti_sayisi >= ilk + 2, f"{kotu}: yenilenmedi")
                assert len(k) == 1, kotu  # alarm "bitti" sayılmadı
            durum["cevap"] = httpx.Response(200, content=UNAVAILABLE)  # makineye ulaşılamıyor
            await kosul(lambda: i.bagli.is_set())
            await asyncio.sleep(0.3)
            assert len(k) == 1 and i.bilinen_deger(("P3-HP", "A101")) is None
            durum["cevap"] = httpx.Response(200, content=_akis({"P3-HP": {}}))  # makine döndü
            await kosul(lambda: len(k) == 2)
            assert olaylar(k)[-1] == ("alarm_bitti", "P3-HP", "A101")
            assert all(r.url.host == "ajan" for r in istekler)  # yönlendirme izlenmedi
        finally:
            dur.set()
            await asyncio.wait_for(gorev, 5)
            await http.aclose()

    asyncio.run(senaryo())


@pytest.mark.parametrize("tur", ["sessiz", "damla"])
def test_mtconnect_izleyici_takilan_ajanda_yeniden_baglanir(tur):
    """Bağlantıyı kabul edip hiç cevap vermeyen ya da cevabı bayt bayt veren ajan izleyiciyi
    kilitlemez."""

    async def ajan(okuyucu, yazici):
        try:
            await okuyucu.readline()
            if tur == "damla":
                yazici.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
                while True:
                    yazici.write(b" ")
                    await yazici.drain()
                    await asyncio.sleep(0.05)
            await asyncio.sleep(3600)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            yazici.close()

    async def senaryo():
        sunucu = await asyncio.start_server(ajan, "127.0.0.1", 0)
        port = sunucu.sockets[0].getsockname()[1]
        dur = asyncio.Event()
        i = mtconnect.MTConnectIzleyici(f"http://127.0.0.1:{port}", Kuyruk(), zaman_asimi=0.2,
                                        ilk_bekleme=HIZLI)  # fmt: skip
        gorev = asyncio.create_task(i.calis(dur))
        try:
            await kosul(lambda: i.baglanti_sayisi >= 3, "takılan ajanda kilitlendi", 10)
            assert not i.bagli.is_set()
        finally:
            dur.set()
            await asyncio.wait_for(gorev, 5)
            sunucu.close()

    asyncio.run(senaryo())


def test_mtconnect_ajani_yalnizca_get_current():
    async def senaryo():
        a = await mtconnect.MTConnectAjani(lambda: {"P3-HP": {}}, ("127.0.0.1", 0)).baslat()
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{a.port}") as h:
                assert (await h.get("/current")).status_code == 200
                assert (await h.get("/current?at=5")).status_code == 200
                assert (await h.get("/probe")).status_code == 404
                assert (await h.post("/current")).status_code == 405
            # Bağlanıp hiçbir şey göndermeyen istemci varken de durdurulabilir.
            _, yazici = await asyncio.open_connection("127.0.0.1", a.port)
            await asyncio.wait_for(a.durdur(), 3)
            yazici.close()
        finally:
            await a.durdur()

    asyncio.run(senaryo())


# --- MQTT uçtan uca: bozuk yayınlar bağlantıyı koparmaz ----------------------------------------


def test_mqtt_bozuk_yayinlar_yok_sayilir_gecerliler_islenir(istemci, test_veritabani, kaynak):
    import paho.mqtt.client as paho

    bozuklar = [
        (KONU, b"belki"), (KONU, b"\xff\xfe"), (KONU, b"1" * 2000), (KONU, b'{"aktif": "evet"}'),
        (KONU, b'{"aktif": true, "zaman": "dun"}'), (f"{ONEK}/P3 HP/alarm/A101", b"1"),
        (f"{ONEK}/P3-HP/alarm/A 1", b"1"), (KONU, b"{" + b'"a":[' * 150 + b"]" * 150 + b"}"),
    ]  # fmt: skip

    async def senaryo(o: Ortam):
        await o.simulator_ac()
        await o.baglayici_ac()
        izleyici = o.baglayici.izleyici
        yayinci = paho.Client(paho.CallbackAPIVersion.VERSION2)
        yayinci.connect("127.0.0.1", o.portlar["mqtt"])
        yayinci.loop_start()
        try:
            for konu, icerik in bozuklar:
                yayinci.publish(konu, icerik, qos=1)
            await kosul(lambda: izleyici.yok_sayilan == len(bozuklar), "bozuklar sayılmadı")
            yayinci.publish(f"{ONEK}/XX-99/alarm/A1", b"1", qos=1)  # API'de olmayan makine
            await kosul(lambda: o.baglayici.gonderici.atilan == 1, "bilinmeyen makine atılmadı")
            yayinci.publish(f"{ONEK}/P3-HP/alarm/A104", b"1", qos=1)  # zamansız, geçerli
            await kosul(lambda: o.aktifler() == {("P3-HP", "A104")})
            yayinci.publish(f"{ONEK}/P3-HP/alarm/A104", b"0", qos=1)
            await kosul(lambda: not o.aktifler() and not o.acik_arizalar())
        finally:
            yayinci.disconnect()
            yayinci.loop_stop()
        assert izleyici.baglanti_sayisi == 1 and izleyici.bagli.is_set()

    calistir(istemci, test_veritabani, kaynak, senaryo, protokol="mqtt")


# --- Dört protokol aynı PLC'de --------------------------------------------------------------


@pytest.mark.parametrize("tohum", range(3 if YOGUN else 1))
def test_dort_protokol_ayni_plcyi_izler_ayni_sonuca_varir(
    istemci, test_veritabani, kaynaklar, tohum
):
    """Tek simülatör dört protokolü birden konuşur; her protokole ayrı bir kaynakla bağlanan dört
    bağlayıcı yüzlerce rastgele değişimden sonra API'de aynı süren alarmlara varır. Modbus'tan
    yazılan bobin de (FC 5) PLC'ye işler ve diğer üç protokolden görünür."""
    from pymodbus.client import AsyncModbusTcpClient

    kaynak_listesi = {p: kaynaklar() for p in PROTOKOLLER}
    adet = 2000 if YOGUN else 300
    rng = random.Random(f"dort-{tohum}")

    async def ana():
        o = Ortam(istemci, test_veritabani, kaynak_listesi["opcua"], hepsi=True)
        dur = asyncio.Event()
        calisanlar = []
        try:
            sim = await o.simulator_ac()
            for p, (ad, anahtar) in kaynak_listesi.items():
                b = o.baglayici_olustur(Kuyruk(), baglayici_adresi(p, o.portlar), anahtar)
                calisanlar.append((p, ad, b, asyncio.create_task(b.calis(dur))))
            for p, _, b, _ in calisanlar:
                await kosul(lambda b=b: b.izleyici.bagli.is_set(), f"{p} bağlanmadı")
            alarmlar = sorted(sim.dugumler)
            for _ in range(adet):
                await sim.ayarla(*rng.choice(alarmlar), rng.random() < 0.4)
                await asyncio.sleep(rng.choice([0, 0.001, 0.01, 0.03]))
            for kod in ("A401", "A501"):  # P3-RB A401 = bobin 8, P3-TR A501 = bobin 9
                await sim.ayarla("P3-RB" if kod == "A401" else "P3-TR", kod, False)
            modbus_istemci = AsyncModbusTcpClient("127.0.0.1", port=o.portlar["modbus"])
            await modbus_istemci.connect()
            await modbus_istemci.write_coil(9, True, device_id=1)
            modbus_istemci.close()
            son = asyncio.get_running_loop().time() + 5
            while ("P3-TR", "A501") not in await sim.aktifler():
                assert asyncio.get_running_loop().time() < son, "Modbus yazması PLC'ye işlemedi"
                await asyncio.sleep(0.02)
            beklenen = await sim.aktifler()
            for p, ad, b, _ in calisanlar:
                await kosul(
                    lambda ad=ad, b=b: o.aktifler(ad) == beklenen and len(b.kuyruk) == 0,
                    f"{p}: {o.aktifler(ad) ^ beklenen} farklı", 30,
                )  # fmt: skip
                assert {m for _, m, _ in o.acik_arizalar(ad)} == {m for m, _ in beklenen}, p
                assert b.gonderici.atilan == 0, p
        finally:
            dur.set()
            for _, _, b, gorev in calisanlar:
                await asyncio.wait_for(gorev, 10)
                await b.http.aclose()
            if o.sim:
                await o.sim.durdur()
            o.db.close()

    asyncio.run(bekci(ana()))


def test_simulator_eski_opcua_bildirimi_durumu_geri_cevirmez():
    """ayarla() art arda açıp kapatınca sunucu aboneliğinin geç gelen "açık" bildirimi Modbus
    ve MTConnect görünümünü geri çevirmemeli."""

    async def senaryo():
        sim = await Simulator(f"opc.tcp://127.0.0.1:{bos_port()}/t/",
                              modbus=("127.0.0.1", bos_port())).baslat()  # fmt: skip
        try:
            for _ in range(200 if YOGUN else 50):
                await sim.ayarla("P3-HP", "A101", True)
                await sim.ayarla("P3-HP", "A101", False)
            await asyncio.sleep(0.3)  # bildirimler gelsin
            assert sim.degerler[("P3-HP", "A101")][0] is False
            assert sim.modbus_bitleri[0] is False
            assert sim._mtconnect_durumu()["P3-HP"] == {}
        finally:
            await sim.durdur()

    asyncio.run(senaryo())
