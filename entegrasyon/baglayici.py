"""Bağlayıcıyı çalıştırır: makinenin alarmlarını izler ve Hata Asistanı'na gönderir.

    python -m entegrasyon.baglayici

Protokol adresten seçilir:
    opc.tcp://plc:4840/...          OPC UA (makineler ve alarmlar kendiliğinden bulunur)
    modbus://plc:502                Modbus TCP (MODBUS_HARITASI dosyası gerekir)
    mqtt://broker:1883/önek         MQTT (konu: önek/<makine>/alarm/<kod>)
    http://ajan:5000                MTConnect (?cihaz=ad:MAKINE ile cihaz adı eşlenir)

Ortam değişkenleri:
    HATA_ASISTANI_ADRESI  API adresi (varsayılan http://127.0.0.1:8000)
    ENTEGRASYON_ANAHTARI  kaynağın anahtarı (python -m scripts.entegrasyon kaynak-ekle verir)
    KAYNAK_ADRESI         makinenin ya da ağ geçidinin adresi (eski adı OPCUA_ADRESI de olur)
    MODBUS_HARITASI       Modbus için bit haritası (JSON; bkz. entegrasyon/modbus.py)
    KUYRUK_DOSYASI        gönderilemeyen olaylar (varsayılan ./baglayici-kuyruk.sqlite)
    NABIZ_DOSYASI         sağlık denetimi için; makine bağlantısı canlıyken güncellenir

Günlüğe anahtar ve parola yazılmaz.
"""

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import httpx

from entegrasyon.gonderici import Gonderici
from entegrasyon.izleyici import Izleyici, gizle
from entegrasyon.kuyruk import Kuyruk

gunluk = logging.getLogger("entegrasyon")


def izleyici_sec(adres: str, modbus_haritasi: dict | None = None):
    """Adresin şemasından izleyici sınıfı ve ek ayarları. Bilinmeyen şemada ValueError."""
    sema = adres.split("://", 1)[0].lower() if "://" in adres else ""
    if sema == "opc.tcp":
        from entegrasyon.opcua import OpcUaIzleyici

        return OpcUaIzleyici, {}
    if sema in ("modbus", "modbus+tcp"):
        if modbus_haritasi is None:
            raise ValueError("Modbus için MODBUS_HARITASI (bit haritası) gerekir")
        from entegrasyon.modbus import ModbusIzleyici

        return ModbusIzleyici, {"harita": modbus_haritasi}
    if sema in ("mqtt", "mqtts"):
        from entegrasyon.mqtt import MqttIzleyici

        return MqttIzleyici, {}
    if sema in ("http", "https"):
        from entegrasyon.mtconnect import MTConnectIzleyici

        return MTConnectIzleyici, {}
    raise ValueError(
        f"Desteklenmeyen adres: {gizle(adres)} (opc.tcp://, modbus://, mqtt://, http://)"
    )


class Baglayici:
    """İzleyici (makine -> kuyruk) ve gönderici (kuyruk -> API) birlikte."""

    def __init__(
        self,
        kaynak_adresi: str,
        api_adresi: str,
        anahtar: str,
        kuyruk: Kuyruk,
        http: httpx.AsyncClient | None = None,
        nabiz: Path | None = None,
        ilk_bekleme: float = 1.0,
        modbus_haritasi: dict | None = None,
    ):
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(10, read=30))
        self.api_adresi, self.anahtar, self.kuyruk = api_adresi.rstrip("/"), anahtar, kuyruk
        self.gonderici = Gonderici(
            kuyruk, self.api_adresi, anahtar, self.http, ilk_bekleme=ilk_bekleme
        )
        sinif, ek = izleyici_sec(kaynak_adresi, modbus_haritasi)
        self.izleyici: Izleyici = sinif(
            kaynak_adresi, kuyruk, haber_ver=self.gonderici.haber_ver, uzlas=self._uzlas,
            nabiz=nabiz, ilk_bekleme=ilk_bekleme, **ek,
        )  # fmt: skip

    async def _uzlas(self) -> list[dict]:
        cevap = await self.http.get(
            f"{self.api_adresi}/entegrasyon/aktif-alarmlar",
            headers={"X-API-Anahtari": self.anahtar},
        )
        cevap.raise_for_status()
        return cevap.json()

    async def calis(self, dur: asyncio.Event) -> None:
        """`dur` gelene kadar çalışır."""
        await asyncio.gather(self.gonderici.calis(dur), self.izleyici.calis(dur))


async def _ana() -> None:
    anahtar = os.environ.get("ENTEGRASYON_ANAHTARI") or os.environ.get("SIMULATOR_ANAHTARI")
    kaynak = os.environ.get("KAYNAK_ADRESI") or os.environ.get("OPCUA_ADRESI")
    if not anahtar or not kaynak:
        sys.exit("ENTEGRASYON_ANAHTARI ve KAYNAK_ADRESI tanımlı olmalı (bkz. README, entegrasyon).")
    harita = None
    if os.environ.get("MODBUS_HARITASI"):
        from entegrasyon.modbus import harita_oku

        try:
            harita = harita_oku(os.environ["MODBUS_HARITASI"])
        except (OSError, ValueError) as hata:
            sys.exit(f"Modbus haritası okunamadı: {hata}")
    api_adresi = os.environ.get("HATA_ASISTANI_ADRESI", "http://127.0.0.1:8000")
    kuyruk = Kuyruk(os.environ.get("KUYRUK_DOSYASI", "baglayici-kuyruk.sqlite"))
    nabiz = Path(os.environ["NABIZ_DOSYASI"]) if os.environ.get("NABIZ_DOSYASI") else None
    try:
        baglayici = Baglayici(
            kaynak, api_adresi, anahtar, kuyruk, nabiz=nabiz, modbus_haritasi=harita
        )
    except ValueError as hata:  # adres hatalı: anlaşılır mesajla çık (yeniden denemek boşuna)
        kuyruk.kapat()
        sys.exit(f"KAYNAK_ADRESI hatalı: {hata}")
    dur = asyncio.Event()
    for sinyal in (signal.SIGTERM, signal.SIGINT):
        try:
            asyncio.get_running_loop().add_signal_handler(sinyal, dur.set)
        except (NotImplementedError, AttributeError):  # Windows
            pass
    gunluk.info("bağlayıcı çalışıyor: %s -> %s (%d olay kuyrukta)", gizle(kaynak), api_adresi,
                len(kuyruk))  # fmt: skip
    try:
        await baglayici.calis(dur)
    finally:
        await baglayici.http.aclose()
        kuyruk.kapat()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    for gurultulu in ("asyncua", "pymodbus", "aiomqtt"):
        logging.getLogger(gurultulu).setLevel(logging.ERROR)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # her isteği yazmasın
    # MQTT (aiomqtt) Windows'ta seçici olay döngüsü ister; diğer protokoller iki döngüde de çalışır.
    dongu = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    try:
        asyncio.run(_ana(), loop_factory=dongu)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
