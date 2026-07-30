# Direk Tepe Kuvveti Analiz Programı

## Kurulum
```
pip install -r requirements.txt
```

## Çalıştırma
```
streamlit run app.py
```
`pole_core.py` dosyasının `app.py` ile **aynı klasörde** (repo kökünde) olması gerekir. Streamlit Cloud'da "Main file path" ayarının `app.py` olduğundan emin olun.

## Kullanım Adımları
1. AutoCAD projesini **DXF** formatında dışa aktarıp yükleyin (DWG değil).
2. Kablo hattı ve etiket (metin) katmanlarını seçin.
3. "Direkleri Tespit Et" ile otomatik direk/segment tespiti yapın.
4. Tespit edilen direk adlarını ve (biliniyorsa) mevcut direk tiplerini
   düzenleyin.
5. Kablo tiplerinin çekme kuvvetlerini ve direk kapasite tablosunu
   TEDAŞ şartnamenize göre güncelleyin.
6. "Hesapla ve Excel Raporu Oluştur" ile sonuçları görüntüleyin ve
   Excel raporunu indirin.

## Kablo / Direk Etiketi Kısaltmaları
Çizimlerdeki bazı kısaltmalar otomatik olarak tanınır ve Excel raporunda
"Direk Etiketi Yorumu" ile kablo tipi sütunlarında okunabilir hale getirilir:

- **Kablo harf kodları:** `R` = Rose, `P` = Pansy, `SW` = Swallow,
  `AER` = Alpek. Örn. `3xR` → 3x Rose kablo.
- **İzolatör/donanım etiketleri:** `<sayı><harf>+<sayı><harf>` deseni,
  örn. `4P+R` → 4x Pansy + 1x Rose izolatör.
- **Bilinen direk tipi kodları:** `GK1` → Müşterek (Uzun) K1 Tipi Direk.

Bu eşleştirmeler `pole_core.py` içindeki `CABLE_LETTER_CODE_MAP` ve
`POLE_TAG_CODE_MAP` sözlüklerinden genişletilebilir.

## Önemli Not
Kuvvet hesabı, direğe bağlı her hattın çekme kuvvetini vektörel olarak
toplayan basitleştirilmiş bir modeldir. Rüzgar/buz yükü ve sıcaklık gibi
TEDAŞ şartnamesindeki detaylı katsayıları içermez; sonuçlar sahada
uygulanmadan önce ilgili mühendis tarafından doğrulanmalıdır.
