#!/usr/bin/env python3
"""
Ar-Ge Prototip List (Excel) -> seed.json dönüştürücü.

Kullanım:
    pip install openpyxl
    python tools/excel_to_seed.py "Ar-Ge Prototip List.xlsx" seed/seed.json
    python server.py --import seed/seed.json      # veritabanını bu veriyle değiştirir (önce yedek alır)

Excel'in "List" sayfası okunur. Sütunlar:
    Müşteri | Proje | Platform | Faz | Termin | Alt Parça | Üretici | Ham Madde | Araç Set | Talep Miktarı | Sorumlu | Durum
Hücre renkleri anlamlıdır:
    sarı = Sanel üretecek, turuncu = alt tedarikçi, yeşil = bitti (Hazır), kırmızı "?" = üretici belirsiz.
Ürün ağacı (alt parçalar ve araç seti başına adetler) seed/bom.json'dan alınır;
orada olmayan yeni bir proje görülürse, Excel'deki alt parçalarıyla (adet 1) otomatik eklenir.
"""
import collections
import datetime as dt
import json
import re
import sys
from pathlib import Path

try:
    import openpyxl
except ImportError:
    sys.exit("Bu araç için openpyxl gerekli:  pip install openpyxl")

HERE = Path(__file__).resolve().parent.parent

# Excel'deki "Proje" adı -> (ürün id, ürün adı, müşteri, program)
PROJECT_MAP = {
    "H84X Ambient": ("ambient", "Ambient", "Ford", "H84X"),
    "H84X Ceiling Left": ("ceiling-left", "Ceiling Left", "Ford", "H84X"),
    "H84X Ceiling Right": ("ceiling-right", "Ceiling Right", "Ford", "H84X"),
    "H84X Overhead": ("overhead", "Overhead", "Ford", "H84X"),
    "H84X Puddle": ("puddle", "Puddle", "Ford", "H84X"),
    "H84X Reading": ("reading", "Reading Lamp", "Ford", "H84X"),
    "H84X Stowage": ("stowage", "Stowage", "Ford", "H84X"),
    "H84X Uppershelf Sto.": ("uppershelf", "Uppershelf Stowage", "Ford", "H84X"),
    "25A Converter": ("conv25a", "25A Converter", "Ford", "25A Converter"),
    "Entrance Lamp": ("entrance", "Entrance Lamp", "Daimler Truck", "Entrance Lamp"),
    "STI Left": ("sti-left", "STI Left", "", "STI"),
    "STI Right": ("sti-right", "STI Right", "", "STI"),
}


def slug(s):
    s = s.lower()
    for a, b in (("ı", "i"), ("ş", "s"), ("ğ", "g"), ("ü", "u"), ("ö", "o"), ("ç", "c")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "urun"


def fill_tag(cell):
    f = cell.fill
    if not f or not f.fill_type:
        return None
    c = f.fgColor
    if c.type == "rgb":
        return {"FFFFFF00": "yellow", "FFFF0000": "red"}.get(c.rgb)
    if c.type == "theme":
        # Office teması: 5 = turuncu (accent2), 6 = yeşil (accent3); 0/2 = gri tonları
        return {5: "orange", 6: "green"}.get(c.theme, "gray" if c.theme in (0, 2) else None)
    return None


def clean_material(v):
    """Yazım farklarını birleştirir; revizyon yazımına (Rev01, Rev2…) dokunmaz."""
    if v is None:
        return ""
    v = str(v).strip()
    if v == "?":
        return ""
    v = v.replace("PC+ABS", "PC-ABS")
    v = re.sub(r"3D Reçine\s*-\s*", "3D Reçine - ", v)
    v = v.replace("3D Reçine - Gri", "3D Reçine - Gray")
    return re.sub(r"\s+", " ", v)


def iso(d):
    return d.strftime("%Y-%m-%d") if isinstance(d, (dt.datetime, dt.date)) else None


def convert(xlsx_path, bom_path):
    bom = json.loads(Path(bom_path).read_text(encoding="utf-8"))
    products = bom["products"]
    config = bom["config"]
    ws = openpyxl.load_workbook(xlsx_path)["List"]

    rows = []
    for r in range(2, ws.max_row + 1):
        v = [ws.cell(r, c).value for c in range(1, 13)]
        if not any(x is not None for x in v):
            continue
        mus, proj, plat, faz, termin, parca, ure, ham, aset, qty, sor, dur = v
        if not proj:
            continue
        proj = str(proj).strip()
        if proj in PROJECT_MAP:
            pid, pname, cust, prog = PROJECT_MAP[proj]
        else:
            pid, pname, cust, prog = slug(proj), proj, (str(mus).strip() if mus else ""), proj
        if pid not in products:
            products[pid] = {"name": pname, "program": prog, "customer": cust,
                             "ord": max([p.get("ord", 0) for p in products.values()] + [0]) + 1, "parts": []}
        color = fill_tag(ws.cell(r, 1)) or fill_tag(ws.cell(r, 6)) or fill_tag(ws.cell(r, 7))
        maker = "" if ure in (None, "?") else str(ure).strip()
        material = clean_material(ham)
        if maker == "Titreşim Fikstürü":          # Excel'de yanlış sütuna yazılmış
            material, maker = material or maker, "Sanel"
        ds = str(dur).strip() if dur else ""
        note = ""
        if ds == "Gönderildi":
            status = "Gönderildi"
        elif ds == "Hazır":
            status = "Hazır"
        elif color == "green":
            status, note = "Hazır", ds
        elif ds.startswith("Reçine siparişi"):
            status, note = "Sipariş Verilecek", ds
        elif ds.startswith("Data hazır"):
            status, note = "Teklif Alınacak", ds
        elif ds.startswith("Data termini"):
            status, note = "Data Bekleniyor", ds
        elif ds.startswith("Sipariş edildi"):
            status = "Sipariş Verildi"
        else:
            status, note = "Açık", ds
        if "Stoktan" in ds:
            ptype = "Stok"
        elif color == "yellow":
            ptype = "Sanel"
        elif color == "orange":
            ptype = "Alt Tedarikçi"
        elif maker == "Sanel":
            ptype = "Sanel"
        elif maker.startswith("OutSource") or maker in ("YSH", "Aksem"):
            ptype = "Alt Tedarikçi"
        else:
            ptype = "Belirsiz"
        part = str(parca).strip() if parca else None
        if part == "Gövde üst":
            part = "Gövde Üst"
        rows.append(dict(pid=pid, phase=(str(faz).strip() if faz else ""), platform=(str(plat).strip() if plat else ""),
                         due=iso(termin), part=part, type=ptype, maker=maker, material=material,
                         sets=aset if isinstance(aset, (int, float)) else None,
                         qty=qty if isinstance(qty, (int, float)) else None,
                         owner=(str(sor).strip() if sor else ""), status=status, note=note))

    # Yeni projelerin alt parçalarını ürün ağacına ekle
    for x in rows:
        p = products[x["pid"]]
        if x["part"] and not any(pp["part"] == x["part"] for pp in p["parts"]):
            if x["pid"] not in {v[0] for v in PROJECT_MAP.values()}:
                p["parts"].append({"part": x["part"], "perSet": 1, "type": x["type"], "maker": x["maker"],
                                   "material": x["material"], "owner": x["owner"]})

    # Talepler: (ürün, faz, platform, termin) gruplanır; aynı ürün/faz/platformda set sayısı uyumluysa
    # farklı terminler tek talepte birleşir ve farklı olan kalem kendi terminini taşır.
    groups = collections.OrderedDict()
    for x in rows:
        groups.setdefault((x["pid"], x["phase"], x["platform"], x["due"]), []).append(x)
    merged = []
    for key, xs in groups.items():
        sets = max([y["sets"] for y in xs if y["sets"]] or [0]) or None
        target = None
        for g in merged:
            if g["key"][:3] == key[:3] and key[3] != g["key"][3] and (g["sets"] == sets or not g["sets"] or not sets) \
                    and all(y["sets"] is None and y["part"] for y in xs + g["items"]):
                target = g
                break
        for y in xs:
            y["itemdue"] = key[3]
        if target:
            target["items"] += xs
            target["sets"] = target["sets"] or sets
        else:
            merged.append({"key": key, "sets": sets, "items": xs})

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    requests = {}
    for n, g in enumerate(merged, 1):
        pid, phase, platform, due = g["key"]
        real = [y for y in g["items"] if y["part"]]
        direct = None if real else g["items"][0]["qty"]
        if real:
            due = collections.Counter(y["itemdue"] for y in real).most_common(1)[0][0]
        items, seen = {}, set()
        for j, y in enumerate(real, 1):
            it = {"part": y["part"], "type": y["type"], "maker": y["maker"], "material": y["material"],
                  "qty": y["qty"], "delivered": None, "owner": y["owner"], "status": y["status"],
                  "note": y["note"], "ord": j, "updatedAt": now}
            if y["itemdue"] and y["itemdue"] != due:
                it["due"] = y["itemdue"]
                if abs(int(y["itemdue"][:4]) - int((due or y["itemdue"])[:4])) >= 1:
                    it["note"] = (it["note"] + " | " if it["note"] else "") + "KONTROL: termin yılı diğer kalemlerden farklı"
            k = (y["part"], y["material"], y["qty"])
            if k in seen:
                it["note"] = (it["note"] + " | " if it["note"] else "") + "KONTROL: aynı kalem iki kez girilmiş olabilir"
            seen.add(k)
            items[f"i{j:02d}"] = it
        requests[f"r{n:03d}"] = {"product": pid, "phase": phase or platform, "platform": platform, "due": due,
                                 "sets": int(g["sets"]) if g["sets"] else None, "direct": direct, "spare": 0,
                                 "note": "", "createdAt": now, "updatedAt": now, "items": items}

    # Listeleri Excel'de görülen değerlerle tamamla
    def add(key, vals):
        for v in vals:
            if v and v not in config[key]:
                config[key].append(v)
    add("phases", [r["phase"] for r in requests.values()])
    add("platforms", [r["platform"] for r in requests.values()])
    add("people", [x["owner"] for x in rows])
    add("makers", [x["maker"] for x in rows])
    add("customers", [p["customer"] for p in products.values()])
    add("programs", [p["program"] for p in products.values()])
    return {"products": products, "requests": requests, "config": config,
            "source": {"file": Path(xlsx_path).name, "rows": len(rows), "convertedAt": now}}


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    bom = sys.argv[3] if len(sys.argv) > 3 else HERE / "seed" / "bom.json"
    out = convert(sys.argv[1], bom)
    Path(sys.argv[2]).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    n_items = sum(len(r["items"]) for r in out["requests"].values())
    print(f"{out['source']['rows']} satır -> {len(out['requests'])} talep, {n_items} alt parça, "
          f"{len(out['products'])} ürün  ->  {sys.argv[2]}")
