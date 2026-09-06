#!/usr/bin/env python3
"""Génère le modèle MLX whisper-large-v3-turbo QUANTIFIÉ 8 bits depuis le fp16.

q8 = sortie texte STRICTEMENT identique au fp16 (mesuré 0,00 % WER/CER sur le
corpus dictée + réunion), mais ~2x moins de RAM (1543 -> 833 Mo « active ») et
824 Mo sur disque au lieu de 1614. C'est le modèle EMBARQUÉ par l'app (dictée
sous 2 Go de RAM). Le fp16 reste la source dev (download_mlx.py).

Idempotent : ne régénère pas si déjà présent. Lancé une fois en dev, puis le q8
est commité/bundlé. Reproductible.

Usage : ./venv/bin/python quantize_model.py
"""
import os, json, gc
import mlx.core as mx
import mlx.nn as nn
from mlx.utils import tree_flatten

ROOT = os.path.dirname(os.path.abspath(__file__))
FP16 = os.path.join(ROOT, "models", "whisper-large-v3-turbo-mlx")
Q8 = os.path.join(ROOT, "models", "whisper-large-v3-turbo-mlx-q8")
BITS, GROUP = 8, 64

if os.path.exists(os.path.join(Q8, "weights.safetensors")):
    print(f"q8 déjà présent : {Q8}")
    raise SystemExit(0)
if not os.path.exists(os.path.join(FP16, "config.json")):
    raise SystemExit(f"ECHEC : modèle fp16 source absent ({FP16}). "
                     f"Lance d'abord download_mlx.py.")

import mlx_whisper.load_models as lm
print(f"Chargement fp16 depuis {FP16} …")
model = lm.load_model(FP16, dtype=mx.float16)
print(f"Quantification {BITS} bits (group_size={GROUP}) …")
nn.quantize(model, group_size=GROUP, bits=BITS,
            class_predicate=lambda p, m: isinstance(m, (nn.Linear, nn.Embedding)))

os.makedirs(Q8, exist_ok=True)
weights = dict(tree_flatten(model.parameters()))
mx.save_safetensors(os.path.join(Q8, "weights.safetensors"), weights)
cfg = json.load(open(os.path.join(FP16, "config.json")))
cfg["quantization"] = {"group_size": GROUP, "bits": BITS}
json.dump(cfg, open(os.path.join(Q8, "config.json"), "w"))
del model, weights
gc.collect(); mx.clear_cache()
sz = os.path.getsize(os.path.join(Q8, "weights.safetensors")) / 2 ** 20
print(f"q8 généré : {Q8}  ({sz:.0f} Mo)")
