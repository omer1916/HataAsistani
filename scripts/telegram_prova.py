"""Telegram botunun uçtan uca provası, token olmadan.

Botun gerçek kodu, Telegram Bot API'sini taklit eden yerel bir HTTP sunucusuna karşı çalışır:
eşleştirme, soru, fotoğraflı talep, komutlar ve arıza bildirimi. Gerçek Telegram'a istek gitmez;
veritabanı, embedding modeli ve LLM gerçektir (LLM ayarlıysa birkaç kuruşluk istek atılır).

Kullanım (veritabanı hazır olmalı: docker compose up -d):
    python -m scripts.telegram_prova

Prova sırasında açılan bakım talebi veritabanında kalır; eklenen arıza ve sahte eşleştirmeler
silinir, bakim/operator hesaplarının gerçek Telegram bağlantıları geri yüklenir (prova yarıda
kesilse bile). Çalışan bir bot varsa prova arızası için o da bildirim dener; sahte sohbet
numaralarına gidemez, kimseye mesaj düşmez.
"""

import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from psycopg import sql

from app import telegram_api
from app.db import TZ, baglan
from app.embedding import varsayilan_embedder
from app.telegram_bot import Bot, kod_uret
from scripts.telegram_bot import BILDIRIM_KANALI, bildirim_dongusu, llm_getir, tur

ADRES = ("127.0.0.1", 8765)
BAKIM, OPERATOR = 111, 222  # sahte sohbet numaraları
FOTOGRAF = b"\xff\xd8\xff\xe0" + b"prova" * 300

bekleyen: list[dict] = []
giden: list[tuple[int, str]] = []


class SahteTelegram(BaseHTTPRequestHandler):
    """Bot API'nin botun kullandığı kısmı: getMe, getUpdates, sendMessage, getFile, dosya."""

    def log_message(self, *_):
        pass

    def _sonuc(self, sonuc) -> None:
        govde = json.dumps({"ok": True, "result": sonuc}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(govde)

    def do_POST(self):
        veri = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        yontem = self.path.rsplit("/", 1)[-1]
        if yontem == "getMe":
            return self._sonuc({"username": "andon_prova_bot"})
        if yontem == "getUpdates":
            offset = veri.get("offset") or 0
            return self._sonuc([g for g in bekleyen if g["update_id"] >= offset])
        if yontem == "sendMessage":
            giden.append((veri["chat_id"], veri["text"]))
            return self._sonuc({"message_id": len(giden)})
        if yontem == "sendChatAction":
            return self._sonuc(True)
        if yontem == "getFile":
            return self._sonuc({"file_path": "photos/1.jpg", "file_size": len(FOTOGRAF)})
        self.send_response(404)
        self.end_headers()

    def do_GET(self):  # dosya indirme
        self.send_response(200)
        self.end_headers()
        self.wfile.write(FOTOGRAF)


def mesaj(chat_id: int, metin: str | None = None, aciklama: str | None = None) -> None:
    no = len(bekleyen) + 1
    m: dict = {"message_id": no, "chat": {"id": chat_id, "type": "private"}}
    if aciklama is None:
        m["text"] = metin
    else:
        m["photo"] = [{"file_id": "kucuk", "file_size": 100},
                      {"file_id": "buyuk", "file_size": len(FOTOGRAF)}]  # fmt: skip
        m["caption"] = aciklama
    bekleyen.append({"update_id": no, "message": m})


def main() -> None:
    sunucu = ThreadingHTTPServer(ADRES, SahteTelegram)
    threading.Thread(target=sunucu.serve_forever, daemon=True).start()
    telegram_api.API_ADRESI = f"http://{ADRES[0]}:{ADRES[1]}"
    api = telegram_api.TelegramAPI("000000:PROVA")

    conn = baglan()
    conn.autocommit = True
    # Prova bakim/operator hesaplarını sahte sohbetlere bağlar; bu, hesapların gerçek
    # bağlantısını siler (bir hesap tek sohbete bağlanır). Sonda geri yüklenir.
    gercek_baglantilar = conn.execute(
        "SELECT kullanici_adi, chat_id, baglanma FROM telegram_baglantilari "
        "WHERE kullanici_adi IN ('bakim', 'operator')"
    ).fetchall()
    embedder = varsayilan_embedder()
    bot = Bot(api, conn, embedder, llm_getir)
    bildirim_conn = baglan()
    bildirim_conn.autocommit = True
    bildirici = Bot(api, bildirim_conn, embedder, llm_getir)
    dur = threading.Event()
    bildirim = threading.Thread(
        target=bildirim_dongusu, args=(bildirici,), kwargs={"dur": dur}, daemon=True
    )
    bildirim.start()
    ariza_id, gecikme = None, None
    try:
        simdi = datetime.now(TZ)
        mesaj(BAKIM, "Merhaba")  # henüz eşleşmemiş
        mesaj(BAKIM, f"/baglan {kod_uret(conn, 'bakim', simdi)}")
        mesaj(OPERATOR, f"/start {kod_uret(conn, 'operator', simdi)}")
        mesaj(BAKIM, "Pres 3 hattında son 7 günde kaç arıza oldu, en çok hangi tip?")
        mesaj(OPERATOR, aciklama="P3-HP'nin altında yağ birikintisi var, kaçak olabilir")
        mesaj(BAKIM, "/oee Pres 3")
        mesaj(BAKIM, "/plan 12")
        mesaj(OPERATOR, "/plan")
        mesaj(BAKIM, "/rapor")
        tur(api, bot, None)

        # MES'in yazacağı kayıt: hattı durduran yeni bir arıza. Bildirim iş parçacığı
        # tetikleyicinin sinyaliyle uyanır; iki alıcıya (bakım, operatör) gidene kadar beklenir.
        while bildirici.son_ariza_id is None:  # başlangıç noktası belirlenmeden ekleme
            time.sleep(0.05)
        onceki = len(giden)
        baslangic = time.monotonic()
        (ariza_id,) = conn.execute(
            "INSERT INTO ariza_kayitlari (makine_id, baslangic, ariza_tipi, onem, hat_durdu, "
            "aciklama) SELECT id, now(), 'hidrolik', 'yuksek', true, "
            "'Hidrolik basınç set değerinin altına düştü' FROM makineler WHERE kod = 'P3-HP' "
            "RETURNING id"
        ).fetchone()
        while len(giden) < onceki + 2 and time.monotonic() - baslangic < 30:
            time.sleep(0.01)
        if len(giden) >= onceki + 2:
            gecikme = time.monotonic() - baslangic
    finally:
        dur.set()
        # Bekleyen bildirim döngüsü uyanıp çıksın.
        conn.execute(sql.SQL("NOTIFY {}").format(sql.Identifier(BILDIRIM_KANALI)))
        bildirim.join(5)
        if ariza_id is not None:
            conn.execute("DELETE FROM ariza_kayitlari WHERE id = %s", [ariza_id])
        conn.execute(
            "DELETE FROM telegram_baglantilari WHERE chat_id IN (%s, %s)", [BAKIM, OPERATOR]
        )
        if gercek_baglantilar:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO telegram_baglantilari (kullanici_adi, chat_id, baglanma) "
                    "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    gercek_baglantilar,
                )
        sunucu.shutdown()

    for chat_id, metin in giden:
        kim = "BAKIM" if chat_id == BAKIM else "OPERATÖR"
        print(f"\n===== bot -> {kim} =====\n{metin}")
    if gecikme is None:
        print("\nUYARI: arıza bildirimi 30 sn içinde gelmedi.")
    else:
        print(f"\nArıza bildirimi kayıttan {gecikme * 1000:.0f} ms sonra iki alıcıya gönderildi.")


if __name__ == "__main__":
    main()
