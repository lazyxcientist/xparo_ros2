"""Bidirectional file sync (see /home/scientist/.claude/plans/
breezy-splashing-koala.md): normalize_for_hash/content_hash. Kept in
lockstep with the identical Django-side copy at
apps/analytics/sync_hash.py's own test file -- same three cases on both
sides.
"""
from xparo.sync_hash import content_hash, normalize_for_hash


class TestNormalizeForHash:
    def test_crlf_and_lf_normalize_to_the_same_text(self):
        assert normalize_for_hash("a\r\nb\r\n") == normalize_for_hash("a\nb\n")

    def test_trailing_whitespace_per_line_is_stripped(self):
        assert normalize_for_hash("a   \nb\t\n") == normalize_for_hash("a\nb\n")

    def test_trailing_newline_count_does_not_matter(self):
        assert normalize_for_hash("a\nb") == normalize_for_hash("a\nb\n\n\n")

    def test_a_real_semantic_difference_still_differs(self):
        assert normalize_for_hash("a\nb\n") != normalize_for_hash("a\nc\n")


class TestContentHash:
    def test_identical_normalized_content_hashes_the_same(self):
        assert content_hash("a\nb\n") == content_hash("a\r\nb\r\n")

    def test_different_content_hashes_differently(self):
        assert content_hash("a\n") != content_hash("b\n")

    def test_a_different_split_of_the_same_concatenated_text_does_not_collide(self):
        assert content_hash("ab", "c") != content_hash("a", "bc")

    def test_header_source_is_part_of_the_hash_for_cpp_style_files(self):
        assert content_hash("same source\n", "header a\n") != content_hash("same source\n", "header b\n")
