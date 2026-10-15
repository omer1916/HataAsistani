"""Makine tarafı: PLC/CNC'lerden alarm okuyup Hata Asistanı'na gönderen bağlayıcı.

Hata Asistanı API'sinden ayrı bir süreçtir ve onun koduna bağımlı değildir (httpx ve protokol
kütüphaneleri); fabrika ağında makinelerin yanında çalışabilir.

- kuyruk.py:    gönderilmeyi bekleyen olaylar, diskte (bağlayıcı yeniden başlasa da kaybolmaz)
- gonderici.py: kuyruğu API'ye sırayla, yeniden deneyerek boşaltır
- izleyici.py:  bütün protokollerin ortak kısmı: değişimden olay, yeniden bağlanma, uzlaşma
- opcua.py:     OPC UA (Siemens S7-1500, Beckhoff, Fanuc, Kepware; abonelikle)
- modbus.py:    Modbus TCP (basit PLC'ler; bit haritasıyla, yoklayarak)
- mqtt.py:      MQTT (IoT cihazları, Node-RED ve diğer ağ geçitleri)
- mtconnect.py: MTConnect (CNC'ler: Haas, Mazak, Okuma; ajanı yoklayarak)
- simulator.py: Pres 3 hattının PLC'si gibi davranan simülatör, dört protokolü de konuşur
- baglayici.py: hepsini çalıştırır, protokolü adresten seçer (python -m entegrasyon.baglayici)

Teslim garantisi: olay kuyruktan ancak API kabul ettikten sonra silinir (en az bir kez); API
aynı olay_id'yi ikinci kez işlemez (tam bir kez etkisi). Olaylar kaynak başına sırayla gider.
"""
