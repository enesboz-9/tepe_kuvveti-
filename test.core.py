# -*- coding: utf-8 -*-
"""
test_pole_core.py
------------------
pole_core.py çekirdek mantığı için temel birim testleri.

Çalıştırmak için:
    pip install pytest
    pytest test_pole_core.py -v
"""

import math
import pytest

from pole_core import (
    parse_cable_composition,
    parse_pole_equipment_tag,
    parse_conductor_count,
    classify_text,
    cluster_vertices,
    compute_resultant_force,
    recommend_pole_type,
    Pole,
    PoleSegmentRef,
    extract_polylines,
    extract_texts,
)


# --------------------------------------------------------------------------
# Kablo / direk etiketi çözümleme testleri
# --------------------------------------------------------------------------

def test_parse_cable_composition_single_letter():
    assert parse_cable_composition("3xR") == (3, "3x Rose")


def test_parse_cable_composition_bracketed():
    # (5xR) -> mevcut hat, [3xR] -> BYSK hat; parantezler temizlenmeli
    assert parse_cable_composition("(5xR)") == (5, "5x Rose")
    assert parse_cable_composition("[3xR]") == (3, "3x Rose")


def test_parse_cable_composition_combined():
    total, desc = parse_cable_composition("4P+R")
    assert total == 5
    assert "4x Pansy" in desc and "1x Rose" in desc


def test_parse_cable_composition_full_name_og():
    # OG (kuş isimli) iletkenler tam isimle de yazılabilir
    assert parse_cable_composition("3xSWALLOW") == (3, "3x Swallow")
    assert parse_cable_composition("HAWK") == (1, "1x Hawk")


def test_parse_cable_composition_unknown_returns_none():
    assert parse_cable_composition("XYZ") is None


def test_parse_pole_equipment_tag_known_code():
    assert parse_pole_equipment_tag("GK1") == "Müşterek (Uzun) K1 Tipi Direk"


def test_parse_pole_equipment_tag_composition():
    result = parse_pole_equipment_tag("2P+2R")
    assert result is not None
    assert result.endswith("izolatör")


def test_parse_conductor_count():
    assert parse_conductor_count("3xR") == 3
    assert parse_conductor_count("SW") == 1
    assert parse_conductor_count("50/8") == 1  # sayısal kablo kesiti kodu -> varsayılan 1


# --------------------------------------------------------------------------
# Metin sınıflandırma testleri
# --------------------------------------------------------------------------

def test_classify_text_span_length():
    assert classify_text("42.5") == "span_length"


def test_classify_text_pole_id():
    assert classify_text("A01") == "pole_id"
    assert classify_text("T25") == "pole_id"


def test_classify_text_pole_type():
    assert classify_text("G-12I") == "pole_type"


def test_classify_text_cable_spec():
    assert classify_text("3xR") == "cable_spec"


def test_classify_text_ignore():
    assert classify_text("HAR.MÜH") == "ignore"


# --------------------------------------------------------------------------
# Geometri / kümeleme testleri
# --------------------------------------------------------------------------

def test_cluster_vertices_merges_close_points():
    points = [(0, 0), (0.5, 0.5), (100, 100)]
    clusters = cluster_vertices(points, tolerance=3.0)
    assert len(clusters) == 2  # ilk iki nokta birleşmeli, üçüncüsü ayrı kalmalı


# --------------------------------------------------------------------------
# Kuvvet hesabı testleri
# --------------------------------------------------------------------------

def test_compute_resultant_force_opposing_cancels_out():
    # Aynı büyüklükte, zıt yönlerde iki hat birbirini götürmeli (yaklaşık 0)
    pole = Pole(pole_id="P1", coord=(0, 0))
    pole.segments = [
        PoleSegmentRef(other_point=(10, 0), cable_spec="3xR", conductor_count=3),
        PoleSegmentRef(other_point=(-10, 0), cable_spec="3xR", conductor_count=3),
    ]
    tension_lookup = {"3xR": 500.0}
    force, details = compute_resultant_force(pole, tension_lookup)
    assert force == pytest.approx(0.0, abs=1e-6)
    assert len(details) == 2


def test_compute_resultant_force_single_line():
    pole = Pole(pole_id="P1", coord=(0, 0))
    pole.segments = [
        PoleSegmentRef(other_point=(10, 0), cable_spec="3xR", conductor_count=3),
    ]
    tension_lookup = {"3xR": 500.0}
    force, _ = compute_resultant_force(pole, tension_lookup)
    assert force == pytest.approx(1500.0)  # 500 kgf x 3 iletken


def test_compute_resultant_force_with_load_factor():
    # Rüzgar/buz yük katsayısı çarpanı doğru uygulanmalı
    pole = Pole(pole_id="P1", coord=(0, 0))
    pole.segments = [
        PoleSegmentRef(other_point=(10, 0), cable_spec="3xR", conductor_count=3),
    ]
    tension_lookup = {"3xR": 500.0}
    load_factor_lookup = {"3xR": 1.2}
    force, _ = compute_resultant_force(pole, tension_lookup, load_factor_lookup)
    assert force == pytest.approx(1500.0 * 1.2)


def test_compute_resultant_force_perpendicular_lines():
    # 90 derece açılı iki eşit kuvvet -> bileşke = F * sqrt(2)
    pole = Pole(pole_id="P1", coord=(0, 0))
    pole.segments = [
        PoleSegmentRef(other_point=(10, 0), cable_spec="R", conductor_count=1),
        PoleSegmentRef(other_point=(0, 10), cable_spec="R", conductor_count=1),
    ]
    tension_lookup = {"R": 500.0}
    force, _ = compute_resultant_force(pole, tension_lookup)
    assert force == pytest.approx(500.0 * math.sqrt(2))


# --------------------------------------------------------------------------
# Direk tipi önerisi testleri
# --------------------------------------------------------------------------

def test_recommend_pole_type_picks_lowest_sufficient_capacity():
    capacity_table = {"9 Ağaç": 400.0, "10I": 800.0, "12I": 1200.0, "K Tipi": 2000.0}
    tip, kapasite = recommend_pole_type(force=700.0, capacity_table=capacity_table, safety_factor=1.0)
    assert tip == "10I"
    assert kapasite == 800.0


def test_recommend_pole_type_applies_safety_factor():
    capacity_table = {"9 Ağaç": 400.0, "10I": 800.0, "12I": 1200.0, "K Tipi": 2000.0}
    # 700 kgf x 1.5 güvenlik katsayısı = 1050 kgf -> 12I gerekir
    tip, kapasite = recommend_pole_type(force=700.0, capacity_table=capacity_table, safety_factor=1.5)
    assert tip == "12I"


def test_recommend_pole_type_insufficient_capacity_warns():
    capacity_table = {"9 Ağaç": 400.0}
    tip, _ = recommend_pole_type(force=1000.0, capacity_table=capacity_table, safety_factor=1.0)
    assert "YETERSİZ" in tip


# --------------------------------------------------------------------------
# Katman filtreleme testleri (boş liste = hiçbir şey eşleşmemeli)
# --------------------------------------------------------------------------

class _FakeDoc:
    """extract_polylines/extract_texts'i gerçek bir DXF dosyası olmadan test
    etmek için minimal bir sahte modelspace/doc nesnesi."""

    def __init__(self, entities):
        self._entities = entities

    def modelspace(self):
        return self

    def query(self, _query_str):
        return self._entities


class _FakeLine:
    def __init__(self, layer, p1, p2):
        self.dxf = type("dxf", (), {})()
        self.dxf.layer = layer
        self.dxf.color = 256
        self.dxf.start = type("pt", (), {"x": p1[0], "y": p1[1]})()
        self.dxf.end = type("pt", (), {"x": p2[0], "y": p2[1]})()

    def dxftype(self):
        return "LINE"


def test_extract_polylines_empty_layer_list_matches_nothing():
    doc = _FakeDoc([_FakeLine("KABLO", (0, 0), (10, 0))])
    segments = extract_polylines(doc, [])
    assert segments == []


def test_extract_polylines_none_layer_list_matches_everything():
    doc = _FakeDoc([_FakeLine("KABLO", (0, 0), (10, 0))])
    segments = extract_polylines(doc, None)
    assert len(segments) == 1


def test_extract_polylines_selected_layer_filters_correctly():
    doc = _FakeDoc([
        _FakeLine("KABLO", (0, 0), (10, 0)),
        _FakeLine("DIGER", (0, 0), (5, 5)),
    ])
    segments = extract_polylines(doc, ["KABLO"])
    assert len(segments) == 1
    assert segments[0]["layer"] == "KABLO"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
