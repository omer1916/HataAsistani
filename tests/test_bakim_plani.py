"""Bakım planı: Weibull (sansürlü) en çok olabilirlik, model seçimi, sırt çantası ve plan."""

import itertools
import json
import math
import random
from datetime import timedelta
from functools import cache

import psycopg
import pytest

from app import guvenilirlik as g
from app.auth import Kullanici
from app.bakim_plani import ANALIZ_GUN, bakim_plani
from app.planlama import Aday, acgozlu, sirt_cantasi
from app.tools import AracBaglami, arac_calistir
from scripts.seed import SUREN_ARIZALAR
from tests.conftest import BAKIM, OPERATOR, SABIT_AN, SahteEmbedder, yetki

# --- Weibull ------------------------------------------------------------------------------


def _ornek(rng: random.Random, beta: float, eta: float, n: int, sansur: float = 2.5):
    """n ömür üretir; her biri [0, sansur x eta] aralığında rastgele bir anda gözlemden çıkabilir
    (sağdan sansür). (arızalar, sansürlüler) döner."""
    arizalar, sansurlu = [], []
    for _ in range(n):
        omur, cikis = rng.weibullvariate(eta, beta), rng.uniform(0, sansur * eta)
        (arizalar if omur <= cikis else sansurlu).append(min(omur, cikis))
    return arizalar, sansurlu


@pytest.mark.parametrize(("beta", "eta"), [(0.7, 100), (1.0, 100), (2.5, 100), (4.0, 30)])
def test_mle_bilinen_parametreleri_bulur(beta, eta):
    arizalar, sansurlu = _ornek(random.Random(1), beta, eta, 3000)
    assert len(sansurlu) > 600  # sansür gerçekten var
    tahmin = g.weibull_uydur(arizalar, sansurlu)
    assert tahmin.beta == pytest.approx(beta, rel=0.06)
    assert tahmin.eta == pytest.approx(eta, rel=0.06)


def test_sansurlu_gozlemi_atmak_mtbfi_kisaltir():
    arizalar, sansurlu = _ornek(random.Random(2), 1.5, 100, 2000)
    dogru = g.weibull_uydur(arizalar, sansurlu)
    yanlis = g.weibull_uydur(arizalar)  # makine "henüz bozulmadı" bilgisi atılınca
    assert yanlis.ortalama() < 0.8 * dogru.ortalama()


@pytest.mark.parametrize("tohum", range(10))
def test_mle_scipy_ile_ayni(tohum):
    stats = pytest.importorskip("scipy.stats")
    rng = random.Random(tohum)
    arizalar, sansurlu = _ornek(rng, rng.uniform(0.6, 3), 50, rng.randint(12, 40))
    tahmin = g.weibull_uydur(arizalar, sansurlu)
    veri = stats.CensoredData(uncensored=arizalar, right=sansurlu)
    beta, _, eta = stats.weibull_min.fit(veri, floc=0)
    assert tahmin.beta == pytest.approx(beta, rel=1e-3)
    assert tahmin.eta == pytest.approx(eta, rel=1e-3)


def test_weibull_sinir_durumlari():
    assert g.weibull_uydur([1, 2, 3, 4]) is None  # 5'ten az arıza
    assert g.weibull_uydur([5.0] * 6).beta == g.BETA_ARALIGI[1]  # hepsi eşit: β sınırda
    with pytest.raises(ValueError):
        g.weibull_uydur([1, 2, 3, 4, 0])


def test_ortalama_ve_kosullu_olasilik():
    assert g.Weibull(1.0, 100).ortalama() == pytest.approx(100)
    assert g.Weibull(2.0, 100).ortalama() == pytest.approx(100 * math.sqrt(math.pi) / 2)
    ustel = g.Weibull(1.0, 100)
    # Hafızasızlık: sabit riskte makinenin yaşı önümüzdeki haftayı etkilemez.
    olasiliklar = [ustel.kosullu_ariza_olasiligi(yas, 50) for yas in (0, 10, 500)]
    assert olasiliklar == pytest.approx([1 - math.exp(-0.5)] * 3)
    asinan = g.Weibull(2.5, 100)
    artan = [asinan.kosullu_ariza_olasiligi(yas, 20) for yas in (0, 50, 100, 200)]
    assert artan == sorted(artan) and all(0 <= p <= 1 for p in artan)
    assert asinan.kosullu_ariza_olasiligi(30, 0) == 0
    assert asinan.kosullu_ariza_olasiligi(1e6, 20) == pytest.approx(1)  # taşma yok


def test_model_secimi():
    rng = random.Random(5)
    az = [rng.expovariate(0.01) for _ in range(9)]
    secim = g.model_sec(az, [10])
    assert (secim.tur, secim.weibull, secim.olabilirlik_orani) == ("ustel", None, None)
    assert secim.secilen.eta == pytest.approx((sum(az) + 10) / 9)
    assert g.model_sec([], [10]) is None

    asinma, sansur = _ornek(rng, 3.0, 100, 80)
    secim = g.model_sec(asinma, sansur)
    assert secim.tur == "weibull" and secim.olabilirlik_orani > g.KI_KARE_1_YUZDE_95
    assert secim.secilen.beta == pytest.approx(3.0, rel=0.25)


def test_olabilirlik_orani_saf_ustel_veride_nadiren_weibull_secer():
    """%5 düzeyindeki test, saf üstel 40 aralıkta Weibull'ü yaklaşık %5 oranında seçmeli."""
    rng = random.Random(11)
    secilen = sum(
        g.model_sec([rng.expovariate(0.01) for _ in range(40)], [rng.expovariate(0.01)]).tur
        == "weibull"
        for _ in range(600)
    )
    assert secilen / 600 < 0.09


# --- Sırt çantası --------------------------------------------------------------------------


def _deger(adaylar, secilen):
    return sum(adaylar[i].deger for i in secilen)


@pytest.mark.parametrize("tohum", range(300))
def test_dinamik_programlama_kaba_kuvvetle_ayni(tohum):
    rng = random.Random(tohum)
    adaylar = [
        Aday(rng.randint(1, 8), round(rng.uniform(0, 50), 1)) for _ in range(rng.randint(0, 10))
    ]
    kapasite = rng.randint(0, 30)
    secilen = sirt_cantasi(adaylar, kapasite)
    assert sum(adaylar[i].sure for i in secilen) <= kapasite
    en_iyi = max(
        (
            _deger(adaylar, kume)
            for r in range(len(adaylar) + 1)
            for kume in itertools.combinations(range(len(adaylar)), r)
            if sum(adaylar[i].sure for i in kume) <= kapasite
        ),
        default=0,
    )
    assert _deger(adaylar, secilen) == pytest.approx(en_iyi)
    assert _deger(adaylar, acgozlu(adaylar, kapasite)) <= en_iyi + 1e-9


def test_acgozlu_yontemin_yanildigi_ornek():
    # Ders kitabı örneği: oranı en yüksek olanı almak kapasitede boşluk bırakır.
    adaylar = [Aday(10, 60), Aday(20, 100), Aday(30, 120)]
    assert acgozlu(adaylar, 50) == [0, 1]  # 160
    assert sirt_cantasi(adaylar, 50) == [1, 2]  # 220


def test_sirt_cantasi_sinirlar():
    adaylar = [Aday(3, 10), Aday(2, 0), Aday(4, 5)]
    assert sirt_cantasi(adaylar, 0) == []
    assert sirt_cantasi(adaylar, 100) == [0, 2]  # değeri sıfır olan seçilmez
    assert sirt_cantasi([], 10) == []


# --- Plan (test veritabanı) ----------------------------------------------------------------


@pytest.fixture(scope="module")
def baglanti(test_veritabani):
    with psycopg.connect(test_veritabani, autocommit=True) as conn:
        yield conn


@pytest.fixture(scope="module")
def plan(baglanti):
    return bakim_plani(baglanti, SABIT_AN, kapasite_saat=16, ufuk_gun=7)


def test_plan_verisi_python_hesabiyla_ayni(plan, test_verisi):
    kod = {m["id"]: m["kod"] for m in test_verisi["makineler"]}
    alt = SABIT_AN - timedelta(days=ANALIZ_GUN)
    arizalar = [a for a in test_verisi["ariza_kayitlari"] if alt <= a["baslangic"] <= SABIT_AN]
    durus: dict[int, int] = {}
    for d in test_verisi["vardiya_duruslari"]:
        if d["ariza_id"] is not None:
            durus[d["ariza_id"]] = durus.get(d["ariza_id"], 0) + d["sure_dk"]

    makineler = {r["makine_kodu"]: r for r in plan["makineler"]}
    assert set(makineler) == set(kod.values())
    for makine_kodu, r in makineler.items():
        kendi = [a for a in arizalar if kod[a["makine_id"]] == makine_kodu]
        assert r["ariza_sayisi"] == len(kendi)
        assert r["aralik_sayisi"] == max(len(kendi) - 1, 0)
        if kendi:
            beklenen = sum(durus.get(a["id"], 0) for a in kendi) / len(kendi)
            assert r["ariza_basina_durus_dk"] == pytest.approx(beklenen)
    assert {k for k, r in makineler.items() if r["su_an_arizali"]} == {
        kod_ for kod_, *_ in SUREN_ARIZALAR
    }


def test_ustel_modelin_mtbfi_sansurlu_gozlemi_icerir(plan, test_verisi):
    """Üstel modelde MTBF = (arızalar arası sürelerin toplamı + son tamirden beri geçen süre)
    / aralık sayısı. Sansürlü süre unutulursa makine olduğundan sık bozuluyor görünür."""
    kod = {m["id"]: m["kod"] for m in test_verisi["makineler"]}
    alt = SABIT_AN - timedelta(days=ANALIZ_GUN)
    ustel = [r for r in plan["makineler"] if r["model"] == "ustel"]
    assert len(ustel) > 5
    for r in ustel:
        kendi = sorted(
            (a for a in test_verisi["ariza_kayitlari"]
             if kod[a["makine_id"]] == r["makine_kodu"] and alt <= a["baslangic"] <= SABIT_AN),
            key=lambda a: (a["baslangic"], a["id"]),
        )  # fmt: skip
        saat = [
            max((b["baslangic"] - a["bitis"]).total_seconds() / 3600, 0.1)
            for a, b in itertools.pairwise(kendi)
        ]
        yas = 0 if r["su_an_arizali"] else (SABIT_AN - kendi[-1]["bitis"]).total_seconds() / 3600
        assert r["mtbf_saat"] == pytest.approx((sum(saat) + yas) / len(saat)), r["makine_kodu"]


def test_plan_modelleri_kurallara_uyar(plan):
    for r in plan["makineler"]:
        assert 0 <= r["ariza_olasiligi"] <= 1 and r["kazanc_dk"] >= 0
        if r["aralik_sayisi"] < g.WEIBULL_ICIN_EN_AZ:
            assert r["model"] in {"ustel", "az_veri", "ariza_yok"} and r["beta"] is None
        else:
            assert (r["model"] == "weibull") == (r["olabilirlik_orani"] > g.KI_KARE_1_YUZDE_95)
        if r["su_an_arizali"]:
            assert r["son_arizadan_beri_saat"] is None and r["ariza_olasiligi"] > 0
        assert r["kazanc_dk"] == pytest.approx(
            r["onlenebilirlik"] * r["ariza_olasiligi"] * r["ariza_basina_durus_dk"]
        )


def _en_iyi_deger(adaylar: list[Aday], kapasite: int) -> float:
    """Bağımsız kontrol: yukarıdan aşağı özyinelemeli çözüm (tablo değil)."""

    @cache
    def en_iyi(i: int, kalan: int) -> float:
        if i == len(adaylar):
            return 0.0
        atla = en_iyi(i + 1, kalan)
        if adaylar[i].sure > kalan:
            return atla
        return max(atla, adaylar[i].deger + en_iyi(i + 1, kalan - adaylar[i].sure))

    return en_iyi(0, kapasite)


@pytest.mark.parametrize("kapasite", [1, 3, 7, 16, 25, 40, 200])
def test_plan_kapasiteyle_en_iyi_secim(baglanti, kapasite):
    p = bakim_plani(baglanti, SABIT_AN, kapasite_saat=kapasite)
    secilen = [r for r in p["makineler"] if r["secildi"]]
    assert p["secilen_saat"] == sum(r["bakim_saat"] for r in secilen) <= kapasite
    assert p["onlenen_durus_dk"] == pytest.approx(sum(r["kazanc_dk"] for r in secilen))
    # Geciken periyodik bakımlar (seed senaryosu: P3-HP yıllık) önce; kalan kapasitede en iyi.
    zorunlu = [r for r in secilen if r["zorunlu"]]
    assert p["periyodik_saat"] == sum(r["bakim_saat"] for r in zorunlu)
    kalan = kapasite - p["periyodik_saat"]
    adaylar = [
        Aday(r["bakim_saat"], r["kazanc_dk"])
        for r in p["makineler"]
        if r["kazanc_dk"] > 0 and not r["zorunlu"]
    ]
    assert p["onlenen_durus_dk"] == pytest.approx(
        sum(r["kazanc_dk"] for r in zorunlu) + _en_iyi_deger(adaylar, kalan)
    )
    assert p["acgozlu_onlenen_durus_dk"] <= p["onlenen_durus_dk"] + 1e-9
    # Sıra: önce seçilenler (zorunlular başta), sonra kazanca göre
    anahtarlar = [(not r["secildi"], not r["zorunlu"], -r["kazanc_dk"]) for r in p["makineler"]]
    assert anahtarlar == sorted(anahtarlar)
    if kapasite == 200:
        assert all(r["secildi"] for r in p["makineler"] if r["kazanc_dk"] > 0 or r["zorunlu"])


def test_geciken_periyodik_bakim_plana_once_girer(baglanti):
    # Seed senaryosu: P3-HP'nin yıllık bakımı SABIT_AN'da 12 gün gecikmiş (6 saat).
    p = bakim_plani(baglanti, SABIT_AN, kapasite_saat=8)
    p3_hp = next(r for r in p["makineler"] if r["makine_kodu"] == "P3-HP")
    assert p3_hp["zorunlu"] and p3_hp["secildi"] and p3_hp["gecikme_gun"] == 12
    assert p3_hp["gecikmis_bakimlar"] == ["Yıllık bakım (12 gün gecikti)"]
    assert p3_hp["bakim_saat"] >= 6 and p["periyodik_saat"] == p3_hp["bakim_saat"]
    assert p["makineler"][0] is p3_hp
    assert sum(r["zorunlu"] for r in p["makineler"]) == 1  # başka geciken yok


def test_kapasite_yetmezse_geciken_bakim_secilmez_ama_isaretli(baglanti):
    p = bakim_plani(baglanti, SABIT_AN, kapasite_saat=1)  # 6 saatlik yıllık bakım sığmaz
    p3_hp = next(r for r in p["makineler"] if r["makine_kodu"] == "P3-HP")
    assert p3_hp["zorunlu"] and not p3_hp["secildi"]
    assert p["periyodik_saat"] == 0 and p["secilen_saat"] <= 1


def test_su_an_arizali_en_sorunlu_makine_plana_girer(plan):
    p3_hp = next(r for r in plan["makineler"] if r["makine_kodu"] == "P3-HP")
    assert p3_hp["su_an_arizali"] and p3_hp["secildi"]
    assert p3_hp["ariza_sayisi"] == max(r["ariza_sayisi"] for r in plan["makineler"])


# --- API ve agent aracı -------------------------------------------------------------------


def test_bakim_plani_api_ve_rol(istemci):
    cevap = istemci.get("/bakim-plani", params={"kapasite_saat": 10, "ufuk_gun": 14})
    assert cevap.status_code == 200
    govde = cevap.json()
    assert (govde["kapasite_saat"], govde["ufuk_gun"]) == (10, 14)
    assert govde["secilen_saat"] <= 10 and any(r["secildi"] for r in govde["makineler"])
    assert istemci.get("/bakim-plani", headers=yetki(OPERATOR)).status_code == 403


@pytest.mark.parametrize(
    "parametreler",
    [{"kapasite_saat": 0}, {"kapasite_saat": 201}, {"ufuk_gun": 0}, {"ufuk_gun": 31},
     {"kapasite_saat": "on"}, {"kapasite": 16}],
    ids=["kapasite-0", "kapasite-201", "ufuk-0", "ufuk-31", "kapasite-metin", "bilinmeyen"],
)  # fmt: skip
def test_bakim_plani_gecersiz_parametre_422(istemci, parametreler):
    cevap = istemci.get("/bakim-plani", params=parametreler, headers=yetki(BAKIM))
    assert cevap.status_code == 422


def _baglam(conn, rol: str) -> AracBaglami:
    return AracBaglami(
        conn=conn, embedder=SahteEmbedder(), kullanici=Kullanici("t", "T", rol), simdi=SABIT_AN
    )


def test_bakim_plani_araci_bakim_rolune_plan_doner(baglanti):
    sonuc = arac_calistir(
        "bakim_plani_oner", json.dumps({"kapasite_saat": 12}), _baglam(baglanti, "bakim")
    )
    plan_ = sonuc["sonuc"]
    assert plan_["kapasite_saat"] == 12 and plan_["secilen_saat"] <= 12
    assert sum(s["bakim_saat"] for s in plan_["plan"]) == plan_["secilen_saat"]
    assert all(0 <= s["ariza_olasiligi_yuzde"] <= 100 for s in plan_["plan"])
    assert "varsayım" in plan_["aciklama"]


def test_bakim_plani_araci_operatore_kapali(baglanti):
    sonuc = arac_calistir("bakim_plani_oner", "{}", _baglam(baglanti, "operator"))
    assert "yalnızca bakım" in sonuc["hata"]
