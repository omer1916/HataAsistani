"""Makine entegrasyonunun zorlu testleri: rastgele olay dizileri, eşzamanlılık, bozuk girdi.

1. Durum makinesi: Hypothesis rastgele olay dizileri üretir (başla, bitir, aynı olayı yeniden
   gönder, bitişi başlangıçtan önce gönder, arızayı elle kapat; zamanlar 60 saniyelik yeniden
   açma sınırının çevresinde). Her adımdan sonra veritabanı, kuralları saf Python'la yeniden
   yazan bağımsız bir modelle alan alan karşılaştırılır.
2. Eşzamanlılık: birçok iş parçacığı aynı makineye aynı anda olay yağdırır; çift arıza, kayıp
   olay, yarım kalmış durum olmamalı.
3. Bozuk girdi: API'ye rastgele JSON gönderilir; hiçbir zaman 500 dönmemeli.

Örnek sayıları ortam değişkeniyle büyür: HA_YOGUN=1 pytest tests/test_entegrasyon_durum.py
"""

import os
import random
import threading
from bisect import insort
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from itertools import count

import psycopg
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from app import entegrasyon as e
from tests.conftest import SABIT_AN

YOGUN = os.environ.get("HA_YOGUN") == "1"
SIMDI = SABIT_AN
T0 = SIMDI - timedelta(days=10)  # en uzun dizi de şimdiyi geçmesin
MAKINELER = ["P3-HP", "P1-HP"]
# (tip, önem, hattı durdurur): üçü katalogdan, X1 kaynağın varsayılanından.
KODLAR = {
    "A101": ("hidrolik", "yuksek", True),
    "A104": ("hidrolik", "orta", False),
    "A201": ("sensor", "dusuk", True),
    "X1": ("mekanik", "dusuk", False),
}
ONEM = {"dusuk": 0, "orta": 1, "yuksek": 2}
# Yeniden açma sınırı (60 sn) ve çevresi daha sık seçilsin.
ADIMLAR = st.one_of(st.sampled_from([0, 1, 59, 60, 61]), st.integers(0, 90), st.integers(0, 3600))
_kaynak_no = count()


# --- Bağımsız model -------------------------------------------------------------------------


@dataclass
class ModelAriza:
    makine: str
    baslangic: object
    tip: str
    onem: str
    hat_durdu: bool
    bitis: object = None


@dataclass
class Model:
    """Kuralların veritabanından bağımsız, saf Python hâli (app/entegrasyon.py'nin modül
    belgesindeki kurallar). Arızalar oluşturulma sırasıyla numaralanır; veritabanı id'leri de
    artan olduğundan sıralar aynıdır."""

    aktif: dict = field(default_factory=dict)  # (makine, kod) -> arıza no
    arizalar: list = field(default_factory=list)
    # (makine, kod) -> [(t, geliş sırası, olay_id)]: veritabanı eşit zamanda geliş sırasına bakar
    bekleyen: dict = field(default_factory=lambda: defaultdict(list))
    sira: object = field(default_factory=count)
    sonuclar: dict = field(default_factory=dict)  # olay_id -> (sonuc, arıza no)

    def basla(self, olay_id, makine, kod, t):
        if olay_id in self.sonuclar:
            return ("tekrar", *self.sonuclar[olay_id])
        tip, onem, durdurur = KODLAR[kod]
        mk = (makine, kod)
        if mk in self.aktif and self.arizalar[self.aktif[mk]].bitis is None:
            sonuc = ("zaten_aktif", self.aktif[mk])
            self.sonuclar[olay_id] = sonuc
            return ("islendi", *sonuc)
        self.aktif.pop(mk, None)  # elle kapatılmış arızanın kalan satırı
        gec = next(
            (b for b in self.bekleyen[mk] if t <= b[0] <= t + timedelta(days=7)),
            None,
        )
        acik = sorted(
            n for (m, _), n in self.aktif.items() if m == makine and self.arizalar[n].bitis is None
        )
        if acik:
            no, sonuc = acik[0], "ariza_eklendi"
            self._yukselt(no, onem, durdurur)
        else:
            adaylar = [
                (a.bitis, n)
                for n, a in enumerate(self.arizalar)
                if a.makine == makine
                and a.bitis is not None
                and a.bitis >= t - e.KISA_ARA
                and a.baslangic <= t
            ]
            aday = max(adaylar) if adaylar else None
            if aday and not (durdurur and not self.arizalar[aday[1]].hat_durdu):
                no, sonuc = aday[1], "ariza_yeniden_acildi"
                self.arizalar[no].bitis = None
                self._yukselt(no, onem, durdurur)
            else:
                self.arizalar.append(ModelAriza(makine, t, tip, onem, durdurur))
                no, sonuc = len(self.arizalar) - 1, "ariza_acildi"
        self.aktif[mk] = no
        if gec:
            self.bekleyen[mk].remove(gec)
            self._kapat(mk, no, gec[0])
            self.sonuclar[gec[2]] = ("gec_eslesti", no)
        self.sonuclar[olay_id] = (sonuc, no)
        return ("islendi", sonuc, no)

    def bitir(self, olay_id, makine, kod, t):
        if olay_id in self.sonuclar:
            return ("tekrar", *self.sonuclar[olay_id])
        mk = (makine, kod)
        if mk not in self.aktif:
            insort(self.bekleyen[mk], (t, next(self.sira), olay_id))
            self.sonuclar[olay_id] = ("aktif_alarm_yok", None)
            return ("islendi", "aktif_alarm_yok", None)
        no = self.aktif[mk]
        sonuc = (self._kapat(mk, no, t), no)
        self.sonuclar[olay_id] = sonuc
        return ("islendi", *sonuc)

    def _yukselt(self, no, onem, durdurur):
        a = self.arizalar[no]
        a.hat_durdu = a.hat_durdu or durdurur
        if ONEM[onem] > ONEM[a.onem]:
            a.onem = onem

    def _kapat(self, mk, no, t):
        del self.aktif[mk]
        if no in self.aktif.values():
            return "alarm_kapandi"
        a = self.arizalar[no]
        if a.bitis is not None:
            return "alarm_kapandi"
        a.bitis = max(t, a.baslangic + timedelta(seconds=1))
        return "ariza_kapandi"


# --- Durum makinesi ---------------------------------------------------------------------------


class EntegrasyonMakinesi(RuleBasedStateMachine):
    url: str = ""
    toplam_dizi = 0  # rapor için: kaç rastgele dizi, kaç olay denendi
    toplam_olay = 0

    @initialize()
    def kur(self):
        self.conn = psycopg.connect(self.url, autocommit=True)
        self.ad = f"test-durum-{os.getpid()}-{next(_kaynak_no)}"
        anahtar = e.kaynak_ekle(
            self.conn, self.ad, "durum makinesi", katalog="pres-paneli",
            varsayilan_tip="mekanik", varsayilan_onem="dusuk", varsayilan_hat_durdurur=False,
        )  # fmt: skip
        self.kaynak = e.kaynak_bul(self.conn, anahtar)
        self.model = Model()
        self.db_id: dict[int, int] = {}  # model arıza no -> veritabanı id
        self.gonderilen: list[e.Olay] = []
        self.t = T0
        self.no = count()
        EntegrasyonMakinesi.toplam_dizi += 1

    def teardown(self):
        if not hasattr(self, "conn"):
            return
        idler = list(self.db_id.values())
        self.conn.execute("DELETE FROM entegrasyon_kaynaklari WHERE ad = %s", [self.ad])
        self.conn.execute("DELETE FROM ariza_kayitlari WHERE id = ANY(%s)", [idler])
        self.conn.close()

    def _gonder(self, olay: e.Olay):
        EntegrasyonMakinesi.toplam_olay += 1
        self.gonderilen.append(olay)
        return e.olay_isle(self.conn, self.kaynak, olay, SIMDI)

    def _olay(self, tur, makine, kod, t):
        return e.Olay(f"{self.ad}-{next(self.no)}", tur, makine, kod, t)

    def _karsilastir(self, gercek: e.Sonuc, beklenen):
        durum, sonuc, no = beklenen
        assert (gercek.durum, gercek.sonuc) == (durum, sonuc), (gercek, beklenen)
        if no is None:
            assert gercek.ariza_id is None
        elif no in self.db_id:
            assert gercek.ariza_id == self.db_id[no], (gercek, beklenen)
        else:
            assert gercek.ariza_id not in self.db_id.values()
            self.db_id[no] = gercek.ariza_id

    @rule(makine=st.sampled_from(MAKINELER), kod=st.sampled_from(sorted(KODLAR)), dt=ADIMLAR)
    def basla(self, makine, kod, dt):
        self.t += timedelta(seconds=dt)
        o = self._olay("alarm_basladi", makine, kod, self.t)
        self._karsilastir(self._gonder(o), self.model.basla(o.olay_id, makine, kod, self.t))

    @rule(makine=st.sampled_from(MAKINELER), kod=st.sampled_from(sorted(KODLAR)), dt=ADIMLAR)
    def bitir(self, makine, kod, dt):
        self.t += timedelta(seconds=dt)
        o = self._olay("alarm_bitti", makine, kod, self.t)
        self._karsilastir(self._gonder(o), self.model.bitir(o.olay_id, makine, kod, self.t))

    @precondition(lambda self: self.gonderilen)
    @rule(veri=st.data())
    def yeniden_gonder(self, veri):
        """Bağlayıcı emin olamadığı olayı yeniden gönderir: hiçbir şey değişmemeli."""
        o = veri.draw(st.sampled_from(self.gonderilen))
        beklenen = ("tekrar", *self.model.sonuclar[o.olay_id])
        self._karsilastir(self._gonder(o), beklenen)

    @rule(makine=st.sampled_from(MAKINELER), kod=st.sampled_from(sorted(KODLAR)),
          sure=st.integers(0, 600))  # fmt: skip
    def bitis_once_gelir(self, makine, kod, sure):
        """Ağda sıra karıştı: aynı alarmın bitişi başlangıcından önce gelir."""
        bas, bit = self.t, self.t + timedelta(seconds=sure)
        ob = self._olay("alarm_bitti", makine, kod, bit)
        self._karsilastir(self._gonder(ob), self.model.bitir(ob.olay_id, makine, kod, bit))
        oa = self._olay("alarm_basladi", makine, kod, bas)
        self._karsilastir(self._gonder(oa), self.model.basla(oa.olay_id, makine, kod, bas))
        self.t = bit

    @precondition(lambda self: any(a.bitis is None for n, a in enumerate(self.model.arizalar)))
    @rule(veri=st.data(), dt=st.integers(1, 120))
    def elle_kapat(self, veri, dt):
        """Bakımcı arızayı elle kapatır (alarm hâlâ sürüyor görünebilir)."""
        acik = [n for n, a in enumerate(self.model.arizalar) if a.bitis is None]
        no = veri.draw(st.sampled_from(acik))
        self.t += timedelta(seconds=dt)
        a = self.model.arizalar[no]
        a.bitis = max(self.t, a.baslangic + timedelta(seconds=1))
        self.conn.execute(
            "UPDATE ariza_kayitlari SET bitis = %s WHERE id = %s", [a.bitis, self.db_id[no]]
        )

    @invariant()
    def veritabani_modelle_ayni(self):
        if not hasattr(self, "conn"):
            return
        aktif = {
            (m, k): a
            for m, k, a in self.conn.execute(
                "SELECT m.kod, a.alarm_kodu, a.ariza_id FROM aktif_alarmlar a "
                "JOIN makineler m ON m.id = a.makine_id WHERE a.kaynak = %s",
                [self.ad],
            )
        }
        assert aktif == {mk: self.db_id[no] for mk, no in self.model.aktif.items()}
        satirlar = {
            r[0]: r[1:]
            for r in self.conn.execute(
                "SELECT r.id, m.kod, r.baslangic, r.bitis, r.ariza_tipi, r.onem, r.hat_durdu "
                "FROM ariza_kayitlari r JOIN makineler m ON m.id = r.makine_id "
                "WHERE r.id = ANY(%s)",
                [list(self.db_id.values())],
            )
        }
        assert len(self.db_id) == len(self.model.arizalar)
        for no, a in enumerate(self.model.arizalar):
            beklenen = (a.makine, a.baslangic, a.bitis, a.tip, a.onem, a.hat_durdu)
            assert satirlar[self.db_id[no]] == beklenen, (no, satirlar[self.db_id[no]], a)
        # Kaynağın bir makinede en fazla bir açık arızası olur.
        acik = [a.makine for a in self.model.arizalar if a.bitis is None]
        assert len(acik) == len(set(acik))


AYAR = settings(
    max_examples=2000 if YOGUN else 120,
    stateful_step_count=60 if YOGUN else 40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


def test_durum_makinesi_rastgele_olay_dizileri(test_veritabani):
    EntegrasyonMakinesi.url = test_veritabani
    run_state_machine_as_test(EntegrasyonMakinesi, settings=AYAR)
    print(
        f"\ndurum makinesi: {EntegrasyonMakinesi.toplam_dizi} dizi, "
        f"{EntegrasyonMakinesi.toplam_olay} olay modelle karşılaştırıldı"
    )


# --- Eşzamanlılık -------------------------------------------------------------------------------


@pytest.fixture
def kaynak(test_veritabani):
    with psycopg.connect(test_veritabani, autocommit=True) as conn:
        son = conn.execute("SELECT max(id) FROM ariza_kayitlari").fetchone()[0]
        ad = f"test-es-{next(_kaynak_no)}"
        anahtar = e.kaynak_ekle(conn, ad, "eşzamanlılık", katalog="pres-paneli")
        yield e.kaynak_bul(conn, anahtar)
        conn.execute("DELETE FROM entegrasyon_kaynaklari WHERE ad = %s", [ad])
        conn.execute("DELETE FROM ariza_kayitlari WHERE id > %s", [son])


def _es_zamanli(url, isler):
    """Her iş kendi bağlantısıyla, hepsi aynı anda başlar; sonuçlar sırasıyla döner."""
    engel = threading.Barrier(len(isler))
    sonuclar = [None] * len(isler)
    hatalar = []

    def calis(i, is_):
        try:
            with psycopg.connect(url, autocommit=True) as conn:
                engel.wait()
                sonuclar[i] = is_(conn)
        except Exception as hata:  # iş parçacığındaki hata teste ulaşsın
            hatalar.append(hata)

    parcaciklar = [threading.Thread(target=calis, args=(i, f)) for i, f in enumerate(isler)]
    for p in parcaciklar:
        p.start()
    for p in parcaciklar:
        p.join(60)
    assert not hatalar, hatalar
    return sonuclar


TUR = 40 if YOGUN else 12


@pytest.mark.parametrize("tur", range(TUR))
def test_ayni_alarm_ayni_anda_tek_ariza_acar(test_veritabani, kaynak, tur):
    t = T0 + timedelta(hours=tur)
    olaylar = [e.Olay(f"es-{tur}-{i}", "alarm_basladi", "P3-HP", "A101", t) for i in range(8)]
    sonuclar = _es_zamanli(
        test_veritabani, [lambda c, o=o: e.olay_isle(c, kaynak, o, SIMDI) for o in olaylar]
    )
    assert sorted(s.sonuc for s in sonuclar) == ["ariza_acildi"] + ["zaten_aktif"] * 7
    assert len({s.ariza_id for s in sonuclar}) == 1


@pytest.mark.parametrize("tur", range(TUR))
def test_farkli_alarmlar_ayni_anda_tek_duruste_toplanir(test_veritabani, kaynak, tur):
    t = T0 + timedelta(hours=tur)
    kodlar = ["A101", "A102", "A103", "A104", "A201", "A202", "A301", "A302"]
    olaylar = [e.Olay(f"fk-{tur}-{k}", "alarm_basladi", "P1-HP", k, t) for k in kodlar]
    sonuclar = _es_zamanli(
        test_veritabani, [lambda c, o=o: e.olay_isle(c, kaynak, o, SIMDI) for o in olaylar]
    )
    assert sorted(s.sonuc for s in sonuclar) == ["ariza_acildi"] + ["ariza_eklendi"] * 7
    assert len({s.ariza_id for s in sonuclar}) == 1
    # Hepsi aynı anda bitince arıza tam bir kez kapanır.
    bitisler = [
        e.Olay(f"fb-{tur}-{k}", "alarm_bitti", "P1-HP", k, t + timedelta(minutes=5)) for k in kodlar
    ]
    sonuclar = _es_zamanli(
        test_veritabani, [lambda c, o=o: e.olay_isle(c, kaynak, o, SIMDI) for o in bitisler]
    )
    assert sorted(s.sonuc for s in sonuclar) == ["alarm_kapandi"] * 7 + ["ariza_kapandi"]


@pytest.mark.parametrize("tur", range(TUR))
def test_ayni_olay_kimligi_farkli_makinelerle_ayni_anda(test_veritabani, kaynak, tur):
    """Bozuk bir bağlayıcı aynı olay_id'yi farklı makine kodlarıyla aynı anda gönderir: yalnızca
    biri işlenir, diğerleri ilk sonucu alır (benzersizlik kısıtı, 500 değil)."""
    t = T0 + timedelta(hours=tur)
    makineler = ["P1-HP", "P2-HP", "P3-HP", "P1-RB", "P2-RB", "P3-RB"]
    olaylar = [e.Olay(f"ayni-{tur}", "alarm_basladi", m, "A101", t) for m in makineler]
    sonuclar = _es_zamanli(
        test_veritabani, [lambda c, o=o: e.olay_isle(c, kaynak, o, SIMDI) for o in olaylar]
    )
    assert sorted(s.durum for s in sonuclar) == ["islendi"] + ["tekrar"] * 5
    assert len({s.ariza_id for s in sonuclar}) == 1


def test_rastgele_eszamanli_yuk_tutarli_kalir(test_veritabani, kaynak):
    """Birçok iş parçacığı birçok makineye rastgele olay yağdırır (aynı olaylar da tekrar
    tekrar gönderilir). Sonunda durum tutarlı: her aktif alarmın arızası açık, kaynağın bir
    makinede en fazla bir açık arızası var, her olay tam bir kez kayıtlı."""
    makineler = ["P1-HP", "P2-HP", "P3-HP"]
    kodlar = ["A101", "A104", "A201", "A301"]
    adet, is_parcacigi = (6000, 12) if YOGUN else (1200, 8)
    rng = random.Random(42)
    olaylar = [
        e.Olay(
            f"yuk-{i}",
            rng.choice(["alarm_basladi", "alarm_bitti"]),
            rng.choice(makineler),
            rng.choice(kodlar),
            T0 + timedelta(seconds=i),
        )
        for i in range(adet)
    ]
    dilimler = [olaylar[i::is_parcacigi] for i in range(is_parcacigi)]
    # Her iş parçacığı kendi dilimini ve rastgele bir başka dilimi (tekrar) gönderir.
    gonderilecek = [d + (k := rng.choice(dilimler))[: len(k) // 3] for d in dilimler]
    isler = [
        lambda c, liste=liste: [e.olay_isle(c, kaynak, o, SIMDI) for o in liste]
        for liste in gonderilecek
    ]
    sonuclar = [s for parca in _es_zamanli(test_veritabani, isler) for s in parca]
    assert all(s.durum in ("islendi", "tekrar") for s in sonuclar)

    with psycopg.connect(test_veritabani, autocommit=True) as c:
        assert c.execute(
            "SELECT count(*), count(DISTINCT olay_id) FROM entegrasyon_olaylari WHERE kaynak = %s",
            [kaynak.ad],
        ).fetchone() == (adet, adet)
        assert c.execute(
            "SELECT count(*) FROM aktif_alarmlar a JOIN ariza_kayitlari r ON r.id = a.ariza_id "
            "WHERE a.kaynak = %s AND r.bitis IS NOT NULL",
            [kaynak.ad],
        ).fetchone() == (0,)
        assert c.execute(
            "SELECT count(*) FROM (SELECT a.makine_id FROM aktif_alarmlar a "
            "JOIN ariza_kayitlari r ON r.id = a.ariza_id WHERE a.kaynak = %s "
            "GROUP BY a.makine_id HAVING count(DISTINCT a.ariza_id) > 1) x",
            [kaynak.ad],
        ).fetchone() == (0,)
        assert c.execute(
            "SELECT count(*) FROM ariza_kayitlari r WHERE r.bitis IS NULL AND EXISTS "
            "(SELECT 1 FROM entegrasyon_olaylari o WHERE o.kaynak = %s AND o.ariza_id = r.id) "
            "AND NOT EXISTS (SELECT 1 FROM aktif_alarmlar a WHERE a.ariza_id = r.id)",
            [kaynak.ad],
        ).fetchone() == (0,)  # açık arızanın en az bir süren alarmı var


# --- Bozuk girdi --------------------------------------------------------------------------------

JSON = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)  # JSON'da yok; ham baytla ayrı test
    | st.text(max_size=40),
    lambda alt: st.lists(alt, max_size=4) | st.dictionaries(st.text(max_size=12), alt, max_size=5),
    max_leaves=12,
)
ALANLAR = ["olay_id", "tur", "makine_kodu", "alarm_kodu", "zaman", "mesaj", "ariza_tipi", "onem",
           "hat_durdurur"]  # fmt: skip
OLAYA_BENZER = st.fixed_dictionaries(
    {},
    optional={
        "olay_id": st.text(max_size=140) | JSON,
        "tur": st.sampled_from(["alarm_basladi", "alarm_bitti", "x", ""]) | JSON,
        "makine_kodu": st.sampled_from(["P3-HP", "p1-hp", "X9-YOK", " ", "P3-HP\x00"]) | JSON,
        "alarm_kodu": st.text(max_size=70) | st.sampled_from(["A101", "SV*", "A_1"]),
        "zaman": st.sampled_from(
            ["2026-10-15T10:00:00+03:00", "2026-10-15T10:00:00", "1970-01-01", "9999-12-31",
             "dün", 0, -1, 1e20, "2026-02-30T00:00:00"]
        ) | JSON,
        "mesaj": st.text(max_size=600) | JSON,
        "ariza_tipi": st.sampled_from(["hidrolik", "diger"]) | JSON,
        "onem": st.sampled_from(["yuksek", "kritik"]) | JSON,
        "hat_durdurur": st.booleans() | JSON,
    },
)  # fmt: skip


@pytest.fixture(scope="module")
def anahtar(test_veritabani, istemci):
    eski = istemci.app.state.hiz_siniri
    istemci.app.state.hiz_siniri = e.HizSiniri(hiz=1e9, kova=1e9)  # sınır başka testte denenir
    with psycopg.connect(test_veritabani, autocommit=True) as conn:
        son = conn.execute("SELECT max(id) FROM ariza_kayitlari").fetchone()[0]
        anahtar = e.kaynak_ekle(conn, "test-bulanik", "bozuk girdi", katalog="pres-paneli")
        yield anahtar
        conn.execute("DELETE FROM entegrasyon_kaynaklari WHERE ad = 'test-bulanik'")
        conn.execute("DELETE FROM ariza_kayitlari WHERE id > %s", [son])
    istemci.app.state.hiz_siniri = eski


@settings(
    max_examples=3000 if YOGUN else 300,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    govde=st.one_of(
        JSON,
        st.fixed_dictionaries({"olaylar": st.lists(OLAYA_BENZER | JSON, max_size=6)}),
        st.fixed_dictionaries({"olaylar": st.lists(OLAYA_BENZER, min_size=1, max_size=6)}),
    )
)
def test_bozuk_girdi_hicbir_zaman_500_donmez(istemci, anahtar, govde):
    cevap = istemci.post(
        "/entegrasyon/olaylar", json=govde, headers={"Authorization": "", "X-API-Anahtari": anahtar}
    )
    assert cevap.status_code in (200, 422), (cevap.status_code, cevap.text, govde)
    if cevap.status_code == 200:
        sonuclar = cevap.json()["sonuclar"]
        assert [s["sira"] for s in sonuclar] == list(range(len(govde["olaylar"])))
        assert all(s["durum"] in ("islendi", "tekrar", "gecersiz", "hata") for s in sonuclar)


@pytest.mark.parametrize(
    "ham",
    [
        b'{"olaylar": [{"zaman": NaN}]}',
        b'{"olaylar": [{"olay_id": "x", "tur": "alarm_basladi", "makine_kodu": "P3-HP", '
        b'"alarm_kodu": "A101", "zaman": Infinity}]}',
        b'{"olaylar": [-Infinity]}',
        b'{"olaylar": [{"olay_id": "\\ud800"}]}',
        b'{"olaylar": [{}]',
        b"\xff\xfe",
        b"",
        b"{" * 5000 + b"}" * 5000,
    ],
    ids=[
        "nan",
        "sonsuz-zaman",
        "eksi-sonsuz",
        "yarim-vekil",
        "kapanmamis",
        "utf16",
        "bos",
        "derin",
    ],  # fmt: skip
)
def test_ham_bozuk_govde(istemci, anahtar, ham):
    cevap = istemci.post(
        "/entegrasyon/olaylar",
        content=ham,
        headers={
            "Authorization": "",
            "X-API-Anahtari": anahtar,
            "Content-Type": "application/json",
        },
    )
    assert cevap.status_code in (200, 422), (cevap.status_code, cevap.text)
    if cevap.status_code == 200:
        assert all(s["durum"] in ("gecersiz", "hata") for s in cevap.json()["sonuclar"])
