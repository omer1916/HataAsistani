"""Modbus TCP: basit PLC'lerin alarm bitlerini (bobin ya da ayrık giriş) yoklayarak izler.

Modbus adres alanı kendini anlatmaz; hangi bitin hangi makinenin hangi alarmı olduğu bir
haritayla verilir (MODBUS_HARITASI dosyası ya da `harita`):

    {"birim": 1, "tablo": "coil", "aralik": 0.2,
     "makineler": {"P2-HP": {"A101": 0, "A102": 1}, "P2-RB": {"A401": 16}}}

`tablo`: "coil" (FC 1) ya da "discrete" (FC 2). Bit 1 alarm var demektir. Değerler `aralik`
saniyede bir okunur; daha kısa süren bir alarm görünmeyebilir (son durum yine doğrudur). Olay
zamanı okuma anıdır (Modbus zaman damgası taşımaz). Okuma hata verirse bağlantı yeniden kurulur.

Aynı dosyada simülatör ve testler için küçük bir Modbus TCP sunucusu var (FC 1, 2, 5, 15).
"""

import asyncio
import json
import logging
import re
import struct
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from pymodbus.client import AsyncModbusTcpClient

from entegrasyon.izleyici import Izleyici

gunluk = logging.getLogger("entegrasyon.modbus")
EN_FAZLA_BIT = 2000  # Modbus'ın tek okumada izin verdiği bit sayısı
MAKINE = re.compile(r"^[A-Za-z0-9-]{1,20}$")
KOD = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def adres_coz(adres: str) -> tuple[str, int]:
    """modbus://host:502 -> (host, 502)."""
    parca = urlparse(adres)
    if parca.scheme not in ("modbus", "modbus+tcp") or not parca.hostname:
        raise ValueError(f"Modbus adresi modbus://host:port olmalı: {adres}")
    return parca.hostname, parca.port or 502


def harita_dogrula(harita: dict) -> dict:
    """Haritayı denetler; hatalı haritayla bağlanmaya hiç çalışılmaz (açık hata mesajı)."""
    if not isinstance(harita, dict) or not isinstance(harita.get("makineler"), dict):
        raise ValueError('Modbus haritasında "makineler" sözlüğü olmalı')
    tablo = harita.get("tablo", "coil")
    if tablo not in ("coil", "discrete"):
        raise ValueError('"tablo" coil ya da discrete olmalı')
    birim = harita.get("birim", 1)
    if not _tam_sayi(birim) or not 0 <= birim <= 247:
        raise ValueError('"birim" 0-247 arası tam sayı olmalı')
    aralik = harita.get("aralik", 0.2)
    if isinstance(aralik, bool) or not isinstance(aralik, int | float) or not 0.05 <= aralik <= 60:
        raise ValueError('"aralik" 0,05-60 saniye olmalı')
    if not harita["makineler"]:
        raise ValueError("Modbus haritasında en az bir makine olmalı")
    goruldu: dict[int, tuple[str, str]] = {}
    for makine, alarmlar in harita["makineler"].items():
        if not isinstance(makine, str) or not MAKINE.match(makine):
            raise ValueError(f"makine kodu geçersiz: {str(makine)[:40]!r}")
        if not isinstance(alarmlar, dict) or not alarmlar:
            raise ValueError(f"{makine}: alarm sözlüğü boş ya da hatalı")
        for kod, bit in alarmlar.items():
            if not isinstance(kod, str) or not KOD.match(kod):
                raise ValueError(f"{makine}: alarm kodu geçersiz: {str(kod)[:70]!r}")
            if not _tam_sayi(bit) or not 0 <= bit <= 65535:
                raise ValueError(f"{makine}/{kod}: adres 0-65535 arası tam sayı olmalı")
            if bit in goruldu:
                raise ValueError(f"{makine}/{kod}: {bit} adresi {goruldu[bit]} için de verilmiş")
            goruldu[bit] = (makine, kod)
    return {"birim": birim, "tablo": tablo, "aralik": float(aralik),
            "adresler": dict(sorted(goruldu.items()))}  # fmt: skip


def _tam_sayi(deger) -> bool:
    return isinstance(deger, int) and not isinstance(deger, bool)


def harita_oku(yol: str | Path) -> dict:
    # utf-8-sig: Not Defteri'nin başa koyduğu BOM da olur.
    return harita_dogrula(json.loads(Path(yol).read_text(encoding="utf-8-sig")))


class ModbusIzleyici(Izleyici):
    PROTOKOL = "Modbus"

    def __init__(self, adres: str, *args, harita: dict, **kwargs):
        super().__init__(adres, *args, **kwargs)
        self.host, self.port = adres_coz(adres)
        self.harita = harita_dogrula(harita) if "makineler" in harita else harita

    async def _oku(self, istemci: AsyncModbusTcpClient) -> None:
        adresler = self.harita["adresler"]
        oku = istemci.read_coils if self.harita["tablo"] == "coil" else istemci.read_discrete_inputs
        bitler: dict[int, bool] = {}
        liste = sorted(adresler)
        # Bitişik olmayan adresleri en fazla EN_FAZLA_BIT'lik parçalarla oku.
        bas = 0
        while bas < len(liste):
            ilk = liste[bas]
            son = bas
            while son + 1 < len(liste) and liste[son + 1] - ilk < EN_FAZLA_BIT:
                son += 1
            adet = liste[son] - ilk + 1
            cevap = await asyncio.wait_for(
                oku(ilk, count=adet, device_id=self.harita["birim"]), timeout=self.zaman_asimi
            )
            if cevap.isError():
                raise ConnectionError(f"Modbus okuma hatası ({ilk}+{adet}): {cevap}")
            for i in range(adet):
                bitler[ilk + i] = bool(cevap.bits[i])
            bas = son + 1
        zaman = datetime.now(UTC)
        for bit, (makine, kod) in adresler.items():
            self.degisti(makine, kod, bitler[bit], zaman)

    async def _baglan_ve_izle(self, dur: asyncio.Event) -> None:
        istemci = AsyncModbusTcpClient(
            self.host, port=self.port, timeout=self.zaman_asimi, retries=0
        )
        try:
            if not await istemci.connect():
                raise ConnectionError(f"{self.host}:{self.port} bağlanılamadı")
            adresler = self.harita["adresler"]
            self._izlemeye_basla(len(adresler))
            gunluk.info(
                "%s: %d makine, %d alarm yoklanıyor (%s, %.2f sn)", self.gorunen_adres,
                len({m for m, _ in adresler.values()}), len(adresler), self.harita["tablo"],
                self.harita["aralik"],
            )  # fmt: skip
            await self._oku(istemci)  # ilk değerler
            koptu = asyncio.Event()
            hata: list[BaseException] = []

            async def yokla():
                try:
                    while not dur.is_set():
                        await asyncio.sleep(self.harita["aralik"])
                        await self._oku(istemci)
                except Exception as e:  # okuma hatası: bağlantıyı yenile
                    hata.append(e)
                    koptu.set()

            gorev = asyncio.create_task(yokla())
            try:
                await self._izle(dur, koptu)
            except ConnectionError:
                raise ConnectionError(str(hata[0]) if hata else "bağlantı koptu") from None
            finally:
                gorev.cancel()
        finally:
            istemci.close()


# --- Simülatör ve testler için küçük Modbus TCP sunucusu --------------------------------------

YASAK_ISLEV, YASAK_ADRES, YASAK_DEGER = 0x01, 0x02, 0x03


class ModbusSunucusu:
    """Bitleri (bobin) `bitler` sözlüğünden okuyan, yazınca `yazildi(adres, deger)` çağıran
    Modbus TCP sunucusu. FC 1 (bobin oku), 2 (ayrık giriş oku; aynı bitler), 5 (tek bobin yaz),
    15 (çok bobin yaz). Bozuk çerçevede bağlantıyı kapatır, bilinmeyen işlevde hata cevabı verir."""

    def __init__(self, bitler: dict[int, bool], yazildi=None, adres=("127.0.0.1", 5020)):
        self.bitler, self.yazildi, self.adres = bitler, yazildi, adres
        self._sunucu: asyncio.Server | None = None
        self._baglantilar: set[asyncio.StreamWriter] = set()
        self.istek_sayisi = 0

    async def baslat(self) -> "ModbusSunucusu":
        self._sunucu = await asyncio.start_server(self._baglanti, *self.adres)
        return self

    @property
    def port(self) -> int:
        return self._sunucu.sockets[0].getsockname()[1]

    async def durdur(self) -> None:
        if self._sunucu:
            self._sunucu.close()
            # Python 3.12'de wait_closed açık bağlantıları bekler: önce onlar kapatılır.
            for yazici in list(self._baglantilar):
                yazici.close()
            await self._sunucu.wait_closed()
            self._sunucu = None

    async def _baglanti(self, okuyucu: asyncio.StreamReader, yazici: asyncio.StreamWriter):
        self._baglantilar.add(yazici)
        try:
            while True:
                baslik = await okuyucu.readexactly(7)
                islem, protokol, uzunluk, birim = struct.unpack(">HHHB", baslik)
                if protokol != 0 or not 2 <= uzunluk <= 254:
                    break  # Modbus değil ya da bozuk çerçeve
                pdu = await okuyucu.readexactly(uzunluk - 1)
                self.istek_sayisi += 1
                cevap = self._isle(pdu)
                yazici.write(struct.pack(">HHHB", islem, 0, len(cevap) + 1, birim) + cevap)
                await yazici.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._baglantilar.discard(yazici)
            yazici.close()

    def _hata(self, islev: int, kod: int) -> bytes:
        return bytes([islev | 0x80, kod])

    def _isle(self, pdu: bytes) -> bytes:
        islev = pdu[0]
        if islev in (1, 2):
            if len(pdu) != 5:
                return self._hata(islev, YASAK_DEGER)
            bas, adet = struct.unpack(">HH", pdu[1:5])
            if not 1 <= adet <= EN_FAZLA_BIT:
                return self._hata(islev, YASAK_DEGER)
            if bas + adet > 65536:
                return self._hata(islev, YASAK_ADRES)
            bitler = [self.bitler.get(bas + i, False) for i in range(adet)]
            baytlar = bytearray((adet + 7) // 8)
            for i, b in enumerate(bitler):
                if b:
                    baytlar[i // 8] |= 1 << (i % 8)
            return bytes([islev, len(baytlar)]) + bytes(baytlar)
        if islev == 5:
            if len(pdu) != 5:
                return self._hata(islev, YASAK_DEGER)
            adres, deger = struct.unpack(">HH", pdu[1:5])
            if deger not in (0x0000, 0xFF00):
                return self._hata(islev, YASAK_DEGER)
            self._yaz(adres, deger == 0xFF00)
            return pdu
        if islev == 15:
            if len(pdu) < 6:
                return self._hata(islev, YASAK_DEGER)
            bas, adet, bayt = struct.unpack(">HHB", pdu[1:6])
            if not 1 <= adet <= 1968 or bayt != (adet + 7) // 8 or len(pdu) != 6 + bayt:
                return self._hata(islev, YASAK_DEGER)
            if bas + adet > 65536:
                return self._hata(islev, YASAK_ADRES)
            for i in range(adet):
                self._yaz(bas + i, bool(pdu[6 + i // 8] >> (i % 8) & 1))
            return pdu[:5]
        return self._hata(islev, YASAK_ISLEV)

    def _yaz(self, adres: int, deger: bool) -> None:
        self.bitler[adres] = deger
        if self.yazildi:
            self.yazildi(adres, deger)
