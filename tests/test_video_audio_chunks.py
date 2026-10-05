import unittest

from tg_summary_bot.bot import audio_chunk_ranges


class VideoAudioChunkTests(unittest.TestCase):
    def test_keeps_short_audio_in_one_chunk(self) -> None:
        self.assertEqual(audio_chunk_ranges(120, 600), [(0, 120)])

    def test_uses_one_bounded_range_when_duration_is_unknown(self) -> None:
        self.assertEqual(audio_chunk_ranges(None, 600), [(0, 600)])

    def test_clamps_invalid_chunk_size(self) -> None:
        self.assertEqual(audio_chunk_ranges(2, 0), [(0, 1), (1, 1)])

    def test_splits_long_audio_into_bounded_ranges(self) -> None:
        self.assertEqual(
            audio_chunk_ranges(1662, 600),
            [(0, 600), (600, 600), (1200, 462)],
        )
