#!/usr/bin/env python3
"""
"H84X PROTOTİPLER GENEL" (matris) Excel'i -> seed.json dönüştürücü.

Kullanım:
    pip install openpyxl
    python tools/genel_to_seed.py "H84X PROTOTİPLER GENEL 3.xlsx" seed/seed.json
    python server.py --import seed/seed.json     # talepleri ve ürünleri bu veriyle değiştirir
                                                 # (önce otomatik yedek alır; kullanıcılar/roller korunur)

Excel'in ilk sayfası ("Güncel") okunur:
    Satır 1   : grup başlıkları  (C: FORD, J: IVECO, M: SANEL)  -> talebin müşterisi
    Satır 2   : teslim yeri      (Ford, Pelzer Pimsa, Assan Hanil, Oto Trim, Farplas)
    Satır 3   : faz              (FAZ 2 EBV, DCV, Batch 1, …, FMC1278 2)
    Satır 4   : termin           (boş olabilir)
    Satır 5.. : ürünler. B = ürün adı, C..T = o fazda istenen ürün adedi,
                W/X/Y = Ford / Iveco / Sanel kodu, AA.. = alt parçalar (stok kodu, ham madde kodu, kütle)
Gri dolgulu ürün satırları alınmaz. Görseller (#VALUE! görünen hücreler) yok sayılır.
Tip, üretici ve sorumlu Excel'de yok; boş gelir ve uygulamadaki ürün ağacından girilir.
"""
import datetime as dt
import json
import re
import sys
from pathlib import Path

try:
    import openpyxl
except ImportError:
    sys.exit("Bu araç için openpyxl gerekli:  pip install openpyxl")

GROUPS = {"FORD": "Ford", "IVECO": "Iveco", "SANEL": "Sanel"}
PHASE_FIX = {"Pre-FMC2": "Pre-FMC 2", "Pre-FMC1": "Pre-FMC 1"}
IMAGE = "#VALUE!"


def clean(v):
    return " ".join(str(v).split()) if v is not None else ""


def slug(s):
    s = s.lower()
    for a, b in (("ı", "i"), ("ş", "s"), ("ğ", "g"), ("ü", "u"), ("ö", "o"), ("ç", "c"), ("/", " ")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def is_gray(cell):
    """Excel'de 'gri' satır: beyaz tema renginin koyulaştırılmış hali ya da gri bir RGB."""
    f = cell.fill
    if not f or not f.fill_type:
        return False
    c = f.fgColor
    if c.type == "theme":
        return c.theme in (0, 2) and (c.tint or 0) < 0
    if c.type == "rgb" and c.rgb:
        rgb = c.rgb[-6:]
        r, g, b = int(rgb[0:2], 16), int(rgb[2:4], 16), int(rgb[4:6], 16)
        return abs(r - g) < 8 and abs(g - b) < 8 and 120 < r < 240
    return False


def split_codes(v):
    """'2 800 0863     1 020 0160' -> ('2 800 0863', '1 020 0160');  'Ömür e sorulacak' -> ('', not)"""
    if v is None:
        return None
    s = str(v).replace("\xa0", " ").strip()
    if not s or s in (IMAGE, '"'):
        return None
    toks = [t.strip(" -") for t in re.split(r"\s{2,}|\n|\s-\s", s) if t.strip(" -")]
    if not toks:
        return None
    looks_code = lambda t: bool(re.search(r"\d", t)) and len(t) <= 24 and not re.search(r"[a-zçğıöşü]{4,}", t)
    if re.fullmatch(r"\d+(?:[.,]\d+)?\s*mm", toks[0], re.I):   # light pipe boyu (ör. 285mm) -> malzeme bilgisi
        return {"stock": "", "material": " / ".join(toks), "note": ""}
    if not looks_code(toks[0]):
        return {"stock": "", "material": "", "note": s}
    return {"stock": toks[0], "material": " / ".join(toks[1:]), "note": ""}


def convert(xlsx_path):
    wb = openpyxl.load_workbook(xlsx_path)
    ws = wb.worksheets[0]
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- faz sütunları (satır 1-4); grup başlığı birleştirilmiş hücrede, sağa doğru geçerli
    phase_cols, group = [], ""
    last_col = 0
    for c in range(1, ws.max_column + 1):
        g = clean(ws.cell(1, c).value).upper()
        if g in GROUPS:
            group = GROUPS[g]
        if clean(ws.cell(1, c).value) and g not in GROUPS and c > 3:
            break  # FORD KODU vb. -> faz bölgesi bitti
        ph = clean(ws.cell(3, c).value)
        if ph and group:
            ph = PHASE_FIX.get(ph, ph)
            site = clean(ws.cell(2, c).value) or group
            due = ws.cell(4, c).value
            due = due.strftime("%Y-%m-%d") if isinstance(due, (dt.datetime, dt.date)) else None
            phase_cols.append({"col": c, "customer": group, "phase": ph, "platform": site, "due": due})
            last_col = c

    # --- kod ve parça sütunları
    hdr = {c: clean(ws.cell(1, c).value) for c in range(1, ws.max_column + 1) if clean(ws.cell(1, c).value)}
    code_col = {k: next((c for c, h in hdr.items() if h.upper() == k), None) for k in ("FORD KODU", "IVECO KODU", "SANEL KODU")}
    part_cols = [c for c, h in hdr.items() if c > max(filter(None, code_col.values()), default=last_col)
                 and not h.lower().startswith("kütle")]
    seen_names = set()
    parts_def = []
    for i, c in enumerate(part_cols):
        name = hdr[c].upper()
        if name in seen_names:      # aynı başlık ikinci kez (ör. MOUNTING BRACKET CAP) -> atla
            continue
        seen_names.add(name)
        nxt = part_cols[i + 1] if i + 1 < len(part_cols) else ws.max_column + 1
        mass = next((m for m in range(c + 1, nxt) if hdr.get(m, "").lower().startswith("kütle")), None)
        parts_def.append({"col": c, "name": re.sub(r"\s*/\s*", " / ", hdr[c]).strip(), "mass": mass})

    # --- ürünler
    products, skipped, rows = {}, [], []
    for r in range(5, ws.max_row + 1):
        name = clean(ws.cell(r, 2).value)
        if not name or name == IMAGE:
            continue
        if is_gray(ws.cell(r, 2)):
            skipped.append(name)
            continue
        pid = slug(name)
        parts = []
        for pdef in parts_def:
            info = split_codes(ws.cell(r, pdef["col"]).value)
            if not info:
                continue
            m = ws.cell(r, pdef["mass"]).value if pdef["mass"] else None
            parts.append({"part": pdef["name"], "perSet": 1, "type": "Belirsiz", "maker": "", "owner": "",
                          "stock": info["stock"], "material": info["material"],
                          "massG": m if isinstance(m, (int, float)) else None, "note": info["note"]})
        def code(k):
            c = code_col[f"{k.upper()} KODU"]
            info = split_codes(ws.cell(r, c).value) if c else None
            return " / ".join(x for x in (info["stock"], info["material"]) if x) if info else ""
        codes = {k: code(k) for k in ("ford", "iveco", "sanel")}
        products[pid] = {"name": name, "customer": "Ford", "program": "H84X", "codes": codes,
                         "ord": len(products) + 1, "parts": parts, "updatedAt": now}
        rows.append((r, pid))

    # --- faz planı ve talepler
    plan = []
    for pc in phase_cols:
        pc["id"] = slug(f"{pc['customer']}-{pc['phase']}-{pc['platform']}")
        plan.append({k: pc[k] for k in ("id", "customer", "phase", "platform", "due")})
    requests, n = {}, 0
    for r, pid in rows:
        p = products[pid]
        for pc in phase_cols:
            q = ws.cell(r, pc["col"]).value
            if not isinstance(q, (int, float)) or q <= 0:
                continue
            q = int(q)
            n += 1
            items = {}
            for j, part in enumerate(p["parts"], 1):
                items[f"i{j:02d}"] = {"part": part["part"], "type": "Belirsiz", "maker": "", "material": part["material"],
                                      "stock": part["stock"], "qty": q * part["perSet"], "delivered": None, "owner": "",
                                      "status": "Açık", "note": part["note"], "ord": j, "updatedAt": now}
            requests[f"r{n:03d}"] = {"product": pid, "customer": pc["customer"], "phase": pc["phase"],
                                     "platform": pc["platform"], "planId": pc["id"], "due": pc["due"],
                                     "sets": q, "direct": None, "spare": 0, "note": "",
                                     "createdAt": now, "updatedAt": now, "items": items}

    phases = []
    for pc in phase_cols:
        if pc["phase"] not in phases:
            phases.append(pc["phase"])
    platforms = []
    for pc in phase_cols:
        if pc["platform"] not in platforms:
            platforms.append(pc["platform"])
    config = {"phases": phases, "platforms": platforms, "people": [], "makers": ["Sanel"],
              "customers": list(GROUPS.values()), "programs": ["H84X"], "plan": plan}
    return {"products": products, "requests": requests, "config": config,
            "source": {"file": Path(xlsx_path).name, "convertedAt": now, "skippedGray": skipped}}


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    out = convert(sys.argv[1])
    Path(sys.argv[2]).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    n_items = sum(len(r["items"]) for r in out["requests"].values())
    print(f"{len(out['products'])} ürün, {len(out['requests'])} talep, {n_items} kalem, "
          f"{len(out['config']['plan'])} faz sütunu -> {sys.argv[2]}")
    if out["source"]["skippedGray"]:
        print("Gri olduğu için alınmayan satırlar:", ", ".join(out["source"]["skippedGray"]))
