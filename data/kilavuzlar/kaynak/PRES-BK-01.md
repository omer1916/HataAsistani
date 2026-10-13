## 1. GENEL BİLGİLER

Bu kılavuz Pres 1, Pres 2 ve Pres 3 hatlarındaki ekipmanın periyodik bakımını ve arıza giderme adımlarını tanımlar. Her pres hattında üç ana ekipman bulunur:

- Hidrolik Pres (makine kodları P1-HP, P2-HP, P3-HP): 400 ton nominal kuvvetli, tek etkili hidrolik pres.
- Rulo Besleme Ünitesi (P1-RB, P2-RB, P3-RB): sac ruloyu açar, düzeltir ve prese besler.
- Transfer Robotu (P1-TR, P2-TR, P3-TR): basılan parçayı vantuzlarla alıp konveyöre bırakır.

Kılavuz bakım teknisyenleri ve bakım mühendisleri içindir. Operatörlerin yapacağı kontroller PRES-OT-01 Pres Hattı Operatör Talimatı'nda anlatılır.

### 1.1 Güvenlik kuralları

- Hidrolik sisteme, kalıp bölgesine veya elektrik panosuna müdahaleden önce kilitleme-etiketleme (LOTO) uygulanır. Ana şalter kilitlenir, kilidin anahtarı müdahaleyi yapan kişide kalır.
- Pres kapatıldıktan sonra hidrolik devrede basınç kalabilir. Akümülatör boşaltma vanası açılır ve mekanik manometrede 0 bar görülmeden hortum veya rakor sökülmez.
- Koç (üst tabla) bakım için yukarıda bırakılacaksa mutlaka emniyet takozları yerleştirilir.
- Hidrolik yağı 50 °C'nin üzerinde olabilir. Isıya dayanıklı eldiven ve gözlük kullanılır.
- Kalıp bölgesinde tek kişiyle çalışılmaz.

<!-- sayfa -->

## 2. TEKNİK ÖZELLİKLER

### 2.1 Hidrolik pres

- Nominal kuvvet: 400 ton
- Normal çalışma basıncı: 200-215 bar
- Emniyet valfi ayarı: 230 bar; sistemin mutlak üst sınırı 250 bar
- Hidrolik tank hacmi: 800 litre
- Hidrolik yağı: ISO VG 46 (stok kodu HYD-002)
- Normal yağ sıcaklığı: 40-55 °C
- Basınç sensörü: 0-250 bar, 4-20 mA çıkışlı (stok kodu SNS-002)
- Ana pompa: değişken debili eksenel pistonlu pompa, 90 kW motor

### 2.2 Alarm kodları

Operatör panelinde görülen alarm kodları ve anlamları:

- A101 Hidrolik basınç düşük: basınç 30 saniyeden uzun süre 180 bar'ın altında kaldı.
- A102 Yağ sıcaklığı yüksek: yağ sıcaklığı 65 °C'yi geçti. 75 °C'de pres kendini durdurur.
- A103 Yağ seviyesi düşük: tanktaki şamandıra minimum seviyenin altında.
- A104 Filtre tıkanık: dönüş filtresindeki fark basıncı 5 bar'ı geçti.
- A201 Işık bariyeri ihlali: koruma alanına giriş algılandı.
- A202 Kalıp emniyet pimi takılı değil.
- A301 Motor sürücü hatası: ana pompa motorunun sürücüsü arıza verdi.
- A302 Acil stop devresi açık: acil stop butonlarından biri basılı ya da devre kopuk.
- A401 Besleme hatası: rulo bitti veya sac sıkıştı.
- A501 Transfer robotu pozisyon hatası.

<!-- sayfa -->

## 3. PERİYODİK BAKIM PLANI

Periyodik bakım Hata Asistanı'nda "periyodik" tipli iş emri olarak açılır ve kapatılırken yapılan işler kayda geçirilir.

### 3.1 Günlük (operatör ve bakım birlikte)

- Tank üzerindeki yağ seviye göstergesi yeşil bölgede mi?
- Presin altında ve hortum bağlantılarında yağ lekesi var mı?
- Paneldeki basınç değeri çalışma sırasında 200-215 bar aralığında mı?

### 3.2 Haftalık

- Dönüş filtresinin fark basıncı göstergesi okunur. Gösterge sarı bölgeye girdiyse filtre değişimi planlanır.
- Hortumlar sürtünme, çatlak ve şişme açısından kontrol edilir.
- Işık bariyeri test çubuğuyla denenir.

### 3.3 Aylık

- Yağ numunesi alınarak partikül ve su analizi için laboratuvara gönderilir.
- Akümülatörün azot ön dolum basıncı kontrol edilir; değer 130 bar olmalıdır.
- Kalıp yayları (stok kodu MEK-004) kırık ve yorulma açısından kontrol edilir.

### 3.4 Her 2000 çalışma saatinde veya 6 ayda bir

- Hidrolik yağ filtresi değiştirilir (stok kodu HYD-001).
- Yağ analizi sonucu kötüyse yağ tamamen değiştirilir ve tank temizlenir.

### 3.5 Yıllık

- Pompa conta seti (HYD-004) ve emniyet valfi ayarı kontrol edilir.
- 5 yaşını dolduran hidrolik hortumlar, görünüşleri iyi olsa bile değiştirilir.

<!-- sayfa -->

## 4. HİDROLİK ARIZALARDA İLK KONTROL

### 4.1 Genel yaklaşım

Pres hattında görülen arızaların en büyük grubu hidrolik arızalardır. Deneyimler, bu arızaların önemli bir kısmında asıl sorunun hidrolik devrede değil basıncı ölçen sensörde olduğunu göstermiştir. Bu nedenle bir hidrolik arızada ilk kontrol her zaman hidrolik basınç sensörüdür: panelde okunan basınç değeri, tank bloğu üzerindeki mekanik manometrenin gösterdiği değerle karşılaştırılır.

- İki değer arasındaki fark 10 bar'dan azsa sensör sağlamdır; arıza hidrolik devrededir.
- Fark 10 bar'dan fazlaysa sorun büyük olasılıkla sensör veya kablosundadır. Sensör Bölüm 6.1'e göre kontrol edilir.

### 4.2 İlk kontrol sırası

Pompa veya valf sökülmeden önce aşağıdaki kontroller sırayla yapılır:

1) Paneldeki alarm kodunu ve alarm anındaki basınç ve sıcaklık değerlerini kaydedin.
2) Panel basıncını mekanik manometreyle karşılaştırın (Bölüm 4.1).
3) Yağ seviyesini ve yağ sıcaklığını kontrol edin.
4) Dönüş filtresinin fark basıncı göstergesine bakın.
5) Hortum, rakor ve silindir keçelerinde gözle kaçak arayın.
6) Pompa çalışırken olağan dışı ses (ıslık, kavitasyon sesi) olup olmadığını dinleyin.

Bu altı adım çoğu arızada sorunun yerini gösterir ve gereksiz parça değişimini önler. Kontrollerin sonucu iş emrine yazılır.

<!-- sayfa -->

### 4.3 Hidrolik basınç düşüklüğü (A101)

Sensörün sağlam olduğu doğrulandıktan sonra olası nedenler şunlardır:

- Emniyet valfinin ayarı kaymış olabilir. Ayar 230 bar olmalıdır; ayar vidasına dokunmadan önce mevcut değer kayda geçirilir.
- Oransal valf (stok kodu HYD-003) komutlara tepki vermiyor olabilir. Valf bobininin direnci ölçülür; 20-24 ohm dışındaki değerler bobin arızasını gösterir.
- Pompa aşınmış olabilir. Pompa çıkışında basınç yükselmiyorsa ve pompa gövdesi aşırı ısınıyorsa pompa revizyonu planlanır.
- Silindir içinde keçe kaçağı olabilir. Koç yük altında aşağı doğru kayıyorsa iç kaçaktan şüphelenilir.

### 4.4 Yağ kaçağı

Kaçak yeri önce temizlenir, sonra pres kısa süre çalıştırılarak kaçağın tam kaynağı bulunur. Rakorlardaki kaçakta rakor torku kontrol edilir; sızıntı sürüyorsa conta veya O-ring değiştirilir. Hasarlı hortumun yerine aynı basınç sınıfında hortum takılır (stok kodu HYD-005).

Kaçak nedeniyle eksilen yağ, filtreli dolum arabasıyla tamamlanır. Tanka asla açık kaptan yağ dökülmez.

<!-- sayfa -->

### 4.5 Yağ sıcaklığı yüksek (A102)

- Eşanjörün soğutma suyu vanalarının açık olduğunu ve suyun aktığını kontrol edin.
- Hava soğutmalı modellerde fanın çalıştığını ve radyatör peteklerinin tozla kapanmadığını kontrol edin.
- Yağ seviyesini kontrol edin; düşük yağ seviyesi sıcaklığı hızla artırır.
- Pres uzun süre yüksek basınçta bekletiliyorsa (örneğin kalıp kapalı bekleme) bekleme süresi kısaltılır.

Sıcaklık 75 °C'ye ulaştığında pres kendini durdurur. Yağ 55 °C'nin altına inmeden pres yeniden çalıştırılmaz.

### 4.6 Yağ seviyesi düşük (A103)

Önce kaçak aranır (Bölüm 4.4). Kaçak yoksa ve seviye yavaş yavaş düşüyorsa silindir keçeleri kontrol edilir. Eksik yağ ISO VG 46 (HYD-002) ile tamamlanır. Farklı viskozitede yağ karıştırılmaz.

### 4.7 Filtre tıkanık (A104)

Dönüş filtresi değiştirilir (HYD-001). Yeni filtre kısa sürede yine tıkanıyorsa yağda aşırı partikül vardır; yağ analizi istenir ve pompa aşınması araştırılır.

<!-- sayfa -->

## 5. MEKANİK VE ELEKTRİK ARIZALAR

### 5.1 Kalıp sıkışması

Kalıp içinde sıkışan parça kesinlikle elle çıkarılmaz. Pres ayar moduna alınır, koç düşük hızda yukarı kaldırılır ve emniyet takozları yerleştirildikten sonra parça aletle çıkarılır. Sıkışmanın nedeni (sac kalınlığı, yağlama, kalıp aşınması) iş emrine yazılır.

### 5.2 Aşırı titreşim ve rulman sesi

Titreşim ölçüm cihazıyla motor ve pompa yataklarında ölçüm yapılır. 4,5 mm/s üzerindeki değerler rulman hasarına işaret eder. Pompa motoru rulmanları 6205 tipidir (stok kodu MEK-001). Bağlantı cıvataları gevşemişse tork anahtarıyla yeniden sıkılır (M12 cıvata seti MEK-005).

### 5.3 Motor sürücü hatası (A301)

Sürücünün kendi ekranındaki hata kodu okunur ve kayda geçirilir. Aşırı akım hatalarında önce motor kabloları ve klemensler kontrol edilir. Sürücünün soğutma fanı çalışmıyorsa sürücü aşırı ısınıp kapanır. Sürücü kartı arızasında kart değiştirilir (stok kodu ELK-003); değişimden sonra motor parametreleri yedekten yüklenir.

### 5.4 Acil stop devresi açık (A302)

Hattaki tüm acil stop butonları gözle kontrol edilir ve basılı olan buton çekilerek bırakılır. Tüm butonlar serbest olduğu hâlde alarm sürüyorsa devrede kablo kopukluğu veya yapışmış kontaktör (ELK-001) aranır. Acil stop devresi hiçbir koşulda köprülenmez.

<!-- sayfa -->

## 6. SENSÖR ARIZALARI

### 6.1 Hidrolik basınç sensörünün kontrolü

Basınç sensörü 0-250 bar aralığını 4-20 mA akım sinyaline çevirir. 0 bar 4 mA'e, 250 bar 20 mA'e karşılık gelir; normal çalışma basıncı olan 210 bar'da yaklaşık 17,4 mA okunmalıdır.

1) Pres çalışırken sensör sinyal hattına seri bağlanan multimetreyle akım ölçülür.
2) Okunan akım, mekanik manometredeki basınca karşılık gelen akımla karşılaştırılır.
3) Akım 4 mA'in altındaysa veya hiç yoksa kablo ve konnektör kontrol edilir.
4) Kablo sağlam olduğu hâlde değer yine hatalıysa sensör değiştirilir.

Sensör değişimi için pres durdurulur, LOTO uygulanır ve hidrolik basınç tamamen boşaltılır. Eski sensör sökülür, yenisi (stok kodu SNS-002) 30 Nm torkla takılır. Değişimden sonra paneldeki ölçüm mekanik manometreyle yeniden karşılaştırılır.

### 6.2 Işık bariyeri (A201)

Bariyer sebepsiz devreye giriyorsa önce verici ve alıcı lensleri temizlenir, ardından hizalama kontrol edilir. Sorun sürerse alıcı ünitesi değiştirilir (SNS-003). Işık bariyeri hiçbir koşulda devre dışı bırakılmaz.

### 6.3 Endüktif sensörler

Koç konumunu algılayan endüktif sensörlerin (SNS-001) algılama mesafesi 8 mm'dir. Sensör ile hedef arasındaki mesafe 4-6 mm'ye ayarlanır.

<!-- sayfa -->

## 7. RULO BESLEME ÜNİTESİ VE TRANSFER ROBOTU

### 7.1 Besleme hatası (A401)

Rulo bittiyse operatör rulo değişimini yapar. Sac sıkışmasında düzeltici merdanelerin baskı ayarı ve sac kenarındaki çapak kontrol edilir. Besleme adımı kaymışsa besleme ünitesinin enkoderi (SNS-005) ve pnömatik kavraması kontrol edilir.

### 7.2 Transfer robotu pozisyon hatası (A501)

Robot manuel modda referans noktasına gönderilir (referanslama). Vantuzların parçayı tutamaması da pozisyon hatasına yol açabilir; vakum değeri en az -0,6 bar olmalıdır. Yırtık vantuzlar değiştirilir (PNO-003).

## 8. ARIZA KAYDI VE ESKALASYON

- Her arıza Hata Asistanı'na kaydedilir. Kayıtta alarm kodu, yapılan kontroller ve değiştirilen parçanın stok kodu yer alır.
- Yüksek önemli bir arıza 30 dakika içinde giderilemezse bakım şefine haber verilir.
- Hat 2 saatten uzun süre durursa üretim müdürü bilgilendirilir.
- Aynı makinede bir ay içinde üçten fazla aynı tip arıza görülürse kök neden analizi başlatılır.
