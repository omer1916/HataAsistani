import copy
import json
from datetime import date

import httpx
import openai
import psycopg
import pytest

from app.agent import (
    MAKS_ADIM,
    SohbetHatasi,
    kaydet,
    sistem_istemi,
    sohbet,
    tarih_araliklari,
)
from app.auth import Kullanici
from app.db import TZ
from app.llm import AracCagrisi, LLMYaniti, maliyet_hesapla
from app.main import llm_getir
from app.tools import AracBaglami, arac_semalari
from tests.conftest import SABIT_AN, SahteEmbedder


def arac_iste(*cagrilar: tuple[str, dict], ek_alan: dict | None = None) -> LLMYaniti:
    """LLM'in bir adımda verilen araçları çağırdığı yanıt."""
    idler = [f"cagri-{ad}-{i}" for i, (ad, _) in enumerate(cagrilar)]
    mesaj = {
        "role": "assistant",
        "tool_calls": [
            {"id": i, "type": "function", "function": {"name": ad, "arguments": json.dumps(a)}}
            for i, (ad, a) in zip(idler, cagrilar, strict=True)
        ],
        **(ek_alan or {}),
    }
    return LLMYaniti(
        mesaj=mesaj,
        metin=None,
        arac_cagrilari=[
            AracCagrisi(i, ad, json.dumps(a)) for i, (ad, a) in zip(idler, cagrilar, strict=True)
        ],
        girdi_token=100,
        cikti_token=20,
    )


def cevap_ver(metin: str) -> LLMYaniti:
    return LLMYaniti({"role": "assistant", "content": metin}, metin, [], 150, 40)


class SenaryoluLLM:
    """Sırayla verilen yanıtları döndürür; kendisine gelen mesajları saklar."""

    saglayici = "sahte"
    model = "sahte-model"

    def __init__(self, *adimlar):
        self.adimlar = list(adimlar)
        self.gelen_mesajlar: list[list[dict]] = []

    def tamamla(self, mesajlar, araclar):
        self.gelen_mesajlar.append(copy.deepcopy(mesajlar))
        adim = self.adimlar.pop(0) if len(self.adimlar) > 1 else self.adimlar[0]
        if isinstance(adim, Exception):
            raise adim
        return adim


def arac_sonuclari(llm: SenaryoluLLM) -> list[dict]:
    """LLM'e geri gönderilen araç sonuçları (son çağrıda görülen geçmişten)."""
    return [json.loads(m["content"]) for m in llm.gelen_mesajlar[-1] if m["role"] == "tool"]


@pytest.fixture
def baglam(test_veritabani):
    with psycopg.connect(test_veritabani, autocommit=True) as conn:
        yield AracBaglami(
            conn=conn,
            embedder=SahteEmbedder(),
            kullanici=Kullanici("test", "Test", "bakim"),
            simdi=SABIT_AN,
        )


def test_arac_semalari_llm_icin_sade():
    semalar = {s["function"]["name"]: s["function"] for s in arac_semalari()}
    assert set(semalar) == {
        "ariza_say",
        "oee_hesapla",
        "bakim_plani_oner",
        "periyodik_bakim_durumu",
        "dokuman_ara",
        "stok_sorgula",
        "bakim_talebi_olustur",
    }
    metin = json.dumps(semalar)
    assert "anyOf" not in metin and '"title"' not in metin
    assert semalar["ariza_say"]["parameters"]["required"] == ["baslangic", "bitis"]
    assert semalar["oee_hesapla"]["parameters"]["required"] == ["baslangic", "bitis"]
    oncelik = semalar["bakim_talebi_olustur"]["parameters"]["properties"]["oncelik"]
    assert oncelik["enum"] == ["dusuk", "orta", "yuksek"]


def test_demo_sorusu_akisi(baglam, test_verisi):
    llm = SenaryoluLLM(
        arac_iste(
            ("ariza_say", {"hat": "Pres 3", "baslangic": "2026-08-01", "bitis": "2026-08-31"}),
            ("dokuman_ara", {"soru": "hidrolik arızada ilk kontrol"}),
            # Gemini'nin thought signature gibi ek alanları olduğu gibi geri gönderilmeli.
            ek_alan={"extra_content": {"google": {"thought_signature": "imza"}}},
        ),
        cevap_ver("Geçen ay 25 arıza oldu. İlk kontrol basınç sensörüdür (PRES-BK-01, s. 4)."),
    )
    sonuc = sohbet("Pres 3 geçen ay kaç arıza, ilk neye bakmalıyım?", llm, baglam)

    makine_hatti = {m["id"]: m["hat_id"] for m in test_verisi["makineler"]}
    beklenen = sum(
        1
        for a in test_verisi["ariza_kayitlari"]
        if makine_hatti[a["makine_id"]] == 3
        and date(2026, 8, 1) <= a["baslangic"].astimezone(TZ).date() <= date(2026, 8, 31)
    )
    ariza, dokuman = arac_sonuclari(llm)
    assert ariza["sonuc"]["toplam"] == beklenen > 0
    assert sum(ariza["sonuc"]["tiplere_gore"].values()) == beklenen
    assert {p["dokuman_kodu"] for p in dokuman["sonuc"]} and all(
        "sayfa" in p for p in dokuman["sonuc"]
    )

    assert sonuc.cevap.startswith("Geçen ay")
    assert sonuc.kaynaklar
    assert [c["ad"] for c in sonuc.arac_cagrilari] == ["ariza_say", "dokuman_ara"]
    assert (sonuc.adim_sayisi, sonuc.girdi_token, sonuc.cikti_token) == (2, 250, 60)
    assert "2026-09-30" in llm.gelen_mesajlar[0][0]["content"]  # sistem isteminde bugünün tarihi
    asistan = llm.gelen_mesajlar[1][2]
    assert asistan["extra_content"] == {"google": {"thought_signature": "imza"}}


@pytest.mark.parametrize(
    ("cagri", "beklenen_hata"),
    [
        (
            ("ariza_say", {"hat": "Pres 9", "baslangic": "2026-08-01", "bitis": "2026-08-31"}),
            "Geçerli hatlar: Pres 1",
        ),
        (("ariza_say", {"hat": "Pres 3", "baslangic": "2026-08-01"}), "bitis: Field required"),
        (("ariza_say", {"baslangic": "2026-09-01", "bitis": "2026-08-01"}), "sonra olamaz"),
        (("stok_sorgula", {"parca_kodu": "YOK-999"}), "stokta tanımlı değil"),
        (("tabloyu_sil", {}), "adında bir araç yok"),
        (
            (
                "bakim_talebi_olustur",
                {"makine_kodu": "P9-XX", "aciklama": "deneme talebi", "oncelik": "orta"},
            ),
            "Geçerli kodlar: B1-BK",
        ),
    ],
)
def test_arac_hatasi_llm_e_duzeltilebilir_mesaj_olarak_doner(baglam, cagri, beklenen_hata):
    llm = SenaryoluLLM(arac_iste(cagri), cevap_ver("tamam"))
    sonuc = sohbet("soru", llm, baglam)
    (arac_sonucu,) = arac_sonuclari(llm)
    assert beklenen_hata in arac_sonucu["hata"]
    assert sonuc.arac_cagrilari[0]["hata"] == arac_sonucu["hata"]


def test_stok_sorgusu_kritik_parcalari_doner(baglam):
    llm = SenaryoluLLM(arac_iste(("stok_sorgula", {"sadece_kritik": True})), cevap_ver("tamam"))
    sohbet("kritik stok", llm, baglam)
    (arac_sonucu,) = arac_sonuclari(llm)
    kodlar = {p["parca_kodu"] for p in arac_sonucu["sonuc"]}
    assert "SNS-002" in kodlar
    assert all(p["kritik"] for p in arac_sonucu["sonuc"])


def test_bir_istekte_tek_bakim_talebi_acilir(baglam):
    talep = {"makine_kodu": "p3-hp", "aciklama": "Basınç düşüyor, kontrol", "oncelik": "yuksek"}
    llm = SenaryoluLLM(
        arac_iste(("bakim_talebi_olustur", talep), ("bakim_talebi_olustur", talep)),
        cevap_ver("Talep açıldı."),
    )
    sohbet("P3-HP için talep aç", llm, baglam)

    ilk, ikinci = arac_sonuclari(llm)
    talep_id = ilk["sonuc"]["talep_id"]
    assert "zaten bir bakım talebi açıldı" in ikinci["hata"]
    satir = baglam.conn.execute(
        """SELECT m.kod, t.oncelik, t.durum, t.olusturan FROM bakim_talepleri t
           JOIN makineler m ON m.id = t.makine_id WHERE t.id = %s""",
        [talep_id],
    ).fetchone()
    assert satir == ("P3-HP", "yuksek", "acik", "test")


def test_adim_siniri_asilinca_durur(baglam):
    llm = SenaryoluLLM(arac_iste(("stok_sorgula", {})))  # hep araç ister, hiç cevap vermez
    sonuc = sohbet("soru", llm, baglam)
    assert sonuc.adim_sayisi == MAKS_ADIM
    assert sonuc.hata and "cevaplayamadım" in sonuc.cevap


def test_llm_hatasinda_kismi_sonuc_korunur(baglam):
    llm = SenaryoluLLM(arac_iste(("stok_sorgula", {})), RuntimeError("bağlantı koptu"))
    with pytest.raises(SohbetHatasi) as hata:
        sohbet("soru", llm, baglam)
    sonuc = hata.value.sonuc
    assert (sonuc.adim_sayisi, sonuc.girdi_token) == (1, 100)
    assert "bağlantı koptu" in sonuc.hata

    kayit_id = kaydet(baglam.conn, "soru", "test", "sahte", sonuc)
    satir = baglam.conn.execute(
        "SELECT girdi_token, hata FROM llm_istekleri WHERE id = %s", [kayit_id]
    ).fetchone()
    assert satir[0] == 100 and "bağlantı koptu" in satir[1]


def test_llm_argumaninda_nul_kaydi_ve_cevabi_bozmaz(baglam):
    # Model bozuk bir argüman üretse bile (NUL baytı) cevap kaybolmamalı, kayıt yazılmalı.
    llm = SenaryoluLLM(
        arac_iste(
            ("ariza_say", {"hat": "Pres 3\x00", "baslangic": "2026-08-01", "bitis": "2026-08-31"})
        ),
        cevap_ver("Tamam.\x00"),
    )
    sonuc = sohbet("soru", llm, baglam)
    assert sonuc.arac_cagrilari[0]["hata"]
    kayit_id = kaydet(baglam.conn, "soru", "test", "sahte", sonuc)
    cevap, argumanlar = baglam.conn.execute(
        "SELECT cevap, arac_cagrilari->0->'argumanlar'->>'hat' FROM llm_istekleri WHERE id = %s",
        [kayit_id],
    ).fetchone()
    assert (cevap, argumanlar) == ("Tamam.", "Pres 3")


@pytest.mark.parametrize(
    ("bugun", "ad", "beklenen"),
    [
        (date(2026, 9, 30), "geçen ay", (date(2026, 8, 1), date(2026, 8, 31))),
        (date(2026, 3, 31), "geçen ay", (date(2026, 2, 1), date(2026, 2, 28))),
        (date(2026, 1, 5), "geçen ay", (date(2025, 12, 1), date(2025, 12, 31))),
        (date(2026, 9, 30), "bu hafta", (date(2026, 9, 28), date(2026, 9, 30))),  # Çarşamba
        (date(2026, 9, 28), "geçen hafta", (date(2026, 9, 21), date(2026, 9, 27))),
        (date(2026, 1, 3), "son 7 gün", (date(2025, 12, 28), date(2026, 1, 3))),
        (date(2026, 1, 1), "dün", (date(2025, 12, 31), date(2025, 12, 31))),
        (date(2026, 9, 30), "bu yıl", (date(2026, 1, 1), date(2026, 9, 30))),
    ],
)
def test_tarih_araliklari(bugun, ad, beklenen):
    assert tarih_araliklari(bugun)[ad] == beklenen


def test_sistem_istemi_hazir_araliklari_ve_rolu_icerir():
    istem = sistem_istemi(SABIT_AN, Kullanici("t", "Test Kişi", "operator"))
    assert "Şu an: 2026-09-30 12:00, Çarşamba" in istem
    assert "- geçen ay: 2026-08-01 - 2026-08-31" in istem
    assert "- bugün: 2026-09-30\n" in istem
    assert "Test Kişi (operatör)" in istem and "bakım kılavuzlarını göremez" in istem


def test_maliyet_hesabi():
    assert maliyet_hesapla("gemini", "gemini-3.8-flash", 1_000_000, 1_000_000) == pytest.approx(4.5)
    assert maliyet_hesapla("ollama", "qwen3:4b", 5000, 5000) == 0.0
    assert maliyet_hesapla("gemini", "bilinmeyen-model", 10, 10) is None


@pytest.fixture
def llm_degistir(istemci):
    """/chat'in kullandığı LLM'i test süresince değiştirir."""

    def degistir(llm):
        istemci.app.dependency_overrides[llm_getir] = lambda: llm

    yield degistir
    istemci.app.dependency_overrides.pop(llm_getir, None)


def test_chat_uc_noktasi_cevap_kaynak_ve_kullanim_doner(istemci, llm_degistir):
    llm_degistir(
        SenaryoluLLM(
            arac_iste(("dokuman_ara", {"soru": "hidrolik basınç sensörü manometre"})),
            cevap_ver("Önce basınç sensörünü kontrol edin (PRES-BK-01, s. 4)."),
        )
    )
    oncesi = istemci.get("/kullanim").json()["istek_sayisi"]
    cevap = istemci.post("/chat", json={"soru": "Hidrolik arızada ilk neye bakılır?"})
    assert cevap.status_code == 200
    govde = cevap.json()
    assert govde["cevap"].startswith("Önce basınç")
    assert govde["kaynaklar"] and {"dokuman_kodu", "sayfa"} <= set(govde["kaynaklar"][0])
    assert govde["kullanim"]["girdi_token"] == 250
    assert istemci.get("/kullanim").json()["istek_sayisi"] == oncesi + 1


def test_chat_llm_kota_hatasi_429_ve_kayit(istemci, llm_degistir):
    istek = httpx.Request("POST", "https://ornek.test")
    kota = openai.RateLimitError("kota", response=httpx.Response(429, request=istek), body=None)
    llm_degistir(SenaryoluLLM(kota))
    oncesi = istemci.get("/kullanim").json()["hatali_istek"]
    cevap = istemci.post("/chat", json={"soru": "merhaba"})
    assert cevap.status_code == 429
    assert istemci.get("/kullanim").json()["hatali_istek"] == oncesi + 1


def test_chat_anahtar_yoksa_503(istemci, monkeypatch):
    monkeypatch.setenv("LLM_SAGLAYICI", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    cevap = istemci.post("/chat", json={"soru": "merhaba"})
    assert cevap.status_code == 503
    assert "GEMINI_API_KEY" in cevap.json()["detail"]
