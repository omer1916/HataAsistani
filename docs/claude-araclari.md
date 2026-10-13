# Claude Code araçları

Bu projede Claude Code ile çalışırken kullanılan güvenlik ve tasarım araçları. Her araç kaynak
kodu okunarak seçildi; listede olup eklenmeyenler ve nedenleri en altta.

## Repoda olanlar: skill'ler (`.claude/skills/`)

Skill'ler düz Markdown talimatlarıdır; repoyu açan her Claude Code oturumu (bulut dahil) onları
kurulum yapmadan görür. Değiştirilmeden, aşağıdaki commit'lerden kopyalandı. Her klasörde kendi
lisans dosyası var.

| Skill'ler | Kaynak | Lisans | Ne için |
|---|---|---|---|
| `penetration-testing-with-strix`, `web-app-penetration-testing`, `api-security-testing`, `find-security-vulnerabilities-in-code`, `fix-security-vulnerabilities-with-strix`, `owasp-top-10-testing`, `application-security-testing`, `ci-security-scanning-with-strix`, `managed-pentesting-with-strix` | [usestrix/strix](https://github.com/usestrix/strix) `007ed1a` | Apache-2.0 | Strix'i çalıştırmak, bulguları düzeltmek, düzeltmeyi yeniden taramayla doğrulamak |
| `emil-design-eng`, `animate`, `review-animations`, `improve-animations`, `find-animation-opportunities`, `animation-vocabulary`, `apple-design`, `mobile-native` | [emilkowalski/skills](https://github.com/emilkowalski/skills) `d16ebe6` | MIT | Arayüz detayı, animasyon kararları, telefonda/tablette uygulama hissi |
| `redesign-existing-projects` | [Leonxlnx/taste-skill](https://github.com/Leonxlnx/taste-skill) `ce26fc2` | MIT | Var olan arayüzü "yapay zekâ yapmış gibi" görünen kalıplara karşı denetleme |
| `supabase-postgres-best-practices` | [supabase/agent-skills](https://github.com/supabase/agent-skills) `c9be0e9` | MIT | Her PostgreSQL için index, pgvector, bağlantı havuzu, EXPLAIN ve kilitlenme kuralları; SQL yazılırken devreye girer |
| `webapp-testing` | [anthropics/skills](https://github.com/anthropics/skills) `8a1541c` | Apache-2.0 | Playwright ile yerel web uygulamasını açıp test eden scriptler |

Emil Kowalski'nin React, React Native ve Swift'e özel skill'leri (`animate-expo`, `ask-sonner`,
`pick-ui-library`, `write-swift`, `prototype`) alınmadı; arayüz vanilla JavaScript. Taste
Skill'in diğer 12 skill'i açılış sayfası, React/Tailwind veya görsel üretimi üzerine.

## Plugin'ler (`.claude/settings.json`)

Dosya repoda; bu depoyu açan her Claude Code oturumu ilk açılışta bu plugin'leri kurmayı önerir.
Üçüncü taraf Impeccable ve UI/UX Pro Max bir sürüm etiketine sabitli. Aynı plugin'leri
bilgisayardaki bütün projelerde açmak için "Bütün projelerde kullanmak için" bölümüne bakın.

| Plugin | Kimden | Ne yapar | Dikkat |
|---|---|---|---|
| `security-guidance` | Anthropic | Düzenlemede tehlikeli kalıp uyarısı (`innerHTML`, `pickle.load`, gömülü anahtar...), tur sonunda diff'in LLM ile güvenlik incelemesi, commit'te dosyalar arası veri akışını izleyen inceleme | Her tur sonunda ve commit'te bir LLM çağrısı yapar (kullanım harcar). Kapatmak için `SECURITY_GUIDANCE_DISABLE=1` |
| `claude-code-setup` | Anthropic | Kod tabanını tarayıp işe yarayacak hook, skill, subagent ve MCP sunucusu önerir. Salt okur | |
| `context7` | Upstash | FastAPI, psycopg, pgvector gibi kütüphanelerin güncel belgelerini getirir | Belgeler topluluk katkılı; ilk kullanımda Context7 girişi ister |
| `playwright` | Microsoft | Claude tarayıcı açıp arayüzü kullanır, ekran görüntüsü alır, formu doldurur | |
| `frontend-design` | Anthropic | Sıradan şablon görünümünden kaçınan arayüz tasarımı | |
| `impeccable` | Paul Bakaus | `/impeccable` ile 24 komut (`audit`, `critique`, `polish`, `harden`...) ve 61 kurallı "yapay zekâ kokan tasarım" dedektörü | Hook'ları her düzenlemede çalışan bir ikili dosya kullanır; dosya GitHub sürümünden SHA-256 doğrulanarak indirilir |
| `ui-ux-pro-max` | NextLevelBuilder | Yerel, aranabilir tasarım veritabanı: 192 renk paleti, 74 yazı tipi eşleşmesi, 119 UX kuralı (Python, bağımlılıksız) | Yanında banner, sunum ve marka skill'leri de gelir |
| `claude-security` | Anthropic | `/claude-security`: ajan ekibiyle derin güvenlik taraması; her bulgu ayrıca doğrulanır, istenirse yama dosyası önerir. Docker ve ayrı LLM anahtarı gerekmez | Taramadan önce süre ve kullanım için onay ister |
| `superpowers` | Jesse Vincent (obra) | Beyin fırtınası, plan, TDD ve sistematik hata ayıklama iş akışı | Küçük işleri de "önce planla" düzenine sokar; her oturum başında talimat yükler. Kapatmak için `/plugin disable superpowers` |
| `semgrep` | Semgrep | Her düzenlemede Semgrep Code, Supply Chain ve Secrets taraması | Ücretsiz Semgrep hesabı gerekir: Claude'a "login to semgrep" denir |
| `pyright-lsp` | Anthropic | Python tip hatalarını düzenleme anında Claude'a gösterir | Bilgisayarda `npm i -g pyright` gerekir; proje tip denetimi kullanmadığı için başta çok uyarı çıkabilir |
| `insecure-defaults`, `sharp-edges`, `supply-chain-risk-auditor`, `static-analysis`, `property-based-testing`, `differential-review`, `variant-analysis`, `post-patch-validation` | Trail of Bits | Tehlikeli varsayılanlar (ör. `DEMO_PAROLA`, boş JWT anahtarı), yanlış kullanıma açık API'ler, PyPI tedarik zinciri riski, yerel CodeQL/Semgrep, Hypothesis testleri, güvenlik odaklı diff incelemesi, bir açığın benzerlerini arama, düzeltmenin açığı kapattığını doğrulama | Hook'ları yok; yalnızca ilgili iş yapılırken devreye girer. Lisans CC BY-SA 4.0 |

Üçüncü taraf iki marketplace bir sürüm etiketine sabitlendi: yeni sürümler incelenmeden
gelmez. Güncellemek için etiket değiştirilir.

Tasarım skill'leri hızlı görünüm için CDN'den yazı tipi veya kütüphane eklemeyi önerebilir.
Bu projede arayüzün dış bağımlılığı yok ve fabrika ağında internet olmayabilir; öneriler bu
kurala göre uygulanmalı.

## Veritabanı MCP'si (`.mcp.json`)

[Postgres MCP Pro](https://github.com/crystaldba/postgres-mcp) (`postgres-mcp==0.3.0`, MIT; `mcp` 2.x ile uyumsuz olduğu için `mcp<2` ile çalıştırılır): Claude yerel andon
veritabanına **salt okunur** (`--access-mode=restricted`) bağlanır; sorgu planlarını inceler,
index önerir, veritabanı sağlığını kontrol eder. Veriyi ve şemayı değiştiremez.

- Bilgisayarda [uv](https://docs.astral.sh/uv/) kurulu olmalı (`uvx` komutu) ve veritabanı açık
  olmalı (`docker compose up -d db`).
- Varsayılan olarak yerel demo veritabanına bağlanır (`127.0.0.1:5432`, veritabanı, kullanıcı ve
  parola `andon`); farklıysa `ANDON_DATABASE_URI` ortam değişkeniyle değiştirilir.
- Index önerileri için veritabanında `pg_stat_statements` ve `hypopg` eklentileri gerekir;
  bunlar olmadan da planlar ve sağlık kontrolü çalışır.
- Bulut oturumlarında veritabanı olmadığı için bağlanamaz; bu bir hata değildir.

## GitHub tarafı

- **CodeQL** (`.github/workflows/codeql.yml`): Python, JavaScript ve iş akışı dosyalarını her
  PR'da ve haftada bir `security-extended` sorgularıyla tarar. Bulgular Security sekmesine
  düşer. Repo ayarlarında CodeQL "default setup" açıksa bu iş akışı yükleme yapamaz; ikisinden
  biri seçilmeli.
- **Dependabot** (`.github/dependabot.yml`): pip, GitHub Actions, Dockerfile ve docker-compose
  imajları için ayda bir, ekosistem başına tek PR. Güvenlik açığı uyarıları ve otomatik
  güvenlik PR'ları için repo ayarlarında Settings → Code security altında "Dependabot alerts"
  ve "Dependabot security updates" açılmalı. Gizli anahtar taraması için aynı yerde "Secret
  scanning" ve "Push protection" açılabilir (açık repolarda ücretsiz).

## Strix ile Hata Asistanı'nı taramak

Strix, uygulamaya gerçekten saldırıp yalnızca çalışan bir saldırı örneğiyle (PoC) kanıtladığı
açıkları raporlar. Yalnızca sahibi olunan sistemlere karşı çalıştırılmalı. Docker ve bir LLM
anahtarı gerekir. Windows'ta aşağıdaki komutlar Git Bash'te çalışır.

```bash
pipx install strix-agent                       # Python 3.12+
export STRIX_LLM="gemini/gemini-3.8-flash"     # LiteLLM model adı; Anthropic/OpenAI da olur
export LLM_API_KEY="$GEMINI_API_KEY"
export STRIX_TELEMETRY=0                       # varsayılan olarak kullanım verisi gönderir

docker compose up -d --build                   # uygulama: http://localhost:8000

# Strix hedef klasörü yazılabilir bağlar; çalışma kopyası yerine temiz bir kopya ver.
git worktree add ../andon-strix HEAD
strix -n -t ../andon-strix -t http://host.docker.internal:8000 \
  --scan-mode standard --max-budget 5 --instruction-file docs/strix-talimat.md
```

[`docs/strix-talimat.md`](strix-talimat.md) Strix'e kullanıcıları, rolleri ve öncelikli
soruları (operatörün bakım kılavuzuna ulaşması, JWT, agent araçlarında SQL injection,
istenmeden bakım talebi açtırma, XSS) anlatır. Sonuçlar `strix_runs/<çalıştırma>/` altına
yazılır (`.gitignore`'da; içinde çalışan saldırı örnekleri var). Rapor okunduktan sonra
Claude'a "Strix bulgularını düzelt" demek `fix-security-vulnerabilities-with-strix`
skill'ini çalıştırır: kök nedeni düzeltir, sonra aynı açığı yeniden tarayarak kapandığını
doğrular.

Ücretsiz Gemini katmanının dakika başına istek sınırı uzun taramaları yavaşlatır.
`--max-budget` dolarsa tarama erken biter ve yine `0` ile çıkar; `run.json`'daki durum ve
maliyet kontrol edilmeli.

## Bütün projelerde kullanmak için

Yukarıdakiler yalnızca bu repoda geçerli. Aynı plugin'leri bilgisayardaki her projede açmak
için (Claude Code kurulu olmalı):

```bash
claude plugin marketplace add anthropics/claude-plugins-official
claude plugin marketplace add "pbakaus/impeccable#skill-v4.4.0"
claude plugin marketplace add "nextlevelbuilder/ui-ux-pro-max-skill#v2.15.0"
claude plugin marketplace add trailofbits/skills

claude plugin install security-guidance@claude-plugins-official
claude plugin install claude-code-setup@claude-plugins-official
claude plugin install context7@claude-plugins-official
claude plugin install playwright@claude-plugins-official
claude plugin install frontend-design@claude-plugins-official
claude plugin install claude-security@claude-plugins-official
claude plugin install superpowers@claude-plugins-official
claude plugin install semgrep@claude-plugins-official
claude plugin install pyright-lsp@claude-plugins-official
claude plugin install impeccable@impeccable
claude plugin install ui-ux-pro-max@ui-ux-pro-max-skill
claude plugin install insecure-defaults@trailofbits
claude plugin install sharp-edges@trailofbits
claude plugin install supply-chain-risk-auditor@trailofbits
claude plugin install static-analysis@trailofbits
claude plugin install property-based-testing@trailofbits
claude plugin install differential-review@trailofbits
claude plugin install variant-analysis@trailofbits
claude plugin install post-patch-validation@trailofbits
```

Skill'ler için:

```bash
npx skills add usestrix/strix
npx skills@latest add emilkowalski/skills
npx skills add supabase/agent-skills --skill supabase-postgres-best-practices
npx skills add anthropics/skills --skill webapp-testing
```

## Eklenmeyenler

| Araç | Neden |
|---|---|
| OmniRoute | Claude aboneliğini başka bir uygulamaya bağlamak Anthropic'in kullanım koşullarına aykırı; projenin issue'larında bağlandıktan dakikalar sonra kapatılan Claude Max hesapları bildirilmiş |
| Agent Reach | LinkedIn, Instagram ve X'i kullanıcının oturum çerezleriyle kazır. Bu sitelerin kullanım koşullarına aykırı; README'si hesap kapatılma riskine karşı yan hesap öneriyor. Kişisel veri toplamak KVKK sorunu da doğurur |
| Supabase plugin'i | Hata Asistanı kendi PostgreSQL + pgvector'ünü kullanıyor; Supabase yok |
| Figma MCP | Projede Figma dosyası yok. Gerçek kullanım için ücretli Dev/Full koltuk gerekiyor (ücretsiz koltukta ayda 6 araç çağrısı) |
| 21st.dev (Magic / 21st MCP) | React + Tailwind bileşenleri üretir ve API anahtarı ister; arayüz vanilla JavaScript |
| Skill UI | ~240 yıldızlı küçük bir proje, Nisan 2026'dan beri güncellenmemiş ve repoda lisans dosyası yok. Başka bir markanın tasarım sistemini kopyalamak telif ve ticari takdim sorunu doğurabilir |
| Find Skills | Kendisi zararsız, ama incelenmemiş üçüncü taraf skill'leri kurar. Snyk'in yaklaşık 4.000 skill üzerindeki ToxicSkills incelemesinde 76'sı kötü amaçlı çıktı. Skill, kurulmadan önce okunmalı |
| Taste Skill'in tamamı | Ana skill açılış sayfası ve React/Tailwind odaklı, diğerleri görsel üretimi üzerine; vanilla CSS ile çalışan `redesign-existing-projects` alındı |
| Vercel web-design-guidelines | Kurallarını her çalışmada sürüme sabitlenmemiş bir internet dosyasından çeker; dosya değişirse Claude'un talimatı da değişir. Aynı işi Impeccable yapıyor |
| Trail of Bits modern-python, fp-check | Biri projeyi pip'ten uv'ye taşımaya yönlendirir; diğeri her cevabın sonunda ek bir LLM kontrolü çalıştırır |
| Logfire, SonarQube, Aikido | Hesap ya da ücretli servis gerektirir |
