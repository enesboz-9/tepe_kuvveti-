# Direk Tepe Kuvveti Analiz Programı

## Kurulum
```
pip install -r requirements.txt
```

## Çalıştırma
```
streamlit run pole_force_app.py
```
`pole_core.py` dosyasının `pole_force_app.py` ile **aynı klasörde** olması gerekir.

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

## Önemli Not
Kuvvet hesabı, direğe bağlı her hattın çekme kuvvetini vektörel olarak
toplayan basitleştirilmiş bir modeldir. Rüzgar/buz yükü ve sıcaklık gibi
TEDAŞ şartnamesindeki detaylı katsayıları içermez; sonuçlar sahada
uygulanmadan önce ilgili mühendis tarafından doğrulanmalıdır.
