"""Makine entegrasyonu yönetimi: kaynak (PLC, CNC, MES) ekler, anahtar verir, katalog düzenler.

    python -m scripts.entegrasyon kaynak-ekle cnc-01 "Fanuc 0i torna" --katalog fanuc
    python -m scripts.entegrasyon kaynaklar
    python -m scripts.entegrasyon anahtar-yenile cnc-01
    python -m scripts.entegrasyon kapat cnc-01        # anahtarı geçersiz kılar (aç: ac)
    python -m scripts.entegrasyon katalog-ekle fanuc "SV*" "Servo alarmı" elektrik yuksek
    python -m scripts.entegrasyon katalog [fanuc]

Anahtar yalnızca bir kez, oluşturulurken gösterilir; veritabanında özeti saklanır. Kaybolursa
anahtar-yenile ile yenisi alınır (eskisi hemen geçersiz olur).
"""

import argparse
import sys

import psycopg

from app import entegrasyon
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
    k.add_argument("--hat-durdurmaz", action="store_true",
                   help="eşlenmemiş alarm hattı durdurmuş sayılmasın")  # fmt: skip

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
    return ayrac


def _kaynak_satiri(k: dict) -> str:
    son = f"{k['son_olay']:%d.%m.%Y %H:%M}" if k["son_olay"] else "yok"
    return (
        f"{k['ad']:<20} {'açık' if k['aktif'] else 'KAPALI':<7} katalog={k['katalog'] or '-':<12} "
        f"süren alarm={k['aktif_alarm']:<3} son olay={son}"
    )


def calistir(argumanlar: list[str], conn: psycopg.Connection) -> str:
    """Komutu çalıştırır, ekrana yazılacak metni döner. Hata: SystemExit (mesajla)."""
    a = _ayrac().parse_args(argumanlar)
    if a.komut == "kaynak-ekle":
        try:
            anahtar = entegrasyon.kaynak_ekle(
                conn, a.ad, a.aciklama, katalog=a.katalog, varsayilan_tip=a.varsayilan_tip,
                varsayilan_onem=a.varsayilan_onem, varsayilan_hat_durdurur=not a.hat_durdurmaz,
            )  # fmt: skip
        except psycopg.errors.UniqueViolation:
            raise SystemExit(f"'{a.ad}' adında bir kaynak zaten var.") from None
        except psycopg.errors.CheckViolation:
            raise SystemExit("Ad küçük harf, rakam, - ve _ olmalı (2-40 karakter).") from None
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
