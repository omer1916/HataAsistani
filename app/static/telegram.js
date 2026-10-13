// Telegram eşleştirme penceresi: kod alma, bağlantı durumu, bağlantıyı kaldırma, gizlilik.

const $ = (id) => document.getElementById(id);

export function telegramPenceresi({ api }) {
  const pencere = $("telegram-penceresi");

  $("telegram-dugmesi").addEventListener("click", ac);
  $("telegram-kod-al").addEventListener("click", kodAl);
  $("telegram-kaldir").addEventListener("click", kaldir);

  function hata(metin) {
    $("telegram-hata").textContent = metin ?? "";
    $("telegram-hata").hidden = !metin;
  }

  async function ac() {
    hata(null);
    $("telegram-kod-alani").hidden = true;
    $("telegram-kod-al").textContent = "Kod al";
    pencere.showModal();
    await durumuYukle();
  }

  async function durumuYukle() {
    try {
      const cevap = await api("/telegram/durum");
      if (!cevap.ok) throw new Error(`HTTP ${cevap.status}`);
      const d = await cevap.json();
      $("telegram-durum").textContent = d.bagli
        ? "Bu hesap bir Telegram sohbetine bağlı; arıza bildirimleri oraya gidiyor."
        : "Bu hesap henüz Telegram'a bağlı değil.";
      $("telegram-kaldir").hidden = !d.bagli;
      $("telegram-bot-adi").textContent = d.bot_kullanici_adi
        ? `@${d.bot_kullanici_adi}`
        : "fabrikanızın Hata Asistanı botunu";
      $("telegram-gizlilik-bildirim").textContent =
        d.bildirim_ayrintisi === "ayrintili"
          ? "Arıza bildirimlerinde bakım rolüne kılavuz alıntısı ve stok bilgisi gider; operatöre yalnızca hattın durduğu."
          : "Arıza bildirimlerinde yalnızca hattın durduğu bildirilir; kılavuz ve stok bilgisi gönderilmez.";
      $("telegram-gizlilik-saklama").textContent =
        `Telegram'dan gönderilen talep fotoğrafları ${d.fotograf_saklama_gun} gün sonra silinir.`;
    } catch (e) {
      if (e.name !== "OturumBitti") hata("Durum alınamadı; tekrar deneyin.");
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
    } catch (e) {
      if (e.name !== "OturumBitti") hata("Kod alınamadı; tekrar deneyin.");
    } finally {
      dugme.disabled = false;
      dugme.textContent = "Yeni kod al";
    }
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
}
