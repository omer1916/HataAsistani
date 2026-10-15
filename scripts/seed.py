"""Veritabanını 6 aylık sahte fabrika verisiyle doldurur.

Kullanım:
    python scripts/seed.py                            # bugüne kadarki 6 ay
    python scripts/seed.py --son "2026-09-30 12:00"   # sabit bitiş: her seferinde aynı veri

Tabloları silip yeniden kurar; eski veri kaybolur.

Veride bilerek bırakılmış desenler var, sorgu yazarken bunları bulabilmelisin:
- Pres 3 en çok arıza veren hat, orada en sık arıza tipi hidrolik.
- Pres 3'ün hidrolik presinde (P3-HP) son iki ayda arızalar artıyor.
- Hafta sonu, özellikle pazar, üretim az olduğu için arıza da az.
- Birkaç yedek parça minimum stok seviyesinin altında.
- Seed anında 3 arıza hâlâ sürüyor; biri Pres 3'ü durdurmuş durumda.
- Pres 3'ün OEE'si en düşük: hem en çok duran hat hem de tasarım hızının altında çalışıyor.
- Pres hatlarında en büyük duruş nedeni arıza değil kalıp değişimi; Pres 3'te bir kalıp
  değişimi diğer preslerin 1,5 katı sürüyor.
- Pres 3'te son iki ayda hurda oranı üç katına çıkıyor (P3-HP'deki hidrolik sorunlarla aynı dönem).
- Gece vardiyasında (3. vardiya) performans gündüzden düşük.
"""

import argparse
import math
import random
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path

import psycopg
from faker import Faker
from psycopg import sql

from app import entegrasyon
from app.auth import parola_hashle
from app.ayarlar import ayarlar
from app.db import TZ, baglan
from app.models import VARDIYA_SAATLERI

# (kullanıcı adı, ad soyad, rol)
DEMO_KULLANICILAR = [
    ("operator", "Demo Operatör", "operator"),
    ("bakim", "Demo Bakım Mühendisi", "bakim"),
]

SEMA_DOSYASI = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"
TELEGRAM_SEMASI = SEMA_DOSYASI.with_name("telegram.sql")  # eşleştirmeler seed'den sonra da kalır
# Entegrasyon kaynakları ve alarm kataloğu kalır; olaylar ve aktif alarmlar sıfırlanır.
ENTEGRASYON_SEMASI = SEMA_DOSYASI.with_name("entegrasyon.sql")
GUN_SAYISI = 183  # yaklaşık 6 ay

# (ad, tip, arıza çarpanı): çarpan 1'den büyükse hat ortalamadan sık arıza verir.
HATLAR = [
    ("Pres 1", "pres", 1.0),
    ("Pres 2", "pres", 0.9),
    ("Pres 3", "pres", 2.2),
    ("Kaynak 1", "kaynak", 1.0),
    ("Kaynak 2", "kaynak", 1.4),
    ("Montaj 1", "montaj", 0.8),
    ("Boya 1", "boya", 1.1),
]

# Hat tipine göre makineler: (kod eki, ad). Makine kodu hat kısaltmasıyla birleşir: P3-HP.
MAKINELER = {
    "pres": [("HP", "Hidrolik Pres"), ("RB", "Rulo Besleme Ünitesi"), ("TR", "Transfer Robotu")],
    "kaynak": [
        ("KR1", "Kaynak Robotu 1"),
        ("KR2", "Kaynak Robotu 2"),
        ("FK", "Fikstür İstasyonu"),
    ],
    "montaj": [("KN", "Konveyör"), ("TT", "Tork Tabancası İstasyonu"), ("TS", "Test Tezgâhı")],
    "boya": [("OI", "Ön İşlem Havuzu"), ("BK", "Boya Kabini"), ("FR", "Kurutma Fırını")],
}

ARIZA_TIPI_AGIRLIKLARI = {
    "pres": {"hidrolik": 40, "mekanik": 25, "elektrik": 15, "sensor": 15, "yazilim": 5},
    "kaynak": {"mekanik": 20, "elektrik": 30, "sensor": 15, "yazilim": 25, "pnomatik": 10},
    "montaj": {"mekanik": 35, "elektrik": 20, "sensor": 20, "yazilim": 10, "pnomatik": 15},
    "boya": {"mekanik": 20, "elektrik": 25, "sensor": 25, "yazilim": 5, "pnomatik": 25},
}

ARIZA_ACIKLAMALARI = {
    "hidrolik": [
        "Hidrolik basınç set değerinin altına düştü",
        "Hidrolik yağ kaçağı tespit edildi",
        "Hidrolik pompa aşırı ısındı",
        "Hidrolik yağ seviyesi düşük",
        "Oransal valf komuta tepki vermiyor",
    ],
    "mekanik": [
        "Kalıp sıkıştı",
        "Rulmandan ses geliyor",
        "Kayış koptu",
        "Zincir dişliden atladı",
        "Bağlantı cıvataları gevşedi",
        "Aşırı titreşim",
    ],
    "elektrik": [
        "Motor sürücüsü arıza verdi",
        "Sigorta attı",
        "Kablo hasarı",
        "Kontaktör yapıştı",
        "Acil stop devresi açık kaldı",
    ],
    "sensor": [
        "Endüktif sensör sinyal vermiyor",
        "Işık bariyeri sebepsiz devreye girdi",
        "Basınç sensörü hatalı değer okuyor",
        "Sıcaklık sensörü kopuk görünüyor",
        "Enkoder hatası",
    ],
    "yazilim": [
        "PLC programı durdu",
        "Robot pozisyon hatası verdi",
        "Operatör paneli (HMI) dondu",
        "Reçete yüklenemedi",
    ],
    "pnomatik": [
        "Hava basıncı düşük",
        "Pnömatik silindir kaçak yapıyor",
        "Valf adası arızası",
        "Vantuz vakum oluşturmuyor",
    ],
}

ONEM_AGIRLIKLARI = {"dusuk": 50, "orta": 35, "yuksek": 15}
ONEM_SURE_MEDYAN_DK = {"dusuk": 20, "orta": 60, "yuksek": 180}
HAT_DURMA_OLASILIGI = {"dusuk": 0.15, "orta": 0.6, "yuksek": 1.0}

TEMEL_ARIZA_ORANI = 0.12  # hafta içi, makine başına günlük beklenen arıza sayısı
GUN_CARPANI = {5: 0.6, 6: 0.2}  # cumartesi, pazar

# Seed anında süren arızalar, "şu an hangi makineler arızalı?" sorusu boş dönmesin diye.
# (makine kodu, arıza tipi, önem, kaç dakika önce başladığı)
SUREN_ARIZALAR = [
    ("P3-HP", "hidrolik", "yuksek", 95),
    ("K2-KR1", "yazilim", "orta", 40),
    ("B1-BK", "pnomatik", "dusuk", 15),
]

# (parça kodu, ad, kategori, miktar, min. miktar, birim, konum)
STOK = [
    ("HYD-001", "Hidrolik yağ filtresi", "hidrolik", 14, 6, "adet", "Depo A-1"),
    ("HYD-002", "Hidrolik yağı ISO VG 46", "hidrolik", 180, 200, "litre", "Depo A-1"),
    ("HYD-003", "Oransal valf", "hidrolik", 2, 2, "adet", "Depo A-2"),
    ("HYD-004", "Hidrolik pompa conta seti", "hidrolik", 5, 3, "adet", "Depo A-2"),
    ("HYD-005", "Hidrolik hortum 1/2 inç", "hidrolik", 12, 8, "metre", "Depo A-3"),
    ("MEK-001", "Rulman 6205", "mekanik", 40, 20, "adet", "Depo B-1"),
    ("MEK-002", "Konveyör zinciri", "mekanik", 25, 10, "metre", "Depo B-1"),
    ("MEK-003", "V kayışı SPZ 1250", "mekanik", 3, 6, "adet", "Depo B-2"),
    ("MEK-004", "Kalıp yayı", "mekanik", 30, 15, "adet", "Depo B-2"),
    ("MEK-005", "Cıvata seti M12", "mekanik", 60, 25, "adet", "Depo B-3"),
    ("ELK-001", "Kontaktör 24V", "elektrik", 8, 5, "adet", "Depo C-1"),
    ("ELK-002", "Sigorta 16A", "elektrik", 50, 30, "adet", "Depo C-1"),
    ("ELK-003", "Motor sürücü kartı", "elektrik", 1, 2, "adet", "Depo C-2"),
    ("ELK-004", "Güç kablosu 3x2,5", "elektrik", 100, 50, "metre", "Depo C-3"),
    ("SNS-001", "Endüktif sensör M18", "sensor", 15, 8, "adet", "Depo D-1"),
    ("SNS-002", "Basınç sensörü 0-250 bar", "sensor", 1, 4, "adet", "Depo D-1"),
    ("SNS-003", "Işık bariyeri alıcısı", "sensor", 4, 2, "adet", "Depo D-2"),
    ("SNS-004", "PT100 sıcaklık sensörü", "sensor", 6, 4, "adet", "Depo D-2"),
    ("SNS-005", "Artımlı enkoder", "sensor", 3, 2, "adet", "Depo D-3"),
    ("PNO-001", "Pnömatik silindir Ø50", "pnomatik", 4, 2, "adet", "Depo E-1"),
    ("PNO-002", "Solenoid valf 5/2", "pnomatik", 2, 4, "adet", "Depo E-1"),
    ("PNO-003", "Vakum vantuzu", "pnomatik", 40, 20, "adet", "Depo E-2"),
    ("PNO-004", "Şartlandırıcı (filtre-regülatör)", "pnomatik", 5, 3, "adet", "Depo E-2"),
]

BAKIM_TALEBI_ACIKLAMALARI = [
    "Makineden alışılmadık bir ses geliyor, kontrol edilmeli",
    "Zeminde yağ lekesi var, kaçak olabilir",
    "Periyodik bakım tarihi geçti",
    "Sensör ara ara sinyal kaybediyor",
    "Çevrim süresi uzadı",
    "Koruma kapağının kilidi zor kapanıyor",
    "Panelde tekrar eden uyarı mesajı var",
    "Gövdede aşırı ısınma hissediliyor",
    "Titreşim arttı",
    "Acil stop butonu sıkışıyor",
]

# Vardiyalar: (numara, başlangıç saati). Her vardiya 8 saat, bunun 30 dakikası planlı mola.
VARDIYALAR = list(VARDIYA_SAATLERI.items())
VARDIYA_DK = 480
MOLA_DK = 30
# Hafta sonu üretim az: cumartesi iki, pazar tek vardiya. Gün, vardiyanın başladığı gündür.
CALISAN_VARDIYALAR = {5: (1, 2), 6: (1,)}  # listede olmayan gün: üç vardiya

# Hat tipine göre: (ideal çevrim süresi sn/parça, olağan performans, olağan hurda oranı)
URETIM_PARAMETRELERI = {
    "pres": (6.0, 0.90, 0.012),
    "kaynak": (40.0, 0.88, 0.008),
    "montaj": (55.0, 0.86, 0.006),
    "boya": (75.0, 0.89, 0.025),
}
PRES_3_PERFORMANSI = 0.80  # 2009 model hat, tasarım hızının altında çalışıyor
GECE_PERFORMANS_KAYBI = 0.03
PRES_3_HURDA_CARPANI = 3.0  # son 60 gün: hidrolik basınç dalgalanması ölçü hatası yapıyor

# Arıza dışı duruşlar. Ürün değişimi (kalıp, fikstür, renk, model), hat tipine göre:
# (vardiya başına beklenen değişim sayısı, değişimin medyan süresi dk)
URUN_DEGISIMI = {"pres": (1.2, 30), "kaynak": (0.5, 25), "montaj": (0.4, 20), "boya": (1.5, 15)}
PRES_3_DEGISIM_DK = 45  # eski hat: kalıp değişimi diğer preslerin 1,5 katı sürüyor
MALZEME_BEKLEME = (0.15, 20)  # (vardiyada yaşanma olasılığı, medyan süresi dk)

# Periyodik bakım planları, makine kodunun ekine göre (HP: hidrolik pres, KR1/KR2: kaynak
# robotu): (ad, aralık gün, tahmini süre saat, yapılacaklar, kılavuzdaki kaynak). Aralıklar ve
# yapılacaklar kılavuzun periyodik bakım bölümünden; sayfalar kaynak metinle test edilir
# (tests/test_periyodik.py). Kılavuzunda takvimi olmayan makineye genel aylık bakım.
_PRES = ("PRES-BK-01", 3)
_KAYNAK = ("KR-BK-01", 1, "2.3 Aylık ve daha uzun aralıklar")
_ROBOT_PLANLARI = [
    ("Aylık bakım", 30, 3, "Robot kablo paketi (dress pack) sürtünme ve kırılma açısından, "
     "soğutma suyu seviyesi eksiklik açısından kontrol edilir.", _KAYNAK),
    ("6 aylık bakım", 182, 2, "Robot eksen redüktörlerinin yağ seviyesi kontrol edilir.", _KAYNAK),
    ("Yıllık bakım", 365, 2, "Robot kontrol ünitesindeki enkoder yedek bataryaları "
     "değiştirilir.", _KAYNAK),
]  # fmt: skip
PERIYODIK_PLANLAR = {
    "HP": [
        ("Aylık bakım", 30, 3, "Yağ numunesi partikül ve su analizine gönderilir; akümülatörün "
         "azot ön dolum basıncı (130 bar) ve kalıp yayları (MEK-004) kontrol edilir.",
         (*_PRES, "3.3 Aylık")),
        ("6 aylık bakım", 182, 4, "Hidrolik yağ filtresi (HYD-001) değiştirilir; yağ analizi "
         "kötüyse yağ tamamen değiştirilir ve tank temizlenir.",
         (*_PRES, "3.4 Her 2000 çalışma saatinde veya 6 ayda bir")),
        ("Yıllık bakım", 365, 6, "Pompa conta seti (HYD-004) ve emniyet valfi ayarı kontrol "
         "edilir; 5 yaşını dolduran hidrolik hortumlar değiştirilir.", (*_PRES, "3.5 Yıllık")),
    ],
    "KR1": _ROBOT_PLANLARI,
    "KR2": _ROBOT_PLANLARI,
}  # fmt: skip
# Kaynağın boş olması "kılavuzda takvim yok" demektir; arayüz ve araç bunu ayrıca gösterir.
GENEL_AYLIK_BAKIM = ("Aylık bakım", 30, 3, "Genel kontrol, temizlik ve yağlama.", None)
# Demo senaryosu: en sık arızalanan Pres 3'ün hidrolik presinin yıllık bakımı 12 gün gecikmiş,
# P1-HP'nin 6 aylık bakımı bu hafta. Değer: sıradaki bakıma kalan gün (eksi: gecikme).
PERIYODIK_SENARYO = {("P3-HP", "Yıllık bakım"): -12, ("P1-HP", "6 aylık bakım"): 4}

TABLOLAR = [
    "hatlar",
    "makineler",
    "periyodik_bakimlar",
    "stok",
    "ariza_kayitlari",
    "is_emirleri",
    "bakim_talepleri",
    "vardiya_uretimi",
    "vardiya_duruslari",
]

Kayit = dict[str, object]


def _poisson(rng: random.Random, beklenen: float) -> int:
    """Poisson dağılımından sayı çeker (Knuth yöntemi, küçük değerler için yeterli)."""
    esik = math.exp(-beklenen)
    k, p = 0, rng.random()
    while p > esik:
        k += 1
        p *= rng.random()
    return k


def _agirlikli_sec(rng: random.Random, agirliklar: dict[str, float]) -> str:
    return rng.choices(list(agirliklar), weights=list(agirliklar.values()))[0]


def _numarala(kayitlar: list[Kayit], sira_alani: str) -> list[Kayit]:
    """Kayıtları zamana göre sıralar ve 1'den başlayan id verir."""
    kayitlar.sort(key=lambda k: k[sira_alani])
    return [{"id": i, **k} for i, k in enumerate(kayitlar, start=1)]


def _periyodik_planlar_uret(
    rng: random.Random, son_an: datetime, ilk_gun: date, makineler: list[Kayit]
) -> list[Kayit]:
    """Her makinenin periyodik bakım planları. Aylık bakımın geçmişi seed'in iş emirlerinde
    olduğundan baz tarihi verinin ilk günüdür. Uzun aralıklı bakımların geçmişi 6 aylık veriden
    eski olabilir: baz tarih (sisteme geçmeden önceki son bakım) sıradaki tarih aralığın içinde
    kalacak şekilde rastgele, senaryodakiler sabit."""
    bugun = son_an.date()
    planlar: list[Kayit] = []
    for makine in makineler:
        ek = str(makine["kod"]).split("-", 1)[1]
        for ad, aralik, sure, yapilacaklar, kaynak in PERIYODIK_PLANLAR.get(
            ek, [GENEL_AYLIK_BAKIM]
        ):
            if ad == "Aylık bakım":
                baz = ilk_gun
            else:
                kalan = PERIYODIK_SENARYO.get((makine["kod"], ad))
                if kalan is None:
                    kalan = rng.randrange(8, aralik)  # gecikmemiş, bu hafta da değil
                baz = bugun + timedelta(days=kalan - aralik)
            dokuman, sayfa, bolum = kaynak or (None, None, None)
            planlar.append(
                {
                    "id": len(planlar) + 1,
                    "makine_id": makine["id"],
                    "ad": ad,
                    "aralik_gun": aralik,
                    "sure_saat": sure,
                    "yapilacaklar": yapilacaklar,
                    "dokuman_kodu": dokuman,
                    "sayfa": sayfa,
                    "bolum": bolum,
                    "baz_tarih": baz,
                }
            )
    return planlar


def veri_uret(son_an: datetime, tohum: int = 42) -> dict[str, list[Kayit]]:
    """`son_an`'da biten 6 aylık sahte veriyi üretir. Aynı girdiler aynı veriyi verir."""
    rng = random.Random(tohum)
    fake = Faker("tr_TR")
    fake.seed_instance(tohum)
    teknisyenler = [f"{fake.first_name()} {fake.last_name()}" for _ in range(8)]
    operatorler = [f"{fake.first_name()} {fake.last_name()}" for _ in range(12)]

    hatlar: list[Kayit] = []
    makineler: list[Kayit] = []
    hat_carpani: dict[int, float] = {}
    hat_tipi: dict[int, str] = {}
    for hat_id, (ad, tip, carpan) in enumerate(HATLAR, start=1):
        hatlar.append(
            {"id": hat_id, "ad": ad, "tip": tip, "ideal_cevrim_sn": URETIM_PARAMETRELERI[tip][0]}
        )
        hat_carpani[hat_id], hat_tipi[hat_id] = carpan, tip
        kisaltma = ad[0] + ad.split()[-1]  # "Pres 3" -> "P3"
        for ek, makine_adi in MAKINELER[tip]:
            makineler.append(
                {
                    "id": len(makineler) + 1,
                    "hat_id": hat_id,
                    "kod": f"{kisaltma}-{ek}",
                    "ad": makine_adi,
                    "kurulum_yili": 2009 if ad == "Pres 3" else rng.randint(2011, 2022),
                }
            )

    stok = [
        {
            "id": i,
            "parca_kodu": kod,
            "ad": ad,
            "kategori": kategori,
            "miktar": miktar,
            "min_miktar": min_miktar,
            "birim": birim,
            "konum": konum,
        }
        for i, (kod, ad, kategori, miktar, min_miktar, birim, konum) in enumerate(STOK, start=1)
    ]
    kategori_parcalari: dict[str, list[int]] = {}
    for parca in stok:
        kategori_parcalari.setdefault(parca["kategori"], []).append(parca["id"])

    ilk_gun = (son_an - timedelta(days=GUN_SAYISI - 1)).date()

    arizalar: list[Kayit] = []
    for gun_no in range(GUN_SAYISI):
        gun = ilk_gun + timedelta(days=gun_no)
        gun_basi = datetime.combine(gun, time(), TZ)
        for makine in makineler:
            oran = (
                TEMEL_ARIZA_ORANI
                * hat_carpani[makine["hat_id"]]
                * GUN_CARPANI.get(gun.weekday(), 1.0)
            )
            tipler = dict(ARIZA_TIPI_AGIRLIKLARI[hat_tipi[makine["hat_id"]]])
            if makine["kod"] == "P3-HP" and gun_no >= GUN_SAYISI - 60:
                oran *= 1.8
                tipler["hidrolik"] *= 2.5

            for _ in range(_poisson(rng, oran)):
                baslangic = gun_basi + timedelta(minutes=rng.randrange(24 * 60))
                if baslangic > son_an:
                    continue
                onem = _agirlikli_sec(rng, ONEM_AGIRLIKLARI)
                tip = _agirlikli_sec(rng, tipler)
                sure_dk = rng.lognormvariate(math.log(ONEM_SURE_MEDYAN_DK[onem]), 0.6)
                bitis = baslangic + timedelta(minutes=max(5, round(sure_dk)))
                arizalar.append(
                    {
                        "makine_id": makine["id"],
                        "baslangic": baslangic,
                        "bitis": bitis if bitis <= son_an else None,
                        "ariza_tipi": tip,
                        "onem": onem,
                        "hat_durdu": rng.random() < HAT_DURMA_OLASILIGI[onem],
                        "aciklama": rng.choice(ARIZA_ACIKLAMALARI[tip]),
                    }
                )
    makine_kodlari = {m["kod"]: m["id"] for m in makineler}
    for kod, tip, onem, dakika_once in SUREN_ARIZALAR:
        arizalar.append(
            {
                "makine_id": makine_kodlari[kod],
                "baslangic": son_an - timedelta(minutes=dakika_once),
                "bitis": None,
                "ariza_tipi": tip,
                "onem": onem,
                "hat_durdu": onem == "yuksek",
                "aciklama": ARIZA_ACIKLAMALARI[tip][0],
            }
        )
    arizalar = _numarala(arizalar, "baslangic")

    is_emirleri: list[Kayit] = []

    def is_emri_ekle(makine_id, ariza_id, tip, aciklama, acilis, bitis, parca_id, parca_adet):
        # `bitis`: işin fiilen bittiği an; son_an'dan sonraysa iş hâlâ sürüyor demektir.
        if bitis is not None and bitis <= son_an:
            durum, kapanis = "kapali", bitis
        else:
            durum, kapanis = rng.choice(["acik", "devam"]), None
        is_emirleri.append(
            {
                "makine_id": makine_id,
                "ariza_id": ariza_id,
                "tip": tip,
                "aciklama": aciklama,
                "durum": durum,
                "atanan": rng.choice(teknisyenler),
                "acilis": acilis,
                "kapanis": kapanis,
                "parca_id": parca_id,
                "parca_adet": parca_adet,
            }
        )

    for ariza in arizalar:
        if ariza["onem"] == "dusuk" and rng.random() > 0.3:
            continue  # küçük arızaların çoğu iş emri açılmadan operatörce giderilir
        acilis = ariza["baslangic"] + timedelta(minutes=rng.randint(5, 20))
        if acilis > son_an:
            continue
        bitis = None
        if ariza["bitis"] is not None:
            bitis = max(ariza["bitis"], acilis) + timedelta(minutes=rng.randint(10, 60))
        parca_id = parca_adet = None
        parcalar = kategori_parcalari.get(ariza["ariza_tipi"])  # yazılım arızasında parça yok
        if parcalar and rng.random() < 0.5:
            parca_id, parca_adet = rng.choice(parcalar), rng.randint(1, 2)
        is_emri_ekle(
            ariza["makine_id"],
            ariza["id"],
            "ariza",
            f"Arıza müdahalesi: {ariza['aciklama']}",
            acilis,
            bitis,
            parca_id,
            parca_adet,
        )

    for makine in makineler:
        for gun_no in range(rng.randrange(30), GUN_SAYISI, 30):
            gun = ilk_gun + timedelta(days=gun_no)
            if gun.weekday() == 6:
                gun += timedelta(days=1)  # pazar bakım yapılmaz
            acilis = datetime.combine(gun, time(8), TZ)
            if acilis > son_an:
                continue
            bitis = acilis + timedelta(hours=rng.randint(2, 4))
            is_emri_ekle(
                makine["id"], None, "periyodik", "Aylık periyodik bakım", acilis, bitis, None, None
            )
    is_emirleri = _numarala(is_emirleri, "acilis")

    bakim_talepleri: list[Kayit] = []
    makine_agirliklari = [3 if m["kod"].startswith("P3") else 1 for m in makineler]
    for _ in range(30):
        olusturma = son_an - timedelta(minutes=rng.randrange(GUN_SAYISI * 24 * 60))
        if (son_an - olusturma).days < 14:
            durum = _agirlikli_sec(rng, {"acik": 70, "onaylandi": 30})
        else:
            durum = _agirlikli_sec(rng, {"tamamlandi": 60, "onaylandi": 15, "reddedildi": 25})
        bakim_talepleri.append(
            {
                "makine_id": rng.choices(makineler, weights=makine_agirliklari)[0]["id"],
                "aciklama": rng.choice(BAKIM_TALEBI_ACIKLAMALARI),
                "oncelik": _agirlikli_sec(rng, ONEM_AGIRLIKLARI),
                "durum": durum,
                "olusturan": rng.choice(operatorler),
                "olusturma": olusturma,
            }
        )
    bakim_talepleri = _numarala(bakim_talepleri, "olusturma")

    # Periyodik bakım planları da kendi üretecini kullanır; diğer tablolar değişmez. Seed'in
    # aylık bakım iş emirleri makinenin aylık planına bağlanır.
    planlar = _periyodik_planlar_uret(
        random.Random(f"periyodik-{tohum}"), son_an, ilk_gun, makineler
    )
    aylik_plan = {p["makine_id"]: p["id"] for p in planlar if p["ad"] == "Aylık bakım"}
    for emir in is_emirleri:
        emir["plan_id"] = aylik_plan[emir["makine_id"]] if emir["tip"] == "periyodik" else None

    # Üretim verisi kendi rastgele sayı üreteciyle üretilir: yukarıdaki tablolar, üretim verisi
    # eklenmeden önceki hâlleriyle birebir aynı kalır.
    vardiyalar, duruslar = _uretim_verisi_uret(
        random.Random(f"uretim-{tohum}"), son_an, ilk_gun, hatlar, makineler, arizalar, is_emirleri
    )

    return {
        "hatlar": hatlar,
        "makineler": makineler,
        "periyodik_bakimlar": planlar,
        "stok": stok,
        "ariza_kayitlari": arizalar,
        "is_emirleri": is_emirleri,
        "bakim_talepleri": bakim_talepleri,
        "vardiya_uretimi": vardiyalar,
        "vardiya_duruslari": duruslar,
    }


def vardiya_baslangici(an: datetime) -> datetime:
    """`an`'ın içinde bulunduğu vardiyanın başlangıcı: 07:00, 15:00 ya da 23:00 (Türkiye saati)."""
    yerel = an.astimezone(TZ)
    if yerel.hour < 7:
        return datetime.combine(yerel.date() - timedelta(days=1), time(23), TZ)
    saat = 23 if yerel.hour >= 23 else 15 if yerel.hour >= 15 else 7
    return datetime.combine(yerel.date(), time(saat), TZ)


def _degdigi_vardiyalar(bas: datetime, bit: datetime) -> list[datetime]:
    """[bas, bit) aralığının değdiği vardiyaların başlangıçları."""
    sonuc = []
    v_bas = vardiya_baslangici(bas)
    while v_bas < bit:
        sonuc.append(v_bas)
        v_bas += timedelta(minutes=VARDIYA_DK)
    return sonuc


def _dk(bas: datetime, bit: datetime) -> int:
    """İki an arasındaki tam dakika (veride bütün anlar dakika başına denk gelir)."""
    return max(0, round((bit - bas).total_seconds() / 60))


def _birlestir(araliklar: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Çakışan aralıkları birleştirir; boş aralıkları atar."""
    sonuc: list[tuple[datetime, datetime]] = []
    for bas, bit in sorted(a for a in araliklar if a[1] > a[0]):
        if sonuc and bas <= sonuc[-1][1]:
            sonuc[-1] = (sonuc[-1][0], max(sonuc[-1][1], bit))
        else:
            sonuc.append((bas, bit))
    return sonuc


def _uretim_verisi_uret(
    rng: random.Random,
    son_an: datetime,
    ilk_gun: date,
    hatlar: list[Kayit],
    makineler: list[Kayit],
    arizalar: list[Kayit],
    is_emirleri: list[Kayit],
) -> tuple[list[Kayit], list[Kayit]]:
    """Her hattın tamamlanmış her vardiyası için üretim kaydı ve duruşlarını üretir.

    Arıza duruşları arıza kayıtlarından hesaplanır: hattı durduran arızanın vardiyanın içinde
    kalan dakikaları. Çakışan dakikalar önce başlayan arızaya yazılır, planlı bakımla örtüşen
    dakikalar duruş sayılmaz. Ürün değişimi ve malzeme beklemesi kalan süreden düşer; toplam
    duruş planlı süreyi aşamaz.
    """
    makine_hatti = {m["id"]: m["hat_id"] for m in makineler}

    durduranlar: dict[tuple[int, datetime], list[Kayit]] = defaultdict(list)
    for ariza in arizalar:
        if ariza["hat_durdu"]:
            for v_bas in _degdigi_vardiyalar(ariza["baslangic"], ariza["bitis"] or son_an):
                durduranlar[(makine_hatti[ariza["makine_id"]], v_bas)].append(ariza)

    bakimlar: dict[tuple[int, datetime], list[tuple[datetime, datetime]]] = defaultdict(list)
    for emir in is_emirleri:
        if emir["tip"] == "periyodik":
            bitis = emir["kapanis"] or son_an
            for v_bas in _degdigi_vardiyalar(emir["acilis"], bitis):
                bakimlar[(makine_hatti[emir["makine_id"]], v_bas)].append((emir["acilis"], bitis))

    vardiyalar: list[Kayit] = []
    duruslar: list[Kayit] = []
    for gun_no in range(GUN_SAYISI):
        gun = ilk_gun + timedelta(days=gun_no)
        for no, saat in VARDIYALAR:
            if no not in CALISAN_VARDIYALAR.get(gun.weekday(), (1, 2, 3)):
                continue
            v_bas = datetime.combine(gun, time(saat), TZ)
            v_bit = v_bas + timedelta(minutes=VARDIYA_DK)
            if v_bit > son_an:
                continue  # yalnızca tamamlanmış vardiyalar

            for hat in hatlar:
                anahtar = (hat["id"], v_bas)
                bakim = _birlestir(
                    [(max(b, v_bas), min(e, v_bit)) for b, e in bakimlar.get(anahtar, [])]
                )
                planli = VARDIYA_DK - MOLA_DK - sum(_dk(b, e) for b, e in bakim)
                vardiya_id = len(vardiyalar) + 1

                olaylar: list[tuple[str, int | None, int]] = []  # (neden, arıza id, süre dk)
                kapsanan = v_bas
                for ariza in sorted(durduranlar.get(anahtar, []), key=lambda a: a["baslangic"]):
                    bas = max(ariza["baslangic"], kapsanan)
                    bit = min(ariza["bitis"] or son_an, v_bit)
                    if bit <= bas:
                        continue
                    kapsanan = bit
                    planli_bakimda = sum(_dk(max(bas, b), min(bit, e)) for b, e in bakim)
                    olaylar.append(("ariza", ariza["id"], _dk(bas, bit) - planli_bakimda))

                beklenen, medyan = URUN_DEGISIMI[hat["tip"]]
                if hat["ad"] == "Pres 3":
                    medyan = PRES_3_DEGISIM_DK
                for _ in range(_poisson(rng, beklenen)):
                    sure = round(rng.lognormvariate(math.log(medyan), 0.35))
                    olaylar.append(("urun_degisimi", None, sure))
                olasilik, medyan = MALZEME_BEKLEME
                if rng.random() < olasilik:
                    sure = round(rng.lognormvariate(math.log(medyan), 0.5))
                    olaylar.append(("malzeme_bekleme", None, sure))

                kalan = planli  # hattın çalıştığı süre; her duruş bundan düşer
                for neden, ariza_id, sure in olaylar:
                    sure = min(sure, kalan)
                    if sure > 0:
                        duruslar.append(
                            {
                                "id": len(duruslar) + 1,
                                "vardiya_id": vardiya_id,
                                "neden": neden,
                                "ariza_id": ariza_id,
                                "sure_dk": sure,
                            }
                        )
                        kalan -= sure

                ideal, performans, hurda_orani = URETIM_PARAMETRELERI[hat["tip"]]
                if hat["ad"] == "Pres 3":
                    performans = PRES_3_PERFORMANSI
                    if gun_no >= GUN_SAYISI - 60:
                        hurda_orani *= PRES_3_HURDA_CARPANI
                if no == 3:
                    performans -= GECE_PERFORMANS_KAYBI
                performans = min(0.98, max(0.5, rng.gauss(performans, 0.03)))
                toplam = int(kalan * 60 / ideal * performans)
                hurda_olasiligi = min(1.0, hurda_orani * rng.lognormvariate(0, 0.3))
                vardiyalar.append(
                    {
                        "id": vardiya_id,
                        "hat_id": hat["id"],
                        "vardiya": no,
                        "baslangic": v_bas,
                        "bitis": v_bit,
                        "planli_sure_dk": planli,
                        "toplam_adet": toplam,
                        "hurda_adet": rng.binomialvariate(toplam, hurda_olasiligi),
                    }
                )
    return vardiyalar, duruslar


def veritabanina_yaz(conn: psycopg.Connection, veri: dict[str, list[Kayit]]) -> None:
    """Şemayı sıfırdan kurar ve veriyi tek transaction içinde yazar."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(SEMA_DOSYASI.read_text(encoding="utf-8"))
        cur.execute(TELEGRAM_SEMASI.read_text(encoding="utf-8"))
        cur.execute(ENTEGRASYON_SEMASI.read_text(encoding="utf-8"))
        entegrasyon.katalogu_yukle(conn)
        for tablo in TABLOLAR:
            kayitlar = veri[tablo]
            kolonlar = list(kayitlar[0])
            ekle = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                sql.Identifier(tablo),
                sql.SQL(", ").join(map(sql.Identifier, kolonlar)),
                sql.SQL(", ").join(map(sql.Placeholder, kolonlar)),
            )
            cur.executemany(ekle, kayitlar)  # secscope: ignore SAST-SQLI-001
            # id'leri biz yazdık; sıradaki kayıt (örn. agent'ın açtığı talep) çakışmasın diye
            # kimlik sayacını en büyük id'ye taşı.
            cur.execute(  # secscope: ignore SAST-SQLI-001
                sql.SQL(
                    "SELECT setval(pg_get_serial_sequence(%s, 'id'), (SELECT max(id) FROM {}))"
                ).format(sql.Identifier(tablo)),
                [tablo],
            )


def demo_kullanicilari_yaz(conn: psycopg.Connection, parola: str) -> None:
    """Her rolden bir demo kullanıcı ekler (şema yeniden kurulduğu için tablo boştur)."""
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO kullanicilar (kullanici_adi, ad_soyad, rol, parola_hash) "
            "VALUES (%s, %s, %s, %s)",
            [(ad, ad_soyad, rol, parola_hashle(parola)) for ad, ad_soyad, rol in DEMO_KULLANICILAR],
        )


def main() -> None:
    ayrac = argparse.ArgumentParser(description="Veritabanını sahte fabrika verisiyle doldurur.")
    ayrac.add_argument(
        "--son", help='Verinin bittiği an, örn. "2026-09-30 12:00". Varsayılan: şimdi'
    )
    ayrac.add_argument("--tohum", type=int, default=42, help="Rastgele sayı tohumu (varsayılan 42)")
    args = ayrac.parse_args()

    if args.son:
        son_an = datetime.fromisoformat(args.son).replace(tzinfo=TZ)
    else:
        son_an = datetime.now(TZ).replace(second=0, microsecond=0)

    veri = veri_uret(son_an, args.tohum)
    try:
        with baglan() as conn:
            veritabanina_yaz(conn, veri)
            demo_kullanicilari_yaz(conn, ayarlar().demo_parola)
    except psycopg.OperationalError as hata:
        sys.exit(f"Veritabanına bağlanılamadı. 'docker compose up -d' çalıştı mı?\n{hata}")

    print(f"Veri {son_an:%Y-%m-%d %H:%M} anında bitecek şekilde yazıldı:")
    for tablo in TABLOLAR:
        print(f"  {tablo:<18} {len(veri[tablo]):>5} kayıt")
    kullanicilar = ", ".join(ad for ad, _, _ in DEMO_KULLANICILAR)
    print(f"Demo kullanıcıları: {kullanicilar} (parola: .env'deki DEMO_PAROLA)")


if __name__ == "__main__":
    main()
