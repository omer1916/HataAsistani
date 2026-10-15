"""Pres 3 hattının PLC'si gibi davranan OPC UA sunucusu (demo ve testler için).

Adres alanı bağlayıcının beklediği yapıdadır: Objects/<makine>/Alarmlar/<kod> (bool). Alarm
kodları pres panelinin kodlarıdır (PRES-BK-01 s. 2): hidrolik pres A1xx-A3xx, rulo besleme A401,
transfer robotu A501.

    python -m entegrasyon.simulator sunucu                    # sunucuyu açar
    python -m entegrasyon.simulator sunucu --rastgele 600     # ~10 dakikada bir rastgele alarm
    python -m entegrasyon.simulator alarm P3-HP A101          # alarm verir
    python -m entegrasyon.simulator alarm P3-HP A101 --bitir  # alarmı bitirir
    python -m entegrasyon.simulator durum                     # süren alarmlar

Demo için şifreleme yoktur (NoSecurity). Gerçek kurulumda OPC UA, sertifikalı ve şifreli
(Basic256Sha256, SignAndEncrypt) çalıştırılır.
"""

import argparse
import asyncio
import logging
import random
import sys
from datetime import UTC, datetime

from asyncua import Client, Server, ua

from entegrasyon.opcua import ALARM_KLASORU

AD_ALANI = "urn:hata-asistani:pres3-plc"
VARSAYILAN_ADRES = "opc.tcp://0.0.0.0:4840/hata-asistani/"
ISTEMCI_ADRESI = "opc.tcp://127.0.0.1:4840/hata-asistani/"
MAKINELER = {
    "P3-HP": ["A101", "A102", "A103", "A104", "A201", "A202", "A301", "A302"],
    "P3-RB": ["A401"],
    "P3-TR": ["A501"],
}
gunluk = logging.getLogger("entegrasyon.simulator")


def _deger(aktif: bool) -> ua.DataValue:
    return ua.DataValue(
        ua.Variant(aktif, ua.VariantType.Boolean), SourceTimestamp=datetime.now(UTC)
    )


class Simulator:
    def __init__(self, adres: str = VARSAYILAN_ADRES, makineler: dict | None = None):
        self.adres, self.makineler = adres, makineler or MAKINELER
        self.dugumler: dict[tuple[str, str], object] = {}
        self.sunucu: Server | None = None

    async def baslat(self) -> "Simulator":
        sunucu = Server()
        await sunucu.init()
        sunucu.set_endpoint(self.adres)
        sunucu.set_server_name("Hata Asistanı · Pres 3 PLC simülatörü")
        sunucu.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        ad_alani = await sunucu.register_namespace(AD_ALANI)
        for makine, kodlar in self.makineler.items():
            nesne = await sunucu.nodes.objects.add_object(ad_alani, makine)
            klasor = await nesne.add_folder(ad_alani, ALARM_KLASORU)
            for kod in kodlar:
                dugum = await klasor.add_variable(ad_alani, kod, False, ua.VariantType.Boolean)
                await dugum.set_writable()  # demo: istemci alarm verebilsin
                self.dugumler[(makine, kod)] = dugum
        await sunucu.start()
        self.sunucu = sunucu
        return self

    async def durdur(self) -> None:
        if self.sunucu is not None:
            await self.sunucu.stop()
            self.sunucu = None

    async def ayarla(self, makine: str, kod: str, aktif: bool) -> None:
        await self.dugumler[(makine, kod)].write_value(_deger(aktif))

    async def aktifler(self) -> set[tuple[str, str]]:
        return {k for k, d in self.dugumler.items() if await d.read_value()}

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


async def _sunucu(adres: str, rastgele: float | None) -> None:
    sim = await Simulator(adres).baslat()
    print(f"PLC simülatörü açık: {adres} ({sum(map(len, MAKINELER.values()))} alarm)", flush=True)
    dur = asyncio.Event()
    try:
        if rastgele:
            await sim.rastgele(rastgele, dur, random.Random())
        else:
            await dur.wait()
    finally:
        await sim.durdur()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("asyncua").setLevel(logging.ERROR)
    ayrac = argparse.ArgumentParser(description="Pres 3 PLC simülatörü (OPC UA)")
    alt = ayrac.add_subparsers(dest="komut", required=True)
    s = alt.add_parser("sunucu", help="sunucuyu açar")
    s.add_argument("--adres", default=VARSAYILAN_ADRES)
    s.add_argument("--rastgele", type=float, metavar="SANIYE", help="ortalama alarm aralığı")
    a = alt.add_parser("alarm", help="alarm verir ya da bitirir")
    a.add_argument("makine", choices=sorted(MAKINELER))
    a.add_argument("kod")
    a.add_argument("--bitir", action="store_true")
    a.add_argument("--adres", default=ISTEMCI_ADRESI)
    d = alt.add_parser("durum", help="süren alarmlar")
    d.add_argument("--adres", default=ISTEMCI_ADRESI)
    args = ayrac.parse_args()

    if args.komut == "sunucu":
        try:
            asyncio.run(_sunucu(args.adres, args.rastgele))
        except KeyboardInterrupt:
            pass
    elif args.komut == "alarm":
        if args.kod not in MAKINELER[args.makine]:
            sys.exit(f"{args.makine} için alarm kodları: {', '.join(MAKINELER[args.makine])}")
        asyncio.run(alarm_ayarla(args.adres, args.makine, args.kod, not args.bitir))
        print(f"{args.makine} {args.kod}: {'bitti' if args.bitir else 'alarm verildi'}")
    else:
        aktifler = asyncio.run(aktif_alarmlar(args.adres))
        print("\n".join(f"{m} {k}" for m, k in aktifler) or "Süren alarm yok.")


if __name__ == "__main__":
    main()
