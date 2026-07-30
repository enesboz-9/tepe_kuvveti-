# -*- coding: utf-8 -*-
"""
Direk Tepe Kuvveti Analiz Programı - Streamlit Arayüzü
-------------------------------------------------------
AutoCAD (DXF) projesinden direk noktalarını ve bu direklere bağlı kablo
hatlarını okur, her direk için tepe kuvvetini (bileşke kuvvet) hesaplar
ve mevcut direk yerine 9 Ağaç / 10I / 12I / K Tipi direklerden en uygun
(yeterli ve en düşük kapasiteli) olanı önerir. Sonuçlar Excel olarak
indirilebilir.

Çekirdek mantık `pole_core.py` modülündedir; bu dosya yalnızca Streamlit
arayüzünü içerir ve `pole_core.py` ile **aynı klasörde** olmalıdır.

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

from pole_core import (
    ezdxf,
    POLE_TYPES_DEFAULT,
    load_dxf,
    extract_polylines,
    extract_texts,
    list_layers,
    build_poles,
    compute_resultant_force,
    compute_angle_between_segments,
    compute_deviation_angle,
    compute_total_force_with_wind,
    normalize_pole_type_text,
    recommend_pole_type,
    parse_conductor_count,
    parse_pole_equipment_tag,
    format_cable_label,
)

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
    "(kablo çekme kuvvetlerinin vektörel toplamı P_R, isteğe bağlı rüzgar "
    "yükü P_w ile P_toplam = √(P_R² + P_w²) olarak birleştirilir). Rüzgar/buz "
    "yükünün kendisi (iletken çapı, buz kalınlığı, açıklık uzunluğuna bağlı) "
    "ve sıcaklık etkisi gibi TEDAŞ şartnamesindeki detaylı katsayılar programda "
    "hesaplanmaz, P_w değeri elle girilmelidir. Sonuçlar saha uygulamasından "
    "önce ilgili mühendis tarafından doğrulanmalıdır."
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
        "Direk tipi metni eşleştirme mesafesi (çizim birimi)",
        min_value=0.01, value=6.0, step=0.5,
        help="'G-12I', 'G-K1' tarzı direk tipi metinleri, o direğe bağlı "
             "hatların üzerine/yakınına (direğe yakın kısımda) yazılır. "
             "Bu değer, bir metnin bu hatlara olan dik mesafesi için eşiktir "
             "(bulunamazsa direğin kendi koordinatına olan mesafe de yedek "
             "olarak denenir). Paftanızın ölçek birimine göre ayarlayın "
             "(UTM/metre tabanlı paftalarda 5-8 birim genelde yeterlidir)."
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
    "Otomatik tespit edilen direkler aşağıdadır. Direk tipi (paftadan) "
    "hatalıysa/boşsa düzeltin, ve varsa mevcut direk tipini (biliniyorsa) "
    "seçin — bu, hangi direklerin **değiştirilmesi gerektiğinin** "
    "belirlenmesi için kullanılır."
)

pole_rows = []
for p in poles:
    specs_here = sorted({s.cable_spec for s in p.segments})
    guessed_type = normalize_pole_type_text(p.detected_type)
    pole_rows.append({
        "Direk ID": p.pole_id,
        "Direk Adı (Paftadan)": p.detected_name,
        "Direk Tipi (Paftadan)": p.detected_type,
        "X": round(p.coord[0], 2),
        "Y": round(p.coord[1], 2),
        "Bağlı Hat Sayısı": len(p.segments),
        "Kablo Tipleri": ", ".join(specs_here),
        "Mevcut Direk Tipi": guessed_type if guessed_type else "Bilinmiyor",
    })
pole_edit_df = pd.DataFrame(pole_rows)

st.caption(
    "**Direk Adı (Paftadan):** çizimdeki 'B04', 'B07' gibi direk adı "
    "etiketinden otomatik okunur, boşsa elle girin. **Mevcut Direk Tipi:** "
    "'Direk Tipi (Paftadan)' metninden (örn. 'G-12I' → '12I') otomatik "
    "tahmin edilir; yanlışsa düzeltin."
)

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
    "(kgf) girin. İletken sayısı, metindeki '3x...' veya 'R/P/SW/AER' gibi "
    "kısaltmalardan otomatik okunmuştur, gerekirse düzeltebilirsiniz."
)
st.caption(
    "**Rüzgar/Buz Yük Katsayısı:** Tekil iletken çekme kuvvetine uygulanan bir "
    "çarpandır; TEDAŞ şartnamesindeki rüzgar/buz yükü etkisini kaba bir "
    "yaklaşımla yansıtmak için kullanılabilir (varsayılan 1.0 = etkisiz). "
    "Bu hâlâ basitleştirilmiş bir model olduğundan, gerçek rüzgar/buz yükü "
    "hesabı (iletken çapı, buz kalınlığı, açıklık uzunluğuna bağlı) için "
    "ilgili mühendis onayı gereklidir."
)
cable_rows = []
for spec in found_specs:
    cable_rows.append({
        "Kablo Tipi (Metin)": spec,
        "İletken Sayısı": parse_conductor_count(spec),
        "Tekil İletken Çekme Kuvveti (kgf)": 500.0,
        "Rüzgar/Buz Yük Katsayısı": 1.0,
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

c3, c4 = st.columns(2)
with c3:
    safety_factor = st.number_input(
        "Güvenlik katsayısı", min_value=1.0, value=1.5, step=0.1,
        help="Hesaplanan kuvvet bu katsayı ile çarpılarak gerekli kapasite belirlenir."
    )
with c4:
    wind_force = st.number_input(
        "Rüzgar Yükü P_w (kgf) — tüm direkler için ortak", min_value=0.0, value=0.0, step=10.0,
        help="Direk gövdesi ve iletkenlere dik gelen rüzgar yükü. Girilirse, "
             "hat gerilmelerinin bileşke kuvvetine (P_R) dik bir bileşen "
             "olarak eklenir: P_toplam = √(P_R² + P_w²). Bu değeri kendi "
             "TEDAŞ şartnamenizdeki rüzgar/buz yükü hesabına göre girin; "
             "program bu hesabı kendisi yapmaz."
    )

st.divider()
calc_clicked = st.button("⚡ Hesapla ve Excel Raporu Oluştur", type="primary")

if calc_clicked:
    tension_lookup = dict(zip(
        edited_cables["Kablo Tipi (Metin)"],
        edited_cables["Tekil İletken Çekme Kuvveti (kgf)"]
    ))
    load_factor_lookup = dict(zip(
        edited_cables["Kablo Tipi (Metin)"],
        edited_cables["Rüzgar/Buz Yük Katsayısı"]
    ))
    capacity_lookup = dict(zip(
        edited_capacity["Direk Tipi"],
        edited_capacity["Tepe Kuvveti Kapasitesi (kgf)"]
    ))
    current_type_lookup = dict(zip(
        edited_poles["Direk ID"], edited_poles["Mevcut Direk Tipi"]
    ))
    type_text_lookup = dict(zip(
        edited_poles["Direk ID"], edited_poles["Direk Tipi (Paftadan)"]
    ))
    name_lookup = dict(zip(
        edited_poles["Direk ID"], edited_poles["Direk Adı (Paftadan)"]
    ))

    result_rows = []
    for p in poles:
        # kullanıcının düzenlediği iletken sayısını da kullan
        cc_override = dict(zip(
            edited_cables["Kablo Tipi (Metin)"], edited_cables["İletken Sayısı"]
        ))
        for seg in p.segments:
            seg.conductor_count = int(cc_override.get(seg.cable_spec, seg.conductor_count))

        force, _details = compute_resultant_force(p, tension_lookup, load_factor_lookup)
        total_force = compute_total_force_with_wind(force, wind_force)
        rec_type, rec_capacity = recommend_pole_type(total_force, capacity_lookup, safety_factor)
        current_type = current_type_lookup.get(p.pole_id, "Bilinmiyor")
        alpha = compute_angle_between_segments(p)
        beta = compute_deviation_angle(p)

        if current_type == "Bilinmiyor":
            needs_change = "Mevcut tip bilinmiyor"
        elif current_type == rec_type:
            needs_change = "Hayır"
        else:
            current_cap = capacity_lookup.get(current_type, None)
            if current_cap is not None and current_cap >= total_force * safety_factor:
                needs_change = "Hayır (mevcut yeterli, opsiyonel iyileştirme mümkün)"
            else:
                needs_change = "Evet"

        # Görseldeki/paftadaki mevcut direk tipinin (örn. "G-12I"), hesaplanan
        # yükü emniyetli taşıyıp taşımadığının otomatik kontrolü (bkz. talimatın
        # "Direk Seçim Kontrolü" adımı: P_toplam <= P_emniyet).
        direk_tipi_metni = type_text_lookup.get(p.pole_id, "")
        guessed_capacity_type = normalize_pole_type_text(direk_tipi_metni)
        if guessed_capacity_type and guessed_capacity_type in capacity_lookup:
            p_emniyet = capacity_lookup[guessed_capacity_type]
            paftadaki_kontrol = (
                f"Emniyetli ({guessed_capacity_type}, kapasite {p_emniyet:g} kgf)"
                if p_emniyet >= total_force * safety_factor
                else f"YETERSİZ ({guessed_capacity_type}, kapasite {p_emniyet:g} kgf)"
            )
        else:
            paftadaki_kontrol = "Kontrol edilemedi (direk tipi paftadan tanınamadı)"

        etiket_yorumu = parse_pole_equipment_tag(direk_tipi_metni) or ""
        kablo_tipleri_yorumlu = ", ".join(
            format_cable_label(s) for s in sorted({s.cable_spec for s in p.segments})
        )

        # menzil (a1: gelen hat, a2: giden hat) -- yalnızca köşe/geçiş
        # direklerinde (tam 2 hat) anlamlıdır; TEDAŞ ortalama menzil kuralı:
        # a_ort = (a1 + a2) / 2
        span_a1 = span_a2 = span_ort = None
        if len(p.segments) == 2:
            span_a1 = p.segments[0].span_length
            span_a2 = p.segments[1].span_length
            if span_a1 is not None and span_a2 is not None:
                span_ort = (span_a1 + span_a2) / 2.0

        result_rows.append({
            "Direk ID": p.pole_id,
            "Direk Adı (Paftadan)": name_lookup.get(p.pole_id, ""),
            "Direk Tipi (Paftadan)": direk_tipi_metni,
            "Direk Etiketi Yorumu": etiket_yorumu,
            "X": round(p.coord[0], 2),
            "Y": round(p.coord[1], 2),
            "Bağlı Hat Sayısı": len(p.segments),
            "Kablo Tipleri": kablo_tipleri_yorumlu,
            "Ön Hat Menzili a1 (m)": round(span_a1, 1) if span_a1 is not None else "",
            "Arka Hat Menzili a2 (m)": round(span_a2, 1) if span_a2 is not None else "",
            "Ortalama Menzil a_ort (m)": round(span_ort, 1) if span_ort is not None else "",
            "Kırılma Açısı α (°)": round(alpha, 1) if alpha is not None else "",
            "Sapma Açısı β (°)": round(beta, 1) if beta is not None else "",
            "P_R - Hat Gerilmeleri Bileşkesi (kgf)": round(force, 1),
            "P_w - Rüzgar Yükü (kgf)": round(wind_force, 1),
            "P_toplam - Toplam Tepe Kuvveti (kgf)": round(total_force, 1),
            "Gerekli Kapasite (Güvenlik Katsayılı, kgf)": round(total_force * safety_factor, 1),
            "Paftadaki Direk Tipi Kontrolü": paftadaki_kontrol,
            "Mevcut Direk Tipi": current_type,
            "Önerilen Direk Tipi": rec_type,
            "Değişmesi Gerekiyor Mu": needs_change,
        })

    result_df = pd.DataFrame(result_rows).sort_values(
        "P_toplam - Toplam Tepe Kuvveti (kgf)", ascending=False
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
            change_df[["Direk ID", "Direk Adı (Paftadan)", "Direk Tipi (Paftadan)",
                       "Mevcut Direk Tipi", "Önerilen Direk Tipi",
                       "P_toplam - Toplam Tepe Kuvveti (kgf)"]],
            use_container_width=True, hide_index=True
        )

    # ------------------ Excel oluştur ------------------
    params_rows = [
        {"Parametre": "Güvenlik Katsayısı", "Değer": safety_factor},
        {"Parametre": "Rüzgar Yükü P_w (kgf)", "Değer": wind_force},
        {"Parametre": "Direk Birleştirme Mesafesi", "Değer": merge_tolerance},
        {"Parametre": "Direk Tipi Metni Eşleştirme Mesafesi", "Değer": name_match_dist},
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
