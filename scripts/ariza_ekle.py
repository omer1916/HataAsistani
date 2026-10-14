"""Demo için şu an başlayan, hattı durduran bir arıza ekler; Telegram bildirimini denemek için.

Kullanım:
    python scripts/ariza_ekle.py                                   # P3-HP, hidrolik, yüksek
    python scripts/ariza_ekle.py K2-KR1 yazilim orta "Robot pozisyon hatası verdi"
    python scripts/ariza_ekle.py --bitir 4321                      # arızayı şimdi bitir

Gerçek bir fabrikada bu kaydı MES (ör. Verimot) yazar. Bot çalışıyorsa kayıt anında (veritabanı
tetikleyicisiyle) eşleşmiş Telegram hesaplarına bildirim gider. Eklenen arıza bitirilene kadar
sürüyor sayılır: makine bakım planında ve vardiya raporunda "şu an arızalı" görünür. Demodan
sonra --bitir ile kapatın.
"""

import argparse
import sys

import psycopg

from app.db import baglan
from app.models import ArizaTipi, Onem

TIPLER = ArizaTipi.__args__
ONEMLER = Onem.__args__


def ekle(conn: psycopg.Connection, a: argparse.Namespace) -> None:
    satir = conn.execute(
        """
        INSERT INTO ariza_kayitlari (makine_id, baslangic, ariza_tipi, onem, hat_durdu, aciklama)
        SELECT id, now(), %s, %s, true, %s FROM makineler WHERE kod = upper(%s)
        RETURNING id
        """,
        [a.tip, a.onem, a.aciklama, a.makine],
    ).fetchone()
    if satir is None:
        sys.exit(f"'{a.makine}' kodlu makine yok.")
    print(f"Arıza #{satir[0]} eklendi: {a.makine.upper()}, {a.tip}, {a.onem}, hattı durdurdu.")
    print(f"Bitirmek için: python scripts/ariza_ekle.py --bitir {satir[0]}")


def bitir(conn: psycopg.Connection, ariza_id: int) -> None:
    satir = conn.execute(
        "UPDATE ariza_kayitlari SET bitis = greatest(now(), baslangic) "
        "WHERE id = %s AND bitis IS NULL RETURNING id",
        [ariza_id],
    ).fetchone()
    if satir is None:
        sys.exit(f"#{ariza_id} numaralı süren bir arıza yok.")
    print(f"Arıza #{ariza_id} bitirildi.")


def main() -> None:
    ayrac = argparse.ArgumentParser(description="Hattı durduran bir demo arızası ekler.")
    ayrac.add_argument("makine", nargs="?", default="P3-HP")
    ayrac.add_argument("tip", nargs="?", default="hidrolik", choices=TIPLER)
    ayrac.add_argument("onem", nargs="?", default="yuksek", choices=ONEMLER)
    ayrac.add_argument("aciklama", nargs="?", default="Hidrolik basınç set değerinin altına düştü")
    ayrac.add_argument("--bitir", type=int, metavar="ID", help="süren arızayı şimdi bitirir")
    a = ayrac.parse_args()
    try:
        with baglan() as conn:
            if a.bitir is not None:
                bitir(conn, a.bitir)
            else:
                ekle(conn, a)
    except psycopg.OperationalError as hata:
        sys.exit(f"Veritabanına bağlanılamadı: {hata}")


if __name__ == "__main__":
    main()
