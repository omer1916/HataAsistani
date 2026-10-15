"""Entegrasyon yönetimi: kaynak (PLC, CNC, MES) ekler, anahtar verir, katalog düzenler; servis
hesabı (yalnızca okuyan ERP, MES panosu) açar; dış sistemlere giden webhook'ları tanımlar.

    python -m scripts.entegrasyon kaynak-ekle cnc-01 "Fanuc 0i torna" --katalog fanuc
    python -m scripts.entegrasyon kaynak-ekle erp-okuma "ERP panosu" --yalnizca-okuma
    python -m scripts.entegrasyon kaynaklar
    python -m scripts.entegrasyon anahtar-yenile cnc-01
    python -m scripts.entegrasyon kapat cnc-01        # anahtarı geçersiz kılar (aç: ac)
    python -m scripts.entegrasyon katalog-ekle fanuc "SV*" "Servo alarmı" elektrik yuksek
    python -m scripts.entegrasyon katalog [fanuc]

    python -m scripts.entegrasyon webhook-ekle erp https://erp.local/hata-asistani
    python -m scripts.entegrasyon webhook-ekle bakim-kanali https://... --bicim teams \
        --olay ariza_acildi --olay hat_durdu
    python -m scripts.entegrasyon webhooklar
    python -m scripts.entegrasyon webhook-dene erp    # deneme bildirimi
    python -m scripts.entegrasyon webhook-kapat erp   # (aç: webhook-ac, sil: webhook-sil)

Anahtar yalnızca bir kez, oluşturulurken gösterilir; veritabanında özeti saklanır. Kaybolursa
anahtar-yenile ile yenisi alınır (eskisi hemen geçersiz olur). Webhook'un imza anahtarı da
yalnızca eklenirken gösterilir.
"""

import argparse
import sys

import psycopg

from app import entegrasyon, webhook
from app.db import baglan
from app.models import ArizaTipi, Onem

TIPLER = list(ArizaTipi.__args__)
ONEMLER = list(Onem.__args__)


def _ayrac() -> argparse.ArgumentParser:
    ayrac = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    alt = ayrac.add_subparsers(dest="komut", required=True)

    k = alt.add_parser("kaynak-ekle", help="yeni kaynak ve anahtarı")
    k.add_argument("ad", help="küçük harf, rakam, - ve _ (ör. cnc-01)")
    k.add_argument("aciklama")
    k.add_argument("--katalog", help="alarm kodlarının anlamı (ör. pres-paneli)")
    k.add_argument("--varsayilan-tip", choices=TIPLER, default="mekanik")
    k.add_argument("--varsayilan-onem", choices=ONEMLER, default="orta")
    k.add_argument(
        "--hat-durdurmaz", action="store_true", help="eşlenmemiş alarm hattı durdurmuş sayılmasın"
    )
    k.add_argument("--yalnizca-okuma", action="store_true",
                   help="servis hesabı: API'yi okur (GET), olay gönderemez")  # fmt: skip

    alt.add_parser("kaynaklar", help="kaynakları listeler (anahtarsız)")
    for komut, yardim in [("anahtar-yenile", "yeni anahtar"), ("kapat", "kaynağı kapatır"),
                          ("ac", "kaynağı açar")]:  # fmt: skip
        alt.add_parser(komut, help=yardim).add_argument("ad")

    c = alt.add_parser("katalog-ekle", help="alarm kodu ya da önek ('SV*') ekler/günceller")
    c.add_argument("katalog")
    c.add_argument("desen")
    c.add_argument("anlam")
    c.add_argument("tip", choices=TIPLER)
    c.add_argument("onem", choices=ONEMLER)
    c.add_argument("--hat-durdurmaz", action="store_true")
    c.add_argument("--dokuman", help="kılavuz kodu (ör. PRES-BK-01)")
    c.add_argument("--sayfa", type=int)

    alt.add_parser("katalog", help="katalog satırları").add_argument("katalog", nargs="?")

    w = alt.add_parser("webhook-ekle", help="dış sisteme bildirim (ERP, MES, Teams, Slack)")
    w.add_argument("ad", help="küçük harf, rakam, - ve _ (ör. erp)")
    w.add_argument("adres", help="https://...")
    w.add_argument(
        "--olay",
        action="append",
        choices=webhook.OLAYLAR,
        help="yalnızca bu olaylar (birden çok verilebilir; verilmezse hepsi)",
    )
    w.add_argument(
        "--bicim",
        choices=webhook.BICIMLER,
        default="json",
        help="json: imzalı olay (ERP, MES); teams, slack: kanala mesaj",
    )
    alt.add_parser("webhooklar", help="webhook'lar ve gönderim durumu")
    for komut, yardim in [
        ("webhook-dene", "deneme bildirimi"),
        ("webhook-kapat", "gönderimi durdurur (yeni bildirimler bekler)"),
        ("webhook-ac", "gönderimi sürdürür"),
        ("webhook-sil", "webhook'u ve bekleyen bildirimlerini siler"),
    ]:
        alt.add_parser(komut, help=yardim).add_argument("ad")
    return ayrac  # fmt: skip


def _kaynak_satiri(k: dict) -> str:
    son = f"{k['son_olay']:%d.%m.%Y %H:%M}" if k["son_olay"] else "yok"
    if k["yetki"] == "okuma":
        return (
            f"{k['ad']:<20} {'açık' if k['aktif'] else 'KAPALI':<7} servis hesabı (yalnızca okuma)"
        )
    return (
        f"{k['ad']:<20} {'açık' if k['aktif'] else 'KAPALI':<7} katalog={k['katalog'] or '-':<12} "
        f"süren alarm={k['aktif_alarm']:<3} son olay={son}"
    )


def _webhook_satiri(w: dict) -> str:
    son = f"{w['son_gonderim']:%d.%m.%Y %H:%M}" if w["son_gonderim"] else "yok"
    bosluk = " " * 17
    satir = (
        f"{w['ad']:<16} {'açık' if w['aktif'] else 'KAPALI':<7} {w['bicim']:<6} {w['adres']}\n"
        f"{bosluk}olaylar: {', '.join(w['olaylar'])}\n"
        f"{bosluk}gönderilen={w['gonderilen']} bekleyen={w['bekleyen']} "
        f"vazgeçilen={w['vazgecilen']} son gönderim={son}"
    )
    if w["son_hata"]:
        satir += f"\n{bosluk}son hata: {w['son_hata'][:120]}"
    return satir


def _webhook_komutu(a: argparse.Namespace, conn: psycopg.Connection) -> str:
    if a.komut == "webhook-ekle":
        try:
            gizli = webhook.webhook_ekle(conn, a.ad, a.adres, a.olay, a.bicim)
        except webhook.WebhookHatasi as hata:
            raise SystemExit(str(hata)) from None
        metin = f"Webhook eklendi: {a.ad} -> {a.adres}"
        if a.bicim == "json":
            metin += (
                "\nİmza anahtarı (bir daha gösterilmez; alıcı X-Hata-Asistani-Imza başlığını "
                f"bununla doğrular):\n{gizli}"
            )
        return metin + f"\nDenemek için: python -m scripts.entegrasyon webhook-dene {a.ad}"
    if a.komut == "webhooklar":
        satirlar = webhook.webhooklar(conn)
        if not satirlar:
            return "Webhook yok. Eklemek için: webhook-ekle AD ADRES"
        return "\n".join(map(_webhook_satiri, satirlar))
    if a.komut == "webhook-dene":
        satir = conn.execute("SELECT aktif FROM webhooklar WHERE ad = %s", [a.ad]).fetchone()
        if satir is None:
            raise SystemExit(f"'{a.ad}' adında webhook yok.")
        if not satir[0]:
            raise SystemExit(f"{a.ad} kapalı; önce: webhook-ac {a.ad}")
        webhook.deneme_ekle(conn, a.ad)
        return (
            "Deneme bildirimi sıraya kondu. Sonucu birkaç saniye sonra 'webhooklar' gösterir "
            "(webhook göndericisi çalışıyor olmalı)."
        )
    if a.komut == "webhook-sil":
        sonuc = conn.execute("DELETE FROM webhooklar WHERE ad = %s", [a.ad])
    else:
        sonuc = conn.execute(
            "UPDATE webhooklar SET aktif = %s WHERE ad = %s", [a.komut == "webhook-ac", a.ad]
        )
    if not sonuc.rowcount:
        raise SystemExit(f"'{a.ad}' adında webhook yok.")
    return {
        "webhook-sil": f"{a.ad} silindi.",
        "webhook-ac": f"{a.ad} açıldı; bekleyen bildirimler gönderilecek.",
        "webhook-kapat": f"{a.ad} kapatıldı; yeni bildirimler açılınca gönderilmek üzere bekler.",
    }[a.komut]


def calistir(argumanlar: list[str], conn: psycopg.Connection) -> str:
    """Komutu çalıştırır, ekrana yazılacak metni döner. Hata: SystemExit (mesajla)."""
    a = _ayrac().parse_args(argumanlar)
    if a.komut == "kaynak-ekle":
        try:
            anahtar = entegrasyon.kaynak_ekle(
                conn, a.ad, a.aciklama, katalog=a.katalog, varsayilan_tip=a.varsayilan_tip,
                varsayilan_onem=a.varsayilan_onem, varsayilan_hat_durdurur=not a.hat_durdurmaz,
                yetki="okuma" if a.yalnizca_okuma else "olay",
            )  # fmt: skip
        except psycopg.errors.UniqueViolation:
            raise SystemExit(f"'{a.ad}' adında bir kaynak zaten var.") from None
        except psycopg.errors.CheckViolation:
            raise SystemExit("Ad küçük harf, rakam, - ve _ olmalı (2-40 karakter).") from None
        if a.yalnizca_okuma:
            return (
                f"Servis hesabı eklendi: {a.ad}\nAnahtar (bir daha gösterilmez; isteklerde "
                f"X-API-Anahtari başlığıyla, yalnızca GET):\n{anahtar}"
            )
        return (
            f"Kaynak eklendi: {a.ad}\nAnahtar (bir daha gösterilmez, bağlayıcıya verin):\n{anahtar}"
        )
    if a.komut == "anahtar-yenile":
        anahtar = entegrasyon.anahtar_yenile(conn, a.ad)
        if anahtar is None:
            raise SystemExit(f"'{a.ad}' adında kaynak yok.")
        return f"Yeni anahtar (eskisi artık geçersiz):\n{anahtar}"
    if a.komut in ("kapat", "ac"):
        sonuc = conn.execute(
            "UPDATE entegrasyon_kaynaklari SET aktif = %s WHERE ad = %s", [a.komut == "ac", a.ad]
        )
        if not sonuc.rowcount:
            raise SystemExit(f"'{a.ad}' adında kaynak yok.")
        return f"{a.ad} {'açıldı' if a.komut == 'ac' else 'kapatıldı; anahtarı reddedilecek'}."
    if a.komut == "kaynaklar":
        satirlar = entegrasyon.durum(conn, olay_sayisi=0)["kaynaklar"]
        if not satirlar:
            return "Kaynak yok. Eklemek için: kaynak-ekle AD AÇIKLAMA"
        return "\n".join(map(_kaynak_satiri, satirlar))
    if a.komut.startswith("webhook"):
        return _webhook_komutu(a, conn)
    if a.komut == "katalog-ekle":
        if (a.dokuman is None) != (a.sayfa is None):
            raise SystemExit("--dokuman ve --sayfa birlikte verilmeli.")
        try:
            conn.execute(
                "INSERT INTO alarm_katalogu (katalog, desen, anlam, ariza_tipi, onem, "
                "hat_durdurur, dokuman_kodu, sayfa) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (katalog, desen) DO UPDATE SET anlam = excluded.anlam, "
                "ariza_tipi = excluded.ariza_tipi, onem = excluded.onem, "
                "hat_durdurur = excluded.hat_durdurur, dokuman_kodu = excluded.dokuman_kodu, "
                "sayfa = excluded.sayfa",
                [a.katalog, a.desen, a.anlam, a.tip, a.onem, not a.hat_durdurmaz, a.dokuman,
                 a.sayfa],
            )  # fmt: skip
        except psycopg.errors.CheckViolation:
            raise SystemExit(
                "Desen harf, rakam, . _ - olmalı (en fazla 64), önek için sonda tek '*'."
            ) from None
        return f"{a.katalog}: {a.desen} = {a.anlam} ({a.tip}, {a.onem})"
    satirlar = conn.execute(
        "SELECT katalog, desen, anlam, ariza_tipi, onem, hat_durdurur, dokuman_kodu, sayfa "
        "FROM alarm_katalogu WHERE %(k)s::text IS NULL OR katalog = %(k)s "
        "ORDER BY katalog, desen",
        {"k": a.katalog},
    ).fetchall()
    return (
        "\n".join(
            f"{k:<12} {d:<8} {anlam:<34} {tip:<9} {onem:<6} "
            f"{'durdurur' if hd else '-':<9} {f'{dok} s. {s}' if dok else ''}"
            for k, d, anlam, tip, onem, hd, dok, s in satirlar
        )
        or "Katalog boş."
    )


def main() -> None:
    try:
        with baglan() as conn:
            conn.autocommit = True
            print(calistir(sys.argv[1:], conn))
    except psycopg.OperationalError as hata:
        sys.exit(f"Veritabanına bağlanılamadı. 'docker compose up -d' çalıştı mı?\n{hata}")


if __name__ == "__main__":
    main()
