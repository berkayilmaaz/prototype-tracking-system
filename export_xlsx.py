"""Excel'e aktar: "H84X PROTOTİPLER GENEL" düzeninde .xlsx üretir (yalnızca Python standart kütüphanesi).

Sayfa 1 "Genel": satırlar ürün, sütunlar müşteri → teslim yeri → faz → termin (Excel'deki gibi 4 başlık satırı),
hücrede o teslimata istenen ürün adedi; TOPLAM sütunu ve satırı formüllü; ardından Ford/Iveco/Sanel kodları ve
her alt parça için stok kodu + ham madde (+ kütle). Renk: yeşil = tamamı hazır, kırmızı = termini geçti ve
eksik var, sarı = 7 gün içinde. Onay bekleyen ve reddedilen talepler dahil edilmez.
Sayfa 2 "Kalemler": tüm onaylı talep kalemleri tek tabloda (filtreli).

Yeni ürün, yeni faz/teslim yeri ya da fazsız teslimat eklendiğinde sütun ve satırlar kendiliğinden oluşur.
"""
import datetime as dt
import io
import re
import zipfile
from xml.sax.saxutils import escape

DONE = ("Hazır", "Gönderildi")
CUST_ORDER = ["Ford", "Iveco", "Sanel"]
CUST_FILL = {"Ford": 8, "Iveco": 9, "Sanel": 10}   # styles: fill indeksleri

# ---------------------------------------------------------------- stiller
FONTS = [  # (boyut, kalın, renk)
    (10, False, None), (10, True, None), (12, True, None), (13, True, None), (9, False, "FF595959")]
FILLS = [None, "gray125", "FF3A3A3A", "FFF2F2F2", "FFDDEBF7", "FFC6EFCE", "FFFFC7CE", "FFFFEB9C",
         "FFD9E1F2", "FFD5ECE8", "FFE7E6E6"]
# (font, fill, border, yatay hizalama, dikey orta, satır kaydır, sayı biçimi)
XF = {
    "default": (0, 0, 0, None, False, False, 0),
    "head": (1, 3, 1, "center", True, True, 0),
    "grp_Ford": (2, 8, 1, "center", True, False, 0),
    "grp_Iveco": (2, 9, 1, "center", True, False, 0),
    "grp_Sanel": (2, 10, 1, "center", True, False, 0),
    "grp": (2, 3, 1, "center", True, False, 0),
    "date": (1, 3, 1, "center", True, False, 164),
    "prod": (1, 0, 1, "center", True, True, 0),
    "qty": (2, 0, 1, "center", True, False, 0),
    "qty_done": (2, 5, 1, "center", True, False, 0),
    "qty_late": (2, 6, 1, "center", True, False, 0),
    "qty_soon": (2, 7, 1, "center", True, False, 0),
    "total": (3, 4, 1, "center", True, False, 0),
    "text": (0, 0, 1, "center", True, True, 0),
    "sep": (0, 2, 0, None, False, False, 0),
    "num": (0, 0, 1, "center", True, False, 0),
    "lhead": (1, 3, 1, "left", True, True, 0),
    "lcell": (0, 0, 1, "left", True, False, 0),
    "ldate": (0, 0, 1, "center", True, False, 164),
    "lnum": (0, 0, 1, "right", True, False, 0),
    "note": (4, 0, 0, "left", True, False, 0),
    "sw_done": (0, 5, 1, None, False, False, 0),
    "sw_late": (0, 6, 1, None, False, False, 0),
    "sw_soon": (0, 7, 1, None, False, False, 0),
}
XF_NAMES = list(XF)
S = {k: i for i, k in enumerate(XF_NAMES)}


def styles_xml():
    fonts = "".join(
        '<font>' + ('<b/>' if b else '') + '<sz val="%s"/>' % sz + ('<color rgb="%s"/>' % c if c else '')
        + '<name val="Calibri"/><family val="2"/></font>'
        for sz, b, c in FONTS)
    fills = []
    for f in FILLS:
        if f is None:
            fills.append('<fill><patternFill patternType="none"/></fill>')
        elif f == "gray125":
            fills.append('<fill><patternFill patternType="gray125"/></fill>')
        else:
            fills.append(f'<fill><patternFill patternType="solid"><fgColor rgb="{f}"/><bgColor indexed="64"/></patternFill></fill>')
    thin = '<{0} style="thin"><color rgb="FFA6A6A6"/></{0}>'
    borders = ('<border><left/><right/><top/><bottom/><diagonal/></border>'
               '<border>' + "".join(thin.format(s) for s in ("left", "right", "top", "bottom")) + '<diagonal/></border>')
    xfs = []
    for name in XF_NAMES:
        fo, fi, bo, h, vc, wr, nf = XF[name]
        al = ""
        if h or vc or wr:
            al = "<alignment" + (f' horizontal="{h}"' if h else "") + (' vertical="center"' if vc else "") + (' wrapText="1"' if wr else "") + "/>"
        x = '<xf numFmtId="%d" fontId="%d" fillId="%d" borderId="%d" xfId="0"' % (nf, fo, fi, bo)
        x += (' applyNumberFormat="1"' if nf else '') + ' applyFont="1" applyFill="1" applyBorder="1"'
        x += (' applyAlignment="1">' + al + '</xf>') if al else '/>'
        xfs.append(x)
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<numFmts count="1"><numFmt numFmtId="164" formatCode="dd\\.mm\\.yyyy"/></numFmts>'
            f'<fonts count="{len(FONTS)}">{fonts}</fonts><fills count="{len(FILLS)}">{"".join(fills)}</fills>'
            f'<borders count="2">{borders}</borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            f'<cellXfs count="{len(xfs)}">{"".join(xfs)}</cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')


# ---------------------------------------------------------------- sayfa yazıcı
def col_name(i):  # 1 -> A
    s = ""
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")


class Sheet:
    def __init__(self, name):
        self.name, self.cells, self.widths, self.heights = name, {}, {}, {}
        self.merges, self.freeze, self.autofilter = [], None, None

    def set(self, r, c, v, style="default"):
        self.cells[(r, c)] = (v, S[style])

    def style(self, r, c, style):
        v = self.cells.get((r, c), (None, 0))[0]
        self.cells[(r, c)] = (v, S[style])

    def merge(self, r1, c1, r2, c2, style=None):
        if (r1, c1) != (r2, c2):
            self.merges.append(f"{col_name(c1)}{r1}:{col_name(c2)}{r2}")
        if style:
            for r in range(r1, r2 + 1):
                for c in range(c1, c2 + 1):
                    if (r, c) not in self.cells or (r, c) != (r1, c1):
                        self.style(r, c, style)

    def xml(self):
        rows = {}
        for (r, c), v in self.cells.items():
            rows.setdefault(r, {})[c] = v
        out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
               'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
               '<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>']
        out.append("<sheetViews><sheetView workbookViewId=\"0\" zoomScale=\"90\">")
        if self.freeze:
            fr, fc = self.freeze
            out.append(f'<pane xSplit="{fc - 1}" ySplit="{fr - 1}" topLeftCell="{col_name(fc)}{fr}" activePane="bottomRight" state="frozen"/>'
                       f'<selection pane="bottomRight" activeCell="{col_name(fc)}{fr}" sqref="{col_name(fc)}{fr}"/>')
        out.append("</sheetView></sheetViews><sheetFormatPr defaultRowHeight=\"15\"/>")
        if self.widths:
            out.append("<cols>" + "".join(f'<col min="{c}" max="{c}" width="{w}" customWidth="1"/>' for c, w in sorted(self.widths.items())) + "</cols>")
        out.append("<sheetData>")
        for r in sorted(rows):
            ht = self.heights.get(r)
            out.append(f'<row r="{r}"' + (f' ht="{ht}" customHeight="1"' if ht else "") + ">")
            for c in sorted(rows[r]):
                v, st = rows[r][c]
                ref = f"{col_name(c)}{r}"
                if v is None or v == "":
                    out.append(f'<c r="{ref}" s="{st}"/>')
                elif isinstance(v, bool):
                    out.append(f'<c r="{ref}" s="{st}" t="b"><v>{int(v)}</v></c>')
                elif isinstance(v, (int, float)):
                    out.append(f'<c r="{ref}" s="{st}"><v>{v}</v></c>')
                elif isinstance(v, dt.date):
                    out.append(f'<c r="{ref}" s="{st}"><v>{(v - dt.date(1899, 12, 30)).days}</v></c>')
                elif isinstance(v, str) and v.startswith("="):
                    out.append(f'<c r="{ref}" s="{st}"><f>{escape(v[1:])}</f></c>')
                else:
                    t = escape(_BAD.sub("", str(v)))
                    out.append(f'<c r="{ref}" s="{st}" t="inlineStr"><is><t xml:space="preserve">{t}</t></is></c>')
            out.append("</row>")
        out.append("</sheetData>")
        if self.autofilter:
            out.append(f'<autoFilter ref="{self.autofilter}"/>')
        if self.merges:
            out.append(f'<mergeCells count="{len(self.merges)}">' + "".join(f'<mergeCell ref="{m}"/>' for m in self.merges) + "</mergeCells>")
        out.append('<pageMargins left="0.4" right="0.4" top="0.5" bottom="0.5" header="0.3" footer="0.3"/>'
                   '<pageSetup orientation="landscape" paperSize="9" fitToWidth="1" fitToHeight="0"/></worksheet>')
        return "".join(out)


def workbook_bytes(sheets):
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
          + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                    for i in range(1, len(sheets) + 1)) + "</Types>")
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
          + "".join(f'<sheet name="{escape(s.name)}" sheetId="{i}" r:id="rId{i}"/>' for i, s in enumerate(sheets, 1))
          + "</sheets>")
    names = ['<definedName name="_xlnm._FilterDatabase" localSheetId="%d" hidden="1">\'%s\'!%s</definedName>'
             % (i, s.name, re.sub(r"([A-Z]+)(\d+)", r"$\1$\2", s.autofilter))
             for i, s in enumerate(sheets) if s.autofilter]
    wb += ("<definedNames>" + "".join(names) + "</definedNames>" if names else "") + "</workbook>"
    wrels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                       for i in range(1, len(sheets) + 1))
             + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wrels)
        z.writestr("xl/styles.xml", styles_xml())
        for i, s in enumerate(sheets, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", s.xml())
    return buf.getvalue()


# ---------------------------------------------------------------- veri
def _approved(r):
    return not r.get("deleted") and ((r.get("approval") or {}).get("state") or "approved") == "approved"


def _date(iso):
    try:
        return dt.date.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def _norm_type(t):
    return "Alt Tedarikçi" if t == "Fason" else (t or "Belirsiz")


def build(state, today=None):
    today = today or dt.date.today()
    products = state.get("products") or {}
    cfg = state.get("config") or {}
    phases, platforms = cfg.get("phases") or [], cfg.get("platforms") or []
    reqs = {k: r for k, r in (state.get("requests") or {}).items() if _approved(r)}

    def cust(r):
        return r.get("customer") or (products.get(r.get("product")) or {}).get("customer") or "—"

    def ckey(c, ph, plat, due, adhoc=False, label=""):
        return (c, "__adhoc" if adhoc else ph, plat or c, due or "", label if adhoc else "")

    # Sütunlar: faz planı satırları (talebi olmasa da, Excel'deki gibi) + taleplerdeki teslimatlar
    cols = {}
    for x in cfg.get("plan") or []:
        k = ckey(x.get("customer") or "—", x.get("phase") or "", x.get("platform"), x.get("due"))
        cols.setdefault(k, {"c": k[0], "ph": x.get("phase") or "", "plat": k[2], "due": x.get("due"), "adhoc": False, "label": ""})
    for r in reqs.values():
        c = cust(r)
        k = ckey(c, r.get("phase") or "", r.get("platform"), r.get("due"), r.get("adhoc"), r.get("label") or "")
        cols.setdefault(k, {"c": c, "ph": r.get("phase") or "", "plat": k[2], "due": r.get("due"), "adhoc": bool(r.get("adhoc")), "label": r.get("label") or ""})
    oi = lambda lst, v: lst.index(v) if v in lst else len(lst) + 1
    ordered = sorted(cols.items(), key=lambda kv: (
        oi(CUST_ORDER, kv[1]["c"]), kv[1]["c"], 1 if kv[1]["adhoc"] else 0, oi(phases, kv[1]["ph"]), kv[1]["ph"],
        oi(platforms, kv[1]["plat"]), kv[1]["due"] or "9999"))
    groups = []  # [(müşteri, [(anahtar, sütun bilgisi)])]
    for k, v in ordered:
        if not groups or groups[-1][0] != v["c"]:
            groups.append((v["c"], []))
        groups[-1][1].append((k, v))

    prods = sorted(products.items(), key=lambda kv: (kv[1].get("ord") or 0, kv[1].get("name") or ""))
    # aynı ürün × teslimat için talepler (adet toplanır, durum hesaplanır)
    cell = {}
    for r in reqs.values():
        k = ckey(cust(r), r.get("phase") or "", r.get("platform"), r.get("due"), r.get("adhoc"), r.get("label") or "")
        cell.setdefault((r.get("product"), k), []).append(r)

    # alt parça grupları: ürünlerin sırasına göre ilk görülen sıra
    part_order, has_mass = [], set()
    for _, p in prods:
        for x in p.get("parts") or []:
            nm = (x.get("part") or "").strip()
            if nm and nm not in part_order:
                part_order.append(nm)
            if nm and x.get("massG") not in (None, ""):
                has_mass.add(nm)

    sh = Sheet("Genel")
    HR = 4
    sh.widths[1] = 26
    sh.set(1, 1, "ÜRÜN", "head"); sh.merge(1, 1, HR, 1, "head")
    col = 2
    qty_cols = []
    for c, items in groups:
        c1 = col
        for k, v in items:
            sh.widths[col] = 12.5
            sh.set(2, col, v["plat"], "head")
            sh.set(3, col, ("Fazsız" + (" · " + v["label"] if v["label"] else "")) if v["adhoc"] else v["ph"], "head")
            d = _date(v["due"])
            sh.set(4, col, d if d else "tarih yok", "date")
            qty_cols.append((col, k, v))
            col += 1
        sty = "grp_" + c if "grp_" + c in S else "grp"
        sh.set(1, c1, c.upper(), sty); sh.merge(1, c1, 1, col - 1, sty)
        sh.widths[col] = 1.4
        col += 1
    first_q, last_q = (qty_cols[0][0], qty_cols[-1][0]) if qty_cols else (None, None)
    tot_col = col
    sh.widths[tot_col] = 11
    sh.set(1, tot_col, "TOPLAM", "head"); sh.merge(1, tot_col, HR, tot_col, "head")
    col += 1
    sh.widths[col] = 1.4; col += 1
    code_cols = {}
    for key, lbl in (("ford", "FORD KODU"), ("iveco", "IVECO KODU"), ("sanel", "SANEL KODU")):
        sh.widths[col] = 17 if key != "ford" else 19
        sh.set(1, col, lbl, "head"); sh.merge(1, col, HR, col, "head")
        code_cols[key] = col; col += 1
    sh.widths[col] = 1.4; col += 1
    part_cols = {}
    for nm in part_order:
        sh.widths[col] = 18
        sh.set(1, col, nm.upper(), "head"); sh.merge(1, col, HR, col, "head")
        part_cols[nm] = [col]; col += 1
        if nm in has_mass:
            sh.widths[col] = 8
            sh.set(1, col, "Kütle\n(g)", "head"); sh.merge(1, col, HR, col, "head")
            part_cols[nm].append(col); col += 1
        sh.widths[col] = 1.4; col += 1
    last_col = col - 1
    for r in range(1, HR + 1):  # ayraç sütunları başlıkta da koyu
        for cc in range(2, last_col + 1):
            if sh.widths.get(cc) == 1.4:
                sh.set(r, cc, None, "sep")
    sh.heights.update({1: 24, 2: 30, 3: 30, 4: 18})

    row = HR + 1
    for pid, p in prods:
        sh.heights[row] = 34
        sh.set(row, 1, p.get("name") or pid, "prod")
        for cc, k, v in qty_cols:
            rs = cell.get((pid, k)) or []
            q = sum((r.get("sets") or r.get("direct") or 0) for r in rs)
            st = "qty"
            if rs:
                its = [it for r in rs for it in (r.get("items") or {}).values() if it and not it.get("deleted") and it.get("status") != "İptal"]
                done = bool(its) and all(it.get("status") in DONE for it in its)
                d = _date(v["due"])
                if done:
                    st = "qty_done"
                elif d and d < today:
                    st = "qty_late"
                elif d and (d - today).days <= 7:
                    st = "qty_soon"
            sh.set(row, cc, q if rs else None, st)
        if qty_cols:
            sh.set(row, tot_col, f"=SUM({col_name(first_q)}{row}:{col_name(last_q)}{row})", "total")
        else:
            sh.set(row, tot_col, 0, "total")
        codes = p.get("codes") or {}
        for key, cc in code_cols.items():
            sh.set(row, cc, (codes.get(key) or "").strip(), "text")
        parts = {(x.get("part") or "").strip(): x for x in p.get("parts") or []}
        for nm, ccs in part_cols.items():
            x = parts.get(nm)
            txt = "\n".join(v for v in ((x or {}).get("stock"), (x or {}).get("material")) if v) if x else ""
            sh.set(row, ccs[0], txt, "text")
            if len(ccs) > 1:
                m = (x or {}).get("massG")
                sh.set(row, ccs[1], m if isinstance(m, (int, float)) else (m or None), "num")
        for cc in range(2, last_col + 1):
            if sh.widths.get(cc) == 1.4:
                sh.set(row, cc, None, "sep")
        row += 1
    last_row = row - 1
    # TOPLAM satırı
    sh.heights[row] = 26
    sh.set(row, 1, "TOPLAM", "total")
    if last_row >= HR + 1:
        for cc, _, _ in qty_cols:
            sh.set(row, cc, f"=SUM({col_name(cc)}{HR + 1}:{col_name(cc)}{last_row})", "total")
        sh.set(row, tot_col, f"=SUM({col_name(tot_col)}{HR + 1}:{col_name(tot_col)}{last_row})", "total")
    for cc in range(2, tot_col + 1):
        if sh.widths.get(cc) == 1.4:
            sh.set(row, cc, None, "sep")
    # açıklama
    row += 2
    sh.set(row, 2, None, "sw_done"); sh.set(row, 3, "Tamamı hazır", "note")
    sh.set(row, 5, None, "sw_late"); sh.set(row, 6, "Termini geçti, eksik var", "note")
    sh.set(row, 9 if last_col >= 9 else 8, None, "sw_soon"); sh.set(row, 10 if last_col >= 10 else 9, "7 gün içinde", "note")
    sh.set(row + 1, 2, f"Prototip Takip'ten oluşturuldu: {dt.datetime.now().strftime('%d.%m.%Y %H:%M')}. "
                       "Hücrelerde ürün adedi var; onay bekleyen talepler dahil değil. Kalem ayrıntısı: Kalemler sayfası.", "note")
    sh.freeze = (HR + 1, 2)

    # ---- Kalemler
    kl = Sheet("Kalemler")
    H = ["Müşteri", "Ürün", "Program", "Faz", "Teslim Yeri", "Termin", "Alt Parça", "Stok Kodu", "Üretim Tipi", "Üretici",
         "Ham Madde / Rev", "Ürün Adedi", "Talep Miktarı", "Teslim Edilen", "Sorumlu", "Durum", "Not"]
    W = [10, 24, 9, 16, 14, 11, 24, 16, 13, 14, 16, 10, 11, 11, 14, 14, 30]
    for i, (h, w) in enumerate(zip(H, W), 1):
        kl.set(1, i, h, "lhead"); kl.widths[i] = w
    kl.heights[1] = 30
    rows = []
    for r in reqs.values():
        p = products.get(r.get("product")) or (r.get("proposedProduct") or {})
        for it in (r.get("items") or {}).values():
            if not it or it.get("deleted"):
                continue
            ph = ("Fazsız" + (" · " + r["label"] if r.get("label") else "")) if r.get("adhoc") else r.get("phase")
            due = it.get("due") or r.get("due")
            rows.append((due or "9999", p.get("ord") or 0, it.get("ord") or 0,
                         [cust(r), p.get("name") or r.get("product"), p.get("program") or "", ph, r.get("platform") or "",
                          _date(due), it.get("part") or "", it.get("stock") or "", _norm_type(it.get("type")), it.get("maker") or "",
                          it.get("material") or "", r.get("sets") or r.get("direct"), it.get("qty"), it.get("delivered"),
                          it.get("owner") or "", it.get("status") or "Açık", it.get("note") or ""]))
    rows.sort(key=lambda x: x[:3])
    for ri, (_, _, _, vals) in enumerate(rows, 2):
        for ci, v in enumerate(vals, 1):
            kl.set(ri, ci, v if v != "" else None, "ldate" if ci == 6 else "lnum" if ci in (12, 13, 14) else "lcell")
    kl.freeze = (2, 1)
    kl.autofilter = f"A1:{col_name(len(H))}{max(1, len(rows) + 1)}"
    return workbook_bytes([sh, kl])
