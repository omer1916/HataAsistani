"""Bağlayıcıyı çalıştırır: OPC UA sunucusundaki alarmları izler ve Hata Asistanı'na gönderir.

    python -m entegrasyon.baglayici

Ortam değişkenleri:
    HATA_ASISTANI_ADRESI  API adresi (varsayılan http://127.0.0.1:8000)
    ENTEGRASYON_ANAHTARI  kaynağın anahtarı (python -m scripts.entegrasyon kaynak-ekle verir)
    OPCUA_ADRESI          PLC'nin ya da ağ geçidinin OPC UA adresi
    KUYRUK_DOSYASI        gönderilemeyen olaylar (varsayılan ./baglayici-kuyruk.sqlite)
    NABIZ_DOSYASI         sağlık denetimi için; OPC UA bağlantısı canlıyken güncellenir

Günlüğe anahtar yazılmaz.
"""

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import httpx

from entegrasyon.gonderici import Gonderici
from entegrasyon.kuyruk import Kuyruk
from entegrasyon.opcua import OpcUaIzleyici

gunluk = logging.getLogger("entegrasyon")


class Baglayici:
    """İzleyici (OPC UA -> kuyruk) ve gönderici (kuyruk -> API) birlikte."""

    def __init__(
        self,
        opcua_adresi: str,
        api_adresi: str,
        anahtar: str,
        kuyruk: Kuyruk,
        http: httpx.AsyncClient | None = None,
        nabiz: Path | None = None,
        ilk_bekleme: float = 1.0,
    ):
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(10, read=30))
        self.api_adresi, self.anahtar = api_adresi.rstrip("/"), anahtar
        self.gonderici = Gonderici(
            kuyruk, self.api_adresi, anahtar, self.http, ilk_bekleme=ilk_bekleme
        )
        self.izleyici = OpcUaIzleyici(
            opcua_adresi, kuyruk, haber_ver=self.gonderici.haber_ver, uzlas=self._uzlas,
            nabiz=nabiz, ilk_bekleme=ilk_bekleme,
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
    opcua_adresi = os.environ.get("OPCUA_ADRESI")
    if not anahtar or not opcua_adresi:
        sys.exit("ENTEGRASYON_ANAHTARI ve OPCUA_ADRESI tanımlı olmalı (bkz. README, entegrasyon).")
    api_adresi = os.environ.get("HATA_ASISTANI_ADRESI", "http://127.0.0.1:8000")
    kuyruk = Kuyruk(os.environ.get("KUYRUK_DOSYASI", "baglayici-kuyruk.sqlite"))
    nabiz = Path(os.environ["NABIZ_DOSYASI"]) if os.environ.get("NABIZ_DOSYASI") else None
    dur = asyncio.Event()
    for sinyal in (signal.SIGTERM, signal.SIGINT):
        try:
            asyncio.get_running_loop().add_signal_handler(sinyal, dur.set)
        except (NotImplementedError, AttributeError):  # Windows
            pass
    gunluk.info("bağlayıcı çalışıyor: %s -> %s (%d olay kuyrukta)", opcua_adresi, api_adresi,
                len(kuyruk))  # fmt: skip
    try:
        await Baglayici(opcua_adresi, api_adresi, anahtar, kuyruk, nabiz=nabiz).calis(dur)
    finally:
        kuyruk.kapat()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("asyncua").setLevel(logging.ERROR)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # her isteği yazmasın
    try:
        asyncio.run(_ana())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
