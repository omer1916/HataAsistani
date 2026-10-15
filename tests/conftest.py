"""Ortak test hazırlıkları.

API testleri gerçek bir PostgreSQL'e karşı çalışır: `andon_test` veritabanı oluşturulur ve
sabit bir anla üretilen sahte veriyle doldurulur. Beklenen sonuçlar aynı veriden Python'da
hesaplanıp API'nin cevabıyla karşılaştırılır.

Veritabanına ulaşılamazsa bu testler atlanır. ANDON_DB_ZORUNLU=1 verilirse atlanmak yerine
hata verir; CI'da testlerin sessizce atlanmaması için.
"""

import math
import os
import re
import zlib
from datetime import datetime

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from app.auth import Kullanici, token_uret
from app.db import BAGLANTI_ZAMAN_ASIMI, TZ
from app.embedding import BOYUT
from app.main import embedder_getir, rapor_llm_getir, uygulama_olustur
from scripts.ingest import dokumanlari_yaz, kilavuzlari_oku
from scripts.seed import demo_kullanicilari_yaz, veri_uret, veritabanina_yaz

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://andon:andon@127.0.0.1:5432/andon_test",  # secscope: ignore SECRET-DBURL-001
)
SABIT_AN = datetime(2026, 9, 30, 12, 0, tzinfo=TZ)
TEST_PAROLA = "test-parolasi"
BAKIM = Kullanici("bakim", "Demo Bakım Mühendisi", "bakim")
OPERATOR = Kullanici("operator", "Demo Operatör", "operator")


def yetki(kullanici: Kullanici) -> dict[str, str]:
    """İsteğe eklenecek Authorization başlığı."""
    return {"Authorization": f"Bearer {token_uret(kullanici)}"}


class SahteEmbedder:
    """Model indirmeden çalışan, kelime eşleşmesine dayalı deterministik embedder.

    Her kelime sabit bir boyuta düşer (kelime torbası). Anlamı değil ortak kelimeleri yakalar;
    arama akışını ve SQL'i test etmek için yeterli. Anlamsal kalite scripts/arama_olc.py
    ile gerçek modelle ölçülür.
    """

    def _vektor(self, metin: str) -> list[float]:
        vektor = [0.0] * BOYUT
        for kelime in re.findall(r"\w+", metin.lower()):
            vektor[zlib.crc32(kelime.encode()) % BOYUT] += 1.0
        uzunluk = math.sqrt(sum(x * x for x in vektor)) or 1.0
        return [x / uzunluk for x in vektor]

    def pasajlari_vektorle(self, metinler: list[str]) -> list[list[float]]:
        return [self._vektor(m) for m in metinler]

    def soruyu_vektorle(self, soru: str) -> list[float]:
        return self._vektor(soru)


def _veritabanini_olustur(url: str) -> None:
    ad = conninfo_to_dict(url)["dbname"]
    yonetim_url = make_conninfo(url, dbname="postgres", connect_timeout=BAGLANTI_ZAMAN_ASIMI)
    with psycopg.connect(yonetim_url, autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", [ad]).fetchone() is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(ad)))


@pytest.fixture(scope="session")
def test_verisi():
    return veri_uret(SABIT_AN)


@pytest.fixture(scope="session")
def test_veritabani(test_verisi):
    try:
        _veritabanini_olustur(TEST_DB_URL)
        with psycopg.connect(TEST_DB_URL) as conn:
            veritabanina_yaz(conn, test_verisi)
            demo_kullanicilari_yaz(conn, TEST_PAROLA)
            dokumanlari_yaz(conn, kilavuzlari_oku(), SahteEmbedder())
    except psycopg.OperationalError as hata:
        if os.environ.get("ANDON_DB_ZORUNLU") == "1":
            raise
        pytest.skip(f"Test veritabanına ulaşılamadı, veritabanı testleri atlandı: {hata}")
    return TEST_DB_URL


@pytest.fixture(scope="session")
def istemci(test_veritabani):
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DATABASE_URL", test_veritabani)
        uygulama = uygulama_olustur(model_on_yukle=False)
        uygulama.dependency_overrides[embedder_getir] = SahteEmbedder
        # .env'de gerçek bir anahtar olsa bile testler LLM'e istek atmasın; rapor LLM'siz de
        # çıkar. LLM'li rapor testleri bunu senaryolu sahte bir LLM'le değiştirir.
        uygulama.dependency_overrides[rapor_llm_getir] = lambda: None
        # Varsayılan olarak bakım mühendisi girişi; rol testleri başlığı istek bazında değiştirir.
        with TestClient(uygulama, headers=yetki(BAKIM)) as istemci:
            yield istemci


@pytest.fixture
def kaynaklar(test_veritabani):
    """Entegrasyon kaynağı üretir: kaynaklar() -> (ad, anahtar). Test bitince kaynaklar ve
    testte açılan arızalar silinir."""
    from app import entegrasyon

    conn = psycopg.connect(test_veritabani, autocommit=True)
    son = conn.execute("SELECT max(id) FROM ariza_kayitlari").fetchone()[0]
    adlar = []

    def uret(katalog="pres-paneli", yetki="olay"):
        ad = f"test-bag-{len(adlar)}-{os.urandom(4).hex()}"
        adlar.append(ad)
        return ad, entegrasyon.kaynak_ekle(conn, ad, "bağlayıcı testi", katalog=katalog,
                                           yetki=yetki)  # fmt: skip

    yield uret
    conn.execute("DELETE FROM entegrasyon_kaynaklari WHERE ad = ANY(%s)", [adlar])
    conn.execute("DELETE FROM ariza_kayitlari WHERE id > %s", [son])
    conn.close()


@pytest.fixture
def kaynak(kaynaklar):
    return kaynaklar()
