"""Pres 3 hattının PLC'si gibi davranan simülatör (demo ve testler için); dört protokol konuşur.

Durum OPC UA düğümlerindedir (Objects/<makine>/Alarmlar/<kod>, bool); istenirse aynı durum
Modbus bitlerinden, MQTT mesajlarından ve bir MTConnect ajanından da okunur. Alarm kodları pres
panelinin kodlarıdır (PRES-BK-01 s. 2): hidrolik pres A1xx-A3xx, rulo besleme A401, transfer
robotu A501.

    python -m entegrasyon.simulator sunucu                    # yalnızca OPC UA (4840)
    python -m entegrasyon.simulator sunucu --hepsi            # + Modbus, MQTT, MTConnect
    python -m entegrasyon.simulator sunucu --rastgele 600     # ~10 dakikada bir rastgele alarm
    python -m entegrasyon.simulator alarm P3-HP A101          # alarm verir (OPC UA ile yazar)
    python -m entegrasyon.simulator alarm P3-HP A101 --bitir  # alarmı bitirir
    python -m entegrasyon.simulator durum                     # süren alarmlar
    python -m entegrasyon.simulator modbus-haritasi           # bağlayıcının Modbus haritası

Demo için şifreleme yoktur (OPC UA NoSecurity, MQTT anonim). Gerçek kurulumda OPC UA sertifikalı ve
şifreli (Basic256Sha256), MQTT TLS ve parolayla çalıştırılır.
"""

import argparse
import asyncio
import json
import logging
import random
import sys
import threading
from datetime import UTC, datetime

from asyncua import Client, Server, ua

from entegrasyon.izleyici import utc
from entegrasyon.modbus import ModbusSunucusu
from entegrasyon.mtconnect import MTConnectAjani
from entegrasyon.opcua import ALARM_KLASORU

AD_ALANI = "urn:hata-asistani:pres3-plc"
VARSAYILAN_ADRES = "opc.tcp://0.0.0.0:4840/hata-asistani/"
ISTEMCI_ADRESI = "opc.tcp://127.0.0.1:4840/hata-asistani/"
MAKINELER = {
    "P3-HP": ["A101", "A102", "A103", "A104", "A201", "A202", "A301", "A302"],
    "P3-RB": ["A401"],
    "P3-TR": ["A501"],
}
MQTT_ONEKI = "hata-asistani"
gunluk = logging.getLogger("entegrasyon.simulator")


def _deger(aktif: bool) -> ua.DataValue:
    return ua.DataValue(
        ua.Variant(aktif, ua.VariantType.Boolean), SourceTimestamp=datetime.now(UTC)
    )


def modbus_haritasi(makineler: dict | None = None) -> dict:
    """Alarmlar sırayla 0, 1, 2 ... bobinlerine: P3-HP A101 = 0, ... P3-TR A501 = 9."""
    makineler = makineler or MAKINELER
    harita, adres = {}, 0
    for makine, kodlar in makineler.items():
        harita[makine] = {}
        for kod in kodlar:
            harita[makine][kod] = adres
            adres += 1
    return {"birim": 1, "tablo": "coil", "aralik": 0.2, "makineler": harita}


class Simulator:
    def __init__(
        self,
        adres: str = VARSAYILAN_ADRES,
        makineler: dict | None = None,
        modbus: tuple[str, int] | None = None,
        mqtt: tuple[str, int] | None = None,
        mtconnect: tuple[str, int] | None = None,
    ):
        self.adres, self.makineler = adres, makineler or MAKINELER
        self.modbus_adresi, self.mqtt_adresi, self.mtconnect_adresi = modbus, mqtt, mtconnect
        self.dugumler: dict[tuple[str, str], object] = {}
        self.degerler: dict[tuple[str, str], tuple[bool, datetime]] = {}
        self.sunucu: Server | None = None
        self.harita = modbus_haritasi(self.makineler)
        self._bit_anahtari = {
            adres: (m, k)
            for m, kodlar in self.harita["makineler"].items()
            for k, adres in kodlar.items()
        }
        self.modbus_bitleri: dict[int, bool] = {}
        self.modbus: ModbusSunucusu | None = None
        self.mtconnect: MTConnectAjani | None = None
        self._mqtt_araci = None
        self._yayinci = None  # paho istemcisi
        self._yayin_kilidi = threading.Lock()

    # --- Açma, kapama -------------------------------------------------------------------------

    async def baslat(self) -> "Simulator":
        sunucu = Server()
        await sunucu.init()
        sunucu.set_endpoint(self.adres)
        sunucu.set_server_name("Hata Asistanı · Pres 3 PLC simülatörü")
        sunucu.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        ad_alani = await sunucu.register_namespace(AD_ALANI)
        simdi = datetime.now(UTC)
        for makine, kodlar in self.makineler.items():
            nesne = await sunucu.nodes.objects.add_object(ad_alani, makine)
            klasor = await nesne.add_folder(ad_alani, ALARM_KLASORU)
            for kod in kodlar:
                dugum = await klasor.add_variable(ad_alani, kod, False, ua.VariantType.Boolean)
                await dugum.set_writable()  # demo: istemci alarm verebilsin
                self.dugumler[(makine, kod)] = dugum
                self.degerler[(makine, kod)] = (False, simdi)
        await sunucu.start()
        self.sunucu = sunucu
        # İstemcinin (alarm komutu) yazdığı değerler de diğer protokollere yansısın.
        simulator = self

        class Isleyici:
            def datachange_notification(self, node, deger, veri):
                anahtar = simulator._dugum_anahtari.get(node.nodeid)
                if anahtar is None:
                    return
                zaman = utc(veri.monitored_item.Value.SourceTimestamp)
                if zaman < simulator.degerler[anahtar][1]:
                    return  # ayarla() daha yeni değeri yazdı; bu bildirim eski
                simulator._guncelle(anahtar, bool(deger), zaman)

        self._dugum_anahtari = {d.nodeid: k for k, d in self.dugumler.items()}

        abonelik = await sunucu.create_subscription(20, Isleyici())
        await abonelik.subscribe_data_change(list(self.dugumler.values()))

        if self.modbus_adresi:
            self.modbus = await ModbusSunucusu(
                self.modbus_bitleri, self._modbus_yazildi, self.modbus_adresi
            ).baslat()
        if self.mtconnect_adresi:
            self.mtconnect = await MTConnectAjani(
                self._mtconnect_durumu, self.mtconnect_adresi
            ).baslat()
        if self.mqtt_adresi:
            await self._mqtt_baslat()
        return self

    async def durdur(self) -> None:
        if self.modbus:
            await self.modbus.durdur()
            self.modbus = None
        if self.mtconnect:
            await self.mtconnect.durdur()
            self.mtconnect = None
        if self._yayinci is not None:
            yayinci, self._yayinci = self._yayinci, None
            yayinci.disconnect()
            await asyncio.get_running_loop().run_in_executor(None, yayinci.loop_stop)
        if self._mqtt_araci:
            await self._mqtt_araci.shutdown()
            self._mqtt_araci = None
        if self.sunucu is not None:
            await self.sunucu.stop()
            self.sunucu = None

    # --- Durum ----------------------------------------------------------------------------------

    async def ayarla(self, makine: str, kod: str, aktif: bool) -> None:
        deger = _deger(aktif)
        await self.dugumler[(makine, kod)].write_value(deger)
        self._guncelle((makine, kod), aktif, deger.SourceTimestamp)

    def _guncelle(self, anahtar: tuple[str, str], aktif: bool, zaman: datetime) -> None:
        if self.degerler.get(anahtar, (None,))[0] == aktif:
            return
        adres = self.harita["makineler"][anahtar[0]][anahtar[1]]
        self.modbus_bitleri[adres] = aktif
        with self._yayin_kilidi:  # MQTT yeniden bağlanırken baştan yayınla karışmasın
            self.degerler[anahtar] = (aktif, zaman)
            self._yayinla(anahtar, aktif, zaman)

    async def aktifler(self) -> set[tuple[str, str]]:
        return {k for k, d in self.dugumler.items() if await d.read_value()}

    def _modbus_yazildi(self, adres: int, deger: bool) -> None:
        if adres in self._bit_anahtari:
            asyncio.ensure_future(self.ayarla(*self._bit_anahtari[adres], deger))

    def _mtconnect_durumu(self) -> dict:
        cihazlar = {m: {} for m in self.makineler}
        for (makine, kod), (aktif, zaman) in self.degerler.items():
            if aktif:
                cihazlar[makine][kod] = zaman
        return cihazlar

    def _yayinla(self, anahtar: tuple[str, str], aktif: bool, zaman: datetime) -> None:
        if self._yayinci is not None:
            icerik = json.dumps({"aktif": aktif, "zaman": zaman.isoformat()})
            makine, kod = anahtar
            self._yayinci.publish(f"{MQTT_ONEKI}/{makine}/alarm/{kod}", icerik, qos=1, retain=True)

    async def _mqtt_baslat(self) -> None:
        import paho.mqtt.client as paho
        from amqtt.broker import Broker

        host, port = self.mqtt_adresi
        self._mqtt_araci = Broker(
            {"listeners": {"default": {"type": "tcp", "bind": f"{host}:{port}"}},
             # Yalnızca anonim giriş (demo); paket günlüğü ve $SYS yayınları kapalı.
             "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {
                 "allow_anonymous": True}}},
        )  # fmt: skip
        await self._mqtt_araci.start()
        baglanilacak = "127.0.0.1" if host in ("0.0.0.0", "") else host

        def baglandi(istemci, *_):
            # Her bağlanışta bütün durum baştan (retained): kopukken kaybolan yayın kalmasın.
            with self._yayin_kilidi:
                for anahtar, (aktif, zaman) in list(self.degerler.items()):
                    self._yayinla(anahtar, aktif, zaman)

        # paho'nun kendi ağ iş parçacığı (bkz. entegrasyon/mqtt.py); yeniden bağlanmayı o yapar.
        yayinci = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=f"pres3-plc-{id(self)}")
        yayinci.on_connect = baglandi
        yayinci.reconnect_delay_set(0.1, 2)
        yayinci.connect_async(baglanilacak, port)
        self._yayinci = yayinci
        yayinci.loop_start()

    async def rastgele(self, aralik: float, dur: asyncio.Event, rng: random.Random) -> None:
        """Ortalama `aralik` saniyede bir rastgele bir alarm verir; alarm 10-120 sn sürer."""
        while not dur.is_set():
            try:
                await asyncio.wait_for(dur.wait(), timeout=rng.expovariate(1 / aralik))
                return
            except TimeoutError:
                pass
            makine, kod = rng.choice(sorted(self.dugumler))
            await self.ayarla(makine, kod, True)
            gunluk.info("rastgele alarm: %s %s", makine, kod)
            asyncio.get_running_loop().call_later(
                rng.uniform(10, 120),
                lambda m=makine, k=kod: asyncio.ensure_future(self.ayarla(m, k, False)),
            )


async def _dugum(istemci: Client, makine: str, kod: str):
    ad_alani = await istemci.get_namespace_index(AD_ALANI)
    yol = [f"{ad_alani}:{makine}", f"{ad_alani}:{ALARM_KLASORU}", f"{ad_alani}:{kod}"]
    return await istemci.nodes.objects.get_child(yol)


async def alarm_ayarla(adres: str, makine: str, kod: str, aktif: bool) -> None:
    """İstemci olarak bağlanıp alarm değişkenini yazar (çalışan simülatörde alarm vermek için)."""
    async with Client(adres, timeout=5) as istemci:
        await (await _dugum(istemci, makine, kod)).write_value(_deger(aktif))


async def aktif_alarmlar(adres: str) -> list[tuple[str, str]]:
    async with Client(adres, timeout=5) as istemci:
        sonuc = []
        for makine, kodlar in MAKINELER.items():
            for kod in kodlar:
                if await (await _dugum(istemci, makine, kod)).read_value():
                    sonuc.append((makine, kod))
        return sonuc


async def _sunucu(args) -> None:
    host = "0.0.0.0"
    sim = await Simulator(
        args.adres,
        modbus=(host, args.modbus_portu) if args.hepsi else None,
        mqtt=(host, args.mqtt_portu) if args.hepsi else None,
        mtconnect=(host, args.mtconnect_portu) if args.hepsi else None,
    ).baslat()
    protokoller = [f"OPC UA {args.adres}"]
    if args.hepsi:
        protokoller += [f"Modbus :{args.modbus_portu}", f"MQTT :{args.mqtt_portu}",
                        f"MTConnect :{args.mtconnect_portu}"]  # fmt: skip
    print(f"PLC simülatörü açık ({', '.join(protokoller)}), "
          f"{sum(map(len, MAKINELER.values()))} alarm", flush=True)  # fmt: skip
    dur = asyncio.Event()
    try:
        if args.rastgele:
            await sim.rastgele(args.rastgele, dur, random.Random())
        else:
            await dur.wait()
    finally:
        await sim.durdur()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    for gurultulu in ("asyncua", "amqtt", "transitions"):
        logging.getLogger(gurultulu).setLevel(logging.ERROR)
    ayrac = argparse.ArgumentParser(description="Pres 3 PLC simülatörü")
    alt = ayrac.add_subparsers(dest="komut", required=True)
    s = alt.add_parser("sunucu", help="sunucuyu açar")
    s.add_argument("--adres", default=VARSAYILAN_ADRES)
    s.add_argument("--hepsi", action="store_true", help="Modbus, MQTT ve MTConnect de")
    s.add_argument("--modbus-portu", type=int, default=5020)
    s.add_argument("--mqtt-portu", type=int, default=1883)
    s.add_argument("--mtconnect-portu", type=int, default=5000)
    s.add_argument("--rastgele", type=float, metavar="SANIYE", help="ortalama alarm aralığı")
    a = alt.add_parser("alarm", help="alarm verir ya da bitirir")
    a.add_argument("makine", choices=sorted(MAKINELER))
    a.add_argument("kod")
    a.add_argument("--bitir", action="store_true")
    a.add_argument("--adres", default=ISTEMCI_ADRESI)
    d = alt.add_parser("durum", help="süren alarmlar")
    d.add_argument("--adres", default=ISTEMCI_ADRESI)
    alt.add_parser("modbus-haritasi", help="bağlayıcı için Modbus haritası (JSON)")
    args = ayrac.parse_args()

    if args.komut == "sunucu":
        try:
            asyncio.run(_sunucu(args))
        except KeyboardInterrupt:
            pass
    elif args.komut == "alarm":
        if args.kod not in MAKINELER[args.makine]:
            sys.exit(f"{args.makine} için alarm kodları: {', '.join(MAKINELER[args.makine])}")
        asyncio.run(alarm_ayarla(args.adres, args.makine, args.kod, not args.bitir))
        print(f"{args.makine} {args.kod}: {'bitti' if args.bitir else 'alarm verildi'}")
    elif args.komut == "modbus-haritasi":
        print(json.dumps(modbus_haritasi(), indent=2, ensure_ascii=False))
    else:
        aktifler = asyncio.run(aktif_alarmlar(args.adres))
        print("\n".join(f"{m} {k}" for m, k in aktifler) or "Süren alarm yok.")


if __name__ == "__main__":
    main()
