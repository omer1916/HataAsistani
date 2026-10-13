// Hata Asistanı sohbet ekranı. Çerçeve yok; API'ye fetch ile konuşur.

import { guvenliMarkdown } from "./metin.js";
import { oeeEkrani } from "./oee.js";
import { bakimEkrani } from "./bakim.js";
import { raporEkrani } from "./rapor.js";
import { telegramPenceresi } from "./telegram.js";

const OTURUM_ANAHTARI = "andon.oturum";
const ROL_ADLARI = { operator: "Operatör", bakim: "Bakım mühendisi" };
const ORNEKLER = {
  operator: [
    "Panelde A102 alarmı çıktı, ne yapmalıyım?",
    "Bu ay Kaynak 2 hattında kaç arıza oldu?",
    "Boya kalınlığının kabul aralığı nedir?",
    "P3-HP'de hidrolik yağ kaçağı var, yüksek öncelikli bakım talebi açar mısın?",
  ],
  bakim: [
    "Pres 3 hattında geçen ay kaç arıza oldu, bu tip arızada ilk neye bakmalıyım?",
    "P3-HP'de geçen ay kaç hidrolik arıza oldu, kök neden analizi gerekiyor mu?",
    "Basınç sensörünü değiştirmem gerekiyor; hangi parça ve stokta var mı?",
    "Minimum stok seviyesinin altındaki parçalar hangileri?",
  ],
};

const $ = (id) => document.getElementById(id);
const sayi = new Intl.NumberFormat("tr-TR");
const para = new Intl.NumberFormat("tr-TR", { style: "currency", currency: "USD", maximumFractionDigits: 4 });

class OturumBitti extends Error {
  name = "OturumBitti";
}

const EKRANLAR = {
  sohbet: "sohbet-ekrani",
  oee: "oee-ekrani",
  rapor: "rapor-ekrani",
  plan: "plan-ekrani",
};

// Açık ekran adreste tutulur (#oee, #rapor...): sayfa yenilenince ya da bağlantı
// paylaşılınca aynı ekran açılır. Ekranın kendi durumu "?" sonrasındadır (OEE filtresi).
function adresinEkrani() {
  const ad = location.hash.slice(1).split("?")[0];
  return Object.hasOwn(EKRANLAR, ad) ? ad : null;
}

let oturum = oturumuOku();
let bekleniyor = false;
let aktifEkran = adresinEkrani() ?? "sohbet";
const rapor = raporEkrani({ api, eleman, rol: () => oturum?.rol });
const bakim = bakimEkrani({ api, eleman });
telegramPenceresi({ api });
const oee = oeeEkrani({
  api,
  eleman,
  asistanaSor(metin) {
    ekranSec("sohbet");
    sor(metin);
  },
});

// --- Oturum -----------------------------------------------------------------

function oturumuOku() {
  try {
    return JSON.parse(sessionStorage.getItem(OTURUM_ANAHTARI));
  } catch {
    return null;
  }
}

function oturumuYaz(yeni) {
  oturum = yeni;
  try {
    if (yeni) sessionStorage.setItem(OTURUM_ANAHTARI, JSON.stringify(yeni));
    else sessionStorage.removeItem(OTURUM_ANAHTARI);
  } catch {
    // Gizli pencere gibi depolamanın kapalı olduğu durumlarda oturum yalnızca bellekte kalır.
  }
}

function cikisYap(mesaj) {
  oturumuYaz(null);
  $("mesajlar").replaceChildren();
  aktifEkran = "sohbet";
  ekraniGoster();
  if (mesaj) girisHatasiGoster(mesaj);
}

async function api(yol, secenekler = {}) {
  const basliklar = { ...(secenekler.headers ?? {}) };
  if (oturum) basliklar.Authorization = `Bearer ${oturum.token}`;
  const cevap = await fetch(yol, { ...secenekler, headers: basliklar });
  if (cevap.status === 401 && oturum) {
    cikisYap("Oturumun süresi doldu; yeniden giriş yap.");
    throw new OturumBitti();
  }
  return cevap;
}

// --- Ekranlar ---------------------------------------------------------------

function ekraniGoster() {
  const girisli = Boolean(oturum);
  $("giris-ekrani").hidden = girisli;
  $("oturum").hidden = !girisli;
  $("sekmeler").hidden = !girisli;
  if (!girisli) {
    for (const id of Object.values(EKRANLAR)) $(id).hidden = true;
    $("giris-formu").elements.username.focus();
    return;
  }
  $("ad-soyad").textContent = oturum.adSoyad;
  $("rol").textContent = ROL_ADLARI[oturum.rol];
  $("ornekler").replaceChildren(
    ...ORNEKLER[oturum.rol].map((metin) => {
      const dugme = eleman("button", "ornek", metin);
      dugme.type = "button";
      dugme.addEventListener("click", () => sor(metin));
      return dugme;
    }),
  );
  bosDurumuGuncelle();
  // Rol kısıtlı sekmeler (bakım planı) yalnızca o role görünür; asıl kontrol API'de (403).
  for (const sekme of $("sekmeler").querySelectorAll(".sekme[data-rol]")) {
    sekme.hidden = sekme.dataset.rol !== oturum.rol;
    if (sekme.hidden && sekme.dataset.ekran === aktifEkran) aktifEkran = "sohbet";
  }
  ekranSec(aktifEkran);
}

function ekranSec(ad) {
  aktifEkran = ad;
  if (adresinEkrani() !== ad) history.replaceState(null, "", `#${ad}`);
  for (const [ekran, id] of Object.entries(EKRANLAR)) $(id).hidden = ekran !== ad;
  for (const sekme of $("sekmeler").querySelectorAll(".sekme")) {
    if (sekme.dataset.ekran === ad) sekme.setAttribute("aria-current", "page");
    else sekme.removeAttribute("aria-current");
  }
  if (ad === "sohbet") $("soru").focus();
  if (ad === "oee") oee.goster();
  if (ad === "rapor") rapor.goster();
  if (ad === "plan") bakim.goster();
}

for (const sekme of $("sekmeler").querySelectorAll(".sekme")) {
  sekme.addEventListener("click", () => ekranSec(sekme.dataset.ekran));
}

// Adres çubuğuna elle #oee yazılırsa; rol kısıtlı ekranlar ekraniGoster'de yine elenir.
window.addEventListener("hashchange", () => {
  const ad = adresinEkrani();
  if (!oturum || !ad || ad === aktifEkran) return;
  aktifEkran = ad;
  ekraniGoster();
});

function girisHatasiGoster(metin) {
  const alan = $("giris-hatasi");
  alan.textContent = metin;
  alan.hidden = !metin;
}

$("giris-formu").addEventListener("submit", async (olay) => {
  olay.preventDefault();
  const form = olay.currentTarget;
  const dugme = $("giris-dugmesi");
  girisHatasiGoster("");
  dugme.disabled = true;
  dugme.textContent = "Giriş yapılıyor…";
  try {
    const cevap = await fetch("/giris", { method: "POST", body: new URLSearchParams(new FormData(form)) });
    if (!cevap.ok) {
      girisHatasiGoster(
        cevap.status === 401
          ? "Kullanıcı adı veya parola hatalı. Parola .env dosyasındaki DEMO_PAROLA."
          : "Giriş yapılamadı; biraz sonra tekrar deneyin.",
      );
      form.elements.password.select();
      return;
    }
    const veri = await cevap.json();
    oturumuYaz({ token: veri.access_token, adSoyad: veri.ad_soyad, rol: veri.rol });
    form.reset();
    ekraniGoster();
  } catch {
    girisHatasiGoster("Sunucuya ulaşılamadı; uygulamanın çalıştığını kontrol edin.");
  } finally {
    dugme.disabled = false;
    dugme.textContent = "Giriş yap";
  }
});

$("cikis").addEventListener("click", () => cikisYap());

// --- Sohbet -----------------------------------------------------------------

const soruAlani = $("soru");

soruAlani.addEventListener("input", boyutla);
soruAlani.addEventListener("keydown", (olay) => {
  if (olay.key === "Enter" && !olay.shiftKey && !olay.isComposing) {
    olay.preventDefault();
    $("soru-formu").requestSubmit();
  }
});

$("soru-formu").addEventListener("submit", (olay) => {
  olay.preventDefault();
  sor(soruAlani.value);
});

const SORU_EN_FAZLA_YUKSEKLIK = 180;

function boyutla() {
  soruAlani.style.height = "auto";
  const yukseklik = soruAlani.scrollHeight;
  soruAlani.style.height = `${Math.min(yukseklik, SORU_EN_FAZLA_YUKSEKLIK)}px`;
  soruAlani.style.overflowY = yukseklik > SORU_EN_FAZLA_YUKSEKLIK ? "auto" : "hidden";
}

async function sor(metin) {
  const soru = metin.trim();
  if (!soru || bekleniyor) return;
  bekleniyor = true;
  $("gonder").disabled = true;
  soruAlani.value = "";
  boyutla();

  mesajEkle("kullanici").append(eleman("p", null, soru));
  const bekleme = mesajEkle("asistan bekliyor");
  bekleme.append(eleman("span", "dusunuyor", "Kayıtlara ve kılavuzlara bakıyorum…"));

  try {
    const cevap = await api("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ soru }),
    });
    const veri = await cevap.json().catch(() => ({}));
    bekleme.remove();
    if (cevap.ok) asistanMesaji(veri);
    else hataMesaji(hataMetni(cevap.status, veri.detail));
  } catch (hata) {
    bekleme.remove();
    if (!(hata instanceof OturumBitti)) {
      hataMesaji("Sunucuya ulaşılamadı; bağlantıyı kontrol edip soruyu tekrar gönderin.");
    }
  } finally {
    bekleniyor = false;
    $("gonder").disabled = false;
    soruAlani.focus();
  }
}

function hataMetni(durum, ayrinti) {
  if (typeof ayrinti === "string") return ayrinti;
  if (durum === 422) return "Soru geçersiz; 2 ile 2000 karakter arasında olmalı.";
  return `Bir hata oluştu (HTTP ${durum}); soruyu tekrar gönderin, sürerse yöneticiye bildirin.`;
}

const hareketAzalt = window.matchMedia("(prefers-reduced-motion: reduce)");

function mesajEkle(tur) {
  const mesaj = eleman("li", `mesaj ${tur}`);
  $("mesajlar").append(mesaj);
  bosDurumuGuncelle();
  mesaj.scrollIntoView({ behavior: hareketAzalt.matches ? "auto" : "smooth", block: "end" });
  return mesaj;
}

function bosDurumuGuncelle() {
  $("bos-durum").hidden = $("mesajlar").children.length > 0;
}

function hataMesaji(metin) {
  mesajEkle("asistan hata").append(eleman("p", null, metin));
}

function asistanMesaji(veri) {
  const mesaj = mesajEkle("asistan");
  const govde = eleman("div", "govde");
  // Metin önce kaçış karakterlerine çevrilir (metin.js); modelin yazdığı HTML çalışamaz.
  govde.innerHTML = guvenliMarkdown(veri.cevap || "(boş cevap)"); // secscope: ignore SAST-XSS-001
  mesaj.append(govde);

  if (veri.kaynaklar.length) {
    const kaynaklar = eleman("div", "kaynaklar");
    kaynaklar.append(eleman("span", "etiket", "Kaynaklar"));
    for (const k of veri.kaynaklar) {
      const dugme = eleman("button", "kaynak", `${k.dokuman_kodu} · s. ${k.sayfa}`);
      dugme.type = "button";
      dugme.title = [k.dokuman_basligi, k.bolum].filter(Boolean).join(" — ");
      dugme.addEventListener("click", () => pdfAc(k.dokuman_kodu, k.sayfa));
      kaynaklar.append(dugme);
    }
    mesaj.append(kaynaklar);
  }

  const k = veri.kullanim;
  const ayrinti = eleman("details", "ayrinti");
  const ozet = [
    `${k.adim_sayisi} adım`,
    `${sayi.format(k.girdi_token + k.cikti_token)} token`,
    k.maliyet_usd == null ? null : para.format(k.maliyet_usd),
    `${(k.sure_ms / 1000).toLocaleString("tr-TR", { maximumFractionDigits: 1 })} sn`,
  ].filter(Boolean);
  ayrinti.append(eleman("summary", null, ozet.join(" · ")));
  const liste = eleman("ul", "araclar");
  for (const cagri of veri.arac_cagrilari) {
    const satir = eleman("li", cagri.hata ? "arac-hatasi" : null);
    satir.append(eleman("code", null, `${cagri.ad}(${argumanlar(cagri.argumanlar)})`));
    if (cagri.hata) satir.append(eleman("span", null, ` — ${cagri.hata}`));
    liste.append(satir);
  }
  if (!veri.arac_cagrilari.length) liste.append(eleman("li", null, "Araç kullanılmadı."));
  ayrinti.append(liste);
  mesaj.append(ayrinti);
}

function argumanlar(degerler) {
  if (typeof degerler !== "object" || degerler === null) return String(degerler);
  return Object.entries(degerler)
    .map(([ad, deger]) => `${ad}: ${JSON.stringify(deger)}`)
    .join(", ");
}

// PDF, token gerektirdiği için doğrudan bağlantıyla açılamaz: fetch ile indirilir ve sayfa
// içindeki görüntüleyicide istenen sayfada gösterilir. Yeni pencere açılmadığı için açılır
// pencere engelleyicisine takılmaz ve sohbet ekranı kaybolmaz.
let pdfAdresi = null;

async function pdfAc(kod, sayfa) {
  try {
    const cevap = await api(`/dokumanlar/${encodeURIComponent(kod)}/pdf`);
    if (!cevap.ok) throw new Error(`HTTP ${cevap.status}`);
    if (pdfAdresi) URL.revokeObjectURL(pdfAdresi);
    pdfAdresi = URL.createObjectURL(await cevap.blob());
    const adres = `${pdfAdresi}#page=${sayfa}`;
    $("pdf-baslik").textContent = `${kod} · sayfa ${sayfa}`;
    $("pdf-yeni-sekme").href = adres;
    $("pdf-cerceve").src = adres;
    $("pdf-gorunumu").showModal();
  } catch (hata) {
    if (!(hata instanceof OturumBitti)) hataMesaji(`${kod} açılamadı; kaynağa tekrar tıklayın.`);
  }
}

$("pdf-gorunumu").addEventListener("close", () => {
  $("pdf-cerceve").src = "about:blank";
});

// --- Yardımcılar ------------------------------------------------------------

function eleman(etiket, sinif, metin) {
  const el = document.createElement(etiket);
  if (sinif) el.className = sinif;
  if (metin != null) el.textContent = metin;
  return el;
}

ekraniGoster();
