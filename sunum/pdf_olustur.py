"""Hata Asistanı tanıtım PDF'ini (A4, 4 sayfa) üretir ve Chrome ile yazdırır.

    python sunum/ekran_goruntuleri.py --adres http://127.0.0.1:8000   # önce ekranlar
    python sunum/pdf_olustur.py

Ekran görüntüleri çalışan uygulamadan alınır (sunum/ekranlar/); bu betik onlardan ilgili
bölgeleri kırpar, tanitim.html'i yazar ve Chrome/Edge'in başsız modu ile PDF'e basar.
Görünüm Isıtmalı Güneş Bankı sunumuyla aynı aileden: iki sunum art arda gösteriliyor.
"""

import shutil
import subprocess
from pathlib import Path

from PIL import Image

KLASOR = Path(__file__).resolve().parent
EKRANLAR = KLASOR / "ekranlar"
KESITLER = EKRANLAR / "kesit"
CIKTI_HTML = KLASOR / "tanitim.html"
CIKTI_PDF = KLASOR / "Hata-Asistani-Tanitim.pdf"
CHROME_ADAYLARI = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "chrome",
    "google-chrome",
    "chromium",
)

# Ekran görüntülerinden alınacak bölgeler (piksel; görüntüler 2x ölçekle alınır).
KESIT = {
    # Uygulama içeriği ekranın ortasında; yanlardaki boşluk ve üst çubuk kırpılır, yazı büyür.
    "sohbet": (430, 125, 2130, 1340),  # soru, cevap, kaynaklar ve çağrılan araçlar
    "kaynak": (300, 83, 2260, 1250),  # kaynak tıklanınca açılan kılavuz penceresi
    "oee": (180, 140, 2380, 1250),  # göstergeler ve günlük OEE
    "oee-ust": (180, 140, 2380, 640),  # yalnızca göstergeler
    "rapor": (180, 120, 2380, 1120),  # göstergeler ve asistanın denetlenmiş yorumu
    "plan": (540, 455, 2668, 1410),  # özet ve planın seçtiği makineler
    "makine": (178, 340, 2302, 1125),  # özet, süren alarm ve bağlı kaynak
    "periyodik": (178, 440, 2302, 1135),  # özet, geciken ve yaklaşan bakım
}

# Simülatörden verilen A101 alarmında botun gönderdiği mesaj (app/telegram_bot.py
# _ariza_mesaji, 15 Ekim 2026 13:47; kılavuz alıntısı burada kısaltıldı).
TELEGRAM_MESAJI = """
<p><b><span class="emoji">🔴</span> Pres 3 durdu</b> (13:47)<br>P3-HP Hidrolik Pres: hidrolik arıza, yüksek önem<br>
<i>A101 Hidrolik basınç düşük</i></p>
<p><b><span class="emoji">📖</span> Kılavuz (PRES-BK-01, s. 5)</b><br>Sensörün sağlam olduğu doğrulandıktan sonra olası
nedenler: • Emniyet valfinin ayarı kaymış olabilir. Ayar 230 bar olmalıdır… • Oransal valf
(stok kodu HYD-003) komutlara tepki vermiyor olabilir…</p>
<p><b><span class="emoji">📦</span> İlgili yedek parçalar</b><br>• HYD-001 Hidrolik yağ filtresi: 14 adet<br>
• HYD-002 Hidrolik yağı ISO VG 46: 180 litre <span class="emoji">⚠️</span> minimumun altında<br>
• HYD-003 Oransal valf: 2 adet</p>
"""


def kesitleri_hazirla() -> None:
    KESITLER.mkdir(exist_ok=True)
    for ad, kutu in KESIT.items():
        kaynak = ad.removesuffix("-ust")  # oee-ust: oee.png'nin üst kısmı
        with Image.open(EKRANLAR / f"{kaynak}.png") as goruntu:
            goruntu.crop(kutu).save(KESITLER / f"{ad}.png", optimize=True)


CSS = """
@page { size: A4; margin: 0; }
:root {
  --murekkep: #14182b; --murekkep-2: #464d66; --soluk: #77809a; --cizgi: #e2e5ee; --zemin-2: #f3f5fa;
  --vurgu: #1f4fd1; --yesil: #16a34a; --sari: #e0a800; --kirmizi: #dc2626; --gece: #0b1020;
  --baslik: "Barlow Condensed", "Arial Narrow", "Segoe UI", sans-serif;
  --metin: "Barlow", "Segoe UI", Arial, sans-serif;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body { font-family: var(--metin); color: var(--murekkep); font-size: 10.3pt; line-height: 1.42;
  -webkit-print-color-adjust: exact; print-color-adjust: exact; }
h1, h2, h3 { font-family: var(--baslik); margin: 0; line-height: 1.05; text-wrap: balance; }
h2 { font-size: 24pt; font-weight: 650; margin: 4mm 0 2.5mm; max-width: 172mm; }
h3 { font-size: 13pt; font-weight: 600; margin-bottom: 1.5mm; }
p { margin: 0 0 2mm; }
.sayfa { width: 210mm; height: 297mm; padding: 14mm 17mm 13mm; position: relative; overflow: hidden;
  page-break-after: always; break-after: page; }
.sayfa:last-child { page-break-after: auto; break-after: auto; }
.ust-serit { display: flex; justify-content: space-between; align-items: baseline;
  border-bottom: 0.4mm solid var(--cizgi); padding-bottom: 2.5mm; font-size: 9pt; color: var(--soluk); }
.ust-serit span:first-child { font-family: var(--baslik); font-size: 13pt; color: var(--murekkep); }
.alt-not { position: absolute; left: 17mm; right: 17mm; bottom: 7mm; display: flex;
  justify-content: space-between; font-size: 7.5pt; color: var(--soluk); }
.giris { font-size: 10.8pt; color: var(--murekkep-2); max-width: 174mm; margin-bottom: 3.5mm; }
.logo { height: 0.95em; width: auto; aspect-ratio: 83 / 56; vertical-align: -0.12em; margin-right: 0.3em; }

/* Kapak */
.kapak { background: var(--gece); color: #eef1ff; display: flex; flex-direction: column; }
.kapak-ust { display: flex; justify-content: space-between; align-items: center; font-size: 9.5pt;
  color: #aab3d4; }
.kapak .marka { font-family: var(--baslik); font-weight: 650; font-size: 15pt; color: #f4f6ff; }
.kapak h1 { font-size: 33pt; font-weight: 700; margin-top: 9mm; letter-spacing: -0.01em; line-height: 1.0; }
.kapak-alt { font-size: 11.5pt; color: #c3cae4; max-width: 172mm; margin-top: 4.5mm; }
.ayaklar { display: grid; grid-template-columns: repeat(3, 1fr); gap: 3mm; margin-top: 5mm; }
.ayak { background: #151c33; border: 0.3mm solid #263055; border-radius: 2.5mm; padding: 3mm 3.5mm; }
.ayak b { font-family: var(--baslik); font-size: 13pt; font-weight: 600; color: #fff; display: block;
  margin-bottom: 1mm; }
.ayak b span { color: #f5a524; margin-right: 1.5mm; }
.ayak p { font-size: 9pt; color: #b8c0de; margin: 0; }
.kapak .ekran { margin-top: 5mm; }
.kapak .ekran img { border: 0.5mm solid #2a3150; }
.kapak .ekran figcaption { color: #8f98bb; }
.kapak-ekip { margin-top: auto; display: flex; gap: 4mm; align-items: baseline; font-size: 12pt; }
.kapak-ekip span { color: #8f98bb; font-size: 10pt; }
.esin { margin-top: 4mm; padding: 3mm 4.5mm; border-radius: 2.5mm; background: #18203a;
  font-size: 9.6pt; color: #d5dbf2; }
.esin b { color: #fff; }

/* Ekran görüntüleri */
.ekran { margin: 0; }
.ekran img { width: 100%; display: block; border-radius: 2.2mm; border: 0.35mm solid var(--cizgi);
  box-shadow: 0 0.6mm 2.4mm rgba(15, 20, 40, 0.12); }
.ekran figcaption { font-size: 8.3pt; color: var(--soluk); margin-top: 1.6mm; }

/* Akış */
.akis { list-style: none; padding: 0; margin: 0 0 4mm; display: grid;
  grid-template-columns: repeat(4, 1fr); gap: 3mm; counter-reset: adim; }
.akis.bes { grid-template-columns: repeat(5, 1fr); gap: 2.5mm; }
.akis li { position: relative; padding: 3.2mm 3.2mm 3mm; border-radius: 2.5mm; background: var(--zemin-2);
  counter-increment: adim; }
.akis li::before { content: counter(adim); font-family: var(--baslik); font-weight: 700; font-size: 11pt;
  color: #fff; background: var(--vurgu); width: 6.2mm; height: 6.2mm; border-radius: 50%;
  display: grid; place-items: center; margin-bottom: 1.8mm; }
.akis b { font-family: var(--baslik); font-size: 12pt; font-weight: 600; display: block; margin-bottom: 1mm;
  line-height: 1.1; }
.akis p { font-size: 8.8pt; color: var(--murekkep-2); margin: 0; line-height: 1.38; }
.akis code, .kutu code { font-size: 8.1pt; background: #fff; padding: 0 1mm; border-radius: 1mm; }
.uc-kutu { display: grid; grid-template-columns: repeat(3, 1fr); gap: 3mm; margin-top: 4mm; }
.kutu { border: 0.35mm solid var(--cizgi); border-radius: 2.5mm; padding: 3.2mm 3.5mm; }
.kutu h3 { font-size: 12.5pt; }
.kutu p { font-size: 9pt; color: var(--murekkep-2); margin: 0; }

/* Makineden ekibe */
.alt-ikili { display: grid; grid-template-columns: 74mm 1fr; gap: 5mm; margin-top: 4mm; }
.telefon { background: #dfe7ef; border-radius: 3mm; padding: 3mm; }
.telefon-ust { font-size: 8pt; color: var(--murekkep-2); margin-bottom: 2mm; font-weight: 600; }
.emoji { font-family: "Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji", sans-serif;
  font-style: normal; }
.balon { background: #fff; border-radius: 2.5mm 2.5mm 2.5mm 0.6mm; padding: 2.6mm 3mm; font-size: 8.3pt;
  line-height: 1.38; box-shadow: 0 0.3mm 1mm rgba(15, 20, 40, 0.12); }
.balon p { margin: 0 0 1.6mm; }
.balon p:last-child { margin: 0; }
.telefon figcaption { font-size: 7.8pt; color: var(--soluk); margin-top: 1.8mm; }
.protokol { width: 100%; border-collapse: collapse; font-size: 8.8pt; }
.protokol th, .protokol td { text-align: left; vertical-align: top; padding: 1.5mm 1.8mm;
  border-bottom: 0.3mm solid var(--cizgi); }
.protokol th { font-weight: 600; color: var(--murekkep-2); background: var(--zemin-2); }
.protokol td:first-child { font-weight: 600; white-space: nowrap; }
.not-kutusu { margin-top: 3mm; border-left: 0.8mm solid var(--vurgu); background: var(--zemin-2);
  padding: 2.5mm 3.5mm; font-size: 8.9pt; color: var(--murekkep-2); border-radius: 0 2mm 2mm 0; }
.not-kutusu b { color: var(--murekkep); }

/* Panolar */
.iki { display: grid; grid-template-columns: 1fr 1fr; gap: 5mm; }
.bolum { margin-top: 3.5mm; }
.bolum h3 { display: flex; align-items: baseline; gap: 2mm; }
.bolum p { font-size: 9.2pt; color: var(--murekkep-2); }
.etiket { font-family: var(--metin); font-size: 8pt; font-weight: 600; color: var(--vurgu);
  background: #eaf0ff; padding: 0.4mm 2mm; border-radius: 99mm; }

/* Ölçümler */
.sayilar { display: grid; grid-template-columns: repeat(6, 1fr); gap: 2.6mm; margin: 2.5mm 0 3mm; }
.sayi { border-top: 0.6mm solid var(--vurgu); padding-top: 1.8mm; }
.sayi b { font-family: var(--baslik); font-size: 17pt; font-weight: 650; line-height: 1; display: block;
  margin-bottom: 1mm; white-space: nowrap; }
.sayi span { font-size: 7.7pt; color: var(--murekkep-2); line-height: 1.32; display: block; }
.kucuk { font-size: 8pt; color: var(--soluk); }
"""


def logo() -> str:
    """HA monogramı (app/static/index.html'dekiyle aynı çizim); rengi yazıdan gelir."""
    return (
        '<svg class="logo" viewBox="10 22 83 56" aria-hidden="true">'
        '<rect x="10" y="22" width="13" height="56" fill="currentColor"/>'
        '<polygon points="31,78 44.5,78 62,38.6 79.5,78 93,78 68.2,22 55.8,22" fill="currentColor"/>'
        '<polygon points="23,48 66.18,48 71.06,59 23,59" fill="currentColor"/>'
        '<polygon points="62,38.6 57.82,48 66.18,48" fill="#f5a524"/>'
        "</svg>"
    )


def ust_serit(bolum: str) -> str:
    return f'<div class="ust-serit"><span>{logo()}Hata Asistanı</span><span>{bolum}</span></div>'


def alt_not(sayfa: int) -> str:
    return (
        '<div class="alt-not"><span>Bütün veriler ve kılavuzlar kurgusaldır; ekran görüntüleri '
        f"ve bildirim çalışan uygulamadan alındı.</span><span>{sayfa} / 4</span></div>"
    )


def kapak() -> str:
    return f"""
<section class="sayfa kapak">
  <div class="kapak-ust"><span class="marka">{logo()}Hata Asistanı</span><span>Tanıtım · Ekim 2026</span></div>
  <h1>Makine durduğu anda ekibe haber veren, ne yapılacağını kaynağıyla söyleyen asistan</h1>
  <p class="kapak-alt">Fabrikada bir makine durduğunda zamanı iki şey yer: bakım ekibinin durumu
    öğrenmesi ve kılavuzda doğru sayfanın bulunması. Hata Asistanı ikisini de saniyelere indirir.</p>
  <div class="ayaklar">
    <div class="ayak"><b><span>1</span>Makineyi dinler</b><p>PLC ya da CNC alarm verdiği anda
      arıza kendiliğinden kaydedilir, alarm bitince kapanır. Kimsenin bir şey yazması gerekmez.</p></div>
    <div class="ayak"><b><span>2</span>Ekibe haber verir</b><p>Bakım ekibinin telefonuna Telegram'dan
      bildirim gider: kılavuzdaki ilk adım ve yedek parça durumuyla birlikte.</p></div>
    <div class="ayak"><b><span>3</span>Kaynağıyla cevaplar</b><p>Sorulara arıza kayıtlarından ve
      bakım kılavuzlarından cevap verir; her bilginin sayfasını gösterir.</p></div>
  </div>
  <figure class="ekran">
    <img src="ekranlar/kesit/sohbet.png" alt="Sohbet ekranında soru ve kaynaklı cevap">
    <figcaption>Gerçek ekran görüntüsü. Cevap Gemini modelinden geldi: 2 adım, 3,3 saniye,
      $0,0027. "29 arıza" sayısı ve dağılımı veritabanından; adımlar PRES-BK-01 kılavuzunun
      4. sayfasından.</figcaption>
  </figure>
  <div class="esin"><b>Isıtmalı Güneş Bankı ile bağlantısı:</b> bankın "bozulunca kendisi haber
    veren" arıza takip sistemi bu projedeki fikirden doğdu: arızayı kayda geçir, doğru kişiye
    hemen bildir, ne yapılacağını kaynağıyla söyle.</div>
  <div class="kapak-ekip"><b>Ömer Daştan</b><span>Bursa Teknik Üniversitesi · github.com/omer1916/andon</span></div>
</section>"""


def makineden_ekibe() -> str:
    return f"""
<section class="sayfa">
  {ust_serit("01 Makineden bakım ekibine")}
  <h2>Alarm geldiği anda arıza kaydı ve bildirim</h2>
  <p class="giris">Eskiden operatör telefon açar, birisi arızayı deftere ya da sisteme yazar, bakım
    teknisyeni kılavuzu arardı. Şimdi makine kendisi haber veriyor; arızanın yolu şöyle:</p>
  <ol class="akis bes">
    <li><b>Makine alarm verir</b><p>Pres panelinde "A101 hidrolik basınç düşük" yanar.</p></li>
    <li><b>Bağlayıcı alır</b><p>Makinenin yanındaki küçük program alarmı okur, önce diske yazar,
      sonra Hata Asistanı'na gönderir.</p></li>
    <li><b>Arıza açılır</b><p>Kod kataloğa bakılarak anlamlandırılır: hidrolik, yüksek önem, hat
      durdu. Aynı makinedeki alarmlar tek arızada toplanır.</p></li>
    <li><b>Ekip haberdar olur</b><p>Telegram'a bildirim gider; istenirse ERP, MES ya da Teams
      kanalına da imzalı olarak iletilir.</p></li>
    <li><b>Alarm biter, arıza kapanır</b><p>Süre kendiliğinden hesaplanır; OEE'ye, raporlara ve
      bakım planına girer.</p></li>
  </ol>
  <figure class="ekran">
    <img src="ekranlar/kesit/makine.png" alt="Makineler ekranı: süren alarm ve bağlı kaynak">
    <figcaption>Makineler ekranı: simülatörden verilen alarm 10 saniye önce arıza #440 olarak açılmış;
      ekran 5 saniyede bir yenilenir.</figcaption>
  </figure>
  <div class="alt-ikili">
    <figure class="telefon" style="margin:0">
      <div class="telefon-ust">Telegram · Hata Asistanı botu</div>
      <div class="balon">{TELEGRAM_MESAJI}</div>
      <figcaption>Bakım ekibinin aldığı gerçek mesaj: A101 alarmından birkaç saniye sonra
        (kılavuz alıntısı kısaltıldı).</figcaption>
    </figure>
    <div>
      <h3>Her marka, her protokol</h3>
      <table class="protokol">
        <tr><th>Dil</th><th>Kimler konuşur</th></tr>
        <tr><td>OPC UA</td><td>Siemens S7-1500, Beckhoff, Fanuc, Kepware</td></tr>
        <tr><td>Modbus TCP</td><td>Basit ve eski PLC'ler, uzak G/Ç modülleri</td></tr>
        <tr><td>MQTT</td><td>IoT sensörleri, Raspberry Pi, Node-RED</td></tr>
        <tr><td>MTConnect</td><td>CNC tezgâhları: Haas, Mazak, Okuma</td></tr>
        <tr><td>REST API</td><td>MES, ERP ve kendi yazılımınız</td></tr>
      </table>
      <div class="not-kutusu"><b>Bağlantı koparsa kayıp yok.</b> Ağ ya da sunucu giderse olaylar
        bağlayıcının diskinde bekler; bağlantı dönünce sırayla ve yalnızca bir kez işlenir. Kopukken
        biten alarm da bulunup kapatılır.</div>
    </div>
  </div>
  {alt_not(2)}
</section>"""


def soru_sorunca() -> str:
    return f"""
<section class="sayfa">
  {ust_serit("02 Soru sorunca")}
  <h2>Bir soru, iki kaynak, kaynağı gösterilen tek cevap</h2>
  <p class="giris">Yapay zekâ bilgiyi kendisi uydurmaz. Soruyu anlar ve hangi aracın çağrılacağına
    karar verir; sayıyı veritabanı, ne yapılacağını kılavuz verir. Aynı asistana web arayüzünden de
    Telegram'dan da sorulur.</p>
  <ol class="akis">
    <li><b>Soru</b><p>Kullanıcı giriş yapıp sorusunu yazar. Operatör ile bakım mühendisi farklı
      belgeleri görür.</p></li>
    <li><b>Karar</b><p>Yapay zekâ (Gemini) soruyu ikiye ayırır ve hazır araçlardan uygun olanları
      çağırır: <code>ariza_say</code>, <code>dokuman_ara</code>.</p></li>
    <li><b>İki kaynak</b><p>Arıza sayısı PostgreSQL'den sayılır. Kılavuzlar anlamına göre aranır
      (pgvector), yalnızca kullanıcının görebildiği belgelerde.</p></li>
    <li><b>Kaynaklı cevap</b><p>Her bilginin yanında kılavuz kodu ve sayfası yazar; kaynağa
      tıklanınca PDF o sayfada açılır.</p></li>
  </ol>
  <figure class="ekran">
    <img src="ekranlar/kesit/kaynak.png" alt="Kaynağa tıklanınca açılan kılavuz sayfası">
    <figcaption>Cevaptaki "PRES-BK-01 · s. 4" kaynağına tıklandığında kılavuz tam o sayfada
      açılıyor; kullanıcı yapay zekânın söylediğini kendi gözüyle doğrulayabiliyor.</figcaption>
  </figure>
  <div class="uc-kutu">
    <div class="kutu"><h3>Serbest SQL yok</h3><p>Yapay zekâ veritabanına kendisi sorgu yazamaz;
      yalnızca denetlenen araçları çağırabilir. Bir tabloyu silemez, başka veriyi okuyamaz.</p></div>
    <div class="kutu"><h3>Rol bazlı yetki</h3><p>Operatör bakım kılavuzlarını göremez. Yetki
      aramanın içinde: görülmemesi gereken sayfa yapay zekâya hiç ulaşmaz.</p></div>
    <div class="kutu"><h3>Her istek kayıtlı</h3><p>Hangi aracın çağrıldığı, kaç token harcandığı,
      maliyet ve süre her istekte veritabanına yazılır.</p></div>
  </div>
  {alt_not(3)}
</section>"""


def panolar_ve_olcumler() -> str:
    return f"""
<section class="sayfa">
  {ust_serit("03 Panolar, dış sistemler ve ölçümler")}
  <h2>Arızayı sormadan görmek, bakımı gününde yapmak</h2>
  <div class="bolum" style="margin-top:0">
    <h3>Periyodik bakım</h3>
    <p>Bakım aralıkları kılavuzdaki takvimden gelir: geciken bakım ▼ kırmızı, yaklaşan ◆ sarı.
      "Yapıldı" denince sıradaki tarih kendiliğinden kayar; bakım ekibine her sabah Telegram'dan
      liste gider. Kaynağa tıklanınca kılavuz o sayfada açılır.</p>
    <figure class="ekran"><img src="ekranlar/kesit/periyodik.png" alt="Periyodik bakım ekranı"></figure>
  </div>
  <div class="bolum">
    <h3>OEE paneli</h3>
    <p>Bir hattın gerçekte ne kadar verimli çalıştığı: kullanılabilirlik × performans × kalite.
      Renkler fabrikalardaki Andon ışıkları gibi; renk körleri için her seviyenin bir şekli de var.</p>
    <figure class="ekran"><img src="ekranlar/kesit/oee-ust.png" alt="OEE göstergeleri"></figure>
  </div>
  <div class="iki">
    <div class="bolum">
      <h3>Vardiya raporu <span class="etiket">sayılar denetlendi</span></h3>
      <p>Sayılar veritabanından, yorum yapay zekâdan gelir. Yorumdaki her sayının verilerde geçmesi
        gerekir; uydurulmuş bir sayı varsa yorumu kurallar yazar.</p>
    </div>
    <div class="bolum">
      <h3>Bakım planı</h3>
      <p>Son 90 günün arızalarından her makinenin arızalanma olasılığı hesaplanır; ekibin sınırlı
        saati, beklenen duruşu en çok azaltacak makinelere dağıtılır.</p>
    </div>
  </div>
  <div class="not-kutusu"><b>Dış sistemlerle konuşur.</b> Arıza açıldı, kapandı, hat durdu ve bakım
    yapıldı olayları ERP'ye, MES'e, Teams ve Slack kanallarına imzalı (HMAC-SHA256) bildirim olarak
    gider; alıcıya ulaşılamazsa saatlerce yeniden denenir, sıra bozulmaz. ERP panosu gibi yalnızca
    okuyan sistemler, hiçbir şeyi değiştiremeyen bir servis hesabıyla bağlanır.</div>
  <h3 style="margin-top:4mm">Sonuçlara ne kadar güvenilir?</h3>
  <div class="sayilar">
    <div class="sayi"><b>30 / 30</b><span>değerlendirme sorusu gerçek modelle doğru (sayı, kaynak,
      yetki, "bilmiyorum" diyebilme)</span></div>
    <div class="sayi"><b>%100</b><span>kılavuz aramasında doğru sayfa ilk 3 sonuçta</span></div>
    <div class="sayi"><b>3.116</b><span>otomatik test, her değişiklikte gerçek veritabanıyla</span></div>
    <div class="sayi"><b>128.533</b><span>rastgele makine olayı: veritabanı bağımsız modelle birebir
      aynı</span></div>
    <div class="sayi"><b>4 protokol</b><span>her senaryo dördüyle ayrı ayrı uçtan uca denendi</span></div>
    <div class="sayi"><b>0</b><span>yapay zekânın yazdığı SQL; yalnızca hazır araçlar</span></div>
  </div>
  <p class="kucuk">Teknolojiler: Python, FastAPI, PostgreSQL + pgvector, Google Gemini, OPC UA,
    Modbus, MQTT, MTConnect, Telegram Bot API, Docker. Kod ve ölçümlerin ayrıntısı:
    github.com/omer1916/andon</p>
  {alt_not(4)}
</section>"""


def html_yaz() -> None:
    CIKTI_HTML.write_text(
        f"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<title>Hata Asistanı · Tanıtım</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600;700&family=Barlow:wght@400;500;600&display=swap">
<style>{CSS}</style>
</head>
<body>
{kapak()}
{makineden_ekibe()}
{soru_sorunca()}
{panolar_ve_olcumler()}
</body>
</html>
""",
        encoding="utf-8",
    )


def main() -> None:
    kesitleri_hazirla()
    html_yaz()
    chrome = next((c for c in CHROME_ADAYLARI if Path(c).exists() or shutil.which(c)), None)
    if chrome is None:
        raise SystemExit(f"Chrome/Edge bulunamadı; HTML hazır: {CIKTI_HTML}")
    subprocess.run(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-pdf-header-footer",
            "--virtual-time-budget=15000",
            f"--print-to-pdf={CIKTI_PDF}",
            CIKTI_HTML.as_uri(),
        ],
        check=True,
        capture_output=True,
    )
    print(f"PDF yazıldı: {CIKTI_PDF}")


if __name__ == "__main__":
    main()
