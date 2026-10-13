"""Hata Asistanı Telegram botu: sahadan soru, fotoğraflı bakım talebi, arıza bildirimi ve komutlar.

Güvenlik:
- Bot yalnızca özel sohbetlerde çalışır; grup sohbetleri yok sayılır.
- Eşleşmemiş bir sohbete tanıtım mesajından başka hiçbir şey gönderilmez.
- Eşleştirme, web arayüzünden alınan 10 dakikalık tek kullanımlık kodla yapılır. Kodun kendisi
  değil SHA-256 özeti saklanır ve yanlış kod denemesi sohbet başına sınırlıdır.
- Agent, eşleşen kullanıcının rolüyle çalışır: operatör telefondan da bakım kılavuzlarını
  göremez. Arıza bildiriminde kılavuz alıntısı ve stok yalnızca bakım rolüne gider.
- Soru sayısı sohbet başına sınırlıdır (LLM maliyeti).

Veri gizliliği:
- Bot mesajları uçtan uca şifreli değildir, Telegram sunucularından geçer. Bildirimin ayrıntısı
  ayarlanabilir: "kisa" modda kılavuz alıntısı ve stok gönderilmez, yalnızca hattın durduğu.
- Kullanıcı /gizlilik ile neyin saklandığını ve nereye gittiğini, /cikis ile bağlantısının ve
  bekleyen kodlarının silindiğini görür; bağlanırken kısa bir bilgilendirme alır.
- Yalnızca Telegram'ın sıkıştırdığı fotoğraflar kabul edilir (dosya olarak gönderilen resim
  değil): Telegram bu sırada konum gibi EXIF bilgilerini siler. Fotoğraflar belirli bir süre
  sonra silinir (eski_verileri_temizle).
- Botun günlüğüne mesaj içeriği ve token yazılmaz.
"""

import hashlib
import html
import re
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Literal

import psycopg

from app import agent, bakim_plani, rag, rapor, sorgular
from app.auth import ROL_ADLARI, ROL_ERISIMI, Kullanici
from app.db import TZ
from app.embedding import Embedder
from app.llm import LLM
from app.rapor import yuzde_metni
from app.telegram_api import Telegram, TelegramHatasi
from app.tools import AracBaglami

KOD_GECERLILIK = timedelta(minutes=10)
EN_FAZLA_YANLIS_KOD = 5  # KOD_GECERLILIK içinde, sohbet başına
SORU_SINIRI, SORU_PENCERESI_SN = 10, 60  # 60 saniyede en fazla 10 soru
EN_BUYUK_FOTOGRAF = 5 * 1024 * 1024
BildirimAyrintisi = Literal["ayrintili", "kisa"]
ONEM_ADLARI = {"dusuk": "düşük", "orta": "orta", "yuksek": "yüksek"}

TANITIM = (
    "Merhaba, ben Hata Asistanı. Fabrika verisini ve kılavuzları yalnızca eşleşmiş hesaplarla "
    "paylaşabilirim.\n\nHata Asistanı web arayüzünde sağ üstteki <b>Telegram</b> düğmesinden "
    "bir kod alın ve buraya <code>/baglan 123456</code> biçiminde yazın."
)


def yardim_metni(kullanici: Kullanici) -> str:
    satirlar = [
        "Soru yazabilirsiniz, örneğin: <i>Pres 3'te bu hafta kaç arıza oldu?</i>",
        "Fotoğraf gönderip altına sorunu yazarsanız bakım talebi açarım, örneğin: "
        "<i>P3-HP'de yağ kaçağı var</i>.",
        "",
        "/rapor son vardiyanın raporu",
        "/oee [hat] son 7 günün OEE'si, örneğin <code>/oee Pres 3</code>",
    ]
    if kullanici.rol == "bakim":
        satirlar.append("/plan [saat] haftalık bakım planı, örneğin <code>/plan 16</code>")
    satirlar += [
        "/gizlilik neyin saklandığı ve nereye gittiği",
        "/cikis bu sohbetin bağlantısını kaldırır",
    ]
    return "\n".join(satirlar)


LLM_ADLARI = {"gemini": "Google Gemini API'sine", "ollama": "fabrikanın kendi sunucusundaki modele"}


def gizlilik_metni(saglayici: str | None, fotograf_saklama_gun: int) -> str:
    """KVKK aydınlatma metninin kısa hâli: ne saklanıyor, nereye gidiyor, nasıl silinir."""
    llm = LLM_ADLARI.get(saglayici or "", "yapılandırılmış dil modeline")
    return "\n".join(
        [
            "<b>Veri gizliliği</b>",
            "• <b>Saklanan:</b> Telegram sohbet numaranız ve Hata Asistanı kullanıcı adınızla "
            "eşleştirme zamanı. Telefon numaranız, adınız ve profiliniz saklanmaz.",
            f"• <b>Sorularınız</b> cevaplanmak için {llm} gönderilir; soru, cevap ve token "
            "sayısı Hata Asistanı'nın istek kaydında kullanıcı adınızla tutulur (web arayüzündeki "
            "sorular gibi).",
            f"• <b>Fotoğraflar</b> açılan bakım talebine eklenir ve {fotograf_saklama_gun} gün "
            "sonra silinir. Telegram sıkıştırırken konum gibi bilgileri siler; dosya olarak "
            "gönderilen resimler kabul edilmez.",
            "• Bu sohbet <b>uçtan uca şifreli değildir</b>; mesajlar Telegram sunucularından "
            "geçer. Kişisel veya gizli bilgi yazmayın.",
            "• <b>Silme:</b> /cikis bağlantınızı ve bekleyen kodlarınızı hemen siler; "
            "bildirimler durur. İstek kayıtlarının silinmesi için yöneticinize başvurun.",
        ]
    )


# --- Eşleştirme (API ve bot ortak) ---------------------------------------------------------


def kod_ozeti(kod: str) -> str:
    return hashlib.sha256(kod.encode()).hexdigest()


def kod_uret(conn: psycopg.Connection, kullanici_adi: str, simdi: datetime | None = None) -> str:
    """Kullanıcı için yeni bir 6 haneli kod üretir; kullanıcının eski kodu geçersiz olur."""
    simdi = simdi or datetime.now(TZ)
    with conn.transaction():
        conn.execute(
            "DELETE FROM telegram_kodlari WHERE kullanici_adi = %s OR son_gecerlilik < %s",
            [kullanici_adi, simdi],
        )
        while True:
            # Başka bir kullanıcının geçerli koduyla çakışırsa yeni kod: üzerine yazılsaydı o
            # kullanıcının sohbeti bu hesaba bağlanabilirdi.
            kod = f"{secrets.randbelow(10**6):06d}"
            eklendi = conn.execute(
                "INSERT INTO telegram_kodlari (kod_ozeti, kullanici_adi, son_gecerlilik) "
                "VALUES (%s, %s, %s) ON CONFLICT (kod_ozeti) DO NOTHING RETURNING 1",
                [kod_ozeti(kod), kullanici_adi, simdi + KOD_GECERLILIK],
            ).fetchone()
            if eklendi:
                return kod


def eslestir(conn: psycopg.Connection, kod: str, chat_id: int, simdi: datetime) -> str | None:
    """Kod geçerliyse sohbeti kullanıcıya bağlar ve kodu siler; kullanıcı adını döner.
    Kodu aldıktan sonra silinmiş bir kullanıcıya bağlanılmaz: tablolar arasında yabancı anahtar
    olmadığından (bkz. sql/telegram.sql) aksi hâlde sahipsiz bir bağlantı kalırdı."""
    with conn.transaction():
        satir = conn.execute(
            "DELETE FROM telegram_kodlari k USING kullanicilar u "
            "WHERE k.kod_ozeti = %s AND k.son_gecerlilik > %s "
            "AND u.kullanici_adi = k.kullanici_adi RETURNING k.kullanici_adi",
            [kod_ozeti(kod), simdi],
        ).fetchone()
        if satir is None:
            return None
        conn.execute(
            "DELETE FROM telegram_baglantilari WHERE chat_id = %s OR kullanici_adi = %s",
            [chat_id, satir[0]],
        )
        conn.execute(
            "INSERT INTO telegram_baglantilari (kullanici_adi, chat_id) VALUES (%s, %s)",
            [satir[0], chat_id],
        )
    return satir[0]


def sohbetin_kullanicisi(conn: psycopg.Connection, chat_id: int) -> Kullanici | None:
    satir = conn.execute(
        "SELECT k.kullanici_adi, k.ad_soyad, k.rol FROM telegram_baglantilari b "
        "JOIN kullanicilar k USING (kullanici_adi) WHERE b.chat_id = %s",
        [chat_id],
    ).fetchone()
    return Kullanici(*satir) if satir else None


def baglantiyi_sil(conn: psycopg.Connection, kullanici_adi: str) -> None:
    """Kullanıcının Telegram bağlantısını ve bekleyen kodlarını siler (web'den ya da /cikis)."""
    with conn.transaction():
        conn.execute("DELETE FROM telegram_baglantilari WHERE kullanici_adi = %s", [kullanici_adi])
        conn.execute("DELETE FROM telegram_kodlari WHERE kullanici_adi = %s", [kullanici_adi])


def bagli_mi(conn: psycopg.Connection, kullanici_adi: str) -> bool:
    sorgu = "SELECT EXISTS (SELECT 1 FROM telegram_baglantilari WHERE kullanici_adi = %s)"
    return conn.execute(sorgu, [kullanici_adi]).fetchone()[0]


# --- Biçim -------------------------------------------------------------------------------


def _kacir(metin: str) -> str:
    """Telegram HTML'i için yalnızca <, > ve & kaçırılır. Tırnaklar da kaçırılırsa (html.escape
    varsayılanı) mesajda "OEE&#x27;si" görünebiliyor."""
    return html.escape(metin, quote=False)


_YER_TUTUCU = ""  # Unicode özel kullanım alanı; girdiden önce silinir


def telegram_html(metin: str) -> str:
    """Agent'ın kısıtlı Markdown'ını Telegram HTML'ine çevirir. Önce bütün metin kaçırılır;
    modelin ya da kılavuzun ürettiği hiçbir etiket Telegram'a etiket olarak gitmez.

    Etiketler her zaman doğru iç içe geçer; Telegram yanlış iç içe geçmiş HTML'i ("can't parse
    entities") reddeder ve cevap hiç gitmez. Bunun için kod parçaları önce yer tutucuya alınır
    (içleri biçimlenmez), kalın ve italik de bir etiketin sınırını aşamaz: `**a *b** c*` gibi
    çakışan işaretlerde italik uygulanmaz, yıldızlar metinde kalır."""
    kodlar: list[str] = []

    def kodu_sakla(m: re.Match) -> str:
        kodlar.append(f"<code>{m.group(1)}</code>")
        return f"{_YER_TUTUCU}{len(kodlar) - 1}{_YER_TUTUCU}"

    satirlar = []
    for satir in _kacir(metin.replace(_YER_TUTUCU, "")).splitlines():
        satir = re.sub(r"`([^`\n]+)`", kodu_sakla, satir)
        baslik = re.fullmatch(r"\s*#{1,6}\s+(.+)", satir)
        if baslik:  # başlık zaten kalın; içindeki ** işaretleri atılır
            satirlar.append(f"<b>{baslik.group(1).replace('**', '')}</b>")
            continue
        # Alt maddeler (girintili) girintili ve farklı işaretle kalsın.
        satir = re.sub(r"^([ \t]*)[-*•]\s+", lambda m: "    ◦ " if m.group(1) else "• ", satir)
        satir = re.sub(r"\*\*([^<>]+?)\*\*", r"<b>\1</b>", satir)
        satir = re.sub(r"(?<![*\w])\*(?![\s*])([^*<>]+?)(?<!\s)\*(?![*\w])", r"<i>\1</i>", satir)
        satirlar.append(satir)
    return re.sub(
        f"{_YER_TUTUCU}(\\d+){_YER_TUTUCU}", lambda m: kodlar[int(m.group(1))], "\n".join(satirlar)
    )


def cevap_metni(sonuc: agent.SohbetSonucu) -> str:
    metin = telegram_html(sonuc.cevap or "(boş cevap)")
    kaynaklar = dict.fromkeys(f"{p['dokuman_kodu']} s. {p['sayfa']}" for p in sonuc.kaynaklar)
    if kaynaklar:
        metin += f"\n\n<i>Kaynaklar: {_kacir(', '.join(kaynaklar))}</i>"
    return metin


def _isik(oran: float | None) -> str:
    """Andon renkleri: %85+ yeşil, %65-85 sarı, altı kırmızı."""
    if oran is None:
        return "⚪"
    return "🟢" if oran >= 0.85 else "🟡" if oran >= 0.65 else "🔴"


def _kisalt(metin: str, sinir: int) -> str:
    metin = " ".join(metin.split())
    return metin if len(metin) <= sinir else metin[: sinir - 1].rsplit(" ", 1)[0] + "…"


def rapor_ozeti(r: dict) -> str:
    """Vardiya raporunun Telegram'a sığan özeti (tamamı web arayüzünde)."""
    f = r["fabrika"]
    satirlar = [
        f"<b>Vardiya raporu: {r['tarih']:%d.%m.%Y}, {r['vardiya']}. vardiya</b>",
        f"{_isik(f['oee'])} Fabrika OEE {yuzde_metni(f['oee'])} (kullanılabilirlik "
        f"{yuzde_metni(f['kullanilabilirlik'])}, performans {yuzde_metni(f['performans'])}, kalite "
        f"{yuzde_metni(f['kalite'])})",
        "",
        _kacir(r["yorum"]["ozet"]),
    ]
    for baslik, maddeler in (
        ("Dikkat", r["yorum"]["dikkat"]),
        ("Öneriler", r["yorum"]["oneriler"]),
    ):
        if maddeler:
            satirlar += ["", f"<b>{baslik}</b>", *(f"• {_kacir(m)}" for m in maddeler)]
    satirlar += ["", "<b>En düşük hatlar</b>"]
    for h in r["hatlar"][:3]:
        yedi = (
            f" (son 7 gün {yuzde_metni(h['son_7_gun_oee'])})"
            if h["son_7_gun_oee"] is not None
            else ""
        )
        satirlar.append(f"{_isik(h['oee'])} {_kacir(h['hat'])}: {yuzde_metni(h['oee'])}{yedi}")
    satirlar += [
        "",
        f"Vardiyada başlayan arıza: {len(r['arizalar'])} · açık bakım talebi: "
        f"{r['acik_talep_sayisi']}",
        "<i>Yorum "
        + ("asistandan, sayı denetiminden geçti" if r["yorum_kaynagi"] == "llm" else "kurallarla")
        + ". Ayrıntılar web arayüzünde.</i>",
    ]
    return "\n".join(satirlar)


# --- Bot ---------------------------------------------------------------------------------


class Bot:
    def __init__(
        self,
        api: Telegram,
        conn: psycopg.Connection,
        embedder: Embedder,
        llm_getir: Callable[[], LLM | None],
        bildirim_ayrintisi: BildirimAyrintisi = "ayrintili",
        fotograf_saklama_gun: int = 90,
        saat: Callable[[], float] = time.monotonic,
        simdi: Callable[[], datetime] = lambda: datetime.now(TZ),
    ):
        self.api, self.conn, self.embedder, self.llm_getir = api, conn, embedder, llm_getir
        self.bildirim_ayrintisi = bildirim_ayrintisi
        self.fotograf_saklama_gun = fotograf_saklama_gun
        self.saat, self.simdi = saat, simdi
        self._yanlis_kodlar: dict[int, deque[float]] = defaultdict(deque)
        self._sorular: dict[int, deque[float]] = defaultdict(deque)
        self.son_ariza_id: int | None = None  # bildirilen son arıza; ilk turda belirlenir
        # Görülmüş ama henüz hattı durdurmamış açık arızalar: MES hat_durdu'yu sonradan
        # true yaparsa o turda bildirilir.
        self._izlenen: set[int] = set()

    def _gonder(self, chat_id: int, metin: str) -> None:
        self.api.mesaj_gonder(chat_id, metin)

    def islenemedi(self, guncelleme: dict) -> None:
        """Mesaj beklenmedik bir hatayla işlenemediyse kullanıcıya söyler; sessiz kalmasın."""
        mesaj = guncelleme.get("message") or {}
        if mesaj.get("chat", {}).get("type") != "private":
            return
        try:
            self._gonder(mesaj["chat"]["id"], "Mesajınız işlenemedi; biraz sonra tekrar deneyin.")
        except TelegramHatasi:
            pass

    def _yaziyor(self, chat_id: int) -> None:
        try:
            self.api.yaziyor(chat_id)
        except TelegramHatasi:
            pass  # "yazıyor…" göstergesi gitmese de cevap gider

    @staticmethod
    def _pencerede(zamanlar: deque[float], simdi: float, pencere: float) -> int:
        while zamanlar and simdi - zamanlar[0] > pencere:
            zamanlar.popleft()
        return len(zamanlar)

    def guncellemeyi_isle(self, guncelleme: dict) -> None:
        mesaj = guncelleme.get("message")
        if not mesaj or mesaj.get("chat", {}).get("type") != "private":
            return  # yalnızca özel sohbetler
        chat_id = mesaj["chat"]["id"]
        metin = (mesaj.get("text") or mesaj.get("caption") or "").replace("\x00", "").strip()
        ilk, _, arguman = metin.partition(" ")
        komut = ilk.split("@")[0].lower() if ilk.startswith("/") else ""
        arguman = arguman.strip()

        if komut in ("/baglan", "/start") and arguman:  # /start KOD: tek tıkla bağlantı
            return self._baglan(chat_id, arguman)
        kullanici = sohbetin_kullanicisi(self.conn, chat_id)
        if kullanici is None:
            return self._gonder(chat_id, TANITIM)

        komutlar = {
            "/start": lambda: self._gonder(chat_id, yardim_metni(kullanici)),
            "/yardim": lambda: self._gonder(chat_id, yardim_metni(kullanici)),
            "/help": lambda: self._gonder(chat_id, yardim_metni(kullanici)),
            "/cikis": lambda: self._cikis(chat_id, kullanici),
            "/gizlilik": lambda: self._gizlilik(chat_id),
            "/rapor": lambda: self._rapor(chat_id, kullanici),
            "/oee": lambda: self._oee(chat_id, arguman),
            "/plan": lambda: self._plan(chat_id, kullanici, arguman),
        }
        if komut:
            eylem = komutlar.get(komut)
            if eylem is None:
                return self._gonder(chat_id, "Bu komutu tanımıyorum.\n\n" + yardim_metni(kullanici))
            return eylem()
        if mesaj.get("photo"):
            return self._fotografli_talep(chat_id, kullanici, mesaj, metin)
        if not metin:
            return self._gonder(chat_id, "Yalnızca yazı ve fotoğraf anlayabiliyorum.")
        self._soru(chat_id, kullanici, metin[:2000])

    def _baglan(self, chat_id: int, kod: str) -> None:
        yanlislar = self._yanlis_kodlar[chat_id]
        simdi = self.saat()
        if self._pencerede(yanlislar, simdi, KOD_GECERLILIK.total_seconds()) >= EN_FAZLA_YANLIS_KOD:
            return self._gonder(
                chat_id, "Çok fazla yanlış kod denendi. 10 dakika sonra yeni bir kodla deneyin."
            )
        ad = (
            eslestir(self.conn, kod, chat_id, self.simdi()) if re.fullmatch(r"\d{6}", kod) else None
        )
        kullanici = sohbetin_kullanicisi(self.conn, chat_id) if ad else None
        if kullanici is None:
            yanlislar.append(simdi)
            return self._gonder(
                chat_id, "Kod geçersiz ya da süresi dolmuş. Web arayüzünden yeni bir kod alın."
            )
        yanlislar.clear()
        self._gonder(
            chat_id,
            f"Bağlandı: <b>{_kacir(kullanici.ad_soyad)}</b> "
            f"({ROL_ADLARI[kullanici.rol]}).\n\n<i>Bu sohbet uçtan uca şifreli değildir. Neyin "
            "saklandığını /gizlilik ile görebilir, bağlantıyı /cikis ile silebilirsiniz.</i>"
            f"\n\n{yardim_metni(kullanici)}",
        )

    def _cikis(self, chat_id: int, kullanici: Kullanici) -> None:
        baglantiyi_sil(self.conn, kullanici.kullanici_adi)
        self._gonder(
            chat_id, "Bağlantınız ve bekleyen kodlarınız silindi. Artık bildirim almayacaksınız."
        )

    def _gizlilik(self, chat_id: int) -> None:
        llm = self.llm_getir()
        self._gonder(
            chat_id, gizlilik_metni(llm.saglayici if llm else None, self.fotograf_saklama_gun)
        )

    def _sinirda_mi(self, chat_id: int) -> bool:
        """Soru sınırı aşıldıysa kullanıcıya söyler ve True döner; aşılmadıysa sayar."""
        zamanlar, simdi = self._sorular[chat_id], self.saat()
        if self._pencerede(zamanlar, simdi, SORU_PENCERESI_SN) >= SORU_SINIRI:
            self._gonder(
                chat_id, f"Dakikada en fazla {SORU_SINIRI} soru sorulabilir; biraz bekleyin."
            )
            return True
        zamanlar.append(simdi)
        return False

    def _llm(self, chat_id: int) -> LLM | None:
        """Ayarlı LLM; yoksa kullanıcıya söyler ve None döner."""
        llm = self.llm_getir()
        if llm is None:
            self._gonder(chat_id, "Asistan şu an ayarlı değil (LLM anahtarı tanımlı değil).")
        return llm

    def _agent(
        self, chat_id: int, kullanici: Kullanici, soru: str, llm: LLM
    ) -> tuple[agent.SohbetSonucu | None, AracBaglami]:
        """Agent'ı kullanıcının rolüyle çalıştırıp kaydeder. Hata olursa kullanıcıya söyler."""
        baglam = AracBaglami(
            conn=self.conn, embedder=self.embedder, kullanici=kullanici, simdi=self.simdi()
        )
        self._yaziyor(chat_id)
        try:
            sonuc = agent.sohbet(soru, llm, baglam)
        except agent.SohbetHatasi as hata:
            agent.kaydet(self.conn, soru, kullanici.kullanici_adi, llm.saglayici, hata.sonuc)
            self._gonder(
                chat_id, "Asistan şu an cevap veremiyor (kota ya da bağlantı). Biraz sonra deneyin."
            )
            return None, baglam
        agent.kaydet(self.conn, soru, kullanici.kullanici_adi, llm.saglayici, sonuc)
        return sonuc, baglam

    def _soru(self, chat_id: int, kullanici: Kullanici, soru: str) -> None:
        if self._sinirda_mi(chat_id) or (llm := self._llm(chat_id)) is None:
            return
        sonuc, _ = self._agent(chat_id, kullanici, soru, llm)
        if sonuc is not None:
            self._gonder(chat_id, cevap_metni(sonuc))

    def _fotografli_talep(
        self, chat_id: int, kullanici: Kullanici, mesaj: dict, aciklama: str
    ) -> None:
        if not aciklama:
            return self._gonder(
                chat_id,
                "Fotoğrafın altına makine kodunu ve sorunu yazın, örneğin: "
                "<i>P3-HP'de yağ kaçağı var</i>.",
            )
        # LLM yoksa fotoğraf boşuna indirilmesin (5 MB'a kadar).
        if self._sinirda_mi(chat_id) or (llm := self._llm(chat_id)) is None:
            return
        foto = mesaj["photo"][-1]  # Telegram aynı fotoğrafın boyutlarını küçükten büyüğe verir
        if foto.get("file_size", 0) > EN_BUYUK_FOTOGRAF:
            return self._gonder(chat_id, "Fotoğraf çok büyük (en fazla 5 MB).")
        try:
            icerik = self.api.dosya_indir(foto["file_id"], EN_BUYUK_FOTOGRAF)
        except TelegramHatasi:
            return self._gonder(chat_id, "Fotoğraf indirilemedi; tekrar gönderin.")

        soru = (
            f"Sahadan fotoğraflı bildirim: {aciklama[:500]}\n"
            "Bu sorun için bakım talebi aç; öncelik belirtilmemişse sorunun ciddiyetine göre "
            "seç. Makine kodu anlaşılmıyorsa talep açma, makine kodunu sor."
        )
        sonuc, baglam = self._agent(chat_id, kullanici, soru, llm)
        if sonuc is None:
            return
        if baglam.acilan_talepler:
            talep_id = baglam.acilan_talepler[0]
            self.conn.execute(
                "INSERT INTO talep_fotograflari (talep_id, icerik, mime) VALUES (%s, %s, %s)",
                [talep_id, icerik, "image/jpeg"],
            )
            ek = f"\n\n📷 Fotoğraf #{talep_id} numaralı talebe eklendi."
        else:
            ek = "\n\nTalep açılmadı; fotoğrafı makine koduyla birlikte tekrar gönderin."
        self._gonder(chat_id, cevap_metni(sonuc) + ek)

    def _rapor(self, chat_id: int, kullanici: Kullanici) -> None:
        baslangic = sorgular.son_vardiya_baslangici(self.conn)
        if baslangic is None:
            return self._gonder(chat_id, "Henüz tamamlanmış bir vardiya yok.")
        self._yaziyor(chat_id)
        llm = self.llm_getir()
        r, yorum_sonucu = rapor.rapor_olustur(self.conn, llm, baslangic)
        rapor.kaydet(self.conn, kullanici.kullanici_adi, llm, r, yorum_sonucu)
        self._gonder(chat_id, rapor_ozeti(r))

    def _oee(self, chat_id: int, hat: str) -> None:
        bugun: date = self.simdi().date()
        aralik = {"baslangic": bugun - timedelta(days=6), "bitis": bugun}
        if hat:
            if hata := sorgular.hat_hatasi(self.conn, hat):
                return self._gonder(chat_id, _kacir(hata))
            ad = sorgular.hat_adini_bul(self.conn, hat)
            (t,) = sorgular.oee_hesapla(self.conn, hat=ad, **aralik)
            pareto = sorgular.durus_pareto(self.conn, hat=ad, **aralik)
            satirlar = [
                f"<b>{_kacir(ad)}, son 7 gün</b>",
                f"{_isik(t['oee'])} OEE {yuzde_metni(t['oee'])}",
                f"Kullanılabilirlik {yuzde_metni(t['kullanilabilirlik'])} · performans "
                f"{yuzde_metni(t['performans'])} · kalite {yuzde_metni(t['kalite'])}",
            ]
            if pareto:
                d = pareto[0]
                satirlar.append(f"En büyük duruş: {rapor.durus_adi(d)}, {d['sure_dk']} dk")
            return self._gonder(chat_id, "\n".join(satirlar))
        hatlar = sorgular.oee_hesapla(self.conn, grup="hat", **aralik)
        hatlar.sort(key=lambda h: h["oee"] or 0)
        satirlar = ["<b>Son 7 günde hatların OEE'si</b>"] + [
            f"{_isik(h['oee'])} {_kacir(h['hat'])}: {yuzde_metni(h['oee'])}" for h in hatlar
        ]
        self._gonder(chat_id, "\n".join(satirlar))

    def _plan(self, chat_id: int, kullanici: Kullanici, arguman: str) -> None:
        if kullanici.rol != "bakim":
            return self._gonder(chat_id, "Bakım planı yalnızca bakım mühendislerine açık.")
        # isdigit() değil: "²" isdigit() için rakamdır ama int() onu çeviremez.
        kapasite = int(arguman) if re.fullmatch(r"[0-9]{1,3}", arguman) else 16
        if not 1 <= kapasite <= 200:
            kapasite = 16
        plan = bakim_plani.bakim_plani(self.conn, self.simdi(), kapasite_saat=kapasite)
        satirlar = [f"<b>Bu haftanın bakım planı</b> ({kapasite} saat)"]
        for r in (r for r in plan["makineler"] if r["secildi"]):
            not_ = " · şu an arızalı, tamirden sonra" if r["su_an_arizali"] else ""
            satirlar.append(
                f"• <b>{r['makine_kodu']}</b> {_kacir(r['makine_adi'])} ({r['hat']}): "
                f"{r['bakim_saat']} sa, arıza olasılığı %{r['ariza_olasiligi'] * 100:.0f}, "
                f"önlenmesi beklenen ~{r['kazanc_dk']:.0f} dk{not_}"
            )
        satirlar.append(
            f"\nToplam {plan['secilen_saat']} saat, önlenmesi beklenen duruş "
            f"~{plan['onlenen_durus_dk']:.0f} dk. Ayrıntılar web arayüzünde."
        )
        self._gonder(chat_id, "\n".join(satirlar))

    # --- Arıza bildirimi ---------------------------------------------------------------

    def bildirimleri_gonder(self) -> int:
        """Son turdan beri başlayan ya da sonradan hattı durdurduğu kaydedilen arızaları
        eşleşmiş herkese bildirir; gönderilen mesaj sayısını döner. İlk turda yalnızca
        başlangıç noktasını belirler (geçmiş arızalar bildirilmez). Seed yeniden kurulduysa
        (en büyük id küçüldüyse) de. Her arıza en fazla bir kez bildirilir."""
        son = self.conn.execute("SELECT coalesce(max(id), 0) FROM ariza_kayitlari").fetchone()[0]
        onceki = self.son_ariza_id
        if onceki is None or son < onceki:
            self.son_ariza_id, self._izlenen = son, set()
            return 0
        if son == onceki and not self._izlenen:
            return 0
        with self.conn.cursor() as cur:
            # Üst sınır `son`: max(id)'den sonra eklenen arıza bu turda bildirilirse bir sonraki
            # turda (id > son) ikinci kez bildirilirdi.
            cur.execute(
                """
                SELECT a.id, a.hat_durdu, a.bitis IS NULL, a.baslangic, a.ariza_tipi, a.onem,
                       a.aciklama, m.kod, m.ad, h.ad
                FROM ariza_kayitlari a
                JOIN makineler m ON m.id = a.makine_id
                JOIN hatlar h    ON h.id = m.hat_id
                WHERE (a.id > %s AND a.id <= %s) OR a.id = ANY(%s)
                ORDER BY a.id
                """,
                [onceki, son, list(self._izlenen)],
            )
            satirlar = cur.fetchall()
        # Durum göndermeden önce ilerler: gönderim yarıda kesilirse tekrar değil eksik bildirim.
        self.son_ariza_id = son
        self._izlenen = {s[0] for s in satirlar if not s[1] and s[2]}
        arizalar = [(s[0], *s[3:]) for s in satirlar if s[1]]
        if not arizalar:
            return 0
        alicilar = self.conn.execute(
            "SELECT b.chat_id, k.rol FROM telegram_baglantilari b "
            "JOIN kullanicilar k USING (kullanici_adi) ORDER BY b.chat_id"
        ).fetchall()
        gonderilen = 0
        # Kılavuz alıntısı ve stok yalnızca bakım rolüne ve yalnızca "ayrintili" modda gider.
        ayrintili = self.bildirim_ayrintisi == "ayrintili"
        for ariza in arizalar:
            mesajlar = {
                "bakim": self._ariza_mesaji(ariza, ayrintili=ayrintili),
                "operator": self._ariza_mesaji(ariza, ayrintili=False),
            }
            for chat_id, rol in alicilar:
                try:
                    self._gonder(chat_id, mesajlar[rol])
                    gonderilen += 1
                except TelegramHatasi:
                    pass  # kullanıcı botu engellemiş olabilir; diğerlerine devam
        return gonderilen

    def _ariza_mesaji(self, ariza: tuple, ayrintili: bool) -> str:
        _, baslangic, tip, onem, aciklama, kod, makine_adi, hat = ariza
        metin = (
            f"🔴 <b>{_kacir(hat)} durdu</b> ({baslangic.astimezone(TZ):%H:%M})\n"
            f"{kod} {_kacir(makine_adi)}: {tip} arıza, {ONEM_ADLARI[onem]} önem\n"
            f"<i>{_kacir(aciklama)}</i>"
        )
        if not ayrintili:
            return metin + "\n\nBakım ekibine bildirildi. Ayrıntılar web arayüzünde."
        try:
            parcalar = rag.dokuman_ara(
                self.conn,
                self.embedder,
                f"{makine_adi} {tip} arıza: {aciklama}",
                1,
                erisim=ROL_ERISIMI["bakim"],
            )
        except psycopg.Error:
            parcalar = []  # kılavuzlar yüklenmemiş olabilir; bildirim yine gitsin
        if parcalar:
            p = parcalar[0]
            metin += (
                f"\n\n📖 <b>Kılavuz ({p['dokuman_kodu']}, s. {p['sayfa']})</b>\n"
                f"{_kacir(_kisalt(p['icerik'], 300))}"
            )
        stok = [s for s in sorgular.stok_getir(self.conn) if s["kategori"] == tip][:4]
        if stok:
            metin += "\n\n📦 <b>İlgili yedek parçalar</b>\n" + "\n".join(
                f"• {s['parca_kodu']} {_kacir(s['ad'])}: {s['miktar']} {s['birim']}"
                + (" ⚠️ minimumun altında" if s["kritik"] else "")
                for s in stok
            )
        return metin

    # --- Saklama süresi ----------------------------------------------------------------

    def eski_verileri_temizle(self) -> tuple[int, int]:
        """Süresi geçmiş kodları ve saklama süresini aşan fotoğrafları siler; sınır penceresi
        geçmiş sohbetleri bellekten atar. (silinen kod, silinen fotoğraf) döner."""
        an = self.saat()
        for zamanlar, pencere in (
            (self._yanlis_kodlar, KOD_GECERLILIK.total_seconds()),
            (self._sorular, SORU_PENCERESI_SN),
        ):
            for chat_id in [c for c, z in zamanlar.items() if not self._pencerede(z, an, pencere)]:
                del zamanlar[chat_id]
        simdi = self.simdi()
        kodlar = self.conn.execute(
            "DELETE FROM telegram_kodlari WHERE son_gecerlilik < %s", [simdi]
        ).rowcount
        fotograflar = self.conn.execute(
            "DELETE FROM talep_fotograflari WHERE eklenme < %s",
            [simdi - timedelta(days=self.fotograf_saklama_gun)],
        ).rowcount
        return kodlar, fotograflar
