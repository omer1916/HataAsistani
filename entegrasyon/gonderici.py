"""Kuyruktaki olayları Hata Asistanı API'sine sırayla gönderir.

- Olaylar en fazla `parti` adetlik paketlerle, kuyruk sırasıyla gider; paket ancak API kabul
  edince (200) kuyruktan silinir. Paketteki tek tek reddedilen olaylar (gecersiz, hata)
  kalıcıdır: günlüğe yazılır ve silinir, yoksa kuyruk sonsuza kadar tıkanırdı.
- Ağ hatası ya da 5xx: artan bekleme (1, 2, 4 ... en fazla 60 sn, rastgele kaydırmalı; birçok
  bağlayıcı aynı anda API'ye yüklenmesin), sonra aynı paket.
- 429: API'nin istediği kadar (Retry-After) bekler.
- 401: anahtar yanlış ya da kaynak kapalı. Olaylar silinmez; 30 sn arayla yeniden dener.
- 413 ya da 422 (paket bütünüyle reddedildi): paket ikiye bölünür; tek olay da reddediliyorsa o
  olay günlüğe yazılıp atılır.
"""

import asyncio
import logging
import random

import httpx

from entegrasyon.kuyruk import Kuyruk

gunluk = logging.getLogger("entegrasyon.gonderici")
KALICI_RET = ("gecersiz", "hata")


async def bekle(dur: asyncio.Event, sure: float) -> None:
    """`sure` saniye ya da `dur` gelene kadar bekler."""
    try:
        await asyncio.wait_for(dur.wait(), timeout=max(0.0, sure))
    except TimeoutError:
        pass


class Gonderici:
    def __init__(
        self,
        kuyruk: Kuyruk,
        adres: str,
        anahtar: str,
        http: httpx.AsyncClient | None = None,
        parti: int = 100,
        en_uzun_bekleme: float = 60.0,
        ilk_bekleme: float = 1.0,
    ):
        self.kuyruk, self.adres, self.anahtar = kuyruk, adres.rstrip("/"), anahtar
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(10, read=30))
        self.parti, self.en_uzun_bekleme, self.ilk_bekleme = parti, en_uzun_bekleme, ilk_bekleme
        self._parti = parti
        self._ardisik_hata = 0
        self._yeni = asyncio.Event()
        self.kabul = 0  # API'nin işlediği ya da zaten bildiği olay
        self.atilan = 0  # kalıcı olarak reddedilip atılan olay
        self.sonuclar: list[dict] = []  # son paketin sonuçları (izleme ve testler için)

    def haber_ver(self) -> None:
        """Kuyruğa olay eklendi: bekliyorsa hemen uyansın."""
        self._yeni.set()

    async def bir_tur(self) -> float | None:
        """Bir paket göndermeyi dener. Kuyruk boşsa None; değilse bir sonraki denemeden önce
        beklenecek saniye (0: hemen devam)."""
        parti = self.kuyruk.al(self._parti)
        if not parti:
            return None
        try:
            cevap = await self.http.post(
                f"{self.adres}/entegrasyon/olaylar",
                json={"olaylar": [olay for _, olay in parti]},
                headers={"X-API-Anahtari": self.anahtar},
            )
            sonuclar = cevap.json()["sonuclar"] if cevap.status_code == 200 else None
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as hata:
            return self._geri_cekil(f"API'ye ulaşılamadı ({type(hata).__name__})")

        if sonuclar is not None and len(sonuclar) == len(parti):
            for (_, olay), sonuc in zip(parti, sonuclar, strict=True):
                if sonuc.get("durum") in KALICI_RET:
                    self.atilan += 1
                    gunluk.warning(
                        "olay reddedildi ve atıldı: %s (%s)", olay.get("olay_id"), sonuc.get("hata")
                    )
                else:
                    self.kabul += 1
            self.kuyruk.sil([no for no, _ in parti])
            self.sonuclar = sonuclar
            self._ardisik_hata = 0
            self._parti = min(self.parti, self._parti * 2)  # bölünmüş paket yeniden büyüsün
            return 0.0
        if cevap.status_code == 429:
            try:
                return max(self.ilk_bekleme, float(cevap.headers.get("retry-after", "1")))
            except ValueError:
                return self.ilk_bekleme
        if cevap.status_code == 401:
            gunluk.error(
                "API anahtarı reddetti (yanlış anahtar ya da kaynak kapalı); %d olay bekliyor",
                len(self.kuyruk),
            )
            return 30 * self.ilk_bekleme
        if cevap.status_code in (413, 422):
            if len(parti) > 1:
                self._parti = max(1, len(parti) // 2)
                return 0.0
            no, olay = parti[0]
            self.atilan += 1
            self.kuyruk.sil([no])
            gunluk.error(
                "API olayı bütünüyle reddetti (%d), atıldı: %s",
                cevap.status_code,
                olay.get("olay_id"),
            )
            return 0.0
        return self._geri_cekil(f"API {cevap.status_code} döndü")

    def _geri_cekil(self, neden: str) -> float:
        self._ardisik_hata += 1
        sure = min(self.en_uzun_bekleme, self.ilk_bekleme * 2 ** (self._ardisik_hata - 1))
        sure *= random.uniform(0.8, 1.2)
        gunluk.warning(
            "%s; %.1f sn sonra yeniden denenecek (%d olay bekliyor)", neden, sure, len(self.kuyruk)
        )
        return sure

    async def calis(self, dur: asyncio.Event) -> None:
        while not dur.is_set():
            self._yeni.clear()
            sure = await self.bir_tur()
            if sure is None:  # kuyruk boş: yeni olay ya da 1 sn
                try:
                    await asyncio.wait_for(self._yeni.wait(), timeout=1)
                except TimeoutError:
                    pass
            elif sure > 0:
                await bekle(dur, sure)

    async def bosalt(self, zaman_asimi: float) -> bool:
        """Kuyruk boşalana kadar (en fazla `zaman_asimi` sn) bekler; boşaldıysa True."""
        son = asyncio.get_running_loop().time() + zaman_asimi
        while len(self.kuyruk):
            if asyncio.get_running_loop().time() > son:
                return False
            await asyncio.sleep(0.05)
        return True
