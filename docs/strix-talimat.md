Hedef: Hata Asistanı (depo adı: andon), fabrika verisi ve bakım kılavuzları üzerinde çalışan bir FastAPI + LLM agent uygulaması.
Kaynak kod /workspace altında, çalışan uygulama http://host.docker.internal:8000 adresinde.
Bütün veriler kurgusaldır; bu bir yerel test kurulumudur.

Kullanıcılar (POST /giris, OAuth2 parola formu, JWT döner):
- operator: yalnızca "operasyon" erişimli kılavuzları görebilir (PRES-OT-01, KAL-PR-01).
- bakim: bütün kılavuzları görebilir (PRES-BK-01, KR-BK-01 dahil).
- Parola her iki kullanıcı için: andon-demo (.env'de DEMO_PAROLA değiştirildiyse o).

Uç noktalar: /saglik, /giris, /hatlar, /arizalar, /oee, /ara, /chat, /rapor/vardiya,
/bakim-plani (yalnızca bakım), /dokumanlar/{kod}/pdf, /talepler/{talep_id}/fotograf (yalnızca
bakım), /telegram/kod, /telegram/durum, /telegram/baglanti, /kullanim (yalnızca bakım).

Telegram botu ayrı bir süreçtir (scripts/telegram_bot.py) ve bu kurulumda çalışmıyor; kaynak
kodu (app/telegram_bot.py) beyaz kutu olarak incelenebilir.

Öncelikli sorular:
1. Yetki sızıntısı: operator token'ıyla bakım kılavuzlarının (PRES-BK-01, KR-BK-01) içeriğine
   herhangi bir yoldan ulaşılabiliyor mu? /ara, /chat (prompt injection ile agent'ı kandırmak),
   /rapor/vardiya (LLM yorumu), /dokumanlar/{kod}/pdf (yol oynama, büyük/küçük harf, URL
   kodlama) dahil.
2. Rol bazlı uç noktalar: operator token'ıyla /bakim-plani, /kullanim ve
   /talepler/{talep_id}/fotograf'a ulaşılabiliyor mu; agent'ın bakim_plani_oner aracı operatöre
   plan döndürüyor mu?
3. JWT: alg=none, süresi dolmuş token, imza anahtarı tahmini, rol alanını değiştirme.
4. Agent araçları: LLM'e verilen ariza_say, oee_hesapla, bakim_plani_oner, stok_sorgula,
   dokuman_ara, bakim_talebi_olustur araçlarının argümanları üzerinden SQL injection veya başka
   bir tabloya erişim.
5. Yazma aracı: kullanıcı açıkça istemeden, ya da bir istekte birden fazla bakım talebi
   açtırılabiliyor mu (dolaylı prompt injection dahil)?
6. Telegram eşleştirmesi (kaynak kod): /telegram/kod'un ürettiği tek kullanımlık kodla başka bir
   kullanıcının hesabına bağlanmak, kodu tahmin etmek ya da yeniden kullanmak mümkün mü?
   Eşleşmemiş bir sohbete ya da gruba veri sızıyor mu?
7. Arayüz: sohbet cevabının HTML'e dönüştürülmesinde XSS (app/static/metin.js); OEE, rapor ve
   bakım planı ekranlarında sunucudan gelen metinlerin DOM'a yazılması.
8. Giriş denemelerinde hız sınırı olmadığı bilinen bir eksik; bunu bulgu olarak raporla ama
   kaba kuvvet denemesiyle zaman harcama.

Kapsam dışı: hizmet dışı bırakma (DoS) ve LLM kotasını tüketmeye yönelik testler; gerçek
Telegram sunucularına istek atmak.
