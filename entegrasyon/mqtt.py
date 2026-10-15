"""MQTT: IoT cihazlarının, ağ geçitlerinin (Node-RED, Raspberry Pi) yayınladığı alarmları dinler.

Konu kuralı: <önek>/<makine kodu>/alarm/<alarm kodu> (önek adresin yolu, varsayılan
"hata-asistani"). İçerik "1"/"0" (true/false, on/off da olur) ya da
{"aktif": true, "zaman": "2026-10-15T11:36:18+03:00"}. Cihaz son durumu "retained" yayınlarsa
bağlayıcı bağlanır bağlanmaz bilir; zaman gönderirse yeniden bağlanınca aynı olay aynı kimlikle
gelir (göndermezse alınma anı kullanılır).

Bozuk konu ya da içerik günlüğe yazılıp yok sayılır; izleyici çökmez. Adres örneği:
mqtt://kullanici:parola@broker:1883/fabrika
"""

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from urllib.parse import unquote, urlparse

import paho.mqtt.client as paho

from entegrasyon.izleyici import Izleyici, utc

gunluk = logging.getLogger("entegrasyon.mqtt")
VARSAYILAN_ONEK = "hata-asistani"
MAKINE = re.compile(r"^[A-Za-z0-9-]{1,20}$")
KOD = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
DOGRU, YANLIS = {"1", "true", "on", "aktif"}, {"0", "false", "off", "pasif"}
ILK_DEGER_SURESI = 1.0  # retained mesajların gelmesi için


def adres_coz(adres: str) -> dict:
    parca = urlparse(adres)
    if parca.scheme not in ("mqtt", "mqtts") or not parca.hostname:
        raise ValueError(f"MQTT adresi mqtt://host:port/önek olmalı: {adres}")
    onek = parca.path.strip("/") or VARSAYILAN_ONEK
    if "+" in onek or "#" in adres:
        raise ValueError("MQTT öneki joker karakter içeremez")
    return {
        "host": parca.hostname,
        "port": parca.port or (8883 if parca.scheme == "mqtts" else 1883),
        "tls": parca.scheme == "mqtts",
        "kullanici": unquote(parca.username) if parca.username else None,
        "parola": unquote(parca.password) if parca.password else None,
        "onek": onek,
    }


def mesaj_coz(onek: str, konu: str, icerik: bytes) -> tuple[str, str, bool, datetime | None]:
    """(makine, kod, aktif, zaman); konu ya da içerik bozuksa ValueError."""
    parcalar = konu.split("/")
    onek_parcalari = onek.split("/")
    if (
        len(parcalar) != len(onek_parcalari) + 3
        or parcalar[: len(onek_parcalari)] != onek_parcalari
        or parcalar[-2] != "alarm"
    ):
        raise ValueError(f"konu kurala uymuyor: {konu[:100]}")
    makine, kod = parcalar[-3], parcalar[-1]
    if not MAKINE.match(makine) or not KOD.match(kod):
        raise ValueError(f"makine ya da alarm kodu geçersiz: {konu[:100]}")
    if len(icerik) > 1024:
        raise ValueError("içerik çok uzun")
    metin = icerik.decode("utf-8", errors="strict").strip()
    zaman = None
    if metin.startswith("{"):
        veri = json.loads(metin)
        aktif = veri.get("aktif") if isinstance(veri, dict) else None
        if not isinstance(aktif, bool):
            raise ValueError('JSON içerikte "aktif" true ya da false olmalı')
        if veri.get("zaman") is not None:
            zaman = utc(datetime.fromisoformat(str(veri["zaman"])))
    elif metin.lower() in DOGRU:
        aktif = True
    elif metin.lower() in YANLIS:
        aktif = False
    else:
        raise ValueError(f"içerik anlaşılmadı: {metin[:40]!r}")
    return makine, kod, aktif, zaman


class MqttIzleyici(Izleyici):
    """paho'nun kendi ağ iş parçacığıyla çalışır; gelen mesajlar olay döngüsüne
    call_soon_threadsafe ile aktarılır. Soketler asyncio'ya kaydedilmez: Windows'ta da her olay
    döngüsünde çalışır (aiomqtt'nin add_reader/add_writer yolu, bağlantı kurulurken kapanan
    sokette Windows'un select'ini çökertiyordu)."""

    PROTOKOL = "MQTT"

    def __init__(self, adres: str, *args, **kwargs):
        super().__init__(adres, *args, **kwargs)
        self.ayar = adres_coz(adres)
        self.yok_sayilan = 0  # bozuk mesaj sayısı (testler ve izleme için)

    def _istemci(self, dongu, abone: asyncio.Event, koptu: asyncio.Event, hata: list):
        a = self.ayar

        def aktar(fonksiyon, *args):
            # paho iş parçacığından olay döngüsüne; döngü kapandıysa (izleyici durdu) sessizce.
            try:
                dongu.call_soon_threadsafe(fonksiyon, *args)
            except RuntimeError:
                pass

        def baglandi(istemci, _veri, _bayraklar, sebep, _ozellik):
            if sebep.is_failure:
                hata.append(f"aracı bağlantıyı reddetti: {sebep}")
                aktar(koptu.set)
            else:
                istemci.subscribe(f"{a['onek']}/+/alarm/+", qos=1)

        def abone_oldu(_istemci, _veri, _mid, sebepler, _ozellik):
            if any(s.is_failure for s in sebepler):
                hata.append(f"abonelik reddedildi: {sebepler[0]}")
                aktar(koptu.set)
            else:
                aktar(abone.set)

        istemci = paho.Client(paho.CallbackAPIVersion.VERSION2, clean_session=True)
        istemci.on_connect = baglandi
        istemci.on_subscribe = abone_oldu
        istemci.on_message = lambda _i, _v, m: aktar(self._mesaj, m.topic, bytes(m.payload))
        istemci.on_disconnect = lambda *_: aktar(koptu.set)
        istemci.connect_timeout = self.zaman_asimi
        if a["kullanici"]:
            istemci.username_pw_set(a["kullanici"], a["parola"])
        if a["tls"]:
            istemci.tls_set()
        return istemci

    async def _baglan_ve_izle(self, dur: asyncio.Event) -> None:
        a = self.ayar
        dongu = asyncio.get_running_loop()
        abone, koptu, hata = asyncio.Event(), asyncio.Event(), []
        istemci = self._istemci(dongu, abone, koptu, hata)
        keepalive = max(2, int(10 * min(1.0, self.ilk_bekleme * 4)))
        # connect() soket bağlanana kadar bekler; olay döngüsünü tutmasın.
        await dongu.run_in_executor(None, istemci.connect, a["host"], a["port"], keepalive)
        self._izlemeye_basla(None)  # retained mesajlar gelmeden
        istemci.loop_start()
        try:
            bekleyenler = [asyncio.ensure_future(abone.wait()), asyncio.ensure_future(koptu.wait())]
            await asyncio.wait(bekleyenler, timeout=self.zaman_asimi,
                               return_when=asyncio.FIRST_COMPLETED)  # fmt: skip
            for b in bekleyenler:
                b.cancel()
            if not abone.is_set():
                raise ConnectionError(hata[0] if hata else "aracı aboneliği onaylamadı")
            gunluk.info("%s: %s/+/alarm/+ dinleniyor", self.gorunen_adres, a["onek"])
            await asyncio.sleep(ILK_DEGER_SURESI * min(1.0, self.ilk_bekleme * 4))
            self._hepsi_geldi.set()
            await self._izle(dur, koptu)
        finally:
            istemci.disconnect()
            await dongu.run_in_executor(None, istemci.loop_stop)

    def _mesaj(self, konu: str, icerik: bytes) -> None:
        try:
            makine, kod, aktif, zaman = mesaj_coz(self.ayar["onek"], konu, icerik)
        except Exception as hata:  # bozuk mesaj (çok derin JSON da) bağlantıyı koparmasın
            self.yok_sayilan += 1
            gunluk.warning("MQTT mesajı yok sayıldı: %s", str(hata)[:150])
            return
        self.degisti(makine, kod, aktif, zaman or datetime.now(UTC))
