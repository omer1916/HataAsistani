"""Bütün protokollerin ortak kısmı: alarm değerlerinden olay üretir, kuyruğa yazar, yeniden
bağlanır ve API'deki süren alarmlarla uzlaşır.

Bir protokol izleyicisi yalnızca `_baglan_ve_izle`'yi yazar: bağlanır, alarmları bulur,
`_izlemeye_basla` der, her değeri `degisti` ile bildirir ve `_izle` ile bağlantı canlı kaldığı
sürece bekler.

- false -> true: alarm_basladi; true -> false: alarm_bitti. olay_id değerin zamanından türer
  (makinenin saati); yeniden bağlanınca aynı alarm aynı kimlikle gelir, API ikinci kez işlemez.
- Bağlanınca her alarmın o anki değeri gelir. Süreç yeni açıldıysa süren alarm "başladı" olarak
  gönderilir (API zaten biliyorsa "tekrar" ya da "zaten_aktif" der); kopukken biten alarm
  "bitti" olur.
- Kuyruk boşalınca API'deki süren alarmlarla uzlaşılır: bağlantı yokken biten alarm için "bitti"
  gönderilir. API o an kapalıysa uzlaşma UZLASMA_ARALIGI'nda bir yeniden denenir.
- Bağlantı koparsa (sunucu kapandı, ağ gitti, cevap gelmiyor) artan aralıklarla yeniden
  bağlanır; izleyici hiçbir hatayla ölmez.
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from entegrasyon.gonderici import bekle
from entegrasyon.kuyruk import Kuyruk

gunluk = logging.getLogger("entegrasyon.izleyici")
YOKLAMA = 2.0  # saniye; bağlantının canlı olduğu bu aralıkla denetlenir
UZLASMA_ARALIGI = 30.0


def olay_kimligi(makine: str, kod: str, aktif: bool, zaman: datetime) -> str:
    """Aynı alarm değişimi her zaman aynı kimliği alır (mikrosaniyeli zaman damgası)."""
    return f"{makine}:{kod}:{int(aktif)}:{int(zaman.timestamp() * 1_000_000)}"


def gizle(adres: str) -> str:
    """Adresteki parolayı günlüğe yazmamak için: mqtt://ali:gizli@h -> mqtt://ali:***@h."""
    return re.sub(r"(://[^:/@]*):[^@/]*@", r"\1:***@", adres)


def utc(zaman: datetime | None) -> datetime:
    if zaman is None:
        return datetime.now(UTC)
    return zaman.replace(tzinfo=UTC) if zaman.tzinfo is None else zaman


class Izleyici:
    PROTOKOL = "?"

    def __init__(
        self,
        adres: str,
        kuyruk: Kuyruk,
        haber_ver: Callable[[], None] = lambda: None,
        uzlas: Callable[[], Awaitable[list[dict]]] | None = None,
        nabiz: Path | None = None,
        zaman_asimi: float = 4.0,
        ilk_bekleme: float = 1.0,
    ):
        self.adres, self.kuyruk, self.haber_ver, self.uzlas = adres, kuyruk, haber_ver, uzlas
        self.gorunen_adres = gizle(adres)  # günlük için (parola yok)
        self.nabiz, self.zaman_asimi, self.ilk_bekleme = nabiz, zaman_asimi, ilk_bekleme
        self.durum: dict[tuple[str, str], bool] = {}  # son bilinen alarm değerleri
        self.bagli = asyncio.Event()  # izleme başladı (testler ve sağlık denetimi için)
        self.uzlasildi = False
        self.baglanti_sayisi = 0
        self._goruldu: set = set()
        self._beklenen: int | None = None
        self._hepsi_geldi = asyncio.Event()

    # --- Olaylar --------------------------------------------------------------------------

    def _olay(
        self, makine: str, kod: str, aktif: bool, zaman: datetime, olay_id: str | None = None
    ) -> None:
        self.kuyruk.ekle(
            {
                "olay_id": olay_id or olay_kimligi(makine, kod, aktif, zaman),
                "tur": "alarm_basladi" if aktif else "alarm_bitti",
                "makine_kodu": makine,
                "alarm_kodu": kod,
                "zaman": zaman.isoformat(),
            }
        )
        self.haber_ver()

    def degisti(self, makine: str, kod: str, aktif: bool, zaman: datetime) -> None:
        """Bir alarmın değeri geldi; bilinen son değerden farklıysa olay olur. Hiç bilinmeyen
        alarm "yok" sayılır: süreç açılınca süren alarm bildirilir, kopukken biten alarm yeniden
        bağlanınca "bitti" olur, kopukken süren alarm yeniden gönderilmez."""
        anahtar = (makine, kod)
        if anahtar not in self._goruldu:
            self._goruldu.add(anahtar)
            if self._beklenen is not None and len(self._goruldu) >= self._beklenen:
                self._hepsi_geldi.set()
        onceki = self.durum.get(anahtar, False)
        self.durum[anahtar] = aktif
        if aktif != onceki:
            self._olay(makine, kod, aktif, zaman)

    def bilinen_deger(self, anahtar: tuple[str, str]) -> bool | None:
        """Uzlaşmada: alarmın buradaki değeri; bilinmiyorsa None (dokunulmaz)."""
        return self.durum.get(anahtar)

    # --- Bağlantı ---------------------------------------------------------------------------

    async def calis(self, dur: asyncio.Event) -> None:
        bekleme = self.ilk_bekleme
        while not dur.is_set():
            try:
                await self._baglan_ve_izle(dur)
                bekleme = self.ilk_bekleme
            except asyncio.CancelledError:
                raise
            except Exception as hata:  # bağlantı her türlü kopabilir; izleyici ölmesin
                gunluk.warning(
                    "%s bağlantısı yok ya da koptu (%s: %s); %.1f sn sonra yeniden denenecek",
                    self.PROTOKOL, type(hata).__name__, str(hata)[:120], bekleme,
                )  # fmt: skip
            finally:
                self.bagli.clear()
            await bekle(dur, bekleme)
            bekleme = min(bekleme * 2, 30 * self.ilk_bekleme)

    async def _baglan_ve_izle(self, dur: asyncio.Event) -> None:
        raise NotImplementedError

    def _izlemeye_basla(self, beklenen: int | None) -> None:
        """Yeni bağlantı: ilk değerler yeniden beklenir. `beklenen` bilinmiyorsa (MQTT,
        MTConnect) ilk değerler kısa bir süre toplanır."""
        self._goruldu = set()
        self._beklenen = beklenen
        self._hepsi_geldi = asyncio.Event()
        if beklenen == 0:
            self._hepsi_geldi.set()
        self.uzlasildi = False
        self.baglanti_sayisi += 1

    async def _ilk_degerleri_bekle(self, sure: float = 10.0) -> None:
        try:
            await asyncio.wait_for(self._hepsi_geldi.wait(), timeout=sure)
        except TimeoutError:
            if self._beklenen is not None:
                gunluk.warning("bazı alarmların ilk değeri gelmedi; yine de izleniyor")

    async def _izle(
        self,
        dur: asyncio.Event,
        koptu: asyncio.Event,
        canli: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        """Bağlantı canlı kaldığı sürece: uzlaşır, nabzı günceller, bağlantıyı denetler."""
        self.bagli.set()
        son_uzlasma = -UZLASMA_ARALIGI
        while not dur.is_set() and not koptu.is_set():
            simdi = asyncio.get_running_loop().time()
            if not self.uzlasildi and simdi - son_uzlasma >= UZLASMA_ARALIGI * self.ilk_bekleme:
                son_uzlasma = simdi
                await self._uzlas()
            if self.nabiz:
                self.nabiz.touch()
            await bekle(dur, YOKLAMA * min(1.0, self.ilk_bekleme * 4))
            if canli is not None and not koptu.is_set():
                # Kapanmamış ama ölü bağlantı: cevap gelmezse hata fırlatır.
                await asyncio.wait_for(canli(), timeout=self.zaman_asimi)
        if koptu.is_set() and not dur.is_set():
            raise ConnectionError("bağlantı koptu")

    async def _uzlas(self) -> None:
        """API'de süren ama burada bitmiş görünen alarmlar için "bitti" gönderir. Önce kuyruk
        boşalmalı: gönderilmemiş bir "bitti" varken API'nin durumu eskidir."""
        if self.uzlas is None:
            self.uzlasildi = True
            return
        if len(self.kuyruk):
            return  # bir sonraki turda
        try:
            sunucudakiler = await self.uzlas()
        except Exception as hata:
            gunluk.warning("API'deki süren alarmlar alınamadı (%s); sonra denenecek", hata)
            return
        for alarm in sunucudakiler:
            anahtar = (alarm["makine_kodu"], alarm["alarm_kodu"])
            deger = self.bilinen_deger(anahtar)
            if deger is None:
                gunluk.warning("API'de süren %s/%s burada bilinmiyor; dokunulmadı", *anahtar)
                continue
            if not deger:
                baslangic = utc(datetime.fromisoformat(alarm["baslangic"]))
                kimlik = f"uzlasma:{anahtar[0]}:{anahtar[1]}:{int(baslangic.timestamp() * 1e6)}"
                self._olay(*anahtar, False, datetime.now(UTC), olay_id=kimlik)
                gunluk.info("%s/%s bağlantı yokken bitmiş; bitti gönderildi", *anahtar)
        self.uzlasildi = True
