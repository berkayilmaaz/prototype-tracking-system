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

Ortam değişkenleri (isteğe bağlı):
    PT_PORT, PT_HOST, PT_DATA        komut satırı seçenekleriyle aynı
    PT_USER_HEADER                   ters vekil (IIS/nginx) kimliği bu başlıkta iletiyorsa, ör. X-Remote-User
    PT_READONLY=1                    yalnızca okuma (yazma istekleri 403 döner)
    PT_BACKUP_KEEP=30                saklanacak günlük yedek sayısı
"""
import argparse
import datetime as dt
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
from urllib.parse import unquote, urlparse

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

    def _user(self):
        if self.user_header:
            u = self.headers.get(self.user_header, "").strip()
            if u:
                return u.split("\\")[-1][:120]
        return (unquote(self.headers.get("X-PT-User", "")) or "").strip()[:120] or "anonim"

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
        if path == "/api/state":
            return self._json(200, self.store.state())
        if path == "/api/health":
            return self._json(200, {"ok": True, "readonly": self.readonly})
        if path == "/api/me":
            return self._json(200, {"user": self._user() if self.user_header else None, "readonly": self.readonly})
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
        col, id_ = t
        by = self._user()
        try:
            if op == "delete":
                seq = self.store.delete(col, id_, by)
                broadcast({"col": col, "id": id_, "doc": None, "rev": seq})
                return self._json(200, {"ok": True})
            data = self._body()
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

    def do_PUT(self):
        self._write("set")

    def do_PATCH(self):
        self._write("update")

    def do_DELETE(self):
        self._write("delete")


def main():
    ap = argparse.ArgumentParser(description="Sanel Ar-Ge Prototip Takip sunucusu")
    ap.add_argument("--host", default=os.environ.get("PT_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PT_PORT", "8080")))
    ap.add_argument("--data", default=os.environ.get("PT_DATA", str(ROOT / "data")))
    ap.add_argument("--import", dest="import_file", help="Verileri bu seed/yedek JSON dosyasıyla değiştir ve çık")
    ap.add_argument("--export", dest="export_file", help="Tüm veriyi bu JSON dosyasına yaz ve çık")
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

    if store.is_empty():
        seed_path = ROOT / "seed" / "seed.json"
        if seed_path.exists():
            n = store.replace_all(json.loads(seed_path.read_text(encoding="utf-8")), by="ilk-kurulum")
            log(f"İlk kurulum: seed.json'dan {n} belge yüklendi.")

    Handler.store = store
    Handler.readonly = os.environ.get("PT_READONLY", "") == "1"
    Handler.user_header = os.environ.get("PT_USER_HEADER", "").strip()
    keep = int(os.environ.get("PT_BACKUP_KEEP", "30"))
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
