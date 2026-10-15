"""Gönderilmeyi bekleyen olaylar: diskte, sıralı (FIFO).

API kapalıyken, ağ koptuğunda ya da bağlayıcı yeniden başladığında olay kaybolmasın diye
SQLite dosyasında tutulur. Olay ancak API kabul ettikten sonra silinir.
"""

import json
import sqlite3
import threading
from pathlib import Path


class Kuyruk:
    def __init__(self, yol: str | Path = ":memory:"):
        self._db = sqlite3.connect(str(yol), check_same_thread=False, isolation_level=None)
        if str(yol) != ":memory:":
            # Yazma bir kez diske inmeden ekle() dönmesin; elektrik kesilse de olay kalsın.
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS kuyruk "
            "(no INTEGER PRIMARY KEY AUTOINCREMENT, olay TEXT NOT NULL)"
        )
        self._kilit = threading.Lock()

    def ekle(self, olay: dict) -> None:
        with self._kilit:
            self._db.execute("INSERT INTO kuyruk (olay) VALUES (?)", [json.dumps(olay)])

    def al(self, adet: int) -> list[tuple[int, dict]]:
        """En eski `adet` olay (silmeden)."""
        with self._kilit:
            satirlar = self._db.execute(
                "SELECT no, olay FROM kuyruk ORDER BY no LIMIT ?", [adet]
            ).fetchall()
        return [(no, json.loads(olay)) for no, olay in satirlar]

    def sil(self, nolar: list[int]) -> None:
        with self._kilit:
            self._db.executemany("DELETE FROM kuyruk WHERE no = ?", [(n,) for n in nolar])

    def __bool__(self) -> bool:
        # __len__ tanımlı olduğu için boş kuyruk "yanlış" sayılırdı: `kuyruk or Kuyruk()` gibi bir
        # ifade boş dosya kuyruğunu sessizce bellekteki yenisiyle değiştirirdi (testte oldu).
        return True

    def __len__(self) -> int:
        with self._kilit:
            return self._db.execute("SELECT count(*) FROM kuyruk").fetchone()[0]

    def kapat(self) -> None:
        with self._kilit:
            self._db.close()
