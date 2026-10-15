"""OPC UA sunucusundaki makine alarmlarını izler, değişiklikleri kuyruğa olay olarak yazar.

Adres alanı kuralı: Objects/<makine kodu>/Alarmlar/<alarm kodu>, her alarm bir bool değişken
(true: alarm var). Makineler ve alarmlar bağlanınca bulunur, yapılandırma gerekmez. Gerçek bir
fabrikada bu yapı PLC'nin kendi OPC UA sunucusunda (Siemens S7-1500, Beckhoff, Fanuc OPC UA
seçeneği) ya da bir ağ geçidinde (Kepware, Node-RED) kurulur.

- false -> true: alarm_basladi; true -> false: alarm_bitti. Zaman, değerin kaynak zaman damgası
  (PLC'nin saati). olay_id ondan türer: yeniden bağlanınca aynı alarm aynı kimlikle gelir ve API
  onu ikinci kez işlemez.
- Bağlanınca her alarmın o anki değeri gelir; süren alarm "başladı" olarak gönderilir (API
  zaten biliyorsa "tekrar" der). Kuyruk boşalınca API'deki süren alarmlarla uzlaşılır: bağlantı
  yokken biten alarm için "bitti" gönderilir. API o an kapalıysa uzlaşma 30 sn arayla denenir.
- Bağlantı koparsa (sunucu kapandı, ağ gitti, sunucu cevap vermiyor) artan aralıklarla
  yeniden bağlanır.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from asyncua import Client

from entegrasyon.gonderici import bekle
from entegrasyon.kuyruk import Kuyruk

gunluk = logging.getLogger("entegrasyon.opcua")
ALARM_KLASORU = "Alarmlar"
YOKLAMA = 2.0  # saniye; bağlantının canlı olduğu bu aralıkla denetlenir
UZLASMA_ARALIGI = 30.0


def olay_kimligi(makine: str, kod: str, aktif: bool, zaman: datetime) -> str:
    """Aynı alarm değişimi her zaman aynı kimliği alır (mikrosaniyeli kaynak zaman damgası)."""
    return f"{makine}:{kod}:{int(aktif)}:{int(zaman.timestamp() * 1_000_000)}"


def _utc(zaman: datetime | None) -> datetime:
    if zaman is None:
        return datetime.now(UTC)
    return zaman.replace(tzinfo=UTC) if zaman.tzinfo is None else zaman


class OpcUaIzleyici:
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
        self.nabiz, self.zaman_asimi, self.ilk_bekleme = nabiz, zaman_asimi, ilk_bekleme
        self.durum: dict[tuple[str, str], bool] = {}  # son bilinen alarm değerleri
        self.bagli = asyncio.Event()  # izleme başladı (testler ve sağlık denetimi için)
        self.uzlasildi = False
        self.baglanti_sayisi = 0

    # --- Olaylar --------------------------------------------------------------------------

    def _olay(
        self, makine: str, kod: str, aktif: bool, zaman: datetime, olay_id: str | None = None
    ):
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
        """Abonelikten gelen değer. İlk değer (bağlanınca) yalnızca süren alarmı bildirir."""
        anahtar = (makine, kod)
        ilk = anahtar not in self._goruldu
        self._goruldu.add(anahtar)
        onceki = self.durum.get(anahtar)
        self.durum[anahtar] = aktif
        if ilk:
            if aktif:
                self._olay(makine, kod, True, zaman)
            if len(self._goruldu) == self._beklenen:
                self._hepsi_geldi.set()
        elif aktif != onceki:
            self._olay(makine, kod, aktif, zaman)

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
                    "OPC UA bağlantısı yok ya da koptu (%s: %s); %.0f sn sonra yeniden denenecek",
                    type(hata).__name__, str(hata)[:120], bekleme,
                )  # fmt: skip
            finally:
                self.bagli.clear()
            await bekle(dur, bekleme)
            bekleme = min(bekleme * 2, 30 * self.ilk_bekleme)

    async def _alarmlari_bul(self, istemci: Client) -> dict:
        alarmlar = {}
        for nesne in await istemci.nodes.objects.get_children():
            makine = (await nesne.read_browse_name()).Name
            if makine in ("Server", "Aliases", "Locations"):
                continue
            for alt in await nesne.get_children():
                if (await alt.read_browse_name()).Name != ALARM_KLASORU:
                    continue
                for alarm in await alt.get_children():
                    alarmlar[alarm.nodeid] = (makine, (await alarm.read_browse_name()).Name)
        return alarmlar

    async def _baglan_ve_izle(self, dur: asyncio.Event) -> None:
        koptu = asyncio.Event()
        istemci = Client(self.adres, timeout=self.zaman_asimi)

        async def kopunca(_hata):
            koptu.set()

        istemci.connection_lost_callback = kopunca
        async with istemci:
            alarmlar = await self._alarmlari_bul(istemci)
            if not alarmlar:
                raise ConnectionError(f"sunucuda '{ALARM_KLASORU}' klasörü olan makine yok")
            self._goruldu: set = set()
            self._beklenen = len(alarmlar)
            self._hepsi_geldi = asyncio.Event()
            self.uzlasildi = False
            self.baglanti_sayisi += 1
            makineler = sorted({m for m, _ in alarmlar.values()})
            gunluk.info(
                "%s: %d makine, %d alarm izleniyor", self.adres, len(makineler), len(alarmlar)
            )

            izleyici = self

            class Isleyici:
                def datachange_notification(self, node, deger, veri):
                    makine, kod = alarmlar[node.nodeid]
                    dv = veri.monitored_item.Value
                    zaman = _utc(dv.SourceTimestamp or dv.ServerTimestamp)
                    izleyici.degisti(makine, kod, bool(deger), zaman)

                def status_change_notification(self, durum):
                    koptu.set()  # abonelik sunucuda kapandı

            abonelik = await istemci.create_subscription(100, Isleyici())
            await abonelik.subscribe_data_change([istemci.get_node(n) for n in alarmlar])
            try:
                await asyncio.wait_for(self._hepsi_geldi.wait(), timeout=10)
            except TimeoutError:
                gunluk.warning("bazı alarmların ilk değeri gelmedi; yine de izleniyor")
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
                # Kapanmamış ama ölü bağlantı: sunucu cevap vermiyorsa hata fırlatır.
                await asyncio.wait_for(
                    istemci.nodes.server_state.read_value(), timeout=self.zaman_asimi
                )
            if koptu.is_set():
                raise ConnectionError("sunucu bağlantıyı kapattı")

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
            if anahtar not in self.durum:
                gunluk.warning("API'de süren %s/%s bu sunucuda yok; dokunulmadı", *anahtar)
                continue
            if not self.durum[anahtar]:
                baslangic = _utc(datetime.fromisoformat(alarm["baslangic"]))
                kimlik = f"uzlasma:{anahtar[0]}:{anahtar[1]}:{int(baslangic.timestamp() * 1e6)}"
                self._olay(*anahtar, False, datetime.now(UTC), olay_id=kimlik)
                gunluk.info("%s/%s bağlantı yokken bitmiş; bitti gönderildi", *anahtar)
        self.uzlasildi = True
