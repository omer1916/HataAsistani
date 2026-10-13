## 1. GENEL BİLGİLER

Bu kılavuz Kaynak 1 ve Kaynak 2 hatlarındaki robotlu kaynak hücrelerinin bakımını ve arıza giderme adımlarını tanımlar. Her hücrede şu ekipman bulunur:

- Kaynak Robotu 1 ve Kaynak Robotu 2 (makine kodları K1-KR1, K1-KR2, K2-KR1, K2-KR2): 6 eksenli, gazaltı (MIG/MAG) kaynağı yapan robotlar.
- Fikstür İstasyonu (K1-FK, K2-FK): parçaları kaynak sırasında pnömatik tutucularla sabitler.

Robotlar koruyucu gaz olarak %82 argon ve %18 CO2 karışımı kullanır.

### 1.1 Güvenlik kuralları

- Robot hücresine girmeden önce hücre kapısı kilitlenir ve robot manuel moda alınır. Kapı kilidinin anahtarı hücreye giren kişide kalır.
- Öğretme (teach) modunda robot hızı 250 mm/s ile sınırlıdır; bu sınır yükseltilmez.
- Torç ve kaynak bölgesi kaynaktan sonra uzun süre sıcak kalır. Torç bakımı en az 10 dakika soğuduktan sonra yapılır.
- Kaynak dumanı emişi çalışmıyorsa kaynak yapılmaz.

## 2. PERİYODİK BAKIM PLANI

### 2.1 Günlük

- Torç nozulunun içindeki kaynak çapağı temizlenir; nozul ve kontak meme gözle kontrol edilir. Aşınmış kontak meme değiştirilir.
- Otomatik torç temizleme istasyonunun çalıştığı ve çapak önleyici sıvı haznesinin dolu olduğu kontrol edilir.
- Gaz tüpü basıncı ve gaz debisi (12-15 L/dk) kontrol edilir.

### 2.2 Haftalık

- Tel sürme makaralarının aşınması ve baskı ayarı kontrol edilir.
- Torç kablosundaki tel kılavuzu (liner) basınçlı havayla temizlenir.

### 2.3 Aylık ve daha uzun aralıklar

- Aylık: robot kablo paketi (dress pack) sürtünme ve kırılma açısından, soğutma suyu seviyesi ise eksiklik açısından kontrol edilir.
- 6 ayda bir: robot eksen redüktörlerinin yağ seviyesi kontrol edilir.
- Yılda bir: robot kontrol ünitesindeki enkoder yedek bataryaları değiştirilir.

<!-- sayfa -->

## 3. KAYNAK KALİTESİ SORUNLARI

### 3.1 Gözenek (porozite)

Kaynak dikişinde gaz boşlukları görülüyorsa sorun büyük olasılıkla koruyucu gazdadır:

1) Gaz debisini kontrol edin; 12-15 L/dk olmalıdır.
2) Gaz hortumunda ve bağlantılarda kaçak arayın (sabunlu su testi).
3) Nozul çapakla tıkanmışsa temizleyin.
4) Hücrede kaynak bölgesine doğru hava akımı (açık kapı, fan) olup olmadığını kontrol edin.
5) Sacın yağlı veya paslı olup olmadığına bakın.

### 3.2 Aşırı sıçrama

Kaynak voltajı ve tel sürme hızı, kaynak programındaki değerlerden sapmış olabilir. Kontak memenin aşınması da ark kararsızlığına ve sıçramaya neden olur; bu durumda kontak meme değiştirilir.

### 3.3 Tel sıkışması

Tel sürme makaralarının baskısı fazla veya az olabilir. Liner kirlenmişse tel takılır; liner temizlenir ya da değiştirilir. Kontak memenin çapı telin çapına uygun olmalıdır (1,0 mm tel için 1,0 mm meme).

### 3.4 Yetersiz nüfuziyet

Kaynak akımı düşük veya kaynak hızı yüksek olabilir. Torç açısı ve torç ile parça arasındaki mesafe (12-15 mm) kontrol edilir. Program değerlerinde değişiklik yalnızca proses mühendisinin onayıyla yapılır.

<!-- sayfa -->

## 4. ROBOT ARIZALARI

### 4.1 Pozisyon hatası ve referans kaybı

Robot programdaki noktalara ulaşamıyor veya dikiş kayıyorsa:

1) Robotu manuel modda referans pozisyonuna gönderin ve eksen değerlerini referans kayıtlarıyla karşılaştırın.
2) Kontrol ünitesinde "enkoder batarya düşük" uyarısı olup olmadığına bakın. Batarya zamanında değiştirilmezse robot kapatıldığında eksen konumlarını kaybeder ve tüm eksenlerin yeniden kalibre edilmesi gerekir.
3) Robot yakın zamanda bir yere çarptıysa torç ucu noktası (TCP) kalibrasyonu yapın. Kalibrasyon mastarıyla ölçülen TCP sapması 0,5 mm'den küçük olmalıdır.

### 4.2 Program durması ve panel donması

PLC ile robot arasındaki haberleşme koptuğunda robot programı durur. Haberleşme kablosu ve bağlantı noktaları kontrol edilir. Operatör paneli donmuşsa panel yeniden başlatılır; robot kontrol ünitesi kapatılmaz. Aynı hata sık tekrarlıyorsa kontrol ünitesinin hata kayıtları indirilir ve robot üreticisinin servisine gönderilir.

### 4.3 Çarpışma algılama

Robot bir çarpışma algıladığında durur. Çarpışmanın nedeni (yanlış yerleştirilmiş parça, fikstür arızası, programda yapılan değişiklik) bulunmadan robot otomatik moda alınmaz. Çarpışmadan sonra torç boynu eğilmişse değiştirilir ve TCP kalibrasyonu yapılır.

### 4.4 Servo motor aşırı akım

Eksenlerden birinde aşırı akım alarmı alınıyorsa önce o eksende mekanik bir engel olup olmadığı kontrol edilir. Kablo paketinin eksene sürtünmesi veya sıkışması da aşırı akıma yol açabilir.

<!-- sayfa -->

## 5. FİKSTÜR İSTASYONU VE PNÖMATİK

Fikstür istasyonundaki tutucular pnömatik silindirlerle çalışır. Hava besleme basıncı 6 bar olmalıdır.

### 5.1 Tutucu kapanmıyor veya parçayı gevşek tutuyor

1) Hat hava basıncını şartlandırıcı (filtre-regülatör) üzerindeki manometreden okuyun. Basınç 5,5 bar'ın altındaysa şartlandırıcının filtresi tıkanmış olabilir (stok kodu PNO-004).
2) Silindirde hava kaçağı olup olmadığını dinleyin. Kaçak yapan silindir değiştirilir (PNO-001).
3) Valf adasındaki ilgili valfin manuel butonuyla silindiri çalıştırın. Silindir elle çalışıyor ama PLC komutuyla çalışmıyorsa solenoid valf bobini arızalıdır (PNO-002).

### 5.2 Parça algılama hatası

Fikstürdeki parça varlık sensörleri endüktif sensördür (SNS-001). Sensör yüzeyi kaynak çapağıyla kaplanırsa yanlış algılama yapar; sensör yüzeyi temizlenir ve sensör ile parça arasındaki mesafe kontrol edilir.

## 6. KAYIT

Her müdahale Hata Asistanı'na iş emri olarak kaydedilir. Kayıtta robot alarm numarası, yapılan kontroller ve değiştirilen parçanın stok kodu belirtilir. Kaynak kalitesiyle ilgili sorunlarda kalite birimine de haber verilir ve etkilenen parçalar KAL-PR-01'e göre ayrılır.
