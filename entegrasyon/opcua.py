"""OPC UA: sunucudaki makine alarmlarını abonelikle izler.

Adres alanı kuralı: Objects/<makine kodu>/Alarmlar/<alarm kodu>, her alarm bir bool değişken
(true: alarm var). Makineler ve alarmlar bağlanınca bulunur, yapılandırma gerekmez. Gerçek bir
fabrikada bu yapı PLC'nin kendi OPC UA sunucusunda (Siemens S7-1500, Beckhoff, Fanuc OPC UA
seçeneği) ya da bir ağ geçidinde (Kepware, Node-RED) kurulur. Olay zamanı değerin kaynak zaman
damgasıdır (PLC'nin saati). Ortak davranış: entegrasyon/izleyici.py.
"""

import asyncio
import logging

from asyncua import Client

from entegrasyon.izleyici import Izleyici, olay_kimligi, utc

__all__ = ["ALARM_KLASORU", "OpcUaIzleyici", "olay_kimligi"]

gunluk = logging.getLogger("entegrasyon.opcua")
ALARM_KLASORU = "Alarmlar"


class OpcUaIzleyici(Izleyici):
    PROTOKOL = "OPC UA"

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
            self._izlemeye_basla(len(alarmlar))
            makineler = sorted({m for m, _ in alarmlar.values()})
            gunluk.info(
                "%s: %d makine, %d alarm izleniyor",
                self.gorunen_adres,
                len(makineler),
                len(alarmlar),
            )
            izleyici = self

            class Isleyici:
                def datachange_notification(self, node, deger, veri):
                    makine, kod = alarmlar[node.nodeid]
                    dv = veri.monitored_item.Value
                    izleyici.degisti(
                        makine, kod, bool(deger), utc(dv.SourceTimestamp or dv.ServerTimestamp)
                    )

                def status_change_notification(self, durum):
                    koptu.set()  # abonelik sunucuda kapandı

            abonelik = await istemci.create_subscription(100, Isleyici())
            await abonelik.subscribe_data_change([istemci.get_node(n) for n in alarmlar])
            await self._ilk_degerleri_bekle()
            await self._izle(dur, koptu, canli=istemci.nodes.server_state.read_value)
