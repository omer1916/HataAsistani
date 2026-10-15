"""Haftalık bakım planı: hangi makinelere bakım yapılırsa beklenen duruş en çok azalır.

Her makine için:
1. Son ANALIZ_GUN günün arızalarından arızalar arası süreler çıkarılır (tamirden sonraki
   arızaya kadar); son arızadan bugüne geçen süre sansürlü gözlemdir. Model
   guvenilirlik.model_sec ile seçilir: az aralıkta üstel, en az 10 aralıkta Weibull ile üstel
   arasında olabilirlik oranı testi. Tek arızası olan makinede arıza sayısından kaba bir oran.
2. Ufuktaki (ör. önümüzdeki 7 gün) arıza olasılığı: makinenin son arızadan beri bozulmadan
   çalıştığı bilinerek hesaplanan koşullu olasılık. Makine şu an arızalıysa tamirden sonra
   sıfırdan başlar (yaş 0); plana "tamirden sonra" notuyla girer.
3. Arıza başına beklenen hat duruşu: bu makinenin arızalarının hattı durdurduğu toplam süre /
   arıza sayısı. Hattı durdurmayan arızalar da paydadadır.
4. Önlenebilirlik: bakımın önleyebileceği arıza payı, arıza tipine göre (yazılım arızası yağ
   değiştirerek önlenmez); makinenin arıza tipi karışımına göre ortalanır.

   kazanç (dk) = önlenebilirlik x arıza olasılığı x arıza başına duruş

Geciken periyodik bakımlar (app/periyodik.py) zorunludur: kapasite önce onlara, en çok
gecikenden başlayarak ayrılır. Kalan kapasite sırt çantası problemiyle kazançlara göre dağıtılır.
"""

import math
from datetime import datetime, timedelta
from itertools import groupby, pairwise

import psycopg

from app import periyodik, sorgular
from app.db import TZ
from app.guvenilirlik import beta_yorumu, model_sec
from app.planlama import Aday, acgozlu, sirt_cantasi

ANALIZ_GUN = 90  # makinenin bugünkü durumunu eski veri değil son aylar yansıtır
VARSAYILAN_BAKIM_SAAT = 3  # periyodik bakım kaydı olmayan makine için
EN_KISA_SURE_SAAT = 0.1  # önceki tamir bitmeden başlayan arıza: neredeyse sıfır aralık
# Bakımın önleyebileceği arıza payı. Varsayımdır; sahadaki bakım kayıtlarıyla güncellenmeli.
ONLENEBILIRLIK = {
    "hidrolik": 0.7,
    "mekanik": 0.7,
    "pnomatik": 0.7,
    "sensor": 0.6,
    "elektrik": 0.4,
    "yazilim": 0.1,
}


def _saat(fark: timedelta) -> float:
    return fark.total_seconds() / 3600


def arizalar_arasi_sureler(
    arizalar: list[dict], simdi: datetime
) -> tuple[list[float], list[float], float]:
    """(tamamlanmış arızalar arası süreler, sansürlü süreler, makinenin yaşı), saat.

    Yaş: son tamirden bu yana geçen süre; aynı zamanda sansürlü tek gözlem. Son arıza hâlâ
    sürüyorsa sansürlü gözlem yoktur ve yaş 0'dır: tamirden sonra makine sıfırdan başlar.
    `arizalar` başlangıç sırasıyla ve en az bir tane olmalı."""
    sureler = [
        max(_saat(sonra["baslangic"] - once["bitis"]), EN_KISA_SURE_SAAT)
        for once, sonra in pairwise(arizalar)
        if once["bitis"] is not None
    ]
    if arizalar[-1]["bitis"] is None:
        return sureler, [], 0.0
    yas = max(_saat(simdi - arizalar[-1]["bitis"]), EN_KISA_SURE_SAAT)
    return sureler, [yas], yas


def makine_riski(satirlar: list[dict], simdi: datetime, ufuk_saat: float, bakim_saat: float):
    """Tek makinenin risk ve kazanç hesabı. `satirlar`: makine_arizalari'nın o makineye ait
    satırları."""
    ilk = satirlar[0]
    arizalar = [s for s in satirlar if s["ariza_id"] is not None]
    risk = {
        "makine_kodu": ilk["makine_kodu"],
        "makine_adi": ilk["makine_adi"],
        "hat": ilk["hat"],
        "ariza_sayisi": len(arizalar),
        "aralik_sayisi": 0,  # modelin dayandığı tamamlanmış arıza aralığı sayısı
        "su_an_arizali": bool(arizalar) and arizalar[-1]["bitis"] is None,
        "model": "ariza_yok",
        "beta": None,
        "beta_yorumu": None,
        "olabilirlik_orani": None,
        "mtbf_saat": None,
        "son_arizadan_beri_saat": None,
        "ariza_olasiligi": 0.0,
        "ariza_basina_durus_dk": 0.0,
        "onlenebilirlik": 0.0,
        "kazanc_dk": 0.0,
        "bakim_saat": max(1, math.ceil(bakim_saat)),
        "gecikmis_bakimlar": [],  # geciken periyodik bakımlar; bakim_plani doldurur
        "gecikme_gun": None,
        "zorunlu": False,
    }
    if not arizalar:
        return risk

    risk["ariza_basina_durus_dk"] = sum(a["durus_dk"] for a in arizalar) / len(arizalar)
    risk["onlenebilirlik"] = sum(ONLENEBILIRLIK[a["ariza_tipi"]] for a in arizalar) / len(arizalar)
    sureler, sansurlu, yas = arizalar_arasi_sureler(arizalar, simdi)
    risk["aralik_sayisi"] = len(sureler)
    risk["son_arizadan_beri_saat"] = None if risk["su_an_arizali"] else yas

    secim = model_sec(sureler, sansurlu)
    if secim is not None:
        risk.update(
            model=secim.tur,
            beta=secim.weibull.beta if secim.weibull else None,
            beta_yorumu=beta_yorumu(secim.secilen.beta),
            olabilirlik_orani=secim.olabilirlik_orani,
            mtbf_saat=secim.secilen.ortalama(),
            ariza_olasiligi=secim.secilen.kosullu_ariza_olasiligi(yas, ufuk_saat),
        )
    else:  # tek arıza, aralık yok: arıza sayısından kaba bir sabit oran
        oran = len(arizalar) / (ANALIZ_GUN * 24)
        risk.update(
            model="az_veri", mtbf_saat=1 / oran, ariza_olasiligi=-math.expm1(-oran * ufuk_saat)
        )
    risk["kazanc_dk"] = (
        risk["onlenebilirlik"] * risk["ariza_olasiligi"] * risk["ariza_basina_durus_dk"]
    )
    return risk


def bakim_plani(
    conn: psycopg.Connection, simdi: datetime, kapasite_saat: int = 16, ufuk_gun: int = 7
) -> dict:
    satirlar = sorgular.makine_arizalari(conn, simdi - timedelta(days=ANALIZ_GUN), simdi)
    bakim_sureleri = sorgular.bakim_sureleri(conn)
    riskler = [
        makine_riski(
            list(grup), simdi, ufuk_gun * 24, bakim_sureleri.get(kod, VARSAYILAN_BAKIM_SAAT)
        )
        for kod, grup in groupby(satirlar, key=lambda s: s["makine_kodu"])
    ]

    # Geciken periyodik bakımı olan makine zorunlu; süresi geciken bakımların toplamı (riskten
    # gelen süreden kısa değil). Kapasite yetmezse en çok gecikenler alınır.
    gecikmisler = periyodik.gecikmis_bakimlar(conn, simdi.astimezone(TZ).date())
    for r in riskler:
        planlar = gecikmisler.get(r["makine_kodu"], [])
        if planlar:
            r["zorunlu"] = True
            r["gecikme_gun"] = max(-b["kalan_gun"] for b in planlar)
            r["gecikmis_bakimlar"] = [
                f"{b['bakim']} ({-b['kalan_gun']} gün gecikti)" for b in planlar
            ]
            r["bakim_saat"] = max(r["bakim_saat"], sum(b["sure_saat"] for b in planlar))
    secilen, kalan = set(), kapasite_saat
    for r in sorted(
        (r for r in riskler if r["zorunlu"]), key=lambda r: (-r["gecikme_gun"], r["makine_kodu"])
    ):
        if r["bakim_saat"] <= kalan:
            secilen.add(r["makine_kodu"])
            kalan -= r["bakim_saat"]
    zorunlu_kazanc = sum(r["kazanc_dk"] for r in riskler if r["makine_kodu"] in secilen)

    adaylar = [r for r in riskler if r["kazanc_dk"] > 0 and not r["zorunlu"]]
    esyalar = [Aday(r["bakim_saat"], r["kazanc_dk"]) for r in adaylar]
    secilen |= {adaylar[i]["makine_kodu"] for i in sirt_cantasi(esyalar, kalan)}
    acgozlu_kazanc = zorunlu_kazanc + sum(adaylar[i]["kazanc_dk"] for i in acgozlu(esyalar, kalan))
    for r in riskler:
        r["secildi"] = r["makine_kodu"] in secilen
    riskler.sort(
        key=lambda r: (not r["secildi"], not r["zorunlu"], -r["kazanc_dk"], r["makine_kodu"])
    )

    return {
        "hesaplama_ani": simdi,
        "analiz_gun": ANALIZ_GUN,
        "ufuk_gun": ufuk_gun,
        "kapasite_saat": kapasite_saat,
        "secilen_saat": sum(r["bakim_saat"] for r in riskler if r["secildi"]),
        "periyodik_saat": sum(r["bakim_saat"] for r in riskler if r["secildi"] and r["zorunlu"]),
        "onlenen_durus_dk": sum(r["kazanc_dk"] for r in riskler if r["secildi"]),
        "acgozlu_onlenen_durus_dk": acgozlu_kazanc,
        "makineler": riskler,
    }
