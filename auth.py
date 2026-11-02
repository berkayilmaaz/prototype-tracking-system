"""
Sanel Ar-Ge Prototip Takip — kimlik doğrulama ve yetkiler

Ek kütüphane gerektirmez (yalnızca Python standart kütüphanesi).

  * Şirket kullanıcıları Active Directory'ye LDAP "simple bind" ile doğrulanır:
    kullanıcının adı ve şifresiyle AD'ye bağlanılır; AD kabul ederse şifre doğrudur.
    Şifre hiçbir yerde saklanmaz.
  * Bir "yerel yönetici" (local admin) hesabı vardır. Şifresi bu sunucuda (PBKDF2 özetiyle)
    saklanır; AD'ye ulaşılamasa da giriş yapabilir.
  * Roller: viewer (görüntüleme), engineer (proje mühendisi: talep açar, yönetici onaylar),
    editor (düzenleme), admin (düzenleme + talep onayı + yetki verme), none (giriş engelli).
    Yönetici rolündeki herkes başkalarına rol verebilir.
  * Oturum: rastgele bir anahtar tarayıcıya HttpOnly çerez olarak verilir; veritabanında
    yalnızca anahtarın SHA-256 özeti tutulur.
"""
import datetime as dt
import hashlib
import hmac
import os
import re
import secrets
import socket
import ssl
import threading
import time
from urllib.parse import urlparse

ROLES = ("viewer", "engineer", "editor", "admin", "none")
ROLE_TR = {"viewer": "Görüntüleme", "engineer": "Proje Mühendisi", "editor": "Düzenleme", "admin": "Yönetici",
           "none": "Engelli"}
RANK = {"viewer": 1, "engineer": 2, "editor": 3, "admin": 4}
USER_RE = re.compile(r"^[a-z0-9._\-]{1,64}$")
COOKIE = "pt_session"


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_username(raw):
    """'SANELELK\\berkay.yilmaz', 'berkay.yilmaz@sanelelk.local', ' Berkay.Yilmaz ' -> 'berkay.yilmaz'"""
    u = (raw or "").strip()
    if "\\" in u:
        u = u.split("\\")[-1]
    if "@" in u:
        u = u.split("@")[0]
    u = u.lower()
    return u if USER_RE.match(u) else None


# ---------------------------------------------------------------- şifre özeti (yerel yönetici)
def hash_password(pw, iterations=240_000):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(pw, stored):
    try:
        algo, it, salt, dk = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        cand = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(cand.hex(), dk)
    except Exception:
        return False


# ---------------------------------------------------------------- en küçük LDAP istemcisi (BER)
# LDAP mesajları ASN.1 BER ile kodlanır: her alan = etiket (1 bayt) + uzunluk + içerik.
def _len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag, content):
    return bytes([tag]) + _len(len(content)) + content


def _int(n, tag=0x02):
    return _tlv(tag, n.to_bytes(max(1, (n.bit_length() + 8) // 8), "big", signed=True))


def _str(s, tag=0x04):
    return _tlv(tag, s.encode("utf-8") if isinstance(s, str) else s)


def _read_tlv(buf, i):
    """buf[i:]'den bir alan okur -> (etiket, içerik, sonraki_konum)"""
    tag = buf[i]
    n = buf[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(buf[i:i + k], "big")
        i += k
    return tag, buf[i:i + n], i + n


def _children(content):
    out, i = [], 0
    while i < len(content):
        tag, val, i = _read_tlv(content, i)
        out.append((tag, val))
    return out


class LdapError(Exception):
    def __init__(self, code, message, user_message):
        super().__init__(message)
        self.code = code
        self.user_message = user_message


def _ad_reason(diag):
    """AD, 49 (invalidCredentials) hatasında 'data 52e' gibi bir alt kod verir."""
    m = re.search(r"data ([0-9a-f]{3})", diag or "")
    sub = m.group(1) if m else ""
    return {
        "525": "Kullanıcı bulunamadı.",
        "52e": "Kullanıcı adı veya şifre hatalı.",
        "530": "Bu saatte giriş izniniz yok.",
        "531": "Bu bilgisayardan giriş izniniz yok.",
        "532": "Şifrenizin süresi dolmuş; Windows'ta şifrenizi yenileyin.",
        "533": "Hesabınız devre dışı.",
        "701": "Hesabınızın süresi dolmuş.",
        "773": "Şifrenizi değiştirmeniz gerekiyor; önce Windows'ta değiştirin.",
        "775": "Hesabınız kilitlenmiş; IT ile görüşün.",
    }.get(sub, "Kullanıcı adı veya şifre hatalı.")


class LdapClient:
    def __init__(self, url, domain, bind_format="{user}@{domain}", base_dn="", user_attr="sAMAccountName",
                 timeout=6.0, tls_verify=True, ca_file=""):
        u = urlparse(url)
        if u.scheme not in ("ldap", "ldaps") or not u.hostname:
            raise ValueError("PT_LDAP_URL 'ldap://sunucu:389' ya da 'ldaps://sunucu:636' biçiminde olmalı")
        self.secure = u.scheme == "ldaps"
        self.host, self.port = u.hostname, u.port or (636 if self.secure else 389)
        self.domain = domain
        self.bind_format = bind_format
        self.base_dn = base_dn or ",".join("dc=" + p for p in domain.split(".") if p)
        self.user_attr = user_attr
        self.timeout = timeout
        self.tls_verify = tls_verify
        self.ca_file = ca_file

    def describe(self):
        return f"{'ldaps' if self.secure else 'ldap'}://{self.host}:{self.port} (alan: {self.domain})"

    def _connect(self):
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.secure:
            ctx = ssl.create_default_context(cafile=self.ca_file or None)
            if not self.tls_verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            s = ctx.wrap_socket(s, server_hostname=self.host)
        s.settimeout(self.timeout)
        return s

    @staticmethod
    def _recv_msg(s, pending):
        """Soketten tam bir LDAPMessage gelene kadar okur."""
        buf = pending
        while True:
            if len(buf) >= 2:
                n = buf[1]
                hdr = 2
                if n & 0x80:
                    k = n & 0x7F
                    if len(buf) >= 2 + k:
                        n = int.from_bytes(buf[2:2 + k], "big")
                        hdr = 2 + k
                    else:
                        n = None
                if n is not None and len(buf) >= hdr + n:
                    return buf[:hdr + n], buf[hdr + n:]
            chunk = s.recv(65536)
            if not chunk:
                raise LdapError(-1, "bağlantı kapandı", "Kimlik sunucusu bağlantıyı kapattı.")
            buf += chunk

    def authenticate(self, username, password):
        """Başarılıysa {'display': ..} döner, değilse LdapError fırlatır."""
        if not password:  # boş şifre LDAP'ta "anonim giriş" sayılır ve BAŞARILI döner -> asla kabul etme
            raise LdapError(49, "boş şifre", "Şifre boş olamaz.")
        bind_name = self.bind_format.format(user=username, domain=self.domain)
        try:
            s = self._connect()
        except ssl.SSLCertVerificationError as e:
            raise LdapError(-1, f"sertifika doğrulanamadı: {e!r}",
                            "Kimlik sunucusunun sertifikası doğrulanamadı (PT_LDAP_CA_FILE). IT ile görüşün.")
        except (OSError, ssl.SSLError) as e:
            raise LdapError(-1, f"bağlantı hatası: {e!r}",
                            "Kimlik sunucusuna (Active Directory) ulaşılamadı. IT ile görüşün.")
        try:
            # BindRequest ::= [APPLICATION 0] SEQUENCE { version 3, name, simple [0] password }
            bind = _tlv(0x60, _int(3) + _str(bind_name) + _str(password, 0x80))
            s.sendall(_tlv(0x30, _int(1) + bind))
            msg, rest = self._recv_msg(s, b"")
            _, body, _ = _read_tlv(msg, 0)
            parts = _children(body)
            op_tag, op = parts[1]
            if op_tag != 0x61:
                raise LdapError(-1, f"beklenmeyen yanıt {op_tag:#x}", "Kimlik sunucusundan beklenmeyen yanıt.")
            res = _children(op)
            code = int.from_bytes(res[0][1], "big")
            diag = res[2][1].decode("utf-8", "replace") if len(res) > 2 else ""
            if code == 49:
                raise LdapError(49, diag, _ad_reason(diag))
            if code == 8:
                raise LdapError(8, diag, "Kimlik sunucusu şifreli bağlantı istiyor (ldaps://...:636). IT ile görüşün.")
            if code != 0:
                raise LdapError(code, diag, f"Giriş yapılamadı (LDAP kodu {code}).")
            display = self._display_name(s, rest, username)
            try:
                s.sendall(_tlv(0x30, _int(3) + bytes([0x42, 0x00])))  # UnbindRequest
            except OSError:
                pass
            return {"display": display}
        except LdapError:
            raise
        except (OSError, ssl.SSLError, IndexError, ValueError) as e:
            raise LdapError(-1, f"iletişim hatası: {e!r}", "Kimlik sunucusuyla iletişim kurulamadı.")
        finally:
            try:
                s.close()
            except OSError:
                pass

    def _display_name(self, s, pending, username):
        """Giriş yapan kullanıcının adını (displayName) okur; olmazsa kullanıcı adını döner."""
        try:
            flt = _tlv(0xA3, _str(self.user_attr) + _str(username))  # equalityMatch
            attrs = _tlv(0x30, _str("displayName"))
            search = _tlv(0x63, _str(self.base_dn) + _int(2, 0x0A) + _int(0, 0x0A) + _int(1) + _int(5)
                          + _tlv(0x01, b"\x00") + flt + attrs)
            s.sendall(_tlv(0x30, _int(2) + search))
            buf = pending
            for _ in range(20):
                msg, buf = self._recv_msg(s, buf)
                _, body, _ = _read_tlv(msg, 0)
                parts = _children(body)
                tag, op = parts[1]
                if tag == 0x64:  # SearchResultEntry
                    entry = _children(op)
                    for a_tag, a in _children(entry[1][1]):
                        kv = _children(a)
                        if kv[0][1].decode().lower() == "displayname":
                            vals = _children(kv[1][1])
                            if vals and vals[0][1]:
                                return vals[0][1].decode("utf-8", "replace")
                elif tag == 0x65:  # SearchResultDone
                    break
        except Exception:
            pass
        return username


# ---------------------------------------------------------------- deneme sınırı
class Throttle:
    """Aynı kullanıcı ya da IP için 5 dakikada 5 hatalı denemeden sonra 5 dakika bekletir
    (AD hesabının kilitlenmesini de önler)."""
    def __init__(self, limit=5, window=300):
        self.limit, self.window = limit, window
        self.fails = {}
        self.lock = threading.Lock()

    def blocked(self, *keys):
        t = time.time()
        with self.lock:
            for k in keys:
                xs = [x for x in self.fails.get(k, []) if t - x < self.window]
                self.fails[k] = xs
                if len(xs) >= self.limit:
                    return int(self.window - (t - xs[0])) + 1
        return 0

    def fail(self, *keys):
        with self.lock:
            for k in keys:
                self.fails.setdefault(k, []).append(time.time())

    def ok(self, *keys):
        with self.lock:
            for k in keys:
                self.fails.pop(k, None)


# ---------------------------------------------------------------- kullanıcılar ve oturumlar
class Auth:
    def __init__(self, conn, lock, ldap=None, default_role="viewer", admin_user="admin", session_hours=168):
        if default_role not in ROLES:
            raise ValueError("PT_DEFAULT_ROLE viewer, engineer, editor ya da none olmalı")
        self.conn, self.lock, self.ldap = conn, lock, ldap
        self.default_role, self.admin_user = default_role, admin_user
        self.session_hours = session_hours
        self.t_user = Throttle(limit=5)    # aynı kullanıcı adı: 5 hatalı deneme / 5 dk
        self.t_ip = Throttle(limit=20)     # aynı IP (vekil arkasında herkes aynı IP olabilir): 20 / 5 dk
        with self.lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS users(
                    username TEXT PRIMARY KEY, display TEXT, role TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'ldap', pw_hash TEXT,
                    added_by TEXT, added_at TEXT, updated_by TEXT, updated_at TEXT, last_login TEXT);
                CREATE TABLE IF NOT EXISTS sessions(
                    token_hash TEXT PRIMARY KEY, username TEXT NOT NULL,
                    created TEXT, expires TEXT NOT NULL, ip TEXT);
                CREATE INDEX IF NOT EXISTS sessions_user ON sessions(username);
            """)

    # yerel yönetici
    def has_local_admin(self):
        with self.lock:
            r = self.conn.execute("SELECT 1 FROM users WHERE username=? AND source='local' AND pw_hash IS NOT NULL",
                                  (self.admin_user,)).fetchone()
        return bool(r)

    def set_local_admin_password(self, pw):
        if len(pw) < 10:
            raise ValueError("Yönetici şifresi en az 10 karakter olmalı")
        t = iso(utcnow())
        with self.lock:
            self.conn.execute(
                "INSERT INTO users(username,display,role,source,pw_hash,added_by,added_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(username) DO UPDATE SET pw_hash=excluded.pw_hash, role='admin', source='local'",
                (self.admin_user, "Yerel yönetici", "admin", "local", hash_password(pw), "kurulum", t))
            self.conn.execute("DELETE FROM sessions WHERE username=?", (self.admin_user,))

    # giriş
    def login(self, raw_user, password, ip):
        """-> (token, kullanıcı bilgisi) ya da (None, hata mesajı)"""
        user = normalize_username(raw_user)
        if not user or not password:
            return None, "Kullanıcı adı ve şifre gerekli."
        wait = self.t_user.blocked(user) or self.t_ip.blocked(ip)
        if wait:
            return None, f"Çok fazla hatalı deneme. {(wait + 59) // 60} dakika sonra tekrar deneyin."
        with self.lock:
            row = self.conn.execute("SELECT role, source, pw_hash, display FROM users WHERE username=?",
                                    (user,)).fetchone()
        if row and row[1] == "local":
            if not row[2] or not verify_password(password, row[2]):
                self.t_user.fail(user)
                self.t_ip.fail(ip)
                return None, "Kullanıcı adı veya şifre hatalı."
            display = row[3] or user
        else:
            if not self.ldap:
                return None, "Şirket hesabıyla giriş ayarlanmamış (PT_LDAP_URL)."
            try:
                info = self.ldap.authenticate(user, password)
            except LdapError as e:
                if e.code == 49:
                    self.t_user.fail(user)
                    self.t_ip.fail(ip)
                return None, e.user_message
            display = info["display"]
        self.t_user.ok(user)
        t = iso(utcnow())
        with self.lock:
            if not row:
                self.conn.execute("INSERT INTO users(username,display,role,source,added_by,added_at,last_login) "
                                  "VALUES(?,?,?,?,?,?,?)", (user, display, self.default_role, "ldap", "ilk giriş", t, t))
                role = self.default_role
            else:
                role = row[0]
                self.conn.execute("UPDATE users SET display=COALESCE(?,display), last_login=? WHERE username=?",
                                  (display if row[1] != "local" else None, t, user))
        if role == "none":
            return None, "Bu uygulamaya giriş yetkiniz yok. Ar-Ge yöneticisine başvurun."
        token = secrets.token_urlsafe(32)
        exp = utcnow() + dt.timedelta(hours=self.session_hours)
        with self.lock:
            self.conn.execute("DELETE FROM sessions WHERE expires < ?", (iso(utcnow()),))
            self.conn.execute("INSERT INTO sessions(token_hash,username,created,expires,ip) VALUES(?,?,?,?,?)",
                              (self._h(token), user, t, iso(exp), ip))
        return token, self.user_info(user)

    def logout(self, token):
        if token:
            with self.lock:
                self.conn.execute("DELETE FROM sessions WHERE token_hash=?", (self._h(token),))

    @staticmethod
    def _h(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def session_user(self, token):
        """Çerezdeki anahtardan geçerli kullanıcıyı bulur (rol her istekte tablodan okunur)."""
        if not token:
            return None
        with self.lock:
            r = self.conn.execute(
                "SELECT s.username, u.role, u.display FROM sessions s JOIN users u ON u.username=s.username "
                "WHERE s.token_hash=? AND s.expires > ?", (self._h(token), iso(utcnow()))).fetchone()
        if not r or r[1] == "none":
            return None
        return {"user": r[0], "role": r[1], "display": r[2] or r[0]}

    def header_user(self, raw):
        """Ters vekil (IIS) kimliği başlıkta iletiyorsa: kullanıcıyı tanı, gerekirse varsayılan rolle ekle."""
        user = normalize_username(raw)
        if not user:
            return None
        with self.lock:
            r = self.conn.execute("SELECT role, display FROM users WHERE username=?", (user,)).fetchone()
            if not r:
                t = iso(utcnow())
                self.conn.execute("INSERT INTO users(username,display,role,source,added_by,added_at,last_login) "
                                  "VALUES(?,?,?,?,?,?,?)", (user, user, self.default_role, "ldap", "ilk giriş", t, t))
                r = (self.default_role, user)
        if r[0] == "none":
            return None
        return {"user": user, "role": r[0], "display": r[1] or user}

    def user_info(self, user):
        with self.lock:
            r = self.conn.execute("SELECT username, display, role FROM users WHERE username=?", (user,)).fetchone()
        return {"user": r[0], "display": r[1] or r[0], "role": r[2]}

    # yetki paneli
    def list_users(self):
        with self.lock:
            rows = self.conn.execute("SELECT username, display, role, source, added_by, added_at, updated_by, "
                                     "updated_at, last_login FROM users ORDER BY source DESC, username").fetchall()
        keys = ("user", "display", "role", "source", "addedBy", "addedAt", "updatedBy", "updatedAt", "lastLogin")
        return [dict(zip(keys, r)) for r in rows]

    def set_role(self, actor, raw_user, role):
        user = normalize_username(raw_user)
        if not user:
            raise ValueError("Geçersiz kullanıcı adı (ör. berkay.yilmaz)")
        if role not in ROLES:
            raise ValueError("Geçersiz rol")
        if user == actor:
            raise PermissionError("Kendi rolünüzü değiştiremezsiniz.")
        t = iso(utcnow())
        with self.lock:
            r = self.conn.execute("SELECT source FROM users WHERE username=?", (user,)).fetchone()
            if r and r[0] == "local":
                raise PermissionError("Yerel yönetici hesabının rolü değiştirilemez.")
            self.conn.execute(
                "INSERT INTO users(username,display,role,source,added_by,added_at,updated_by,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(username) DO UPDATE SET role=excluded.role, "
                "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (user, user, role, "ldap", actor, t, actor, t))
            if role == "none":
                self.conn.execute("DELETE FROM sessions WHERE username=?", (user,))
        return user

    def remove_user(self, actor, raw_user):
        user = normalize_username(raw_user)
        if not user:
            raise ValueError("Geçersiz kullanıcı adı")
        if user == actor:
            raise PermissionError("Kendinizi silemezsiniz.")
        with self.lock:
            r = self.conn.execute("SELECT source FROM users WHERE username=?", (user,)).fetchone()
            if r and r[0] == "local":
                raise PermissionError("Yerel yönetici hesabı silinemez.")
            self.conn.execute("DELETE FROM users WHERE username=?", (user,))
            self.conn.execute("DELETE FROM sessions WHERE username=?", (user,))
        return user


def ldap_from_env():
    url = os.environ.get("PT_LDAP_URL", "").strip()
    if not url:
        return None
    return LdapClient(
        url,
        os.environ.get("PT_LDAP_DOMAIN", "").strip(),
        bind_format=os.environ.get("PT_LDAP_BIND_FORMAT", "{user}@{domain}"),
        base_dn=os.environ.get("PT_LDAP_BASE_DN", "").strip(),
        user_attr=os.environ.get("PT_LDAP_USER_ATTR", "sAMAccountName"),
        timeout=float(os.environ.get("PT_LDAP_TIMEOUT", "6")),
        tls_verify=os.environ.get("PT_LDAP_TLS_VERIFY", "1") != "0",
        ca_file=os.environ.get("PT_LDAP_CA_FILE", "").strip(),
    )
