# Sanel Ar-Ge Prototip Takip 

Ar-Ge biriminin prototip talep listesini (eski "Ar-Ge Prototip List.xlsx") tarayıcıdan, birden fazla kişinin aynı anda kullanabileceği bir web uygulaması. Kullanıcılar talepleri, alt parçaları, durumları ve terminleri tarayıcıdan günceller; değişiklikler açık olan diğer ekranlara anında yansır.

## Özet

| | |
|---|---|
| Çalışma ortamı | Python 3.9 veya üstü (yalnızca standart kütüphane, `pip install` gerekmez) ya da Docker |
| Veritabanı | Tek dosya SQLite: `data/prototip.db` |
| Port | 8080 (değiştirilebilir) |
| Dış bağlantı | Yok. Sayfa internetten hiçbir şey yüklemez |
| Giriş | Şirket kullanıcı adı ve şifresiyle (Active Directory / LDAP) + bir yerel yönetici hesabı |
| Yetki | Kullanıcı bazında rol: Görüntüleme, Düzenleme, Yönetici (yetki verebilir), Engelli |
| Yedek | Günlük otomatik (`data/backups/`, son 30 gün) + istenince JSON dışa aktarma |
| Kayıt geçmişi | Her değişiklik kim/ne zaman bilgisiyle `history` tablosuna yazılır |

## Sistem şeması

![Prototip Takip sistem şeması](docs/mimari.png)

- **Tarayıcı** sayfayı (`app/index.html`) sunucudan alır; bütün okuma/yazma `REST /api/...` ile yapılır.
- **server.py** değişikliği SQLite'a yazar, `history` tablosuna kim/ne zaman bilgisini ekler ve aynı olayı **SSE** (`/api/events`) ile açık olan tüm sekmelere gönderir; başka kullanıcılar sayfayı yenilemeden görür.
- **İlk kurulumda** veritabanı boşsa `seed/seed.json` yüklenir (Excel'den `tools/excel_to_seed.py` ile üretilir).
- **Yedek:** her gün `data/backups/` altına tutarlı bir `.db` kopyası alınır.
- **Giriş:** kullanıcı adı ve şifre `server.py` → `auth.py` üzerinden Active Directory'ye (LDAP) sorulur; şifre hiçbir yerde saklanmaz. Kullanıcının rolü SQLite'ta tutulur ve her istekte kontrol edilir.

Şemanın kaynağı `docs/mimari.html` (tarayıcıda açılır) ve `docs/mimari.svg`.

## Klasör yapısı

```
prototip-takip/
├─ server.py            Sunucu (HTTP + API + canlı güncelleme + yedek)
├─ auth.py              Giriş (AD/LDAP), yerel yönetici, roller ve oturumlar
├─ app/index.html       Arayüz (tek dosya)
├─ seed/seed.json       İlk kurulum verisi (güncel Excel'den üretildi)
├─ seed/bom.json        Ürün ağacı ve açılır liste tanımları
├─ tools/excel_to_seed.py   Excel → seed.json dönüştürücü (openpyxl gerekir)
├─ docs/                Sistem şeması (png, svg, html)
├─ data/                Veritabanı ve yedekler (ilk çalıştırmada oluşur)
├─ Dockerfile, docker-compose.yml
└─ baslat.bat           Windows'ta hızlı deneme
```

## Kurulum (IT için, adım adım)

Aşağıdaki adımlar Docker kurulu bir Linux sanal makine içindir (önerilen). Docker olmadan kurulum en altta.

### 0. Gereksinimler

| | |
|---|---|
| Sunucu | Küçük bir sanal makine yeterli (uygulama ~25 MB RAM kullanır, veri 1 MB'tan küçük) |
| Yazılım | Docker + Docker Compose (ya da Docker olmadan Python 3.9+) |
| Ağ (gelen) | 8080/TCP yalnızca iç ağdan; isteğe bağlı iç adres, ör. `prototip.sanel.local` |
| Ağ (giden) | Sunucudan Active Directory'ye: `10.34.10.1:389` (ldaps kullanılacaksa `:636`) |
| İnternet | Yalnızca imaj derlenirken `python:3.12-slim` indirmek için; uygulama çalışırken gerekmez |

### 1. Kodu alın

```bash
git clone https://gitlab.sanel.com.tr/Berkay.Yilmaz/prototype-tracking-system.git
cd prototype-tracking-system
ls data          # yalnızca .keep olmalı; veritabanı ilk açılışta oluşur
```

### 2. Ayarları kontrol edin

`docker-compose.yml` içinde AD adresi hazır gelir:

```yaml
PT_LDAP_URL: ldap://10.34.10.1:389      # şifreli bağlantı için: ldaps://10.34.10.1:636
PT_LDAP_DOMAIN: sanelelk.local
PT_DEFAULT_ROLE: viewer                 # ilk kez giren herkes "Görüntüleme" rolü alır
```

Yerel yönetici şifresini bu dosyaya **yazmayın** (git'e gider); 5. adımda komutla verilir.

### 3. Başlatın

```bash
docker compose up -d --build
docker compose ps                      # STATUS: Up ... (healthy)
docker logs prototip-takip
```

Logda şunlar görülmeli:

```
İlk kurulum: seed.json'dan 71 belge yüklendi.
Giriş zorunlu. Şirket hesabı: ldap://10.34.10.1:389 (alan: sanelelk.local) | ilk girişte rol: viewer
UYARI: Yerel yönetici şifresi yok. 'python server.py --set-admin-password' ile belirleyin.
Prototip Takip çalışıyor: http://0.0.0.0:8080  (veri: /data)
```

(Yönetici uyarısı 5. adımdan sonra kaybolur.)

### 4. Active Directory bağlantısını deneyin

```bash
docker compose exec prototip-takip python server.py --ldap-test berkay.yilmaz
# Şifre sorar. Beklenen: "BAŞARILI. Görünen ad: Berkay Yılmaz"
```

Başarısız olursa → aşağıdaki **Sorun giderme** tablosu.

### 5. Yerel yönetici şifresini belirleyin

```bash
docker compose exec prototip-takip python server.py --set-admin-password
# Şifreyi iki kez sorar (en az 10 karakter). Şifreyi kurum şifre kasasına kaydedin.
```

### 6. Tarayıcıdan ilk giriş ve yetkiler

1. `http://<sunucu-adresi>:8080` açın, kullanıcı adı `admin` ve 5. adımdaki şifreyle girin.
2. **Yetkiler** sekmesinde *Kullanıcı adı* alanına `berkay.yilmaz` yazın, rol **Yönetici** seçip *Ekle / güncelle*.
3. Bundan sonra yetkileri Ar-Ge (Berkay Yılmaz) dağıtır. Diğer şirket kullanıcıları kendi Windows kullanıcı adı ve şifresiyle girer; ilk girişte "Görüntüleme" rolü alır.

### 7. Yedek ve iç adres

- **Yedek:** sunucu her gün `data/backups/prototip-YYYY-AA-GG.db` dosyasını oluşturur (son 30 gün). Kurum yedeklemesine `data/backups/` klasörünü ekleyin. (Çalışan `data/prototip.db` dosyasını tek başına kopyalamayın; son değişiklikler yanındaki `-wal` dosyasında bekler.)
- **İç adres / güvenlik duvarı:** 8080'i yalnızca iç ağa açın; isteğe bağlı DNS kaydı (ör. `prototip.sanel.local`).

### Kontrol listesi

- [ ] `docker compose ps` → healthy
- [ ] `--ldap-test` → BAŞARILI
- [ ] `admin` ile giriş yapıldı, `berkay.yilmaz` Yönetici yapıldı
- [ ] Başka bir bilgisayardan şirket hesabıyla giriş yapılabiliyor
- [ ] İki farklı bilgisayarda açıkken birinde yapılan değişiklik diğerinde anında görünüyor
- [ ] `data/backups/` kurum yedeklemesine eklendi

### Sorun giderme

| Belirti | Neden / çözüm |
|---|---|
| `--ldap-test`: "Kimlik sunucusuna ulaşılamadı" | Sunucudan `10.34.10.1:389`'a erişim yok (güvenlik duvarı). Konteyner içinden deneyin: `docker compose exec prototip-takip python -c "import socket;socket.create_connection(('10.34.10.1',389),3);print('ok')"` |
| "Kullanıcı adı veya şifre hatalı" (şifre doğru olduğu halde) | AD `kullanici@sanelelk.local` biçimini kabul etmiyor olabilir. `docker-compose.yml`'a `PT_LDAP_BIND_FORMAT: 'SANELELK\{user}'` ekleyip `docker compose up -d` |
| "Şifreli bağlantı istiyor" | AD'de LDAP signing zorunlu. `PT_LDAP_URL: ldaps://10.34.10.1:636` kullanın |
| "Sertifikası doğrulanamadı" (ldaps) | Kurum CA sertifikasını `data/sirket-ca.pem` olarak koyup `PT_LDAP_CA_FILE: /data/sirket-ca.pem` |
| "Hesabınız kilitlenmiş / şifrenin süresi dolmuş" | AD hesabıyla ilgili; Windows'ta düzeltilmeli |
| "Çok fazla hatalı deneme" | 5 hatalı denemeden sonra 5 dakika bekleme; kendiliğinden açılır |
| Yönetici şifresi unutuldu | 5. adımdaki komutu tekrar çalıştırın (yeni şifre verilir) |
| Sayfa açılıyor ama liste boş / eski veri | `data/` klasöründe repodan gelmiş eski bir veritabanı olabilir. Konteyneri durdurup `data/prototip.db*` dosyalarını silin ve tekrar başlatın (ilk kurulumda veri yoksa) |
| Canlı güncelleme gecikiyor (vekil arkasında) | Vekilde `/api/events` için tamponlamayı kapatın (nginx: `proxy_buffering off;`) |

### Güncelleme (yeni sürüm geldiğinde)

```bash
git pull
docker compose up -d --build      # veri ./data'da kalır, kaybolmaz
```

### Docker olmadan: Windows sunucu

1. Python 3.9+ kurun ("Add to PATH" işaretli).
2. Klasörü örneğin `D:\prototip-takip` altına kopyalayın.
3. Deneme: `baslat.bat` (içindeki `PT_LDAP_URL` ve `PT_LDAP_DOMAIN` satırlarının başındaki `REM`'i kaldırın) → `http://localhost:8080`.
4. Yönetici şifresi: `python server.py --set-admin-password`
5. Kalıcı çalışma için Windows hizmeti (örnek: NSSM):
   ```
   nssm install PrototipTakip "C:\Program Files\Python312\python.exe" "D:\prototip-takip\server.py --port 8080 --data D:\prototip-takip\data"
   nssm set PrototipTakip AppDirectory D:\prototip-takip
   nssm set PrototipTakip AppEnvironmentExtra PT_LDAP_URL=ldap://10.34.10.1:389 PT_LDAP_DOMAIN=sanelelk.local
   nssm start PrototipTakip
   ```
6. Güvenlik duvarında 8080'i yalnızca iç ağa açın.

### Docker olmadan: Linux (systemd)

```ini
# /etc/systemd/system/prototip-takip.service
[Service]
WorkingDirectory=/opt/prototip-takip
Environment=PT_LDAP_URL=ldap://10.34.10.1:389 PT_LDAP_DOMAIN=sanelelk.local
ExecStart=/usr/bin/python3 server.py --port 8080 --data /var/lib/prototip-takip
Restart=always
User=prototip
[Install]
WantedBy=multi-user.target
```

İlk çalıştırmada veritabanı boşsa `seed/seed.json` otomatik yüklenir.

## Giriş ve yetkiler

`PT_LDAP_URL` ayarlıyken uygulama açılışta giriş ekranı gösterir. Kullanıcılar Windows'ta kullandıkları kullanıcı adı ve şifreyle girer (`berkay.yilmaz`, `SANELELK\berkay.yilmaz` ve `berkay.yilmaz@sanelelk.local` yazımlarının hepsi kabul edilir).

**Nasıl çalışır:** Sunucu, girilen ad ve şifreyle Active Directory'ye bağlanmayı dener (LDAP simple bind, `kullanici@sanelelk.local`). AD kabul ederse şifre doğrudur. Şifre uygulamada saklanmaz; yalnızca kullanıcının adı ve rolü `data/prototip.db` içindeki `users` tablosuna yazılır. Oturum, tarayıcıya verilen HttpOnly bir çerezle sürer (varsayılan 7 gün).

| Rol | Ne yapabilir |
|---|---|
| Görüntüleme (`viewer`) | Liste ve grafikleri görür, Excel'e aktarır |
| Düzenleme (`editor`) | Talep, parça, durum, ürün ağacı ve listeleri değiştirir |
| Yönetici (`admin`) | Düzenler ve **Yetkiler** sekmesinden başkalarına rol verir (başka yönetici de atayabilir) |
| Engelli (`none`) | Giriş yapamaz |

- Şirket hesabıyla ilk kez giren herkes `PT_DEFAULT_ROLE` rolüyle (varsayılan: Görüntüleme) eklenir. Henüz giriş yapmamış birine de kullanıcı adını yazarak önceden rol verilebilir.
- Rol değişikliği hemen geçerli olur; "Engelli" yapılan kişinin açık oturumu kapanır. "Kaldır" kişiyi listeden siler, bir sonraki girişinde varsayılan rolü alır.
- Kimse kendi rolünü değiştiremez; yerel yönetici hesabı değiştirilemez ve silinemez.
- Aynı kullanıcı adıyla 5 dakika içinde 5 hatalı denemeden sonra 5 dakika beklenir (AD hesabının kilitlenmesini de önler).
- Her değişiklik `history` tablosuna kullanıcı adıyla kaydedilir.

### İlk kurulumda yerel yönetici

Yerel yönetici (`admin`), AD'ye ulaşılamasa da giriş yapabilen ve ilk rolleri dağıtan hesaptır. Şifresi bu sunucuda PBKDF2 özetiyle saklanır. Kurulumdan sonra bir kez:

```bash
docker compose exec prototip-takip python server.py --set-admin-password
# Docker olmadan:  python server.py --set-admin-password
```

Sonra tarayıcıdan `admin` ile girip **Yetkiler** sekmesinden Ar-Ge'de düzenleme yapacak kişilere *Düzenleme*, yetki dağıtacak kişilere *Yönetici* rolü verin.

### AD bağlantısını denemek

```bash
docker compose exec prototip-takip python server.py --ldap-test berkay.yilmaz
# Sunucu: ldap://10.34.10.1:389 (alan: sanelelk.local) | bağlanılacak ad: berkay.yilmaz@sanelelk.local
# Şifre:
# BAŞARILI. Görünen ad: Berkay Yılmaz
```

Hata alınırsa mesaj nedenini söyler (sunucuya ulaşılamadı, şifre hatalı, hesap kilitli, şifreli bağlantı gerekli vb.).

### Güvenlik notları (IT)

- **`ldap://` (389) şifreleri ağda açık taşır.** Mümkünse `PT_LDAP_URL=ldaps://10.34.10.1:636` kullanın. Sertifika kurum içi bir CA ile imzalıysa CA dosyasını `PT_LDAP_CA_FILE` ile verin. AD "LDAP signing" zorunlu tutuyorsa `ldap://` ile giriş reddedilir; uygulama bunu "şifreli bağlantı gerekli" diye bildirir.
- **Tarayıcı ile uygulama arası da HTTP'dir;** giriş şifresi bu yolda da açık gider. Önüne IIS/nginx koyup HTTPS açmanız ve `PT_COOKIE_SECURE=1` ayarlamanız önerilir. Vekilde `/api/events` için tamponlamayı kapatın (nginx: `proxy_buffering off;`); bu adres canlı güncelleme akışıdır (Server-Sent Events).
- Uygulama yalnızca iç ağdan erişilebilir olmalı. Ters vekil arkasında `PT_HOST=127.0.0.1` ile çalıştırın.
- İsterseniz giriş ekranı yerine IIS Windows Authentication kullanılabilir: vekil kullanıcı adını bir başlıkla iletir, `PT_USER_HEADER` o başlığın adına ayarlanır (rol yine Yetkiler sekmesinden verilir).

## Ayarlar (ortam değişkenleri)

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `PT_PORT` | 8080 | Dinlenen port |
| `PT_HOST` | 0.0.0.0 | Dinlenen adres (yalnızca vekil arkasındaysa `127.0.0.1`) |
| `PT_DATA` | `./data` | Veritabanı ve yedek klasörü |
| `PT_USER_HEADER` | (boş) | Kimliği ileten başlık adı |
| `PT_READONLY` | (boş) | `1` ise yazma kapalı |
| `PT_BACKUP_KEEP` | 30 | Saklanacak günlük yedek sayısı |
| `PT_LDAP_URL` | (boş) | AD adresi, ör. `ldap://10.34.10.1:389` veya `ldaps://10.34.10.1:636`. Ayarlıysa giriş zorunlu |
| `PT_LDAP_DOMAIN` | (boş) | AD alan adı, ör. `sanelelk.local` |
| `PT_LDAP_BIND_FORMAT` | `{user}@{domain}` | AD'ye bağlanılırken kullanılan ad; gerekirse `SANELELK\{user}` |
| `PT_LDAP_BASE_DN` | alan adından | Görünen adı (displayName) aramak için kök, ör. `dc=sanelelk,dc=local` |
| `PT_LDAP_CA_FILE` | (boş) | `ldaps` için kurum CA sertifikası |
| `PT_LDAP_TLS_VERIFY` | 1 | `0` ise ldaps sertifikası doğrulanmaz (önerilmez) |
| `PT_DEFAULT_ROLE` | viewer | İlk kez giren kullanıcının rolü: `viewer`, `editor`, `none` |
| `PT_ADMIN_USER` | admin | Yerel yönetici kullanıcı adı |
| `PT_SESSION_HOURS` | 168 | Oturum süresi (saat) |
| `PT_COOKIE_SECURE` | (boş) | `1`: çerez yalnızca HTTPS'te gönderilir |
| `PT_AUTH` | (boş) | `1`: LDAP olmadan da giriş zorunlu (yalnızca yerel yönetici) |

## Yedekleme ve geri yükleme

- Sunucu her gün `data/backups/prototip-YYYY-AA-GG.db` dosyasını oluşturur.
- Anlık tam yedek: tarayıcıdan `/api/export.json` ya da `python server.py --export yedek.json`.
- Geri yükleme: `python server.py --import yedek.json`. Bu komut önce mevcut verinin yedeğini alır, sonra veriyi değiştirir. Sunucuyu durdurup çalıştırın.
- Kurum yedekleme sistemine `data/backups/` klasörünü eklemek yeterli (bu dosyalar çalışırken tutarlı alınmış kopyalardır).

## Excel'den yeniden veri yükleme

Ar-Ge yeni bir Excel listesi gönderir ve uygulamadaki verinin tamamen o listeyle değiştirilmesi istenirse:

```bash
pip install openpyxl
python tools/excel_to_seed.py "Ar-Ge Prototip List.xlsx" seed/seed.json
python server.py --import seed/seed.json
```
Dikkat: Bu işlem uygulamada sonradan yapılmış değişikliklerin üzerine yazar; önceki hali otomatik yedeklenir.

## API (bilgi için)

| Yöntem | Adres | Açıklama |
|---|---|---|
| GET | `/api/state` | Tüm ürünler, talepler, listeler |
| PUT | `/api/docs/{koleksiyon}/{id}` | Belgeyi yaz/değiştir |
| PATCH | `/api/docs/{koleksiyon}/{id}` | İç içe birleştirerek güncelle |
| DELETE | `/api/docs/{koleksiyon}/{id}` | Sil |
| GET | `/api/events` | Canlı değişiklik akışı (SSE) |
| GET | `/api/export.json` | Tam yedek |
| GET | `/api/health` | Sağlık kontrolü |
| POST | `/api/login`, `/api/logout` | Giriş / çıkış (JSON) |
| GET / PUT / DELETE | `/api/admin/users`, `/api/admin/users/{kullanıcı}` | Yetki paneli (yalnızca Yönetici) |

Koleksiyonlar: `products` (ürün ağacı), `requests` (talepler; alt parçalar `items` alanında), `config` (`lists` belgesi: faz, platform, sorumlu, üretici listeleri).

## PostgreSQL'e geçiş (ileride)

Şimdilik tüm veri tek SQLite dosyasında. Veriler belge (JSON) olarak tutulduğu için tablolar sade: `docs` (ürünler, talepler, listeler), `history` (değişiklik kaydı), `users` (roller), `sessions` (oturumlar). Şirket PostgreSQL sunucusu hazır olduğunda yalnızca `server.py` içindeki `Store` sınıfı ile `auth.py` içindeki SQL'ler PostgreSQL'e uyarlanacak; veri `/api/export.json` (veya `.db` dosyası) üzerinden taşınacak. Arayüzde değişiklik gerekmez.

## Kapasite

50 eşzamanlı kullanıcıya ve on binlerce kaleme kadar ek ayar gerekmez. Sunucu, açık her sekme için bir canlı bağlantı tutar.
