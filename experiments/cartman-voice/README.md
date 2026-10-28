# Russian Cartman reference experiment

Local personal synthetic-voice experiment. Generated speech must be identified as synthetic, not an authentic new actor recording. Everything for this pipeline stays in this directory; source video is read-only. No bot changes or training.

## Findings for this episode

`../South Park_22s_04ep.mp4` has **one** unlabeled stereo AAC audio track (global index 1), 44.1 kHz, ~192 kb/s. There is no English reference track or center channel. The requested clips contain automatically detected Russian; ASR cannot prove dubbing studio or speaker identity. The channels are effectively dual mono: L/R correlation >0.99999 in both clips. Center/side tricks cannot recover a separate dialogue signal.

Consequently **Method B and the subtraction/AI hybrid cannot be evaluated with this input**. Do not mistake L−R for English soundtrack subtraction. HTDemucs and MDX Extra provide two AI candidates, not a replacement English track. Other-character and underlying-English speech may survive both.

## Tools and setup

FFmpeg/FFprobe 8.0.1 are installed. The repository already has `.venv-tts` with torch, scipy, numpy, soundfile and Qwen3-TTS, and `.venv` with faster-whisper and cached large-v3. Reuse these rather than downloading another ASR/TTS model.

All commands below run from **this directory**:

```bash
# Only add the missing separator packages locally; do not change the existing TTS environment.
uv pip install --python ../../.venv-tts/bin/python --target .deps --no-deps \
  demucs==4.1.0 julius==0.2.8 lameenc==1.8.4 sphn==0.2.1
```

This overlay uses Demucs' other dependencies already installed in `.venv-tts`. On a different machine use an isolated environment with `numpy scipy soundfile demucs==4.1.0 faster-whisper`; install CUDA-compatible PyTorch following its official instructions. Models are downloaded only on an explicit `ai` command. Demucs 4.1.0 loads its HF models through the standard `~/.cache/huggingface/hub/` cache; its legacy Torch fallback uses `models/torch/`. ASR has `local_files_only=True`: a missing model fails instead of downloading silently.

## Inspect and extract

Edit `segments.json` before processing. Source/output paths are relative to the configuration file. Set global audio stream indices only after checking FFprobe. Speech margins default to 0.2 seconds before/after. Source cuts use decoded sample indices and preserve native rate/channels in float32 WAV.

```bash
../../.venv-tts/bin/python pipeline.py inspect
../../.venv-tts/bin/python pipeline.py extract
../../.venv-tts/bin/python pipeline.py channels
```

`inspect` saves complete FFprobe JSON, `source_analysis.md`, all audio tracks as stream-copy MKA and decoded float WAV. `extract` saves both requested intervals plus margins and sample-accurate `timing.json`. No intermediate is overwritten: choose another output directory to rerun an existing stage. Reports can be regenerated; human edits belong in the configuration or a separate listening notes file.

## Separation and transcription

```bash
../../.venv-tts/bin/python pipeline.py ai --model htdemucs --device cuda --shifts 4
../../.venv-tts/bin/python pipeline.py ai --model mdx_extra --device cuda --shifts 4
../../.venv/bin/python pipeline.py transcribe --method raw --detect-language
../../.venv/bin/python pipeline.py transcribe --method ai_htdemucs
../../.venv/bin/python pipeline.py transcribe --method ai_mdx_extra
../../.venv-tts/bin/python pipeline.py review
../../.venv-tts/bin/python pipeline.py report
```

GPU jobs run sequentially. Four random-shift predictions and 50% overlap favor quality; results can vary slightly. Full float vocal and accompaniment stems remain in `output/demucs/`; convenience vocal copies are in `output/separated/`. No enhancement, pitch alteration or compression is applied. Native 44.1 kHz extraction happens before model-specific resampling.

Transcripts are **unverified drafts** in `output/transcripts/`, with per-word timing/confidence. Raw candidate ASR also attempts language identification. Confidence is not proof of wording or voice identity. Listen and preserve repetitions/interjections; mark unclear words rather than guessing. Avoid treating an unfinished boundary word as a complete transcript.

## Listen, edit and export

Open **`output/review.html`** in your browser for all six playable A/B candidates and their ASR drafts. The `review` stage writes mono PCM16 24 kHz audition copies and matching **unverified** UTF-8 texts in `output/review_audio/`; these are not selected final references. Compare raw / HTDemucs / MDX Extra at comfortable matched playback loudness. Use `ffplay -autoexit output/separated/cartman_002_ai_htdemucs.wav`, or any player. Check background music, English leakage, effects, intelligibility, separation artifacts, articulation and identity. Pending evaluation forms are in `output/listening_review.md`.

For each accepted segment, edit these fields in `segments.json`:

```json
{
  "selected_method": "ai_htdemucs",
  "transcript": "Exact Russian words confirmed by listening",
  "transcript_verified": true,
  "reference_start": 0.2,
  "reference_end": 8.0
}
```

Use `raw`, `ai_htdemucs` or `ai_mdx_extra`. Reference trim times are **relative to the margin-inclusive candidate**, not episode timecodes. Use pauses to avoid word clipping; prefer 5–10 seconds and one speaker. The 11-second second requested interval remains intact as raw material. If splitting it, add separate segment IDs in a new configuration/output directory and give each its own exact transcript. No automatic mid-word segmentation.

```bash
../../.venv-tts/bin/python pipeline.py export
../../.venv-tts/bin/python pipeline.py report
```

Export waits for a selected method and verified transcript. It writes mono PCM16 24 kHz WAV + UTF-8 text and provenance JSON to `output/references/`. Stereo downmix uses equal L/R weights. Gain only decreases if necessary for 1 dB peak headroom; no upward normalization. High-quality soxr resampling is used. Check the report for full-scale samples after export; any remaining clipping disqualifies the reference. A model's cleaner background does not automatically make it the best cloning reference.

## Optional Qwen3-TTS validation

The installed implementation accepts paths through `Qwen3TTSModel.generate_voice_clone(ref_audio=..., ref_text=..., language='Russian')`, internally reads the file sample rate and resamples as needed. The local Base model and clone wrapper already exist in `../qwen3-tts/`. After selecting and verifying a reference:

For a model-free compatibility check of all audition candidates, run `../../.venv-tts/bin/python validate_tts.py --audit-candidates`. This exercises the installed Qwen audio-file loader and checks mono PCM16 24 kHz, finite/unclipped samples and readable UTF-8 text. It does **not** prove voice similarity, transcript accuracy, prompt encoding or synthesis quality; results are in `output/qwen_input_validation.json`.

```bash
../../.venv-tts/bin/python validate_tts.py --id cartman_001 \
  --text 'Это синтетический тест голоса. Сегодня мы обсуждаем новый план.'
```

This checks the WAV/text format, passes the verified pair to the existing wrapper and writes separately to `output/tts_validation/`. It does not retrain, fine-tune or change the reference. The wrapper may warn about absent optional SoX/FlashAttention; these are separate from reference-format compatibility. Evaluate generated Russian intelligibility and voice similarity by listening.

CosyVoice's existing `../cosyvoice3/clone.py` accepts the same WAV and transcript via `--ref-audio` / `--ref-text`; use `../../.venv-cosyvoice3/bin/python`. Do not run both TTS models concurrently on the 12 GB GPU.

### User-selected draft-reference comparison

For a quick listening trial, `../../.venv-tts/bin/python compare_tts.py` runs CosyVoice then Qwen using the selected `cartman_002_ai_mdx_extra.wav` directly and its ASR text draft. Both generate the same Russian sentence at speed 1.0, with no style instructions or output post-processing. The draft transcript is explicitly marked unverified in metadata; this does not promote it to a verified final reference.

Results, per-model logs, Russian test text, provenance JSON and `comparison.html` are saved under `output/tts_comparison/`. Use `--text 'Другой русский текст.' --output output/another_trial` for another test, or `--models cosyvoice` / `--models qwen` to run one model. Existing outputs are preserved. Model loading is offline, using the installed local weights.

## Verification

`../../.venv-tts/bin/python -m unittest -v test_pipeline` checks exact sample boundaries, unchanged source bytes, refusal to overwrite, valid WAV/text export without gain inflation, rejection of out-of-range intervals, and the requirement for a verified transcript. It uses synthetic temporary audio, not a copy of the episode.

## Additional episodes

Copy the configuration to a new JSON file, set `source`, choose a unique `output`, replace the segment times, and run each stage with `--config new_episode.json`. Always inspect streams first. If an episode has separate verified English/Russian tracks, first measure offsets and drift in background-only windows and validate channel mapping/mix correlation. Fit cancellation only to speech-free backgrounds and compare held-out windows; reject it if Russian articulation is reduced. This pipeline intentionally does not run unsafe blind subtraction, and this episode offers no data to test such an implementation.
