# Token Paneli

Claude Code ve Codex'in ne kadar token kullandığını gösteren, menü çubuğunda (system tray) duran küçük bir Linux uygulaması.
Hiçbir API'ye bağlanmaz; iki aracın bilgisayarınıza yazdığı oturum loglarını okur.

## Ne gösterir?

Panel katman katman açılır:

1. **Özet** — seçilen aralıktaki (Bugün / 7 gün / 30 gün / Tümü) toplam token, Claude Code ve Codex payı,
   Codex'in 5 saatlik ve haftalık kullanım limiti.
2. **Detaylar** — her araç için istemci bazında döküm: CLI, Desktop, VS Code eklentisi, ACP (Zed);
   her birinde hangi modellerin kullanıldığı ve kaç thread olduğu.
3. **Thread'ler** — seçilen istemcideki thread'ler: başlık, proje, model, son kullanım.
4. **Thread detayı** — modele göre, token türüne göre (önbellek / yeni girdi / çıktı / düşünme) ve
   "ne üzerinde" (Bash, Read, exec_command, apply_patch… araçlarına göre yaklaşık) döküm.

Tray simgesinin üzerine gelince bugünün toplamı görünür. Satırların üzerine gelince tam sayılar çıkar.

## Kurulum (Arch tabanlı dağıtımlar)

```bash
git clone https://github.com/sucuklutank123456789-coder/Token.git
cd Token/packaging/arch
makepkg -si
```

Ardından uygulama menüsünden **Token Paneli**'ni açın ya da terminalden `tokenpanel` çalıştırın.

Oturum açılışında otomatik başlasın isterseniz:

```bash
mkdir -p ~/.config/autostart
cp /usr/share/applications/tokenpanel.desktop ~/.config/autostart/
```

### Paketlemeden çalıştırmak

```bash
sudo pacman -S pyside6
python -m tokenpanel          # depo kökünden
```

### Masaüstü ortamı notları

- **KDE, XFCE, Cinnamon, LXQt**: tray doğrudan çalışır.
- **Hyprland / Sway**: waybar'da `tray` modülü açık olmalı.
- **GNOME**: tray için `gnome-shell-extension-appindicator` eklentisi gerekir. Tray yoksa uygulama normal pencere olarak açılır.

## Kullanım

- Sol tık: paneli aç/kapat. Panel dışına tıklayınca veya `Esc` ile kapanır; `Backspace` bir seviye geri gider.
- Sağ tık: Paneli aç / Yenile / Çıkış.
- Loglar 5 saniyede bir kontrol edilir; yalnızca yeni eklenen satırlar okunur.

Terminal çıktısı (panel açmadan):

```bash
tokenpanel --dump --range 7d
```

## Veri kaynakları

| Araç | Log yeri | İstemci alanı |
|---|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl` (`CLAUDE_CONFIG_DIR` destekli) | `entrypoint`: `cli`, `claude-desktop`, `claude-vscode`, `sdk-ts` (Zed ACP) |
| Codex | `~/.codex/sessions/**/*.jsonl`, `~/.codex/archived_sessions/` (`CODEX_HOME` destekli) | `originator`: `codex-tui`, `Codex Desktop`, `codex_vscode`, `zed` |

Bilinen sınırlar:

- Claude tarafında `sdk-ts`, Agent SDK kullanan her aracı kapsar; yalnızca Zed kullanıyorsanız ACP'ye denk gelir.
- Codex Desktop her sohbeti otomatik bir klasörde açtığı için oradaki "proje" adı o klasörün adıdır.
- "Ne üzerinde" dökümü yaklaşıktır: bir model çağrısının token'ı o çağrıda kullanılan araçlara eşit bölünür.
- Toplamın büyük kısmı genelde önbellekten okunan girdidir; bu ayrıca gösterilir.

## Geliştirme

```bash
python -m unittest discover -s tests -t .
```
