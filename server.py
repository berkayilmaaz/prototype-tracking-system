#!/usr/bin/env python3
"""
Sanel Ar-Ge Prototip Takip — sunucu

Ek kütüphane gerektirmez (yalnızca Python 3.9+ standart kütüphanesi).
Veriler tek bir SQLite dosyasında tutulur: data/prototip.db

Çalıştırma:
    python server.py                         # http://0.0.0.0:8080
    python server.py --port 9000 --data D:\\prototip-data
    python server.py --import seed/seed.json # verileri bu dosyayla değiştir (önce otomatik yedek alır)
    python server.py --export yedek.json     # tüm veriyi JSON olarak dışa aktar
    python server.py --set-admin-password    # yerel yönetici (admin) şifresini belirle
    python server.py --ldap-test berkay.yilmaz   # AD bağlantısını dene (şifre sorar)

Ortam değişkenleri (isteğe bağlı):
    PT_PORT, PT_HOST, PT_DATA        komut satırı seçenekleriyle aynı
    PT_USER_HEADER                   ters vekil (IIS/nginx) kimliği bu başlıkta iletiyorsa, ör. X-Remote-User
    PT_READONLY=1                    yalnızca okuma (yazma istekleri 403 döner)
    PT_BACKUP_KEEP=30                saklanacak günlük yedek sayısı

Giriş ve yetki (ayrıntı: auth.py ve README):
    PT_LDAP_URL=ldap://10.34.10.1:389    Active Directory adresi (ayarlıysa giriş zorunlu olur)
    PT_LDAP_DOMAIN=sanelelk.local        AD alan adı
    PT_AUTH=1                            LDAP olmadan da giriş zorunlu olsun (yalnızca yerel yönetici)
    PT_DEFAULT_ROLE=viewer               ilk kez giren şirket kullanıcısının rolü (viewer / editor / none)
    PT_ADMIN_PASSWORD=...                ilk açılışta yerel yönetici hesabı yoksa bu şifreyle oluşturulur
    PT_SESSION_HOURS=168                 oturum süresi (saat)
    PT_COOKIE_SECURE=1                   uygulama HTTPS arkasındaysa açın
"""
import argparse
import datetime as dt
import getpass
import json
import mimetypes
import os
import queue
import re
import shutil
import sqlite3
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from http.cookies import SimpleCookie
from urllib.parse import unquote, urlparse

import auth as authmod

ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT / "app"
COLLECTIONS = {"products", "requests", "config"}
ID_RE = re.compile(r"^[A-Za-z0-9_\-.~:@+]{1,200}$")
MAX_BODY = 1024 * 1024

DB_LOCK = threading.RLock()
SUBSCRIBERS = set()
SUB_LOCK = threading.Lock()


def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def log(*a):
    print(dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- veritabanı
class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS docs(
                col TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1, updated_at TEXT, updated_by TEXT,
                PRIMARY KEY(col, id));
            CREATE TABLE IF NOT EXISTS history(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, col TEXT, id TEXT,
                op TEXT, by TEXT, data TEXT);
        """)

    def is_empty(self):
        return self.conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 0

    def state(self):
        with DB_LOCK:
            return self._state()

    def _state(self):
        out = {c: {} for c in COLLECTIONS}
        for col, id_, data, ver in self.conn.execute("SELECT col, id, data, version FROM docs"):
            out.setdefault(col, {})[id_] = json.loads(data)
        rev = self.conn.execute("SELECT COALESCE(MAX(seq),0) FROM history").fetchone()[0]
        return {"products": out["products"], "requests": out["requests"],
                "config": out["config"].get("lists", {}), "rev": rev}

    def get(self, col, id_):
        row = self.conn.execute("SELECT data, version FROM docs WHERE col=? AND id=?", (col, id_)).fetchone()
        return (json.loads(row[0]), row[1]) if row else (None, 0)

    def _record(self, col, id_, op, by, data):
        self.conn.execute("INSERT INTO history(ts,col,id,op,by,data) VALUES(?,?,?,?,?,?)",
                          (now(), col, id_, op, by, None if data is None else json.dumps(data, ensure_ascii=False)))
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def set(self, col, id_, data, by):
        with DB_LOCK:
            self.conn.execute("BEGIN")
            try:
                _, ver = self.get(col, id_)
                self.conn.execute(
                    "INSERT INTO docs(col,id,data,version,updated_at,updated_by) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(col,id) DO UPDATE SET data=excluded.data, version=excluded.version, "
                    "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                    (col, id_, json.dumps(data, ensure_ascii=False), ver + 1, now(), by))
                seq = self._record(col, id_, "set", by, data)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return data, seq

    def update(self, col, id_, patch, by):
        with DB_LOCK:
            self.conn.execute("BEGIN")
            try:
                cur, ver = self.get(col, id_)
                if cur is None:
                    self.conn.execute("ROLLBACK")
                    return None, None
                merged = deep_merge(cur, patch)
                self.conn.execute("UPDATE docs SET data=?, version=?, updated_at=?, updated_by=? WHERE col=? AND id=?",
                                  (json.dumps(merged, ensure_ascii=False), ver + 1, now(), by, col, id_))
                seq = self._record(col, id_, "update", by, patch)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return merged, seq

    def delete(self, col, id_, by):
        with DB_LOCK:
            self.conn.execute("BEGIN")
            try:
                self.conn.execute("DELETE FROM docs WHERE col=? AND id=?", (col, id_))
                seq = self._record(col, id_, "delete", by, None)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return seq

    def replace_all(self, seed, by="import"):
        with DB_LOCK:
            self.conn.execute("BEGIN")
            try:
                self.conn.execute("DELETE FROM docs")
                ts = now()
                rows = [("products", k, v) for k, v in seed.get("products", {}).items()]
                rows += [("requests", k, v) for k, v in seed.get("requests", {}).items()]
                rows += [("config", "lists", seed.get("config", {}))]
                for col, id_, data in rows:
                    self.conn.execute("INSERT INTO docs(col,id,data,version,updated_at,updated_by) VALUES(?,?,?,?,?,?)",
                                      (col, id_, json.dumps(data, ensure_ascii=False), 1, ts, by))
                self._record("*", "*", "import", by, {"source": seed.get("source")})
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return len(rows)

    def backup(self, folder: Path, tag: str):
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"prototip-{tag}.db"
        with DB_LOCK:
            b = sqlite3.connect(str(dest))
            self.conn.backup(b)
            b.close()
        return dest


def deep_merge(base, patch):
    """Firestore 'update' gibi: iç içe nesneler birleşir, diğer her şey (diziler dahil) değişir."""
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _state_of(doc):
    """Talebin onay durumu. 'approval' alanı olmayan (yönetici/düzenleyicinin açtığı ya da eski) talepler onaylı sayılır."""
    return ((doc or {}).get("approval") or {}).get("state") or "approved"


def approval_policy(me, op, col, existing, data):
    """Proje mühendisi talep akışı kuralları. -> (yazılacak veri, None) ya da (None, red nedeni)

    - Ürünler ve listeler: Düzenleme ve üstü.
    - Proje Mühendisi: yeni talebi yalnızca 'onay bekliyor' olarak açar; sadece kendi açtığı ve henüz
      onaylanmamış talebi düzenler/geri çeker. Reddedilen talebi düzenlerse tekrar onaya gider.
    - Düzenleme: onaylı talepleri düzenler; onay bekleyenlere dokunamaz, onay veremez.
    - Yönetici: her şey; onay/ret kararı adı ve zamanıyla kaydedilir.
    """
    role, user = me["role"], me["user"]
    rank = authmod.RANK.get(role, 0)
    if col != "requests":
        if rank < authmod.RANK["editor"]:
            return None, "Düzenleme yetkiniz yok."
        return data, None
    t = now()
    old = _state_of(existing) if existing else None
    old_appr = (existing or {}).get("approval") or {}
    owner = old_appr.get("requestedBy")
    new = None if op == "delete" else (data if op == "set" else deep_merge(existing or {}, data))
    if op == "update" and existing is None:
        return data, None  # store.update zaten 404 döner

    if role == "admin":
        if new is not None and (existing is None or _state_of(new) != old):
            a = dict(new.get("approval") or {})
            st = _state_of(new)
            if st in ("approved", "rejected") and new.get("approval"):
                a.update(decidedBy=user, decidedByName=me["display"], decidedAt=t)
                if st == "approved":
                    a["reason"] = ""
            elif st == "pending" and existing is None:
                a.update(requestedBy=user, requestedByName=me["display"], requestedAt=t)
            if a:
                data = dict(data)
                data["approval"] = a if op == "set" else {**(data.get("approval") or {}), **a}
        return data, None

    if role == "engineer":
        if existing is None:
            if op != "set":
                return None, "Talep bulunamadı."
            data = dict(data)
            data["approval"] = {"state": "pending", "requestedBy": user, "requestedByName": me["display"],
                                "requestedAt": t}
            return data, None
        if old == "approved" or owner != user:
            return None, ("Bu talebi değiştiremezsiniz: Proje Mühendisi yalnızca kendi açtığı ve henüz "
                          "onaylanmamış talepleri düzenleyebilir.")
        if op == "delete":
            return data, None
        a = {"state": "pending", "requestedBy": owner, "requestedByName": old_appr.get("requestedByName", user),
             "requestedAt": old_appr.get("requestedAt", t)}
        if old == "rejected":  # düzeltip tekrar gönderdi
            a.update(resubmittedAt=t, lastReason=old_appr.get("reason", ""), reason="", decidedBy=None,
                     decidedByName=None, decidedAt=None)
        data = dict(data)
        data["approval"] = a if op == "set" else {**(data.get("approval") or {}), **a}
        return data, None

    if role == "editor":
        if existing is None:
            if op == "set" and data.get("approval") and _state_of(data) != "approved":
                return None, "Talebi onaya göndermek Proje Mühendisi içindir; Düzenleme rolü talebi doğrudan açar."
            return data, None
        if old != "approved":
            return None, "Onay bekleyen talebi yalnızca talebi açan kişi ve yönetici değiştirebilir."
        if new is not None and _state_of(new) != "approved":
            return None, "Talep onayı ve reddi yalnızca yöneticidedir."
        return data, None

    return None, "Yetkiniz yok."


def broadcast(event):
    msg = json.dumps(event, ensure_ascii=False)
    with SUB_LOCK:
        for q in list(SUBSCRIBERS):
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


def backup_loop(store, folder, keep):
    while True:
        try:
            tag = dt.date.today().isoformat()
            dest = folder / f"prototip-{tag}.db"
            if not dest.exists():
                store.backup(folder, tag)
                log("Günlük yedek alındı:", dest.name)
                olds = sorted(folder.glob("prototip-????-??-??.db"))
                for f in olds[:-keep]:
                    f.unlink(missing_ok=True)
        except Exception as e:  # yedek hatası uygulamayı durdurmasın
            log("Yedek alınamadı:", e)
        time.sleep(3600)


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "PrototipTakip/1.0"
    store: Store = None
    readonly = False
    user_header = ""
    auth: "authmod.Auth" = None   # None ise giriş kapalı (eski davranış: herkes düzenleyebilir)
    backup_dir: Path = None
    backup_keep = 30
    cookie_secure = False

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/events"):
            log(self.address_string(), fmt % args)

    # yardımcılar
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, code_name, message):
        self._json(code, {"error": {"code": code_name, "message": message}})

    def _legacy_user(self):
        if self.user_header:
            u = self.headers.get(self.user_header, "").strip()
            if u:
                return u.split("\\")[-1][:120]
        return (unquote(self.headers.get("X-PT-User", "")) or "").strip()[:120] or "anonim"

    def _token(self):
        c = SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        m = c.get(authmod.COOKIE)
        return m.value if m else None

    def _ident(self):
        """İsteği yapan kişi: {'user', 'display', 'role'} ya da None (giriş yapılmamış)."""
        if not self.auth:
            u = self._legacy_user()
            return {"user": u, "display": u, "role": "editor"}
        if self.user_header and self.headers.get(self.user_header, "").strip():
            return self.auth.header_user(self.headers.get(self.user_header))
        return self.auth.session_user(self._token())

    def _need(self, role):
        """Yetki kontrolü. Yetki yoksa hata yanıtını kendisi gönderir ve None döner."""
        me = self._ident()
        if not me:
            self._err(401, "unauthenticated", "Oturum açmanız gerekiyor.")
            return None
        if authmod.RANK.get(me["role"], 0) < authmod.RANK[role]:
            msg = {"engineer": "Talep oluşturma yetkiniz yok.", "editor": "Düzenleme yetkiniz yok.",
                   "admin": "Bu işlem için yönetici yetkisi gerekiyor."}
            self._err(403, "invalid_argument", msg.get(role, "Yetkiniz yok."))
            return None
        return me

    def _cookie(self, token, max_age):
        v = f"{authmod.COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"
        return v + ("; Secure" if self.cookie_secure else "")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("İstek çok büyük")
        raw = self.rfile.read(n) if n else b"{}"
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Gövde bir JSON nesnesi olmalı")
        return data

    def _doc_target(self, path):
        m = re.match(r"^/api/docs/([^/]+)/([^/]+)$", path)
        if not m:
            return None
        col, id_ = unquote(m.group(1)), unquote(m.group(2))
        if col not in COLLECTIONS or not ID_RE.match(id_):
            return None
        return col, id_

    # GET
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            return self._json(200, {"ok": True, "readonly": self.readonly})
        if path == "/api/me":
            me = self._ident()
            if not me:
                return self._json(401, {"error": {"code": "unauthenticated", "message": "Oturum açmanız gerekiyor."},
                                        "auth": True, "ldap": bool(self.auth and self.auth.ldap)})
            return self._json(200, {
                "user": me["user"] if (self.auth or self.user_header) else None,
                "display": me["display"], "role": me["role"], "auth": bool(self.auth),
                "sso": bool(self.user_header),
                "readonly": self.readonly or me["role"] in ("viewer", "engineer"),
                "canRequest": not self.readonly and me["role"] in ("engineer", "editor", "admin"),
                "canApprove": bool(self.auth) and not self.readonly and me["role"] == "admin",
                "canManage": bool(self.auth) and me["role"] == "admin"})
        if path.startswith("/api/") and not self._need("viewer"):
            return
        if path == "/api/state":
            return self._json(200, self.store.state())
        if path == "/api/admin/users":
            if not self._need("admin"):
                return
            return self._json(200, {"users": self.auth.list_users(), "defaultRole": self.auth.default_role,
                                    "adminUser": self.auth.admin_user, "roles": authmod.ROLE_TR})
        if path.startswith("/api/admin/backup"):
            if self.auth and not self._need("admin"):
                return
            return self._backups(path)
        if path == "/api/export.xlsx":   # "Excel'e aktar": Genel tablo düzeni + Kalemler
            import export_xlsx
            data = export_xlsx.build(self.store.state())
            return self._send_file(data, f"H84X-prototipler-{dt.date.today().strftime('%d.%m.%Y')}.xlsx",
                                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if path == "/api/export.json":
            st = self.store.state()
            body = json.dumps({"products": st["products"], "requests": st["requests"], "config": st["config"],
                               "source": {"exportedAt": now()}}, ensure_ascii=False, indent=1).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="prototip-yedek-{dt.date.today()}.json"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/events":
            return self._events()
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        return self._static(path)

    BACKUP_RE = re.compile(r"^prototip-[A-Za-z0-9_.\-]{1,80}\.db$")

    def _send_file(self, data, name, ctype="application/octet-stream"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _backups(self, path):
        """Yönetim → Yedekler: anlık .db yedeği, günlük yedek listesi ve indirme (yalnızca Yönetici)."""
        folder = self.backup_dir
        if path == "/api/admin/backup.db":   # şu anki verinin tutarlı kopyası (SQLite backup API)
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                dest = self.store.backup(Path(tmp), "anlik")
                data = dest.read_bytes()
            log("Yedek indirildi (anlık):", (self._ident() or {}).get("user"))
            return self._send_file(data, f"prototip-anlik-{dt.datetime.now().strftime('%Y%m%d-%H%M')}.db")
        if path == "/api/admin/backups":
            files = []
            if folder.exists():
                for f in sorted(folder.glob("prototip-*.db"), key=lambda x: x.stat().st_mtime, reverse=True):
                    st = f.stat()
                    files.append({"name": f.name, "size": st.st_size,
                                  "mtime": dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
            return self._json(200, {"backups": files[:60], "keep": self.backup_keep})
        m = re.match(r"^/api/admin/backups/([^/]+)$", path)
        if m:
            name = unquote(m.group(1))
            f = folder / name
            if not self.BACKUP_RE.match(name) or not f.is_file() or f.resolve().parent != folder.resolve():
                return self._err(404, "not_found", "Yedek bulunamadı")
            log("Yedek indirildi:", name, (self._ident() or {}).get("user"))
            return self._send_file(f.read_bytes(), name)
        return self._err(404, "not_found", "Bulunamadı")

    def _events(self):
        q = queue.Queue(maxsize=500)
        with SUB_LOCK:
            SUBSCRIBERS.add(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b"retry: 5000\n\n")
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=25)
                    self.wfile.write(("data: " + msg + "\n\n").encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with SUB_LOCK:
                SUBSCRIBERS.discard(q)

    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        target = (APP_DIR / path.lstrip("/")).resolve()
        if APP_DIR.resolve() not in target.parents or not target.is_file():
            return self._err(404, "not_found", "Bulunamadı")
        body = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    # yazma
    def _write(self, op):
        t = self._doc_target(urlparse(self.path).path)
        if not t:
            return self._err(400, "invalid_argument", "Geçersiz adres")
        if self.readonly:
            return self._err(403, "invalid_argument", "Bu sunucu yalnızca okuma modunda")
        me = self._need("engineer")
        if not me:
            return
        col, id_ = t
        by = me["user"]
        try:
          data = None if op == "delete" else self._body()   # gövde kilitten önce okunur
          with DB_LOCK:   # kontrol + yazma arasında başka biri onay durumunu değiştiremesin
            existing, _ = self.store.get(col, id_)
            data, why = approval_policy(me, op, col, existing, data)
            if why:
                return self._err(403, "invalid_argument", why)
            if op == "delete":
                seq = self.store.delete(col, id_, by)
                broadcast({"col": col, "id": id_, "doc": None, "rev": seq})
                return self._json(200, {"ok": True})
            if op == "set":
                doc, seq = self.store.set(col, id_, data, by)
            else:
                doc, seq = self.store.update(col, id_, data, by)
                if doc is None:
                    return self._err(404, "not_found", "Belge yok; güncellenemedi")
            broadcast({"col": col, "id": id_, "doc": doc, "rev": seq})
            return self._json(200, {"ok": True, "doc": doc})
        except (ValueError, json.JSONDecodeError) as e:
            return self._err(400, "invalid_argument", str(e))
        except Exception as e:
            log("Yazma hatası:", repr(e))
            return self._err(500, "unavailable", "Sunucu hatası")

    def _is_json(self):
        # Başka sitelerden gelen düz form gönderimlerini reddetmek için (CSRF): JSON zorunlu
        return self.headers.get("Content-Type", "").split(";")[0].strip().lower() == "application/json"

    def do_POST(self):
        path = urlparse(self.path).path
        if not self.auth or path not in ("/api/login", "/api/logout"):
            return self._err(404, "not_found", "Bulunamadı")
        if not self._is_json():
            return self._err(400, "invalid_argument", "JSON bekleniyor")
        if path == "/api/logout":
            self.auth.logout(self._token())
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Set-Cookie", self._cookie("", 0))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            data = self._body()
        except (ValueError, json.JSONDecodeError):
            return self._err(400, "invalid_argument", "Geçersiz istek")
        ip = self.client_address[0]
        token, info = self.auth.login(str(data.get("username", "")), str(data.get("password", "")), ip)
        if not token:
            log(ip, "Giriş başarısız:", authmod.normalize_username(str(data.get("username", ""))) or "?", "-", info)
            return self._err(401, "unauthenticated", info)
        log(ip, "Giriş:", info["user"], f"({info['role']})")
        body = json.dumps({"ok": True, **info}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Set-Cookie", self._cookie(token, self.auth.session_hours * 3600))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _admin_users(self, op):
        m = re.match(r"^/api/admin/users/([^/]+)$", urlparse(self.path).path)
        if not self.auth or not m:
            return False
        me = self._need("admin")
        if not me:
            return True
        target = unquote(m.group(1))
        try:
            if op == "set":
                if not self._is_json():
                    return self._err(400, "invalid_argument", "JSON bekleniyor") or True
                role = str(self._body().get("role", ""))
                user = self.auth.set_role(me["user"], target, role)
                log("Yetki:", me["user"], "->", user, "=", role)
            else:
                user = self.auth.remove_user(me["user"], target)
                log("Yetki:", me["user"], "kaldırdı:", user)
            self._json(200, {"ok": True, "users": self.auth.list_users()})
        except PermissionError as e:
            self._err(403, "invalid_argument", str(e))
        except (ValueError, json.JSONDecodeError) as e:
            self._err(400, "invalid_argument", str(e))
        return True

    def do_PUT(self):
        if self._admin_users("set"):
            return
        self._write("set")

    def do_PATCH(self):
        self._write("update")

    def do_DELETE(self):
        if self._admin_users("delete"):
            return
        self._write("delete")


def main():
    ap = argparse.ArgumentParser(description="Sanel Ar-Ge Prototip Takip sunucusu")
    ap.add_argument("--host", default=os.environ.get("PT_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PT_PORT", "8080")))
    ap.add_argument("--data", default=os.environ.get("PT_DATA", str(ROOT / "data")))
    ap.add_argument("--import", dest="import_file", help="Verileri bu seed/yedek JSON dosyasıyla değiştir ve çık")
    ap.add_argument("--export", dest="export_file", help="Tüm veriyi bu JSON dosyasına yaz ve çık")
    ap.add_argument("--set-admin-password", action="store_true", help="Yerel yönetici şifresini belirle ve çık")
    ap.add_argument("--ldap-test", metavar="KULLANICI", help="Active Directory girişini dene ve çık")
    args = ap.parse_args()

    data_dir = Path(args.data)
    store = Store(data_dir / "prototip.db")
    backups = data_dir / "backups"

    if args.export_file:
        st = store.state()
        Path(args.export_file).write_text(json.dumps({k: st[k] for k in ("products", "requests", "config")},
                                                     ensure_ascii=False, indent=1), encoding="utf-8")
        print("Dışa aktarıldı:", args.export_file)
        return
    if args.import_file:
        seed = json.loads(Path(args.import_file).read_text(encoding="utf-8"))
        if not store.is_empty():
            dest = store.backup(backups, "ice-aktarma-oncesi-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
            print("Mevcut veri yedeklendi:", dest)
        n = store.replace_all(seed)
        print(f"{n} belge yüklendi ({len(seed.get('requests', {}))} talep).")
        return

    ldap = authmod.ldap_from_env()
    auth_on = bool(ldap) or os.environ.get("PT_AUTH", "") == "1"
    auth = authmod.Auth(store.conn, DB_LOCK, ldap=ldap,
                        default_role=os.environ.get("PT_DEFAULT_ROLE", "viewer").strip(),
                        admin_user=os.environ.get("PT_ADMIN_USER", "admin").strip().lower(),
                        session_hours=int(os.environ.get("PT_SESSION_HOURS", "168")))

    if args.set_admin_password:
        pw = os.environ.get("PT_ADMIN_PASSWORD") or getpass.getpass(f"'{auth.admin_user}' için yeni şifre: ")
        if not os.environ.get("PT_ADMIN_PASSWORD") and pw != getpass.getpass("Tekrar: "):
            sys.exit("Şifreler aynı değil.")
        try:
            auth.set_local_admin_password(pw)
        except ValueError as e:
            sys.exit(str(e))
        print(f"Yerel yönetici '{auth.admin_user}' şifresi kaydedildi.")
        return
    if args.ldap_test:
        if not ldap:
            sys.exit("PT_LDAP_URL ayarlı değil.")
        user = authmod.normalize_username(args.ldap_test)
        print("Sunucu:", ldap.describe(), "| bağlanılacak ad:", ldap.bind_format.format(user=user, domain=ldap.domain))
        try:
            info = ldap.authenticate(user, getpass.getpass("Şifre: "))
            print("BAŞARILI. Görünen ad:", info["display"])
        except authmod.LdapError as e:
            print("BAŞARISIZ:", e.user_message, f"(kod {e.code}: {e})")
            sys.exit(1)
        return

    if auth_on and not auth.has_local_admin() and os.environ.get("PT_ADMIN_PASSWORD"):
        auth.set_local_admin_password(os.environ["PT_ADMIN_PASSWORD"])
        log(f"Yerel yönetici '{auth.admin_user}' oluşturuldu (PT_ADMIN_PASSWORD).")

    if store.is_empty():
        seed_path = ROOT / "seed" / "seed.json"
        if seed_path.exists():
            n = store.replace_all(json.loads(seed_path.read_text(encoding="utf-8")), by="ilk-kurulum")
            log(f"İlk kurulum: seed.json'dan {n} belge yüklendi.")

    Handler.store = store
    Handler.backup_dir = backups
    Handler.readonly = os.environ.get("PT_READONLY", "") == "1"
    Handler.user_header = os.environ.get("PT_USER_HEADER", "").strip()
    Handler.auth = auth if auth_on else None
    Handler.cookie_secure = os.environ.get("PT_COOKIE_SECURE", "") == "1"
    if auth_on:
        log("Giriş zorunlu. Şirket hesabı:", ldap.describe() if ldap else "kapalı (yalnızca yerel yönetici)",
            "| ilk girişte rol:", auth.default_role)
        if not auth.has_local_admin():
            log("UYARI: Yerel yönetici şifresi yok. 'python server.py --set-admin-password' ile belirleyin.")
        if ldap and not ldap.secure:
            log("UYARI: LDAP şifresiz (ldap://) bağlantı kullanıyor; şifreler ağda açık gider. Mümkünse ldaps:// kullanın.")
    else:
        log("Giriş kapalı: adrese erişen herkes düzenleyebilir (PT_LDAP_URL ayarlanınca giriş zorunlu olur).")
    keep = int(os.environ.get("PT_BACKUP_KEEP", "30"))
    Handler.backup_keep = keep
    threading.Thread(target=backup_loop, args=(store, backups, keep), daemon=True).start()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    log(f"Prototip Takip çalışıyor: http://{args.host}:{args.port}  (veri: {data_dir.resolve()})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("Kapatılıyor.")


if __name__ == "__main__":
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    sys.exit(main())
