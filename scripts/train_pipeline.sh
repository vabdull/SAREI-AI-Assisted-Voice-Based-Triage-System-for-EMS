#!/usr/bin/env bash
# Rebuild the entire ASR pipeline from scratch using SADA's cleaned_text,
# then run an overfit sanity test.
#
# Run from the EMS project root inside WSL:
#   bash scripts/rebuild_and_test.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "============================================"
echo " 1/6  Rebuild manifests (cleaned_text)"
echo "============================================"
python scripts/prepare_manifests.py \
    --data-dir data \
    --output-dir manifests

echo ""
echo "============================================"
echo " 2/6  Filter dialects (Najdi, Hijazi, Khaleeji)"
echo "============================================"
python scripts/filter_dialect.py \
    --data-dir data \
    --manifest-dir manifests \
    --output-dir manifests_ems_dialects

echo ""
echo "============================================"
echo " 3/6  Rebuild tokenizer"
echo "============================================"
python scripts/build_tokenizer.py \
    --manifest manifests_ems_dialects/train_manifest.json \
    --output-dir tokenizer \
    --vocab-size 256

echo ""
echo "============================================"
echo " 4/6  Create overfit debug manifests"
echo "============================================"
mkdir -p manifests_debug
python -c "
import json
for split, n in [('train', 100), ('val', 20)]:
    src = 'manifests_ems_dialects/train_manifest.json'
    dst = f'manifests_debug/{split}_overfit.json'
    with open(src, 'r', encoding='utf-8') as fi, \
         open(dst, 'w', encoding='utf-8') as fo:
        w = 0
        for line in fi:
            if w >= n: break
            e = json.loads(line.strip())
            if e.get('text','').strip():
                fo.write(json.dumps(e, ensure_ascii=False) + '\n')
                w += 1
    print(f'  {dst}: {w} samples')
"

echo ""
echo "============================================"
echo " 5/6  Verify tokenizer round-trip"
echo "============================================"
python -c "
import json, sentencepiece as spm
sp = spm.SentencePieceProcessor()
sp.load('tokenizer/tokenizer.model')
print(f'  Vocab: {sp.get_piece_size()} tokens')
with open('manifests_debug/train_overfit.json','r',encoding='utf-8') as f:
    for i,line in enumerate(f):
        if i>=5: break
        text = json.loads(line)['text']
        ids = sp.encode(text, out_type=int)
        dec = sp.decode(ids)
        ok = 'OK' if dec == text else 'MISMATCH'
        print(f'  [{ok}] {text}')
        if dec != text: print(f'       -> {dec}')
"

echo ""
echo "============================================"
echo " 6/6  Launch overfit training (100 epochs)"
echo "============================================"
echo "  Watch TensorBoard: tensorboard --logdir experiments"
echo ""
python scripts/train_asr.py \
    --config configs/overfit_test.yaml \
    --run-name overfit-sanity

echo ""
echo "============================================"
echo " Done! Check experiments/ for results."
echo "============================================"
