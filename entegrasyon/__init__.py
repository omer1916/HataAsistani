"""Makine tarafı: PLC/CNC'lerden alarm okuyup Hata Asistanı'na gönderen bağlayıcı.

Hata Asistanı API'sinden ayrı bir süreçtir ve onun koduna bağımlı değildir (yalnızca httpx ve
asyncua); fabrika ağında makinelerin yanında çalışabilir.

- kuyruk.py:    gönderilmeyi bekleyen olaylar, diskte (bağlayıcı yeniden başlasa da kaybolmaz)
- gonderici.py: kuyruğu API'ye sırayla, yeniden deneyerek boşaltır
- opcua.py:     OPC UA sunucusundaki alarmları izler, değişiklikleri kuyruğa olay olarak yazar
- simulator.py: Pres 3 hattının PLC'si gibi davranan OPC UA sunucusu (demo ve testler için)
- baglayici.py: hepsini çalıştırır (python -m entegrasyon.baglayici)

Teslim garantisi: olay kuyruktan ancak API kabul ettikten sonra silinir (en az bir kez); API
aynı olay_id'yi ikinci kez işlemez (tam bir kez etkisi). Olaylar kaynak başına sırayla gider.
"""
