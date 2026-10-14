// Telegram eşleştirme penceresi: kod alma, bağlantı durumu, bağlantıyı kaldırma, gizlilik.
// Eşleştirme bir kez yapılır ve kalıcıdır; bağlıyken pencere yalnızca durumu gösterir, üst
// çubuktaki düğme de "Telegram ✓" olur.

const $ = (id) => document.getElementById(id);

// Kod alındıktan sonra kullanıcı Telegram'da kodu gönderene kadar durum bu aralıkla yoklanır.
const YOKLAMA_MS = 3000;

export function telegramPenceresi({ api }) {
  const pencere = $("telegram-penceresi");
  let yoklama = null;

  $("telegram-dugmesi").addEventListener("click", ac);
  $("telegram-kod-al").addEventListener("click", kodAl);
  $("telegram-kaldir").addEventListener("click", kaldir);
  pencere.addEventListener("close", yoklamayiDurdur);

  function hata(metin) {
    $("telegram-hata").textContent = metin ?? "";
    $("telegram-hata").hidden = !metin;
  }

  function yoklamayiDurdur() {
    clearInterval(yoklama);
    yoklama = null;
  }

  function dugmeyiGuncelle(bagli) {
    const dugme = $("telegram-dugmesi");
    dugme.textContent = bagli ? "Telegram ✓" : "Telegram";
    dugme.setAttribute("aria-label", bagli ? "Telegram: bağlı" : "Telegram: bağlı değil");
  }

  async function ac() {
    hata(null);
    $("telegram-kod-alani").hidden = true;
    $("telegram-kod-al").textContent = "Kod al";
    pencere.showModal();
    await durumuYukle();
  }

  // Durumu okur, pencereyi ve üst çubuktaki düğmeyi günceller; bağlı olup olmadığını döner.
  async function durumuYukle({ sessiz = false } = {}) {
    try {
      const cevap = await api("/telegram/durum");
      if (!cevap.ok) throw new Error(`HTTP ${cevap.status}`);
      const d = await cevap.json();
      const bot = d.bot_kullanici_adi ? `@${d.bot_kullanici_adi}` : null;
      dugmeyiGuncelle(d.bagli);
      $("telegram-durum").textContent = d.bagli
        ? "Bu hesap Telegram'a bağlı."
        : "Bu hesap henüz Telegram'a bağlı değil.";
      $("telegram-bagli").hidden = !d.bagli;
      $("telegram-eslestirme").hidden = d.bagli;
      $("telegram-kod-al").hidden = d.bagli;
      $("telegram-kaldir").hidden = !d.bagli;
      $("telegram-bagli-bot").textContent = bot ?? "Telegram";
      $("telegram-bot-adi").textContent = bot ?? "fabrikanızın Hata Asistanı botunu";
      $("telegram-gizlilik-bildirim").textContent =
        d.bildirim_ayrintisi === "ayrintili"
          ? "Arıza bildirimlerinde bakım rolüne kılavuz alıntısı ve stok bilgisi gider; operatöre yalnızca hattın durduğu."
          : "Arıza bildirimlerinde yalnızca hattın durduğu bildirilir; kılavuz ve stok bilgisi gönderilmez.";
      $("telegram-gizlilik-saklama").textContent =
        `Telegram'dan gönderilen talep fotoğrafları ${d.fotograf_saklama_gun} gün sonra silinir.`;
      return d.bagli;
    } catch (e) {
      if (e.name !== "OturumBitti" && !sessiz) hata("Durum alınamadı; tekrar deneyin.");
      return false;
    }
  }

  async function kodAl() {
    hata(null);
    // Çift tıklama iki kod üretmesin: ikinci kod ilkini geçersiz kılar.
    const dugme = $("telegram-kod-al");
    dugme.disabled = true;
    dugme.textContent = "Kod alınıyor…";
    try {
      const cevap = await api("/telegram/kod", { method: "POST" });
      if (!cevap.ok) throw new Error(`HTTP ${cevap.status}`);
      const k = await cevap.json();
      $("telegram-kod").textContent = k.kod;
      $("telegram-komut").textContent = `/baglan ${k.kod}`;
      $("telegram-sure").textContent = `${k.gecerlilik_dk} dakika geçerli, tek kullanımlık.`;
      const baglanti = $("telegram-ac");
      baglanti.hidden = !k.baglanti;
      if (k.baglanti) baglanti.href = k.baglanti;
      $("telegram-kod-alani").hidden = false;
      bagliOlanaKadarYokla(k.gecerlilik_dk * 60_000);
    } catch (e) {
      if (e.name !== "OturumBitti") hata("Kod alınamadı; tekrar deneyin.");
    } finally {
      dugme.disabled = false;
      dugme.textContent = "Yeni kod al";
    }
  }

  // Kullanıcı kodu Telegram'da gönderince pencere kendiliğinden "Bağlı" olur; kod süresi
  // dolunca ya da pencere kapanınca yoklama durur.
  function bagliOlanaKadarYokla(sureMs) {
    yoklamayiDurdur();
    const bitis = Date.now() + sureMs;
    yoklama = setInterval(async () => {
      if (Date.now() > bitis) return yoklamayiDurdur();
      if (await durumuYukle({ sessiz: true })) {
        yoklamayiDurdur();
        $("telegram-kod-alani").hidden = true;
      }
    }, YOKLAMA_MS);
  }

  async function kaldir() {
    const onay = confirm(
      "Telegram bağlantısı kaldırılsın mı? Arıza bildirimleri bu sohbete gelmez; yeniden " +
        "bağlanmak için yeni bir kod gerekir.",
    );
    if (!onay) return;
    hata(null);
    try {
      const cevap = await api("/telegram/baglanti", { method: "DELETE" });
      if (!cevap.ok) throw new Error(`HTTP ${cevap.status}`);
      $("telegram-kod-alani").hidden = true;
      await durumuYukle();
    } catch (e) {
      if (e.name !== "OturumBitti") hata("Bağlantı kaldırılamadı; tekrar deneyin.");
    }
  }

  // Girişten sonra üst çubuktaki düğme bağlantı durumunu göstersin (pencere açılmadan).
  return {
    durumuYenile: () => durumuYukle({ sessiz: true }),
  };
}
