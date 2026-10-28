"""Numerical regression checks for sample timing, immutable inputs and reference export."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

import pipeline


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source = self.base / "source.wav"
        self.rate = 44100
        t = np.arange(self.rate * 2) / self.rate
        self.signal = np.column_stack((0.3 * np.sin(2 * np.pi * 440 * t), 0.2 * np.sin(2 * np.pi * 660 * t))).astype(np.float32)
        sf.write(self.source, self.signal, self.rate, subtype="FLOAT")
        self.digest = hashlib.sha256(self.source.read_bytes()).digest()
        self.out = self.base / "output"
        self.config = {"source": "source.wav", "audio_stream": 0, "margin_before": 0.2,
                       "margin_after": 0.2, "reference_sample_rate": 24000,
                       "segments": [{"id": "test_001", "start": "00:00:00.500", "end": "00:00:01.500",
                                     "transcript": "Тест.", "selected_method": "raw", "transcript_verified": True}]}

    def test_sample_accurate_native_extraction_and_immutable_source(self):
        pipeline.extract(self.config, self.source, self.out)
        actual, rate = sf.read(self.out / "raw/test_001.wav", dtype="float32", always_2d=True)
        self.assertEqual(rate, self.rate)
        np.testing.assert_array_equal(actual, self.signal[13230:74970])
        timing = json.loads((self.out / "timing.json").read_text())[0]
        self.assertEqual(timing["start_sample"], 13230)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).digest(), self.digest)
        with self.assertRaises(FileExistsError):
            pipeline.extract(self.config, self.source, self.out)

    def test_reference_export_preserves_gain_and_utf8(self):
        pipeline.extract(self.config, self.source, self.out)
        pipeline.export(self.config, self.out)
        wav = self.out / "references/test_001.wav"
        info = sf.info(wav)
        self.assertEqual((info.samplerate, info.channels, info.subtype, info.frames), (24000, 1, "PCM_16", 33600))
        self.assertEqual(wav.with_suffix(".txt").read_text(encoding="utf-8"), "Тест.\n")
        self.assertEqual(json.loads(wav.with_suffix(".json").read_text())["gain"], 1)
        x, _ = sf.read(wav)
        self.assertLess(np.max(np.abs(x)), 0.26)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).digest(), self.digest)
        with self.assertRaises(FileExistsError):
            pipeline.export(self.config, self.out)

    def test_unverified_transcript_never_becomes_final_reference(self):
        pipeline.extract(self.config, self.source, self.out)
        self.config["segments"][0]["transcript_verified"] = False
        pipeline.export(self.config, self.out)
        self.assertFalse((self.out / "references/test_001.wav").exists())

    def test_invalid_interval_is_rejected(self):
        self.config["segments"][0]["end"] = "00:00:03.000"
        with self.assertRaises(ValueError):
            pipeline.extract(self.config, self.source, self.out)


if __name__ == "__main__":
    unittest.main()
