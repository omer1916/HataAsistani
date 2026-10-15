"""Hata Asistanı Telegram botunu çalıştırır (uzun yoklama; dışarıya açık bir adres gerekmez).

Kullanım:
    python scripts/telegram_bot.py
    docker compose --profile telegram up -d   # Docker'da, API'nin yanında

Ön koşul: .env'de TELEGRAM_BOT_TOKEN (Telegram'da @BotFather'dan). Veritabanı hazır olmalı
(seed + ingest; Docker'da API servisi açılışta hazırlar).

İki iş parçacığı:
- Mesajlar: Telegram'dan uzun yoklamayla yeni mesaj beklenir (gelen mesaj hemen döner), işlenir.
  Saatte bir saklama süresi dolan veriler silinir.
- Arıza bildirimi: veritabanı, arıza eklendiğinde ya da hattı durdurduğu kaydedildiğinde sinyal
  gönderir (sql/telegram.sql'deki tetikleyici, LISTEN/NOTIFY). Bot sinyali bekler ve arızayı
  anında bildirir; mesaj işlemesini beklemez. Sinyal kaçarsa YEDEK_ARALIK saniyede bir yapılan
  kontrol yakalar. Her iş parçacığının kendi veritabanı bağlantısı vardır.
Her sabah (TELEGRAM_BAKIM_OZETI_SAATI, varsayılan 07:30) bakım rolüne geciken ve o günün
periyodik bakımları gider; gün başına bir kez.
Günlüğe mesaj içeriği ve token yazılmaz.
"""

import os
import signal
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Callable
from datetime import time as dt_time
from pathlib import Path

import psycopg
from psycopg import sql

from app.ayarlar import ayarlar
from app.db import baglan
from app.embedding import varsayilan_embedder
from app.llm import LLMAyarHatasi, llm_olustur
from app.telegram_api import TelegramAPI, TelegramHatasi
from app.telegram_bot import Bot

ZAMAN_ASIMI = 30  # saniye; Telegram'dan mesaj beklenen en uzun süre (mesaj gelince hemen döner)
YEDEK_ARALIK = 15  # saniye; tetikleyicinin sinyali kaçarsa arıza en geç bu kadar sonra bildirilir
BILDIRIM_KANALI = "ariza_kayitlari"  # sql/telegram.sql'deki tetikleyicinin kanalı
# Ana döngü her turda dokunur; docker-compose.yml'deki sağlık denetimi dosyanın yaşına bakar.
NABIZ_DOSYASI = Path(tempfile.gettempdir()) / "hata-asistani-bot.nabiz"
TEMIZLIK_ARALIGI = 3600  # saniye


def llm_getir():
    try:
        return llm_olustur(ayarlar())
    except LLMAyarHatasi:
        return None


def gunluk(mesaj: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {mesaj}", flush=True)


def hatayi_yaz(ne: str, hata: Exception) -> None:
    """Hata mesajı kullanıcının yazdığını içerebilir (ör. veritabanı hatası); günlüğe yalnızca
    hata türü ve kodun neresinde olduğu yazılır."""
    yer = traceback.extract_tb(hata.__traceback__)[-1]
    gunluk(f"{ne}: {type(hata).__name__} ({os.path.basename(yer.filename)}:{yer.lineno})")


def tur(api: TelegramAPI, bot: Bot, offset: int | None) -> int | None:
    """Bir döngü turu: gelen mesajları işler, yeni offset'i döner.

    Yalnızca getUpdates'in hatası dışarı çıkar. İşlenen bir mesajdan sonra çıkan hata yeni
    offset'i kaybettirseydi aynı mesajlar tekrar işlenir, talepler iki kez açılırdı. Veritabanı
    bağlantısı koptuysa ana döngü bunu `conn.closed`'dan anlar."""
    for guncelleme in api.guncellemeler(offset, ZAMAN_ASIMI):
        offset = guncelleme["update_id"] + 1  # işlenemese bile tekrar alınmasın
        try:
            bot.guncellemeyi_isle(guncelleme)
        except Exception as hata:  # tek bir mesaj botu düşürmesin
            hatayi_yaz(f"mesaj {guncelleme['update_id']} işlenemedi", hata)
            bot.islenemedi(guncelleme)
    return offset


def bildirim_turu(bot: Bot) -> None:
    """Yeni arızaları bildirir. Hata dışarı çıkmaz: bağlantı koptuysa bildirim döngüsü bunu
    `conn.closed`'dan anlayıp yeniden bağlanır."""
    try:
        gonderilen = bot.bildirimleri_gonder()
    except Exception as hata:
        hatayi_yaz("bildirimler gönderilemedi", hata)
    else:
        if gonderilen:
            gunluk(f"{gonderilen} arıza bildirimi gönderildi")


def sabah_ozeti_turu(bot: Bot, saat: dt_time) -> None:
    """Sabah özetinin saati geldiyse gönderir. Veritabanı kopması ana döngüye çıkar (yeniden
    bağlanır); başka bir hata botu düşürmez."""
    try:
        gonderilen = bot.sabah_ozetini_gonder(saat)
    except psycopg.OperationalError:
        raise
    except Exception as hata:
        hatayi_yaz("periyodik bakım özeti gönderilemedi", hata)
    else:
        if gonderilen:
            gunluk(f"periyodik bakım özeti {gonderilen} kişiye gönderildi")


def _yeni_baglanti(baglanti_ac: Callable[[], psycopg.Connection]) -> psycopg.Connection:
    conn = baglanti_ac()
    conn.autocommit = True
    return conn


def bildirim_dongusu(
    bot: Bot,
    baglanti_ac: Callable[[], psycopg.Connection] = baglan,
    dur: threading.Event | None = None,
    yedek_aralik: float = YEDEK_ARALIK,
) -> None:
    """Arıza bildirimi iş parçacığı. Tetikleyicinin sinyalini ayrı bir bağlantıda bekler; sinyal
    gelince (ya da yedek_aralik dolunca) yeni arızaları bildirir. `bot`un bağlantısını yalnızca
    bu iş parçacığı kullanır. Veritabanı koparsa 5 sn arayla yeniden bağlanır."""
    dur = dur or threading.Event()
    dinleyici = None
    while not dur.is_set():
        try:
            if dinleyici is None or dinleyici.closed:
                dinleyici = _yeni_baglanti(baglanti_ac)
                dinleyici.execute(sql.SQL("LISTEN {}").format(sql.Identifier(BILDIRIM_KANALI)))
            if bot.conn.closed:
                bot.conn = _yeni_baglanti(baglanti_ac)
            # Önce kontrol, sonra bekleme: kontrol sırasında gelen sinyal kaybolmaz, bağlantıda
            # bekler ve notifies() hemen döner.
            bildirim_turu(bot)
            for _ in dinleyici.notifies(timeout=yedek_aralik, stop_after=1):
                pass
        except Exception as hata:  # iş parçacığı ölürse bildirimler sessizce dururdu
            if isinstance(hata, psycopg.OperationalError):
                gunluk("bildirim: veritabanına ulaşılamıyor; 5 sn sonra tekrar denenecek")
            else:
                hatayi_yaz("bildirim döngüsü", hata)
            for c in (dinleyici, bot.conn):
                if c is not None and not c.closed:
                    c.close()
            dur.wait(5)
    if dinleyici is not None:
        dinleyici.close()


def main() -> None:
    # Docker'da bot 1 numaralı süreçtir: işleyici olmadan SIGTERM yok sayılır ve
    # `docker compose stop` 10 sn bekleyip süreci öldürür.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    a = ayarlar()
    if not a.telegram_bot_token:
        sys.exit(
            "TELEGRAM_BOT_TOKEN tanımlı değil. Telegram'da @BotFather'a /newbot yazıp aldığın "
            "token'ı .env dosyasına ekle (bkz. README, Telegram botu)."
        )
    api = TelegramAPI(a.telegram_bot_token)
    try:
        ben = api.ben()
    except TelegramHatasi as hata:
        sys.exit(f"Telegram'a bağlanılamadı ya da token geçersiz: {hata}")
    gunluk(f"@{ben['username']} çalışıyor (bildirim: {a.telegram_bildirim_ayrintisi})")

    try:
        conn, bildirim_conn = _yeni_baglanti(baglan), _yeni_baglanti(baglan)
    except psycopg.OperationalError as hata:
        sys.exit(f"Veritabanına bağlanılamadı. 'docker compose up -d' çalıştı mı?\n{hata}")
    embedder = varsayilan_embedder()
    secenekler = {
        "bildirim_ayrintisi": a.telegram_bildirim_ayrintisi,
        "fotograf_saklama_gun": a.telegram_fotograf_saklama_gun,
    }
    bot = Bot(api, conn, embedder, llm_getir, **secenekler)
    # Bildirimler kendi Bot nesnesi ve bağlantısıyla ayrı iş parçacığında: uzun bir LLM cevabı
    # beklenirken de arıza anında gider. Bildirim durumu (son arıza) yalnızca orada tutulur.
    bildirici = Bot(api, bildirim_conn, embedder, llm_getir, **secenekler)
    threading.Thread(
        target=bildirim_dongusu, args=(bildirici,), name="ariza-bildirimi", daemon=True
    ).start()
    offset, son_temizlik = None, 0.0
    while True:
        try:
            if bot.conn.closed:
                # Veritabanı dönene kadar mesaj alınmaz; alınsa hepsi hata verip kaybolurdu.
                bot.conn = baglan()
                bot.conn.autocommit = True
                gunluk("veritabanına yeniden bağlanıldı")
            offset = tur(api, bot, offset)
            NABIZ_DOSYASI.touch()  # Docker sağlık denetimi: döngü dönüyor
            sabah_ozeti_turu(bot, a.telegram_bakim_ozeti_saati)
            if time.monotonic() - son_temizlik > TEMIZLIK_ARALIGI:
                kod, foto = bot.eski_verileri_temizle()
                if kod or foto:
                    gunluk(f"temizlik: {kod} eski kod, {foto} eski fotoğraf silindi")
                son_temizlik = time.monotonic()
        except TelegramHatasi as hata:
            gunluk(f"Telegram hatası: {hata}; 5 sn sonra tekrar")
            time.sleep(5)
        except psycopg.OperationalError:
            # Yeniden bağlanma da başarısız olabilir; süreç çökmeden 5 sn sonra tekrar denenir.
            gunluk("veritabanına ulaşılamıyor; 5 sn sonra tekrar denenecek")
            if not bot.conn.closed:
                bot.conn.close()
            time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
