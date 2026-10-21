# Sanel Ar-Ge Prototip Takip 

Ar-Ge biriminin prototip talep listesini (eski "Ar-Ge Prototip List.xlsx") tarayıcıdan, birden fazla kişinin aynı anda kullanabileceği bir web uygulaması. Kullanıcılar talepleri, alt parçaları, durumları ve terminleri tarayıcıdan günceller; değişiklikler açık olan diğer ekranlara anında yansır.

## Özet

| | |
|---|---|
| Çalışma ortamı | Python 3.9 veya üstü (yalnızca standart kütüphane, `pip install` gerekmez) ya da Docker |
| Veritabanı | Tek dosya SQLite: `data/prototip.db` |
| Port | 8080 (değiştirilebilir) |
| Dış bağlantı | Yok. Sayfa internetten hiçbir şey yüklemez |
| Kimlik doğrulama | Uygulamanın kendi girişi yok; kurum içi ağda ya da ters vekil arkasında çalıştırılmalı (aşağıda) |
| Yedek | Günlük otomatik (`data/backups/`, son 30 gün) + istenince JSON dışa aktarma |
| Kayıt geçmişi | Her değişiklik kim/ne zaman bilgisiyle `history` tablosuna yazılır |

## Klasör yapısı

```
prototip-takip/
├─ server.py            Sunucu (HTTP + API + canlı güncelleme + yedek)
├─ app/index.html       Arayüz (tek dosya)
├─ seed/seed.json       İlk kurulum verisi (güncel Excel'den üretildi)
├─ seed/bom.json        Ürün ağacı ve açılır liste tanımları
├─ tools/excel_to_seed.py   Excel → seed.json dönüştürücü (openpyxl gerekir)
├─ data/                Veritabanı ve yedekler (ilk çalıştırmada oluşur)
├─ Dockerfile, docker-compose.yml
└─ baslat.bat           Windows'ta hızlı deneme
```

## Kurulum

### A) Docker (önerilen)

```bash
docker compose up -d --build
# http://<sunucu>:8080
```
Veri `./data` klasöründe kalır; konteyner silinse de kaybolmaz.

### B) Windows sunucu (Docker olmadan)

1. Python 3.9+ kurun ("Add to PATH" işaretli).
2. Klasörü örneğin `D:\prototip-takip` altına kopyalayın.
3. Deneme: `baslat.bat` → tarayıcıda `http://localhost:8080`.
4. Kalıcı çalışma için Windows hizmeti olarak kurun (örnek: NSSM):
   ```
   nssm install PrototipTakip "C:\Program Files\Python312\python.exe" "D:\prototip-takip\server.py --port 8080 --data D:\prototip-takip\data"
   nssm set PrototipTakip AppDirectory D:\prototip-takip
   nssm start PrototipTakip
   ```
5. Güvenlik duvarında 8080'i yalnızca iç ağa açın.

### C) Linux (systemd)

```ini
# /etc/systemd/system/prototip-takip.service
[Service]
WorkingDirectory=/opt/prototip-takip
ExecStart=/usr/bin/python3 server.py --port 8080 --data /var/lib/prototip-takip
Restart=always
User=prototip
[Install]
WantedBy=multi-user.target
```

İlk çalıştırmada veritabanı boşsa `seed/seed.json` otomatik yüklenir.

## Erişim ve kimlik

Uygulamada şifre ekranı yoktur. Önerilen kurulum:

- Sunucuyu yalnızca kurum içi ağdan erişilebilir yapın.
- Kimlik isteniyorsa önüne IIS (Windows Authentication) veya nginx koyun ve kullanıcı adını bir başlıkla iletin. Sonra `PT_USER_HEADER` değişkenini o başlığın adıyla ayarlayın (ör. `X-Remote-User`). Böylece her değişiklik AD kullanıcı adıyla kaydedilir. Bu ayar yokken kullanıcılar adlarını sağ üstteki alana kendileri yazar.
- Salt okunur bir kopya gerekirse ikinci bir örneği `PT_READONLY=1` ile çalıştırın.
- Ters vekil arkasında alt yol (ör. `/prototip/`) kullanılabilir; arayüz tüm isteklerde göreli adres kullanır. Vekilde `/api/events` için tamponlamayı kapatın (nginx: `proxy_buffering off;`). Bu adres canlı güncelleme akışıdır (Server-Sent Events).

## Ayarlar (ortam değişkenleri)

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `PT_PORT` | 8080 | Dinlenen port |
| `PT_HOST` | 0.0.0.0 | Dinlenen adres (yalnızca vekil arkasındaysa `127.0.0.1`) |
| `PT_DATA` | `./data` | Veritabanı ve yedek klasörü |
| `PT_USER_HEADER` | (boş) | Kimliği ileten başlık adı |
| `PT_READONLY` | (boş) | `1` ise yazma kapalı |
| `PT_BACKUP_KEEP` | 30 | Saklanacak günlük yedek sayısı |

## Yedekleme ve geri yükleme

- Sunucu her gün `data/backups/prototip-YYYY-AA-GG.db` dosyasını oluşturur.
- Anlık tam yedek: tarayıcıdan `/api/export.json` ya da `python server.py --export yedek.json`.
- Geri yükleme: `python server.py --import yedek.json`. Bu komut önce mevcut verinin yedeğini alır, sonra veriyi değiştirir. Sunucuyu durdurup çalıştırın.
- Kurum yedekleme sistemine `data/` klasörünü eklemek yeterli.

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

Koleksiyonlar: `products` (ürün ağacı), `requests` (talepler; alt parçalar `items` alanında), `config` (`lists` belgesi: faz, platform, sorumlu, üretici listeleri).

## Kapasite

50 eşzamanlı kullanıcıya ve on binlerce kaleme kadar ek ayar gerekmez. Sunucu, açık her sekme için bir canlı bağlantı tutar.
