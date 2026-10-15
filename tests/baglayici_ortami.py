"""Bağlayıcı testlerinin ortak ortamı: simülatör (istenen protokolle) + bağlayıcı + veritabanı.

Simülatör her testte boş portlarda açılır; bağlayıcı ona seçilen protokolle bağlanır ve olayları
test veritabanına bağlı API'ye gönderir (httpx ASGITransport; ağ yok).
"""

import asyncio
import os
import socket
import sys
import time

import httpx
import psycopg

from entegrasyon.baglayici import Baglayici
from entegrasyon.kuyruk import Kuyruk
from entegrasyon.simulator import Simulator, modbus_haritasi

YOGUN = os.environ.get("HA_YOGUN") == "1"
HIZLI = 0.02  # testlerde bekleme ölçeği (gerçekte 1 sn)
PROTOKOLLER = ["opcua", "modbus", "mqtt", "mtconnect"]
# Modbus zaman damgası taşımaz: olay zamanı okuma anıdır, yeniden bağlanınca kimlik değişir.
ZAMANSIZ = {"modbus"}


def bos_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def deneme_haritasi() -> dict:
    return {**modbus_haritasi(), "aralik": 0.05}


def baglayici_adresi(protokol: str, portlar: dict) -> str:
    p = portlar[protokol]
    return {
        "opcua": f"opc.tcp://127.0.0.1:{p}/test/",
        "modbus": f"modbus://127.0.0.1:{p}",
        "mqtt": f"mqtt://127.0.0.1:{p}/hata-asistani",
        "mtconnect": f"http://127.0.0.1:{p}?aralik=1",
    }[protokol]


class Ortam:
    """Simülatör + bağlayıcı + veritabanı sorguları."""

    def __init__(self, istemci, url, kaynak, kuyruk=None, transport=None, protokol="opcua",
                 hepsi=False):  # fmt: skip
        self.istemci, self.url = istemci, url
        self.ad, self.anahtar = kaynak
        self.protokol = protokol
        acik = PROTOKOLLER if hepsi else {"opcua", protokol}
        self.portlar = {p: bos_port() for p in acik}
        self.adres = baglayici_adresi(protokol, self.portlar)
        self.kuyruk = Kuyruk() if kuyruk is None else kuyruk
        self.transport = transport or httpx.ASGITransport(app=istemci.app)
        self.db = psycopg.connect(url, autocommit=True)
        self.sim: Simulator | None = None
        self.baglayici: Baglayici | None = None
        self.gorev: asyncio.Task | None = None

    async def simulator_ac(self) -> Simulator:
        yerel = "127.0.0.1"
        p = self.portlar
        self.sim = await Simulator(
            f"opc.tcp://127.0.0.1:{p['opcua']}/test/",
            modbus=(yerel, p["modbus"]) if "modbus" in p else None,
            mqtt=(yerel, p["mqtt"]) if "mqtt" in p else None,
            mtconnect=(yerel, p["mtconnect"]) if "mtconnect" in p else None,
        ).baslat()
        return self.sim

    def baglayici_olustur(self, kuyruk=None, adres=None, anahtar=None) -> Baglayici:
        http = httpx.AsyncClient(transport=self.transport, base_url="http://test")
        return Baglayici(
            adres or self.adres, "http://test", anahtar or self.anahtar,
            self.kuyruk if kuyruk is None else kuyruk, http, ilk_bekleme=HIZLI,
            modbus_haritasi=deneme_haritasi(),
        )  # fmt: skip

    async def baglayici_ac(self, kuyruk=None, bekle=True):
        self.dur = asyncio.Event()
        self.baglayici = self.baglayici_olustur(kuyruk)
        self.gorev = asyncio.create_task(self.baglayici.calis(self.dur))
        if bekle:
            await kosul(lambda: self.baglayici.izleyici.bagli.is_set(), "bağlayıcı bağlanmadı")
            await kosul(lambda: self.baglayici.izleyici.uzlasildi, "uzlaşma bitmedi")

    async def baglayici_kapat(self):
        self.dur.set()
        await asyncio.wait_for(self.gorev, 10)
        await self.baglayici.http.aclose()

    async def kapat(self):
        if self.baglayici and self.gorev and not self.gorev.done():
            await self.baglayici_kapat()
        if self.sim:
            await self.sim.durdur()
        self.db.close()

    def acik_arizalar(self, ad=None) -> list[tuple]:
        return self.db.execute(
            "SELECT r.id, m.kod, r.aciklama FROM ariza_kayitlari r "
            "JOIN makineler m ON m.id = r.makine_id WHERE r.bitis IS NULL AND EXISTS "
            "(SELECT 1 FROM entegrasyon_olaylari o WHERE o.kaynak = %s AND o.ariza_id = r.id) "
            "ORDER BY r.id",
            [ad or self.ad],
        ).fetchall()

    def aktifler(self, ad=None) -> set[tuple[str, str]]:
        return {
            (m, k)
            for m, k in self.db.execute(
                "SELECT m.kod, a.alarm_kodu FROM aktif_alarmlar a "
                "JOIN makineler m ON m.id = a.makine_id WHERE a.kaynak = %s",
                [ad or self.ad],
            )
        }

    def olaylar(self, ad=None) -> list[tuple]:
        return self.db.execute(
            "SELECT tur, alarm_kodu, sonuc FROM entegrasyon_olaylari WHERE kaynak = %s ORDER BY id",
            [ad or self.ad],
        ).fetchall()


async def kosul(fonksiyon, mesaj="koşul sağlanmadı", sure=15.0):
    son = time.monotonic() + sure
    while not fonksiyon():
        if time.monotonic() > son:
            raise AssertionError(mesaj)
        await asyncio.sleep(0.02)


SENARYO_SINIRI = 600.0 if YOGUN else 120.0


def gorev_dokumu() -> str:
    """Bütün asyncio görevlerinin nerede beklediği (takılan senaryoyu teşhis etmek için)."""
    satirlar = []
    for gorev in asyncio.all_tasks():
        satirlar.append(f"--- {gorev.get_name()} {gorev.get_coro()!r}")
        for cerceve in gorev.get_stack(limit=12):
            kod = cerceve.f_code
            satirlar.append(f"    {kod.co_filename}:{cerceve.f_lineno} {kod.co_name}")
    return "\n".join(satirlar)


async def bekci(ana, sinir: float = SENARYO_SINIRI):
    """Senaryo `sinir` saniyede bitmezse görev dökümünü yazar (pytest'in yakalamasını aşarak) ve
    testi düşürür: süresiz bekleyen bir await CI'ı sessizce kilitlemesin."""
    gorev = asyncio.ensure_future(ana)
    bitti, _ = await asyncio.wait({gorev}, timeout=sinir)
    if not bitti:
        dokum = gorev_dokumu()
        sys.__stderr__.write(f"\nSENARYO TAKILDI ({sinir:.0f} sn):\n{dokum}\n")
        sys.__stderr__.flush()
        gorev.cancel()
        raise AssertionError(f"senaryo {sinir:.0f} sn'de bitmedi:\n{dokum}")
    return gorev.result()


def calistir(istemci, url, kaynak, senaryo, **ayar):
    async def ana():
        ortam = Ortam(istemci, url, kaynak, **ayar)
        try:
            await senaryo(ortam)
        finally:
            await ortam.kapat()

    asyncio.run(bekci(ana()))


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
