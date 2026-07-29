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
import pandas as pd
import streamlit as st

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


st.set_page_config(page_title="Direk Tepe Kuvveti Analizi", layout="wide")

# STREAMLIT ARAYÜZ
# --------------------------------------------------------------------------

st.title("🗼 Direk Tepe Kuvveti Analizi ve Direk Tipi Önerisi")
st.caption(
    "AutoCAD (DXF) projesindeki direk noktalarını ve kablo hatlarını okuyarak "
    "her direğin tepe kuvvetini hesaplar, 9 Ağaç / 10I / 12I / K Tipi direkler "
    "arasından uygun olanı önerir."
)

st.warning(
    "⚠️ Bu programdaki kuvvet hesabı basitleştirilmiş bir mühendislik modelidir "
    "(kablo çekme kuvvetlerinin vektörel toplamı). Rüzgar/buz yükü, sıcaklık "
    "etkisi gibi TEDAŞ şartnamesindeki detaylı katsayıları içermez. Sonuçlar "
    "saha uygulamasından önce ilgili mühendis tarafından doğrulanmalıdır."
)

if ezdxf is None:
    st.error("ezdxf kütüphanesi kurulu değil. Lütfen `pip install ezdxf` ile kurun.")
    st.stop()

uploaded = st.file_uploader("AutoCAD projesini yükleyin (.dxf formatında)", type=["dxf"])

if uploaded is None:
    st.info(
        "Not: Program yalnızca **DXF** formatını okuyabilir. Eğer projeniz "
        "DWG ise, AutoCAD'de 'Farklı Kaydet' ile DXF formatına çevirip "
        "buraya yükleyin."
    )
    st.stop()

if "doc" not in st.session_state or st.session_state.get("_last_file") != uploaded.name:
    with st.spinner("DXF dosyası okunuyor..."):
        try:
            st.session_state["doc"] = load_dxf(uploaded)
            st.session_state["_last_file"] = uploaded.name
            for k in ["poles", "layers_info"]:
                st.session_state.pop(k, None)
        except Exception as ex:
            st.error(f"DXF okunamadı: {ex}")
            st.stop()

doc = st.session_state["doc"]

if "layers_info" not in st.session_state:
    st.session_state["layers_info"] = list_layers(doc)
layers_info = st.session_state["layers_info"]

st.subheader("1) Katman Seçimi")
st.write(
    "Çizimde bulunan katmanlar aşağıda listelenmiştir. Kablo hatlarının "
    "(yeşil polyline'lar) bulunduğu katmanları ve direk/kablo etiketlerinin "
    "yazıldığı metin katmanlarını seçin."
)

layer_df = pd.DataFrame([
    {"Katman": l, "Polyline Sayısı": v["poly"], "Text Sayısı": v["text"]}
    for l, v in layers_info.items()
])
st.dataframe(layer_df, use_container_width=True, hide_index=True)

poly_layers_all = [l for l, v in layers_info.items() if v["poly"] > 0]
text_layers_all = [l for l, v in layers_info.items() if v["text"] > 0]

col1, col2 = st.columns(2)
with col1:
    cable_layers = st.multiselect(
        "Kablo hattı katmanları", options=poly_layers_all, default=poly_layers_all
    )
with col2:
    text_layers = st.multiselect(
        "Metin (etiket) katmanları", options=text_layers_all, default=text_layers_all
    )

st.subheader("2) Eşleştirme Ayarları")
c1, c2 = st.columns(2)
with c1:
    merge_tolerance = st.number_input(
        "Direk birleştirme / kablo-tipi eşleştirme mesafesi (çizim birimi)",
        min_value=0.01, value=3.0, step=0.5,
        help="Bu mesafeden yakın uç noktalar aynı fiziksel direk kabul edilir; "
             "kablo tipi metinleri de bu mesafenin katları kadar yakın segmentlere eşleştirilir."
    )
with c2:
    name_match_dist = st.number_input(
        "Direk adı metni eşleştirme mesafesi (çizim birimi)",
        min_value=0.01, value=8.0, step=0.5
    )

parse_clicked = st.button("📐 Direkleri Tespit Et", type="primary")

if parse_clicked:
    with st.spinner("Direkler ve kablo hatları tespit ediliyor..."):
        segments = extract_polylines(doc, cable_layers)
        texts = extract_texts(doc, text_layers)
        poles = build_poles(segments, texts, name_match_dist, merge_tolerance)
        # sadece en az 1 hattı olan direkleri tut
        poles = [p for p in poles if len(p.segments) > 0]
        st.session_state["poles"] = poles
        st.session_state["segments_count"] = len(segments)
        st.session_state["texts_count"] = len(texts)

if "poles" not in st.session_state:
    st.stop()

poles = st.session_state["poles"]

st.success(
    f"{st.session_state.get('segments_count', 0)} kablo segmenti ve "
    f"{st.session_state.get('texts_count', 0)} metin okundu → "
    f"{len(poles)} direk tespit edildi."
)

if len(poles) == 0:
    st.error("Hiç direk tespit edilemedi. Katman seçimlerini kontrol edin.")
    st.stop()

# tespit edilen tüm benzersiz kablo tiplerini topla
found_specs = sorted({seg.cable_spec for p in poles for seg in p.segments})

st.subheader("3) Direk Bilgilerini Gözden Geçirin")
st.write(
    "Otomatik tespit edilen direkler aşağıdadır. İsimleri hatalıysa düzeltin, "
    "ve varsa mevcut direk tipini (biliniyorsa) seçin — bu, hangi direklerin "
    "**değiştirilmesi gerektiğinin** belirlenmesi için kullanılır."
)

pole_rows = []
for p in poles:
    specs_here = sorted({s.cable_spec for s in p.segments})
    pole_rows.append({
        "Direk ID": p.pole_id,
        "Direk Adı": p.detected_name if p.detected_name else p.pole_id,
        "X": round(p.coord[0], 2),
        "Y": round(p.coord[1], 2),
        "Bağlı Hat Sayısı": len(p.segments),
        "Kablo Tipleri": ", ".join(specs_here),
        "Mevcut Direk Tipi": "Bilinmiyor",
    })
pole_edit_df = pd.DataFrame(pole_rows)

edited_poles = st.data_editor(
    pole_edit_df,
    use_container_width=True,
    hide_index=True,
    disabled=["Direk ID", "X", "Y", "Bağlı Hat Sayısı", "Kablo Tipleri"],
    column_config={
        "Mevcut Direk Tipi": st.column_config.SelectboxColumn(
            options=["Bilinmiyor"] + POLE_TYPES_DEFAULT
        )
    },
    key="pole_editor",
)

st.subheader("4) Kablo Çekme Kuvveti Parametreleri")
st.write(
    "Çizimde tespit edilen her kablo tipi için **tekil iletken çekme kuvvetini** "
    "(kgf) girin. İletken sayısı, metindeki '3x...' gibi ön ekten otomatik "
    "okunmuştur, gerekirse düzeltebilirsiniz."
)
cable_rows = []
for spec in found_specs:
    cable_rows.append({
        "Kablo Tipi (Metin)": spec,
        "İletken Sayısı": parse_conductor_count(spec),
        "Tekil İletken Çekme Kuvveti (kgf)": 500.0,
    })
cable_param_df = pd.DataFrame(cable_rows)
edited_cables = st.data_editor(
    cable_param_df, use_container_width=True, hide_index=True, key="cable_editor"
)

st.subheader("5) Direk Kapasite Tablosu")
st.write(
    "Aday direk tiplerinin tepe kuvveti kapasitelerini (kgf) girin. Program, "
    "hesaplanan kuvveti karşılayan **en düşük kapasiteli** tipi önerecektir."
)
capacity_rows = [
    {"Direk Tipi": "9 Ağaç", "Tepe Kuvveti Kapasitesi (kgf)": 400.0},
    {"Direk Tipi": "10I", "Tepe Kuvveti Kapasitesi (kgf)": 800.0},
    {"Direk Tipi": "12I", "Tepe Kuvveti Kapasitesi (kgf)": 1200.0},
    {"Direk Tipi": "K Tipi", "Tepe Kuvveti Kapasitesi (kgf)": 2000.0},
]
capacity_df = pd.DataFrame(capacity_rows)
edited_capacity = st.data_editor(
    capacity_df, use_container_width=True, hide_index=True, key="capacity_editor"
)

safety_factor = st.number_input(
    "Güvenlik katsayısı", min_value=1.0, value=1.5, step=0.1,
    help="Hesaplanan kuvvet bu katsayı ile çarpılarak gerekli kapasite belirlenir."
)

st.divider()
calc_clicked = st.button("⚡ Hesapla ve Excel Raporu Oluştur", type="primary")

if calc_clicked:
    tension_lookup = dict(zip(
        edited_cables["Kablo Tipi (Metin)"],
        edited_cables["Tekil İletken Çekme Kuvveti (kgf)"]
    ))
    capacity_lookup = dict(zip(
        edited_capacity["Direk Tipi"],
        edited_capacity["Tepe Kuvveti Kapasitesi (kgf)"]
    ))
    current_type_lookup = dict(zip(
        edited_poles["Direk ID"], edited_poles["Mevcut Direk Tipi"]
    ))
    name_lookup = dict(zip(edited_poles["Direk ID"], edited_poles["Direk Adı"]))

    result_rows = []
    for p in poles:
        # kullanıcının düzenlediği iletken sayısını da kullan
        cc_override = dict(zip(
            edited_cables["Kablo Tipi (Metin)"], edited_cables["İletken Sayısı"]
        ))
        for seg in p.segments:
            seg.conductor_count = int(cc_override.get(seg.cable_spec, seg.conductor_count))

        force, _details = compute_resultant_force(p, tension_lookup)
        rec_type, rec_capacity = recommend_pole_type(force, capacity_lookup, safety_factor)
        current_type = current_type_lookup.get(p.pole_id, "Bilinmiyor")

        if current_type == "Bilinmiyor":
            needs_change = "Mevcut tip bilinmiyor"
        elif current_type == rec_type:
            needs_change = "Hayır"
        else:
            current_cap = capacity_lookup.get(current_type, None)
            if current_cap is not None and current_cap >= force * safety_factor:
                needs_change = "Hayır (mevcut yeterli, opsiyonel iyileştirme mümkün)"
            else:
                needs_change = "Evet"

        result_rows.append({
            "Direk ID": p.pole_id,
            "Direk Adı": name_lookup.get(p.pole_id, p.pole_id),
            "X": round(p.coord[0], 2),
            "Y": round(p.coord[1], 2),
            "Bağlı Hat Sayısı": len(p.segments),
            "Kablo Tipleri": ", ".join(sorted({s.cable_spec for s in p.segments})),
            "Hesaplanan Tepe Kuvveti (kgf)": round(force, 1),
            "Gerekli Kapasite (Güvenlik Katsayılı, kgf)": round(force * safety_factor, 1),
            "Mevcut Direk Tipi": current_type,
            "Önerilen Direk Tipi": rec_type,
            "Değişmesi Gerekiyor Mu": needs_change,
        })

    result_df = pd.DataFrame(result_rows).sort_values(
        "Hesaplanan Tepe Kuvveti (kgf)", ascending=False
    )
    st.session_state["result_df"] = result_df

if "result_df" in st.session_state:
    result_df = st.session_state["result_df"]

    st.subheader("Sonuçlar")
    st.dataframe(result_df, use_container_width=True, hide_index=True)

    change_df = result_df[result_df["Değişmesi Gerekiyor Mu"] == "Evet"]
    st.markdown(f"**Değiştirilmesi gereken direk sayısı: {len(change_df)}**")
    if len(change_df) > 0:
        st.dataframe(
            change_df[["Direk ID", "Direk Adı", "Mevcut Direk Tipi",
                       "Önerilen Direk Tipi", "Hesaplanan Tepe Kuvveti (kgf)"]],
            use_container_width=True, hide_index=True
        )

    # ------------------ Excel oluştur ------------------
    params_rows = [
        {"Parametre": "Güvenlik Katsayısı", "Değer": safety_factor},
        {"Parametre": "Direk Birleştirme Mesafesi", "Değer": merge_tolerance},
        {"Parametre": "Direk Adı Eşleştirme Mesafesi", "Değer": name_match_dist},
    ]
    params_df = pd.DataFrame(params_rows)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        result_df.to_excel(writer, sheet_name="Tüm Direkler", index=False)
        change_df.to_excel(writer, sheet_name="Değişmesi Gerekenler", index=False)
        edited_cables.to_excel(writer, sheet_name="Kablo Parametreleri", index=False)
        edited_capacity.to_excel(writer, sheet_name="Direk Kapasiteleri", index=False)
        params_df.to_excel(writer, sheet_name="Parametreler", index=False)

        # sütun genişliklerini otomatik ayarla
        for sheet_name in writer.sheets:
            ws = writer.sheets[sheet_name]
            for col_cells in ws.columns:
                length = max(len(str(c.value)) if c.value is not None else 0 for c in col_cells)
                col_letter = col_cells[0].column_letter
                ws.column_dimensions[col_letter].width = min(max(length + 2, 10), 45)

    output.seek(0)
    st.download_button(
        "📥 Excel Raporunu İndir",
        data=output,
        file_name="direk_tepe_kuvveti_raporu.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
    )
