"""Docker açılışında veritabanını hazırlar.

Kullanım (Dockerfile'daki başlangıç komutu):
    python -m scripts.hazirla

- Operasyon tabloları yoksa ya da boşsa seed çalıştırır (6 aylık sahte veri + demo kullanıcılar).
- Kılavuzlar yüklenmemişse ingest çalıştırır (ilk seferde embedding modeli indirilir).
- Veri zaten varsa dokunmaz: konteyner yeniden başladığında agent'ın açtığı talepler ve LLM
  kayıtları silinmez.
"""

import sys
import time
from datetime import datetime

import psycopg
from psycopg import sql

from app import entegrasyon
from app.ayarlar import Ayarlar, ayarlar
from app.db import TZ, baglan
from app.embedding import varsayilan_embedder
from scripts import ingest, seed


def veritabanini_bekle(en_fazla_sn: int = 60) -> None:
    son = time.monotonic() + en_fazla_sn
    while True:
        try:
            with baglan():
                return
        except psycopg.OperationalError:
            if time.monotonic() > son:
                raise
            time.sleep(2)


def _dolu_mu(conn: psycopg.Connection, tablo: str) -> bool:
    if conn.execute("SELECT to_regclass(%s)", [tablo]).fetchone()[0] is None:
        return False
    sorgu = sql.SQL("SELECT EXISTS (SELECT 1 FROM {})").format(sql.Identifier(tablo))
    return conn.execute(sorgu).fetchone()[0]  # secscope: ignore SAST-SQLI-001


def main() -> None:
    veritabanini_bekle()
    with baglan() as conn:
        operasyon_var = _dolu_mu(conn, "hatlar")
        # Üretim ve periyodik bakım tabloları sonradan eklendi: eski bir veritabanında yoksa seed
        # yeniden çalışır.
        eksik = [t for t in ("vardiya_uretimi", "periyodik_bakimlar") if not _dolu_mu(conn, t)]
        seed_gerekli = not operasyon_var or bool(eksik)
        ingest_gerekli = not _dolu_mu(conn, "dokuman_parcalari")

    if seed_gerekli:
        if operasyon_var:
            print(
                f"{', '.join(eksik)} tablosu yok; bütün sahte veri yeniden üretiliyor. Daha önce "
                "açılan bakım talepleri silinir; LLM kayıtları ve Telegram bağlantıları korunur.",
                flush=True,
            )
        else:
            print("Operasyon verisi yok; sahte veri üretiliyor...", flush=True)
        if ayarlar().demo_parola == Ayarlar.model_fields["demo_parola"].default:
            print(
                "UYARI: Demo kullanıcılar varsayılan parolayla oluşturuluyor. Uygulamayı ağa "
                "açmadan önce .env'de DEMO_PAROLA'yı değiştirin.",
                flush=True,
            )
        veri = seed.veri_uret(datetime.now(TZ).replace(second=0, microsecond=0))
        with baglan() as conn:
            seed.veritabanina_yaz(conn, veri)
            seed.demo_kullanicilari_yaz(conn, ayarlar().demo_parola)
    with baglan() as conn:  # sonradan eklenen Telegram, entegrasyon, webhook; veriye dokunmaz
        conn.execute(seed.TELEGRAM_SEMASI.read_text(encoding="utf-8"))
        conn.execute(seed.ENTEGRASYON_SEMASI.read_text(encoding="utf-8"))
        conn.execute(seed.WEBHOOK_SEMASI.read_text(encoding="utf-8"))
        entegrasyon.katalogu_yukle(conn)
        if anahtar := ayarlar().simulator_anahtari:
            entegrasyon.demo_kaynagi_hazirla(conn, anahtar)
            print("Simülatör kaynağı .env'deki SIMULATOR_ANAHTARI ile hazır.", flush=True)
    if ingest_gerekli:
        print(
            "Kılavuzlar yüklenmemiş; PDF'ler işleniyor (ilk seferde model indirilir)...", flush=True
        )
        with baglan() as conn:
            ingest.dokumanlari_yaz(conn, ingest.kilavuzlari_oku(), varsayilan_embedder())
    print("Veritabanı hazır.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except psycopg.OperationalError as hata:
        sys.exit(f"Veritabanına bağlanılamadı: {hata}")
