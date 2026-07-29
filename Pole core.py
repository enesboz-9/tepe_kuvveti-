# -*- coding: utf-8 -*-
"""
Direk Tepe Kuvveti Analiz Programı
-----------------------------------
AutoCAD (DXF) projesinden direk noktalarını ve bu direklere bağlı kablo
hatlarını okur, her direk için tepe kuvvetini (bileşke kuvvet) hesaplar
ve mevcut direk yerine 9 Ağaç / 10I / 12I / K Tipi direklerden en uygun
(yeterli ve en düşük kapasiteli) olanı önerir. Sonuçlar Excel olarak
indirilebilir.

ÖNEMLİ NOT: Buradaki tepe kuvveti hesabı basitleştirilmiş bir modeldir
(her hat için düz çekme kuvveti varsayılıp direk üzerindeki tüm hatların
vektörel toplamı alınır). Gerçek saha uygulaması için TEDAŞ şartnamesine
göre kablo çekme kuvveti / rüzgar-buz yükü tablolarının mühendis
tarafından doğrulanması gerekir. Bu program bir ön-değerlendirme /
tarama aracıdır, tek başına nihai karar kaynağı olarak kullanılmamalıdır.
"""

import io
import re
import math
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
# DXF okuma yardımcıları
# --------------------------------------------------------------------------

def load_dxf(file_bytes):
    stream = io.StringIO(file_bytes.read().decode("utf-8", errors="ignore"))
    doc = ezdxf.read(stream)
    return doc


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
    return "pole_name"


def parse_conductor_count(spec_text):
    m = CONDUCTOR_COUNT_RE.match(spec_text)
    if m:
        return int(m.group(1))
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
