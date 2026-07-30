# -*- coding: utf-8 -*-
"""
pole_core.py
------------
Direk Tepe Kuvveti Analiz Programı - çekirdek mantık modülü.

AutoCAD (DXF) projesinden direk noktalarını ve bu direklere bağlı kablo
hatlarını okur, her direk için tepe kuvvetini (bileşke kuvvet) hesaplar
ve mevcut direk yerine 9 Ağaç / 10I / 12I / K Tipi direklerden en uygun
(yeterli ve en düşük kapasiteli) olanı önerir.

Bu dosya `pole_force_app.py` (Streamlit arayüzü) tarafından import edilir
ve onunla **aynı klasörde** bulunmalıdır.

ÖNEMLİ NOT: Buradaki tepe kuvveti hesabı basitleştirilmiş bir modeldir
(her hat için düz çekme kuvveti varsayılıp direk üzerindeki tüm hatların
vektörel toplamı alınır). Gerçek saha uygulaması için TEDAŞ şartnamesine
göre kablo çekme kuvveti / rüzgar-buz yükü tablolarının mühendis
tarafından doğrulanması gerekir. Bu program bir ön-değerlendirme /
tarama aracıdır, tek başına nihai karar kaynağı olarak kullanılmamalıdır.
"""

import io
import os
import re
import math
import tempfile
from dataclasses import dataclass, field

try:
    import ezdxf
except ImportError:
    ezdxf = None

POLE_TYPES_DEFAULT = ["9 Ağaç", "10I", "12I", "K Tipi"]

CABLE_SPEC_RE = re.compile(r"^\s*(\d+)?\s*[xX]?\s*\d+\s*/\s*\d+\s*\+?\s*\d*\s*[A-Za-zÇĞİÖŞÜçğıöşü]*\s*$")
CONDUCTOR_COUNT_RE = re.compile(r"^\s*(\d+)\s*[xX]")
NUMBER_ONLY_RE = re.compile(r"^\s*\d+(\.\d+)?\s*$")

# --------------------------------------------------------------------------
# Kablo / direk etiketi kısaltma sözlükleri
# --------------------------------------------------------------------------
# Çizimlerde kablo hattı etiketi olarak kullanılan harf kodları. Örn: "3xR"
# -> 3 iletkenli Rose tipi kablo, "SW" -> Swallow tipi kablo (1 iletken).
CABLE_LETTER_CODE_MAP = {
    "R": "Rose",
    "P": "Pansy",
    "SW": "Swallow",
    "AER": "Alpek",
}

# Çizimlerde direk etiketi olarak kullanılan, doğrudan bilinen kodlar.
# Örn: "GK1" -> müşterek (uzun) K1 tipi direk.
POLE_TAG_CODE_MAP = {
    "GK1": "Müşterek (Uzun) K1 Tipi Direk",
}

# "4P+R", "2P+2R" gibi izolatör/ekipman donanımı etiketlerini çözmek için:
# <sayı(opsiyonel)><harf kodu> [+ <sayı(opsiyonel)><harf kodu>]...
POLE_EQUIPMENT_PART_RE = re.compile(r"^\s*(\d*)\s*([A-Za-zÇĞİÖŞÜçğıöşü]+)\s*$")


def parse_cable_letter_code(text):
    """"3xR", "SW", "AER", "P" gibi harf kodu tabanlı kablo etiketlerini
    çözer. Eşleşirse (iletken_sayısı, kod, tam_ad) döndürür, yoksa None."""
    t = text.strip()
    m = re.match(r"^(\d+)\s*[xX]\s*([A-Za-zÇĞİÖŞÜçğıöşü]+)$", t)
    if m:
        count = int(m.group(1))
        code = m.group(2).upper()
    else:
        m2 = re.match(r"^([A-Za-zÇĞİÖŞÜçğıöşü]+)$", t)
        if not m2:
            return None
        count = 1
        code = m2.group(1).upper()
    if code in CABLE_LETTER_CODE_MAP:
        return count, code, CABLE_LETTER_CODE_MAP[code]
    return None


def format_cable_label(text):
    """Bir kablo etiketini (mümkünse) okunabilir hale çevirir.
    Örn: "3xR" -> "3xR (3x Rose)". Tanınmıyorsa metni olduğu gibi döndürür."""
    parsed = parse_cable_letter_code(text)
    if parsed:
        count, code, full_name = parsed
        return f"{text.strip()} ({count}x {full_name})"
    return text


def parse_pole_equipment_tag(text):
    """Direk üzerindeki izolatör/donanım ya da direk tipi etiketini
    okunabilir bir açıklamaya çevirir.

    - Doğrudan bilinen direk tipi kodları (örn. "GK1") sözlükten çözülür.
    - "4P+R", "2P+2R" gibi <sayı><harf>+<sayı><harf> desenleri, harf
      kodları CABLE_LETTER_CODE_MAP'te tanınıyorsa "4x Pansy + 1x Rose
      izolatör" şeklinde açılır.

    Çözülemezse None döner.
    """
    if not text:
        return None
    t = text.strip()
    upper_t = t.upper()

    if upper_t in POLE_TAG_CODE_MAP:
        return POLE_TAG_CODE_MAP[upper_t]

    parts = t.split("+")
    if len(parts) < 1:
        return None

    descriptions = []
    for part in parts:
        m = POLE_EQUIPMENT_PART_RE.match(part)
        if not m:
            return None
        count_str, code = m.group(1), m.group(2).upper()
        if code not in CABLE_LETTER_CODE_MAP:
            return None
        count = int(count_str) if count_str else 1
        descriptions.append(f"{count}x {CABLE_LETTER_CODE_MAP[code]}")

    if not descriptions:
        return None
    return " + ".join(descriptions) + " izolatör"


# --------------------------------------------------------------------------
# DXF okuma yardımcıları
# --------------------------------------------------------------------------

def load_dxf(file_bytes):
    """Yüklenen dosyayı geçici bir dosyaya yazıp ezdxf.readfile() ile okur.
    Bu yöntem hem ASCII hem de BINARY DXF dosyalarını, ve dosyanın kendi
    içindeki (Türkçe karakterler için) codepage bilgisini doğru şekilde
    algılayarak okur -- doğrudan UTF-8 metin olarak okumaya çalışmak
    binary DXF'lerde veya farklı codepage'lerde hataya yol açar."""
    raw = file_bytes.read()
    with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name
    try:
        try:
            doc = ezdxf.readfile(tmp_path)
        except ezdxf.DXFStructureError:
            # bazı dosyalar bozuk/eksik yapıya sahip olabilir; recover modülüyle dene
            from ezdxf import recover
            doc, auditor = recover.readfile(tmp_path)
        return doc
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def get_entity_color_layer(entity):
    layer = entity.dxf.layer
    try:
        color = entity.dxf.color
    except Exception:
        color = 256
    return layer, color


def extract_polylines(doc, layers):
    """Modelspace'teki LWPOLYLINE / POLYLINE varlıklarını, verilen katmanlarla
    sınırlı olacak şekilde, ardışık nokta çiftleri (segment) listesi olarak döndürür."""
    msp = doc.modelspace()
    segments = []
    for e in msp.query("LWPOLYLINE POLYLINE"):
        layer, _ = get_entity_color_layer(e)
        if layers and layer not in layers:
            continue
        try:
            points = [tuple(p[:2]) for p in e.get_points(format="xy")]
        except Exception:
            try:
                points = [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
            except Exception:
                continue
        for i in range(len(points) - 1):
            segments.append({
                "p1": points[i],
                "p2": points[i + 1],
                "layer": layer,
                "source": e,
            })
    return segments


def extract_texts(doc, layers):
    msp = doc.modelspace()
    texts = []
    for e in msp.query("TEXT MTEXT"):
        layer, _ = get_entity_color_layer(e)
        if layers and layer not in layers:
            continue
        try:
            if e.dxftype() == "MTEXT":
                content = e.plain_text().strip()
                pos = (e.dxf.insert.x, e.dxf.insert.y)
            else:
                content = e.dxf.text.strip()
                pos = (e.dxf.insert.x, e.dxf.insert.y)
        except Exception:
            continue
        if content:
            texts.append({"text": content, "pos": pos, "layer": layer})
    return texts


def list_layers(doc):
    layers = {}
    msp = doc.modelspace()
    for e in msp.query("LWPOLYLINE POLYLINE"):
        l, _ = get_entity_color_layer(e)
        layers.setdefault(l, {"poly": 0, "text": 0})
        layers[l]["poly"] += 1
    for e in msp.query("TEXT MTEXT"):
        l, _ = get_entity_color_layer(e)
        layers.setdefault(l, {"poly": 0, "text": 0})
        layers[l]["text"] += 1
    return layers


# --------------------------------------------------------------------------
# Geometri yardımcıları
# --------------------------------------------------------------------------

def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def midpoint(a, b):
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def classify_text(txt):
    t = txt.strip()
    if NUMBER_ONLY_RE.match(t):
        return "span_length"
    if CABLE_SPEC_RE.match(t) and "/" in t:
        return "cable_spec"
    if parse_cable_letter_code(t) is not None:
        return "cable_spec"
    return "pole_name"


def parse_conductor_count(spec_text):
    m = CONDUCTOR_COUNT_RE.match(spec_text)
    if m:
        return int(m.group(1))
    letter_code = parse_cable_letter_code(spec_text)
    if letter_code:
        return letter_code[0]
    return 1


def cluster_vertices(all_points, tolerance):
    """Basit union-find tabanlı kümeleme: birbirine tolerance mesafesinden
    yakın noktaları aynı direk kabul eder."""
    n = len(all_points)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # basit ama n^2 -- direk sayısı makul (yüzlerce) olduğu sürece sorun olmaz
    for i in range(n):
        for j in range(i + 1, n):
            if dist(all_points[i], all_points[j]) <= tolerance:
                union(i, j)

    clusters = {}
    for i in range(n):
        r = find(i)
        clusters.setdefault(r, []).append(i)
    return clusters


# --------------------------------------------------------------------------
# Direk / kuvvet modeli
# --------------------------------------------------------------------------

@dataclass
class PoleSegmentRef:
    other_point: tuple
    cable_spec: str
    conductor_count: int


@dataclass
class Pole:
    pole_id: str
    coord: tuple
    detected_name: str = ""
    segments: list = field(default_factory=list)  # PoleSegmentRef


def build_poles(segments, texts, name_match_dist, spec_match_dist):
    # 1) tüm segment uç noktalarını topla ve kümele (aynı fiziksel direk)
    all_points = []
    for s in segments:
        all_points.append(s["p1"])
        all_points.append(s["p2"])
    if not all_points:
        return []

    tol = spec_match_dist  # aynı toleransı direk birleştirme için de kullan
    clusters = cluster_vertices(all_points, tol)

    point_to_cluster = {}
    for cid, idxs in clusters.items():
        for idx in idxs:
            point_to_cluster[idx] = cid

    cluster_coord = {}
    for cid, idxs in clusters.items():
        xs = [all_points[i][0] for i in idxs]
        ys = [all_points[i][1] for i in idxs]
        cluster_coord[cid] = (sum(xs) / len(xs), sum(ys) / len(ys))

    poles = {cid: Pole(pole_id=f"P{i+1}", coord=cluster_coord[cid])
             for i, cid in enumerate(sorted(cluster_coord.keys()))}

    # metinleri türüne göre ayır
    spec_texts = [t for t in texts if classify_text(t["text"]) == "cable_spec"]
    name_texts = [t for t in texts if classify_text(t["text"]) == "pole_name"]

    # 2) her segmente en yakın kablo tipi metnini ata (segment orta noktasına göre)
    for si, s in enumerate(segments):
        mid = midpoint(s["p1"], s["p2"])
        best = None
        best_d = None
        for t in spec_texts:
            d = dist(mid, t["pos"])
            if d <= spec_match_dist * 6 and (best_d is None or d < best_d):
                best = t["text"]
                best_d = d
        cable_spec = best if best else "Bilinmeyen Kablo Tipi"
        cc = parse_conductor_count(cable_spec)

        idx1 = 2 * si
        idx2 = 2 * si + 1
        c1 = point_to_cluster[idx1]
        c2 = point_to_cluster[idx2]

        poles[c1].segments.append(PoleSegmentRef(other_point=s["p2"],
                                                   cable_spec=cable_spec,
                                                   conductor_count=cc))
        poles[c2].segments.append(PoleSegmentRef(other_point=s["p1"],
                                                   cable_spec=cable_spec,
                                                   conductor_count=cc))

    # 3) her direğe en yakın direk-adı metnini ata
    for p in poles.values():
        best = None
        best_d = None
        for t in name_texts:
            d = dist(p.coord, t["pos"])
            if d <= name_match_dist and (best_d is None or d < best_d):
                best = t["text"]
                best_d = d
        p.detected_name = best if best else ""

    return list(poles.values())


def compute_resultant_force(pole, tension_lookup):
    """Direğe bağlı her hat için, hattın tam gerilim kuvvetini (tekil iletken
    çekme kuvveti x iletken sayısı) direk merkezinden hattın gittiği yöne
    doğru bir vektör olarak alır ve tüm hatların vektörel toplamının
    büyüklüğünü döndürür (basitleştirilmiş model)."""
    fx, fy = 0.0, 0.0
    details = []
    for seg in pole.segments:
        dx = seg.other_point[0] - pole.coord[0]
        dy = seg.other_point[1] - pole.coord[1]
        length = math.hypot(dx, dy)
        if length == 0:
            continue
        ux, uy = dx / length, dy / length
        unit_tension = tension_lookup.get(seg.cable_spec, 0.0)
        total_tension = unit_tension * seg.conductor_count
        fx += ux * total_tension
        fy += uy * total_tension
        details.append((seg.cable_spec, seg.conductor_count, total_tension))
    magnitude = math.hypot(fx, fy)
    return magnitude, details


def recommend_pole_type(force, capacity_table, safety_factor):
    """capacity_table: {tip: kapasite} -- yeterli kapasitedeki en düşük
    kapasiteli tipi seçer (artan kapasiteye göre sıralar)."""
    required = force * safety_factor
    sorted_types = sorted(capacity_table.items(), key=lambda x: x[1])
    for tip, kapasite in sorted_types:
        if kapasite >= required:
            return tip, kapasite
    # hiçbiri yetmiyorsa en yüksek kapasiteliyi öner ve uyar
    tip, kapasite = sorted_types[-1]
    return tip + " (YETERSİZ - kontrol edilmeli)", kapasite
