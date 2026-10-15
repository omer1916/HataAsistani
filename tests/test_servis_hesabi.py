"""Servis hesabı: "okuma" yetkili kaynağın anahtarıyla (X-API-Anahtari) API'yi bakım ekibi gibi
okur; yazan, LLM'e soran, silen her istek 403. Olay gönderen anahtar kullanıcı uçlarında geçmez,
servis anahtarı olay gönderemez.
"""

import psycopg
import pytest

from scripts.entegrasyon import calistir as cli
from tests.conftest import OPERATOR, yetki
from tests.test_guvenlik_matrisi import UC_NOKTALAR, sahte_llm  # noqa: F401 (fikstür)

TOKENSIZ = {"Authorization": ""}


@pytest.fixture
def servis(kaynaklar):
    return kaynaklar(katalog=None, yetki="okuma")


@pytest.fixture
def olay_anahtari(kaynaklar):
    return kaynaklar()


@pytest.mark.usefixtures("sahte_llm")
@pytest.mark.parametrize("uc_nokta", UC_NOKTALAR)
def test_servis_hesabi_matrisi(istemci, servis, uc_nokta):
    yontem, yol, govde, gecerli = UC_NOKTALAR[uc_nokta]
    cevap = istemci.request(yontem, yol, json=govde,
                            headers={**TOKENSIZ, "X-API-Anahtari": servis[1]})  # fmt: skip
    beklenen = gecerli["bakim"] if yontem == "GET" else 403
    assert cevap.status_code == beklenen, cevap.text[:200]
    if yontem != "GET":
        assert "yalnızca okuyabilir" in cevap.json()["detail"]


@pytest.mark.parametrize("uc_nokta", [u for u, v in UC_NOKTALAR.items() if v[0] == "GET"])
def test_olay_anahtari_kullanici_uclarinda_gecmez(istemci, olay_anahtari, uc_nokta):
    yontem, yol, _, _ = UC_NOKTALAR[uc_nokta]
    cevap = istemci.request(yontem, yol, headers={**TOKENSIZ, "X-API-Anahtari": olay_anahtari[1]})
    assert cevap.status_code == 401 and cevap.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "anahtar",
    ["", "ha_", "ha_yanlis", "x" * 300, "ha_" + "a" * 300, "Bearer ha_x", "ha_\x00"],
)
def test_gecersiz_servis_anahtari(istemci, anahtar):
    try:
        cevap = istemci.get("/hatlar", headers={**TOKENSIZ, "X-API-Anahtari": anahtar})
    except Exception:  # httpx geçersiz başlık değerini (NUL) göndermeyi reddeder
        return
    assert cevap.status_code == 401


def test_kapatilan_servis_hesabi_reddedilir(istemci, servis, test_veritabani):
    ad, anahtar = servis
    baslik = {**TOKENSIZ, "X-API-Anahtari": anahtar}
    assert istemci.get("/arizalar", headers=baslik).status_code == 200
    with psycopg.connect(test_veritabani, autocommit=True) as conn:
        cli(["kapat", ad], conn)
        assert istemci.get("/arizalar", headers=baslik).status_code == 401
        cli(["anahtar-yenile", ad], conn)
        cli(["ac", ad], conn)
    assert istemci.get("/arizalar", headers=baslik).status_code == 401  # eski anahtar geçersiz


def test_token_varsa_anahtara_bakilmaz(istemci, servis):
    """Geçersiz token + geçerli servis anahtarı: 401 (token sessizce yok sayılıp anahtara
    düşülmez). Geçerli operatör token'ı + servis anahtarı: operatör yetkisi."""
    anahtar = {"X-API-Anahtari": servis[1]}
    cevap = istemci.get("/hatlar", headers={"Authorization": "Bearer bozuk.token", **anahtar})
    assert cevap.status_code == 401
    cevap = istemci.get("/bakim-plani", headers={**yetki(OPERATOR), **anahtar})
    assert cevap.status_code == 403


def test_servis_hesabi_olay_gonderemez_uzlasma_ucu_bos(istemci, servis):
    baslik = {"X-API-Anahtari": servis[1]}
    cevap = istemci.post("/entegrasyon/olaylar", headers=baslik, json={"olaylar": [
        {"olay_id": "x", "tur": "alarm_basladi", "makine_kodu": "P3-HP", "alarm_kodu": "A101",
         "zaman": "2026-10-15T08:00:00+03:00"}]})  # fmt: skip
    assert cevap.status_code == 403 and "okuma" in cevap.json()["detail"]
    assert istemci.get("/entegrasyon/aktif-alarmlar", headers=baslik).json() == []


def test_durum_ve_komut_satiri_servis_hesabini_gosterir(istemci, servis, test_veritabani):
    ad, _ = servis
    kaynak = next(k for k in istemci.get("/entegrasyon/durum").json()["kaynaklar"]
                  if k["ad"] == ad)  # fmt: skip
    assert kaynak["yetki"] == "okuma"
    with psycopg.connect(test_veritabani, autocommit=True) as conn:
        assert "servis hesabı (yalnızca okuma)" in next(
            s for s in cli(["kaynaklar"], conn).splitlines() if s.startswith(ad)
        )
        cikti = cli(["kaynak-ekle", "test-servis-cli", "ERP panosu", "--yalnizca-okuma"], conn)
        try:
            assert cikti.startswith("Servis hesabı eklendi") and "yalnızca GET" in cikti
            anahtar = cikti.splitlines()[-1]
        finally:
            conn.execute("DELETE FROM entegrasyon_kaynaklari WHERE ad = 'test-servis-cli'")
    assert anahtar.startswith("ha_")


def test_openapi_iki_anahtar_semasini_ayirir(istemci):
    semalar = istemci.get("/openapi.json").json()["components"]["securitySchemes"]
    assert semalar["ServisAnahtari"]["name"] == "X-API-Anahtari"
    assert semalar["APIKeyHeader"]["name"] == "X-API-Anahtari"
