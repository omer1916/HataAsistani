"""MTConnect: CNC'lerin (Haas, Mazak, Okuma; Fanuc ve Siemens adaptörle) ajanını yoklar.

Ajanın /current cevabındaki her cihazın (DeviceStream) Condition öğeleri okunur: Fault (ve
istenirse Warning) süren alarmdır, kodu nativeCode (yoksa dataItemId). Listede olmayan alarm
bitmiştir. Olay zamanı koşulun zaman damgasıdır (ajanın saati), biterken Normal'in zamanı.
Bir cihazın bütün koşulları Unavailable ise (ajan makineye ulaşamıyor) o cihazın alarmlarına
dokunulmaz; makine dönünce durum yeniden okunur.

Adres: http://ajan:5000[?cihaz=VMC-3Axis:P3-HP,Mazak-1:P1-HP][&seviye=Fault,Warning][&aralik=1]
`cihaz` ajandaki cihaz adını Hata Asistanı'ndaki makine koduna çevirir (verilmezse ad aynen
kullanılır). Cevap 5 MB'ı geçerse ya da XML bozuksa izleyici çökmez, yeniden bağlanır.

Aynı dosyada simülatör ve testler için küçük bir MTConnect ajanı var.
"""

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse, urlunparse
from xml.sax.saxutils import escape, quoteattr

import httpx

from entegrasyon.izleyici import Izleyici, utc

gunluk = logging.getLogger("entegrasyon.mtconnect")
EN_BUYUK_CEVAP = 5 * 1024 * 1024
MAKINE = re.compile(r"^[A-Za-z0-9-]{1,20}$")
KOD_DISI = re.compile(r"[^A-Za-z0-9._-]")


def adres_coz(adres: str) -> dict:
    parca = urlparse(adres)
    if parca.scheme not in ("http", "https") or not parca.hostname:
        raise ValueError(f"MTConnect adresi http(s)://ajan:port olmalı: {adres}")
    sorgu = parse_qs(parca.query)
    cihazlar = {}
    for deger in sorgu.get("cihaz", []):
        for cift in deger.split(","):
            ad, _, makine = cift.partition(":")
            if not ad or not MAKINE.match(makine):
                raise ValueError(f"cihaz eşlemesi 'ad:MAKINE-KODU' olmalı: {cift}")
            cihazlar[ad] = makine
    seviyeler = set(",".join(sorgu.get("seviye", ["Fault"])).split(","))
    if not seviyeler <= {"Fault", "Warning"}:
        raise ValueError("seviye Fault ve/veya Warning olmalı")
    aralik = float(sorgu.get("aralik", ["1"])[0])
    if not 0.1 <= aralik <= 60:
        raise ValueError("aralik 0,1-60 saniye olmalı")
    temel = urlunparse((parca.scheme, parca.netloc, parca.path.rstrip("/"), "", "", ""))
    return {"temel": temel, "cihazlar": cihazlar, "seviyeler": seviyeler, "aralik": aralik}


def _zaman(metin: str | None) -> datetime:
    try:
        return utc(datetime.fromisoformat(metin)) if metin else datetime.now(UTC)
    except ValueError:
        return datetime.now(UTC)


def _ad(oge: ET.Element) -> str:
    return oge.tag.rsplit("}", 1)[-1]  # ad alanını at (MTConnect sürümleri farklı)


def kod_temizle(kod: str) -> str:
    return KOD_DISI.sub("_", kod)[:64] or "_"


def cevap_coz(xml: bytes, cihazlar: dict, seviyeler: set) -> tuple[set, dict, dict]:
    """(bilinen makineler, {(makine, kod): zaman} süren alarmlar, {(makine, kod): zaman}
    Normal'e dönen kodlar). Bozuk XML'de ValueError."""
    try:
        kok = ET.fromstring(xml)
    except (ET.ParseError, LookupError, ValueError) as hata:  # LookupError: bilinmeyen kodlama
        raise ValueError(f"MTConnect cevabı XML değil: {hata}") from None
    if _ad(kok) == "MTConnectError":
        hatalar = [e.text or e.get("errorCode", "") for e in kok.iter() if _ad(e) == "Error"]
        raise ValueError(f"MTConnect ajanı hata döndü: {'; '.join(hatalar)[:200]}")
    if _ad(kok) != "MTConnectStreams":
        raise ValueError(f"beklenmeyen kök öğe: {_ad(kok)}")
    makineler, aktif, normal = set(), {}, {}
    for cihaz in kok.iter():
        if _ad(cihaz) != "DeviceStream":
            continue
        ad = cihaz.get("name", "")
        makine = cihazlar.get(ad, ad)
        if cihazlar and ad not in cihazlar:
            continue  # eşleme verildiyse yalnızca eşlenen cihazlar
        if not MAKINE.match(makine):
            gunluk.warning("MTConnect cihaz adı makine kodu olamaz: %r", ad[:40])
            continue
        kosullar = [oge for kosul in cihaz.iter() if _ad(kosul) == "Condition" for oge in kosul]
        if kosullar and all(_ad(oge) == "Unavailable" for oge in kosullar):
            continue  # ajan makineye ulaşamıyor: durum bilinmiyor, alarmlara dokunulmaz
        makineler.add(makine)
        for oge in kosullar:
            tur = _ad(oge)
            kod = kod_temizle(oge.get("nativeCode") or oge.get("dataItemId") or "")
            if tur in seviyeler:
                aktif[(makine, kod)] = _zaman(oge.get("timestamp"))
            elif tur == "Normal":
                normal[(makine, kod)] = _zaman(oge.get("timestamp"))
    return makineler, aktif, normal


class MTConnectIzleyici(Izleyici):
    PROTOKOL = "MTConnect"

    def __init__(self, adres: str, *args, http: httpx.AsyncClient | None = None, **kwargs):
        super().__init__(adres, *args, **kwargs)
        self.ayar = adres_coz(adres)
        self.http = http
        self.bilinen_makineler: set[str] = set()

    def bilinen_deger(self, anahtar: tuple[str, str]) -> bool | None:
        # Ajan süren bütün koşulları listeler: bilinen bir cihazda olmayan alarm bitmiştir.
        if anahtar[0] in self.bilinen_makineler:
            return self.durum.get(anahtar, False)
        return None

    async def _cek(self, http: httpx.AsyncClient) -> bytes:
        async with http.stream("GET", f"{self.ayar['temel']}/current") as cevap:
            if cevap.status_code != 200:
                raise ConnectionError(f"ajan {cevap.status_code} döndü")
            parcalar, boyut = [], 0
            async for parca in cevap.aiter_bytes():
                boyut += len(parca)
                if boyut > EN_BUYUK_CEVAP:
                    raise ConnectionError("ajan cevabı 5 MB'tan büyük")
                parcalar.append(parca)
            return b"".join(parcalar)

    async def _yokla(self, http: httpx.AsyncClient) -> None:
        # httpx'in zaman aşımına ek olarak: cevabı damla damla veren ajan da takmasın.
        xml = await asyncio.wait_for(self._cek(http), timeout=self.zaman_asimi * 2)
        makineler, aktif, normal = cevap_coz(xml, self.ayar["cihazlar"], self.ayar["seviyeler"])
        self.bilinen_makineler = makineler
        for anahtar, zaman in aktif.items():
            self.degisti(*anahtar, True, zaman)
        for anahtar, deger in list(self.durum.items()):
            if deger and anahtar not in aktif and anahtar[0] in makineler:
                self.degisti(*anahtar, False, normal.get(anahtar, datetime.now(UTC)))

    async def _baglan_ve_izle(self, dur: asyncio.Event) -> None:
        http = self.http or httpx.AsyncClient(timeout=self.zaman_asimi, follow_redirects=False)
        try:
            self._izlemeye_basla(None)
            await self._yokla(http)  # ilk değerler
            self._hepsi_geldi.set()
            gunluk.info("%s: %d cihaz yoklanıyor", self.gorunen_adres, len(self.bilinen_makineler))
            koptu = asyncio.Event()
            hata: list[BaseException] = []

            async def yokla():
                try:
                    while not dur.is_set():
                        await asyncio.sleep(self.ayar["aralik"] * min(1.0, self.ilk_bekleme * 4))
                        await self._yokla(http)
                except Exception as e:
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
            if self.http is None:
                await http.aclose()


# --- Simülatör ve testler için küçük MTConnect ajanı ------------------------------------------


def akis_xml(cihazlar: dict[str, dict[str, datetime]]) -> bytes:
    """{cihaz adı: {süren alarm kodu: zaman}} -> /current cevabı (MTConnect 1.3 biçimi)."""
    simdi = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    satirlar = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<MTConnectStreams xmlns="urn:mtconnect.org:MTConnectStreams:1.3">',
        f'<Header creationTime="{simdi}" sender="hata-asistani-simulator" instanceId="1" '
        'version="1.3.0" bufferSize="1024" nextSequence="1" firstSequence="1" lastSequence="1"/>',
        "<Streams>",
    ]
    for ad, alarmlar in cihazlar.items():
        satirlar.append(f"<DeviceStream name={quoteattr(ad)} uuid={quoteattr(ad)}>")
        satirlar.append('<ComponentStream component="Controller" name="kontrol" componentId="k1">')
        satirlar.append("<Condition>")
        if not alarmlar:
            satirlar.append(f'<Normal dataItemId="alarm" timestamp="{simdi}" type="SYSTEM"/>')
        for kod, zaman in alarmlar.items():
            ts = zaman.astimezone(UTC).isoformat().replace("+00:00", "Z")
            satirlar.append(
                f'<Fault dataItemId="alarm" timestamp="{ts}" type="SYSTEM" '
                f"nativeCode={quoteattr(kod)}>{escape(kod)}</Fault>"
            )
        satirlar += ["</Condition>", "</ComponentStream>", "</DeviceStream>"]
    satirlar += ["</Streams>", "</MTConnectStreams>"]
    return "\n".join(satirlar).encode()


class MTConnectAjani:
    """/current'ı `durum()` çağrısından üreten küçük HTTP sunucusu (yalnızca GET)."""

    def __init__(self, durum, adres=("127.0.0.1", 5000)):
        self.durum, self.adres = durum, adres
        self._sunucu: asyncio.Server | None = None
        self._baglantilar: set[asyncio.StreamWriter] = set()
        self.istek_sayisi = 0

    async def baslat(self) -> "MTConnectAjani":
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

    async def _baglanti(self, okuyucu, yazici):
        self._baglantilar.add(yazici)
        try:
            satir = await asyncio.wait_for(okuyucu.readline(), 5)
            while (await asyncio.wait_for(okuyucu.readline(), 5)) not in (b"\r\n", b"\n", b""):
                pass  # başlıklar
            yontem, yol, *_ = satir.decode("latin-1").split() + ["", ""]
            self.istek_sayisi += 1
            if yontem != "GET":
                kod, govde = "405 Method Not Allowed", b""
            elif yol.split("?")[0].rstrip("/").endswith("/current") or yol == "/current":
                kod, govde = "200 OK", akis_xml(self.durum())
            else:
                kod, govde = "404 Not Found", b""
            yazici.write(
                f"HTTP/1.1 {kod}\r\nContent-Type: application/xml\r\n"
                f"Content-Length: {len(govde)}\r\nConnection: close\r\n\r\n".encode()
                + govde
            )
            await yazici.drain()
        except (TimeoutError, ConnectionError, ValueError):
            pass
        finally:
            self._baglantilar.discard(yazici)
            yazici.close()
