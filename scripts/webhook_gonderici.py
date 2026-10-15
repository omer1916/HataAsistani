"""Webhook göndericisi: giden kutusundaki bildirimleri dış sistemlere gönderir.

    python -m scripts.webhook_gonderici

Tetikleyicinin sinyalini (LISTEN giden_bildirimler) bekler; sinyal kaçsa da (bağlantı koptu)
en geç YEDEK_ARALIK'ta bir kendisi bakar. Veritabanı ya da alıcı gitse de süreç ölmez. Webhook
yoksa boş bekler; webhook eklemek için: python -m scripts.entegrasyon webhook-ekle (README).
"""

import signal
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import psycopg
from psycopg import sql

from app import webhook
from app.db import baglan

YEDEK_ARALIK = 30.0
NABIZ_DOSYASI = Path(tempfile.gettempdir()) / "hata-asistani-webhook.nabiz"


def gunluk(metin: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} webhook: {metin}", flush=True)


def _yeni_baglanti(baglanti_ac: Callable[[], psycopg.Connection]) -> psycopg.Connection:
    conn = baglanti_ac()
    conn.autocommit = True
    return conn


def dongu(
    baglanti_ac: Callable[[], psycopg.Connection] = baglan,
    dur: threading.Event | None = None,
    http: httpx.Client | None = None,
    yedek_aralik: float = YEDEK_ARALIK,
    nabiz: Path | None = None,
) -> None:
    """`dur` gelene kadar gönderir. Önce gönderir sonra bekler: gönderim sırasında gelen sinyal
    bağlantıda bekler, notifies() hemen döner (kaybolmaz)."""
    dur = dur or threading.Event()
    http = http or httpx.Client(timeout=webhook.ZAMAN_ASIMI, follow_redirects=False)
    conn = dinleyici = None
    while not dur.is_set():
        try:
            if dinleyici is None or dinleyici.closed:
                dinleyici = _yeni_baglanti(baglanti_ac)
                dinleyici.execute(sql.SQL("LISTEN {}").format(sql.Identifier(webhook.KANAL)))
            if conn is None or conn.closed:
                conn = _yeni_baglanti(baglanti_ac)
            while not dur.is_set() and (gonderilen := webhook.bir_tur(conn, http)):
                gunluk(f"{gonderilen} bildirim denendi")
            if nabiz:
                nabiz.touch()
            # Sırada bekleyen ama başka göndericinin kilitlediği satır varsa boşa dönmesin.
            bekle = max(0.2, webhook.sonraki_bekleme(conn, yedek_aralik))
            for _ in dinleyici.notifies(timeout=bekle, stop_after=1):
                pass
        except Exception as hata:  # gönderici ölürse bildirimler sessizce dururdu
            if isinstance(hata, psycopg.OperationalError):
                gunluk("veritabanına ulaşılamıyor; 5 sn sonra tekrar denenecek")
            else:
                gunluk(f"beklenmeyen hata ({type(hata).__name__}: {str(hata)[:200]})")
            for c in (dinleyici, conn):
                if c is not None and not c.closed:
                    c.close()
            dur.wait(5)
    for c in (dinleyici, conn):
        if c is not None and not c.closed:
            c.close()


def main() -> None:
    # Docker'da 1 numaralı süreç: işleyici olmadan SIGTERM yok sayılır.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    gunluk("çalışıyor")
    try:
        dongu(nabiz=NABIZ_DOSYASI)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
