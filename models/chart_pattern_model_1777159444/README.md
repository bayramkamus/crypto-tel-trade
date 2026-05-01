# Chart Pattern Model Export

Bu klasor tasinabilir model paketidir. Mevcut proje yapisindan dosya tasinmadi; checkpoint sadece buraya kopyalandi.

Icerik:

- `checkpoint.pt`: egitilmis EfficientNet-B0 multi-head model checkpoint'i
- `inference.py`: baska projede kullanilabilecek bagimsiz inference kodu
- `requirements.txt`: gerekli Python paketleri

Kurulum:

```bash
python -m pip install -r requirements.txt
```

Python icinden kullanim:

```python
from inference import predict_chart

result = predict_chart("chart.png")
print(result["detected_pattern"], result["signal"])
```

CLI kullanim:

```bash
python inference.py chart.png
```

Model girdisi:

- Chart goruntusu `RGB` olarak okunur.
- Goruntu `224x224` boyutuna resize edilir.
- ImageNet normalize degerleri kullanilir.

Cikti:

- `detected_pattern`
- `pattern_confidence`
- `signal`
- `signal_confidence`
- `signal_from_pattern_rule`
- `heads_agree`
- `pattern_probabilities`
- `signal_probabilities`
