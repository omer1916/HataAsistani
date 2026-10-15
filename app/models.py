"""API'nin girdi ve çıktı modelleri."""

from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# PostgreSQL metin alanları NUL baytı kabul etmez; girdi en baştan reddedilsin (LLM'e gidip
# kota harcamadan, veritabanında 500'e dönüşmeden).
NulsuzMetin = Annotated[str, StringConstraints(pattern=r"^[^\x00]*$")]

HatTipi = Literal["pres", "kaynak", "montaj", "boya"]
VardiyaNo = Literal[1, 2, 3]
VARDIYA_SAATLERI = {1: 7, 2: 15, 3: 23}  # başlangıç saati
VARDIYA_SURESI = timedelta(hours=8)
ArizaTipi = Literal["hidrolik", "mekanik", "elektrik", "sensor", "yazilim", "pnomatik"]
Onem = Literal["dusuk", "orta", "yuksek"]


class Hat(BaseModel):
    id: int
    ad: str = Field(examples=["Pres 3"])
    tip: HatTipi
    makine_sayisi: int


class ArizaFiltresi(BaseModel):
    """`GET /arizalar` sorgu parametreleri. Tarihler gün olarak verilir, iki uç da dahildir."""

    model_config = ConfigDict(extra="forbid")

    hat: NulsuzMetin | None = Field(
        None, description="Hat adı, büyük/küçük harf fark etmez", examples=["Pres 3"]
    )
    baslangic: date | None = Field(
        None, description="Bu günden itibaren (dahil)", examples=["2026-08-01"]
    )
    bitis: date | None = Field(None, description="Bu güne kadar (dahil)", examples=["2026-08-31"])
    ariza_tipi: ArizaTipi | None = None
    limit: int = Field(100, ge=1, le=1000, description="En fazla kaç kayıt dönsün")
    offset: int = Field(0, ge=0, description="Baştan kaç kayıt atlansın")

    @model_validator(mode="after")
    def _tarih_sirasi(self):
        if self.baslangic and self.bitis and self.baslangic > self.bitis:
            raise ValueError("baslangic, bitis'ten sonra olamaz")
        return self


class Ariza(BaseModel):
    id: int
    hat: str
    makine_kodu: str = Field(examples=["P3-HP"])
    makine_adi: str
    baslangic: datetime
    bitis: datetime | None = Field(description="Arıza hâlâ sürüyorsa boş")
    sure_dk: int | None = Field(description="Arıza hâlâ sürüyorsa boş")
    ariza_tipi: ArizaTipi
    onem: Onem
    hat_durdu: bool
    aciklama: str


class ArizaListesi(BaseModel):
    toplam: int = Field(description="Filtreye uyan arıza sayısı; limit ve offset'ten bağımsız")
    arizalar: list[Ariza]


DurusNedeniKodu = Literal["ariza", "urun_degisimi", "malzeme_bekleme"]


class OeeFiltresi(BaseModel):
    """`GET /oee` sorgu parametreleri. Vardiya başladığı güne sayılır; iki uç da dahildir."""

    model_config = ConfigDict(extra="forbid")

    hat: NulsuzMetin | None = Field(
        None, description="Hat adı; boşsa bütün fabrika", examples=["Pres 3"]
    )
    baslangic: date | None = Field(None, examples=["2026-09-01"])
    bitis: date | None = Field(None, examples=["2026-09-30"])

    @model_validator(mode="after")
    def _tarih_sirasi(self):
        if self.baslangic and self.bitis and self.baslangic > self.bitis:
            raise ValueError("baslangic, bitis'ten sonra olamaz")
        return self


class OeeDegerleri(BaseModel):
    vardiya_sayisi: int
    planli_sure_dk: int = Field(description="Vardiya süresi - mola - planlı bakım")
    durus_dk: int = Field(description="Arıza, ürün değişimi ve malzeme beklemesi")
    toplam_adet: int
    hurda_adet: int
    kullanilabilirlik: float | None = Field(description="0-1 arası; veri yoksa boş")
    performans: float | None
    kalite: float | None = Field(description="Hatlar birleşince ideal çevrim süresiyle ağırlıklı")
    oee: float | None = Field(description="kullanılabilirlik x performans x kalite")


class HatOee(OeeDegerleri):
    hat: str


class VardiyaOee(OeeDegerleri):
    vardiya: VardiyaNo = Field(description="1: 07-15, 2: 15-23, 3: 23-07")


class GunlukOee(OeeDegerleri):
    gun: date


class DurusNedeni(BaseModel):
    neden: DurusNedeniKodu
    ariza_tipi: ArizaTipi | None = Field(description="Yalnızca neden 'ariza' ise")
    adet: int = Field(description="Arızada farklı arıza sayısı, diğerlerinde olay sayısı")
    sure_dk: int
    pay: float = Field(description="Toplam duruş içindeki payı, 0-1")
    kumulatif_pay: float = Field(description="Pareto: bu satıra kadarki payların toplamı")


class MakineDurusu(BaseModel):
    makine_kodu: str
    makine_adi: str
    hat: str
    ariza_sayisi: int
    sure_dk: int = Field(description="Arızasıyla hattı durdurduğu süre")


class OeeOzeti(BaseModel):
    toplam: OeeDegerleri
    hatlara_gore: list[HatOee]
    vardiyalara_gore: list[VardiyaOee]
    gunluk: list[GunlukOee]
    duruslar: list[DurusNedeni] = Field(description="Duruş nedenleri, süreye göre (Pareto)")
    makineler: list[MakineDurusu] = Field(description="Hattı en uzun durduran 5 makine")


class RaporIstegi(BaseModel):
    """`POST /rapor/vardiya` gövdesi. Boş bırakılırsa son tamamlanan vardiya raporlanır."""

    model_config = ConfigDict(extra="forbid")

    tarih: date | None = Field(
        None, description="Vardiyanın başladığı gün", examples=["2026-09-29"]
    )
    vardiya: VardiyaNo | None = Field(None, description="1: 07-15, 2: 15-23, 3: 23-07")

    @model_validator(mode="after")
    def _ikisi_birden(self):
        if (self.tarih is None) != (self.vardiya is None):
            raise ValueError("tarih ve vardiya birlikte verilmeli ya da ikisi de boş bırakılmalı")
        return self


RaporMaddesi = Annotated[str, StringConstraints(min_length=3, max_length=300)]


class RaporYorumu(BaseModel):
    """LLM'in (ya da LLM kullanılamazsa kuralların) yazdığı yorum."""

    ozet: str = Field(min_length=10, max_length=800, description="2-3 cümle")
    dikkat: list[RaporMaddesi] = Field(default_factory=list, max_length=5)
    oneriler: list[RaporMaddesi] = Field(default_factory=list, max_length=5)


class RaporHatSatiri(OeeDegerleri):
    hat: str
    son_7_gun_oee: float | None = Field(description="Vardiyadan önceki 7 günün OEE'si")


class RaporDurusu(BaseModel):
    hat: str
    neden: DurusNedeniKodu
    ariza_tipi: ArizaTipi | None
    makine_kodu: str | None
    sure_dk: int


class RaporArizasi(BaseModel):
    hat: str
    makine_kodu: str
    makine_adi: str
    baslangic: datetime
    sure_dk: int | None = Field(description="Vardiya sonunda sürüyorsa boş")
    ariza_tipi: ArizaTipi
    onem: Onem
    hat_durdu: bool
    vardiya_sonunda_suruyor: bool
    aciklama: str


class RaporTalebi(BaseModel):
    id: int
    makine_kodu: str
    hat: str
    oncelik: Onem
    aciklama: str
    olusturma: datetime
    fotograf_var: bool = Field(description="Telegram'dan fotoğrafla açıldıysa")


class KritikParca(BaseModel):
    parca_kodu: str
    ad: str
    miktar: int
    min_miktar: int
    birim: str


class RaporKullanimi(BaseModel):
    model: str | None = Field(description="LLM kullanılmadıysa boş")
    deneme_sayisi: int = Field(description="LLM çağrısı sayısı (0, 1 ya da 2)")
    girdi_token: int
    cikti_token: int
    maliyet_usd: float | None
    sure_ms: int = Field(description="Raporun hazırlanma süresi, veritabanı dahil")


class VardiyaRaporu(BaseModel):
    tarih: date
    vardiya: VardiyaNo
    baslangic: datetime
    bitis: datetime
    fabrika: OeeDegerleri
    hatlar: list[RaporHatSatiri] = Field(description="OEE'si en düşükten yükseğe")
    duruslar: list[RaporDurusu] = Field(description="Vardiyanın en uzun 8 duruşu")
    arizalar: list[RaporArizasi] = Field(description="Vardiyada başlayan arızalar")
    acik_talep_sayisi: int
    acik_talepler: list[RaporTalebi] = Field(description="Önceliği en yüksek 5 açık talep")
    kritik_stok: list[KritikParca]
    yorum: RaporYorumu
    yorum_kaynagi: Literal["llm", "kural"]
    yorum_notu: str | None = Field(
        description="Yorumun neden kurallarla yazıldığı ya da düzeltildiği"
    )
    kullanim: RaporKullanimi
    markdown: str = Field(description="Kopyalanıp paylaşılabilecek düz metin rapor")


class BakimPlaniFiltresi(BaseModel):
    """`GET /bakim-plani` sorgu parametreleri."""

    model_config = ConfigDict(extra="forbid")

    kapasite_saat: int = Field(16, ge=1, le=200, description="Ekibin ayırabileceği bakım saati")
    ufuk_gun: int = Field(7, ge=1, le=30, description="Arıza olasılığının hesaplandığı süre")


class MakineRiski(BaseModel):
    makine_kodu: str
    makine_adi: str
    hat: str
    ariza_sayisi: int = Field(description="Analiz penceresindeki arıza sayısı")
    aralik_sayisi: int = Field(description="Modelin dayandığı tamamlanmış arıza aralığı sayısı")
    su_an_arizali: bool = Field(description="Arızası sürüyor; olasılık tamirden sonrası için")
    model: Literal["weibull", "ustel", "az_veri", "ariza_yok"]
    beta: float | None = Field(description="Weibull şekil tahmini; en az 10 aralıkta hesaplanır")
    beta_yorumu: str | None
    olabilirlik_orani: float | None = Field(description="2(ℓ_Weibull - ℓ_üstel); > 3,84: Weibull")
    mtbf_saat: float | None = Field(description="Ortalama arızalar arası süre")
    son_arizadan_beri_saat: float | None
    ariza_olasiligi: float = Field(description="Ufuk içinde en az bir arıza olasılığı, 0-1")
    ariza_basina_durus_dk: float = Field(description="Geçmişte arıza başına hat duruşu")
    onlenebilirlik: float = Field(description="Bakımın önleyebileceği arıza payı (varsayım)")
    kazanc_dk: float = Field(description="Bakım yapılırsa önlenmesi beklenen duruş")
    bakim_saat: int
    gecikmis_bakimlar: list[str] = Field(
        description="Geciken periyodik bakımlar, örn. 'Yıllık bakım (12 gün gecikti)'"
    )
    gecikme_gun: int | None = Field(description="En çok geciken periyodik bakımın gecikmesi")
    zorunlu: bool = Field(description="Geciken periyodik bakımı var; kapasite önce buna ayrılır")
    secildi: bool


class BakimPlani(BaseModel):
    hesaplama_ani: datetime
    analiz_gun: int
    ufuk_gun: int
    kapasite_saat: int
    secilen_saat: int
    periyodik_saat: int = Field(description="Seçilenlerden geciken periyodik bakımlara ayrılan")
    onlenen_durus_dk: float = Field(description="Seçilen makinelerin kazançlarının toplamı")
    acgozlu_onlenen_durus_dk: float = Field(
        description="Karşılaştırma: değer/süre oranına göre açgözlü seçimin kazancı"
    )
    makineler: list[MakineRiski] = Field(description="Önce seçilenler, sonra kazanca göre")


PeriyodikDurum = Literal["gecikti", "yaklasiyor", "zamaninda"]


class PeriyodikBakim(BaseModel):
    plan_id: int
    makine_kodu: str = Field(examples=["P3-HP"])
    makine_adi: str
    hat: str
    bakim: str = Field(examples=["Yıllık bakım"])
    aralik_gun: int
    sure_saat: int = Field(description="Tahmini süre")
    yapilacaklar: str
    dokuman_kodu: str | None = Field(description="Aralığın geldiği kılavuz; yoksa takvim yok")
    sayfa: int | None
    bolum: str | None
    son_bakim: date
    kayitli: bool = Field(
        description="Son bakım bir iş emrinden; false ise sisteme geçmeden önceki kayıt"
    )
    son_yapan: str | None
    acik_is_emri: bool = Field(description="Bu bakım için kapanmamış bir iş emri var")
    siradaki: date
    kalan_gun: int = Field(description="Sıradaki bakıma kalan gün; eksiyse gecikme")
    durum: PeriyodikDurum


class PeriyodikBakimListesi(BaseModel):
    bugun: date
    yaklasma_gun: int = Field(description="Bu kadar gün içindeki bakım 'yaklaşıyor' sayılır")
    gecikmis: int
    yaklasan: int
    bakimlar: list[PeriyodikBakim] = Field(description="En çok geciken önce")


class PeriyodikBakimFiltresi(BaseModel):
    """`GET /periyodik-bakim` sorgu parametreleri."""

    model_config = ConfigDict(extra="forbid")

    hat: NulsuzMetin | None = Field(None, description="Hat adı", examples=["Pres 3"])
    makine: NulsuzMetin | None = Field(
        None, max_length=20, description="Makine kodu", examples=["P3-HP"]
    )


class BakimYapildi(BaseModel):
    """`POST /periyodik-bakim/{plan_id}/yapildi` gövdesi."""

    model_config = ConfigDict(extra="forbid")

    tarih: date | None = Field(None, description="Yapıldığı gün; boşsa bugün. İleri gün olamaz.")
    sure_saat: float | None = Field(
        None, ge=0.5, le=24, description="Süren saat; boşsa planın tahmini süresi"
    )
    aciklama: NulsuzMetin | None = Field(
        None, max_length=500, description="Yapılan işler, bulgular", examples=["Conta seti değişti"]
    )


class EntegrasyonOlayi(BaseModel):
    """Bir makineden ya da sistemden gelen alarm olayı (POST /entegrasyon/olaylar)."""

    model_config = ConfigDict(extra="forbid")

    olay_id: str = Field(
        pattern=r"^[A-Za-z0-9._:@/+-]{1,128}$",
        description="Kaynağın bu olaya verdiği benzersiz kimlik. Aynı kimlikle tekrar gelen olay "
        "yeniden işlenmez; bağlayıcı emin olmadığı olayı güvenle yeniden gönderebilir.",
        examples=["P3-HP:A101:1:1792053221115"],
    )
    tur: Literal["alarm_basladi", "alarm_bitti"]
    makine_kodu: str = Field(pattern=r"^[A-Za-z0-9-]{1,20}$", examples=["P3-HP"])
    alarm_kodu: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$", examples=["A101"])
    zaman: datetime = Field(
        description="Olayın makinedeki zamanı. Saat dilimi yoksa Türkiye saati sayılır; "
        "gelecekteyse alınma anına çekilir; 30 günden eskiyse reddedilir.",
        examples=["2026-10-15T11:36:18+03:00"],
    )
    mesaj: NulsuzMetin | None = Field(None, max_length=500, description="Makinenin alarm metni")
    ariza_tipi: ArizaTipi | None = Field(None, description="Boşsa katalogdan ya da varsayılandan")
    onem: Onem | None = None
    hat_durdurur: bool | None = None


class EntegrasyonIstegi(BaseModel):
    """Olaylar verildiği sırayla işlenir. Geçersiz bir olay yalnızca kendisi reddedilir
    (durum "gecersiz"), diğerleri işlenir."""

    model_config = ConfigDict(extra="forbid")

    olaylar: list[Any] = Field(
        min_length=1, max_length=500, description="Her biri bir EntegrasyonOlayi"
    )


class EntegrasyonSonucu(BaseModel):
    sira: int = Field(description="Olayın istekteki sırası (0'dan)")
    olay_id: str | None
    durum: Literal["islendi", "tekrar", "gecersiz", "hata"] = Field(
        description="gecersiz ve hata kalıcıdır: aynı olay yeniden gönderilmemeli"
    )
    sonuc: str | None = Field(
        description="ariza_acildi, ariza_eklendi, ariza_yeniden_acildi, zaten_aktif, "
        "ariza_kapandi, alarm_kapandi, aktif_alarm_yok"
    )
    ariza_id: int | None
    eslesme: Literal["katalog", "varsayilan"] | None
    hata: str | None


class EntegrasyonCevabi(BaseModel):
    kaynak: str
    sonuclar: list[EntegrasyonSonucu]


class AktifAlarm(BaseModel):
    kaynak: str
    makine_kodu: str
    alarm_kodu: str
    baslangic: datetime
    ariza_id: int
    ariza_aciklamasi: str


class EntegrasyonKaynagi(BaseModel):
    ad: str
    aciklama: str
    katalog: str | None
    aktif: bool
    yetki: Literal["olay", "okuma"]  # okuma: servis hesabı (yalnızca okur)
    son_olay: datetime | None
    aktif_alarm: int


class EntegrasyonKaydi(BaseModel):
    kaynak: str
    olay_id: str
    tur: str
    makine_kodu: str
    alarm_kodu: str
    mesaj: str | None
    zaman: datetime
    alinma: datetime
    sonuc: str
    ariza_id: int | None


class EntegrasyonDurumu(BaseModel):
    kaynaklar: list[EntegrasyonKaynagi]
    aktif_alarmlar: list[AktifAlarm]
    son_olaylar: list[EntegrasyonKaydi]


class AramaIstegi(BaseModel):
    """`GET /ara` sorgu parametreleri."""

    model_config = ConfigDict(extra="forbid")

    soru: NulsuzMetin = Field(
        min_length=3, max_length=500, examples=["pres hattı arıza ilk kontrol"]
    )
    k: int = Field(3, ge=1, le=10, description="Kaç parça dönsün")


class AramaSonucu(BaseModel):
    dokuman_kodu: str = Field(examples=["PRES-BK-01"])
    dokuman_basligi: str
    sayfa: int
    bolum: str | None = Field(
        description="Başlık yolu, örn. '4. HİDROLİK ... > 4.1 Genel yaklaşım'"
    )
    icerik: str
    benzerlik: float = Field(description="Kosinüs benzerliği; 1'e yakın olan daha alakalı")


class SohbetIstegi(BaseModel):
    model_config = ConfigDict(extra="forbid")

    soru: NulsuzMetin = Field(
        min_length=2,
        max_length=2000,
        examples=["Pres 3 hattında geçen ay kaç arıza oldu, bu tip arızada ilk neye bakmalıyım?"],
    )


class Kaynak(BaseModel):
    dokuman_kodu: str
    dokuman_basligi: str
    sayfa: int
    bolum: str | None


class AracCagrisiOzeti(BaseModel):
    ad: str
    argumanlar: dict | str
    hata: str | None = None


class Kullanim(BaseModel):
    model: str
    adim_sayisi: int
    girdi_token: int
    cikti_token: int
    maliyet_usd: float | None = Field(
        description="Ücretli katman liste fiyatıyla; bilinmiyorsa boş"
    )
    sure_ms: int


class SohbetCevabi(BaseModel):
    cevap: str
    kaynaklar: list[Kaynak] = Field(description="Agent'ın okuduğu kılavuz sayfaları")
    arac_cagrilari: list[AracCagrisiOzeti]
    kullanim: Kullanim


class KullanimOzeti(BaseModel):
    istek_sayisi: int
    hatali_istek: int
    toplam_girdi_token: int
    toplam_cikti_token: int
    toplam_maliyet_usd: float | None
    ortalama_sure_ms: int | None
    p95_sure_ms: int | None


class Token(BaseModel):
    access_token: str
    token_type: Literal["bearer"]
    kullanici_adi: str
    ad_soyad: str
    rol: Literal["operator", "bakim"]


class TelegramKodu(BaseModel):
    kod: str = Field(description="6 haneli, tek kullanımlık; botta /baglan KOD yazılır")
    gecerlilik_dk: int
    baglanti: str | None = Field(
        description="Tek tıkla eşleştirme bağlantısı (t.me/<bot>?start=KOD); bot adı ayarlıysa"
    )


class TelegramDurumu(BaseModel):
    bagli: bool
    bot_kullanici_adi: str | None
    bildirim_ayrintisi: Literal["ayrintili", "kisa"]
    fotograf_saklama_gun: int


class Saglik(BaseModel):
    veritabani: bool
