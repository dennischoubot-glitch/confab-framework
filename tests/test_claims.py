"""Tests for claim extraction and classification."""

import unittest

from confab.claims import (
    Claim,
    ClaimType,
    VerifiabilityLevel,
    extract_claims,
    extract_claims_from_file,
    summarize_claims,
    FILE_PATH_RE,
    ENV_VAR_RE,
    VERIFICATION_TAG_RE,
    BLOCKER_RE,
    PIPELINE_STATUS_RE,
    COUNT_RE,
    META_RULE_RE,
    OPTIONAL_FILE_RE,
    _extract_file_paths,
    _is_assertion_context,
    _is_directive_context,
    _is_optional_reference,
    _is_config_assertion,
    _extract_config_keys,
    _is_process_status_claim,
)


class TestFilePathRegex(unittest.TestCase):
    """Test FILE_PATH_RE pattern matching."""

    def test_backtick_paths(self):
        matches = FILE_PATH_RE.findall("`scripts/deploy.py`")
        paths = [m[0] or m[1] for m in matches]
        self.assertIn("scripts/deploy.py", paths)

    def test_bare_paths(self):
        matches = FILE_PATH_RE.findall("Check core/confab/config.py for details")
        paths = [m[0] or m[1] for m in matches]
        self.assertIn("core/confab/config.py", paths)

    def test_various_extensions(self):
        for ext in ["py", "md", "json", "yaml", "toml", "sh", "js", "ts", "html"]:
            text = f"`some/path/file.{ext}`"
            matches = FILE_PATH_RE.findall(text)
            paths = [m[0] or m[1] for m in matches]
            self.assertTrue(any(f"file.{ext}" in p for p in paths), f"Failed for .{ext}")

    def test_no_match_plain_text(self):
        matches = FILE_PATH_RE.findall("just some plain text without paths")
        self.assertEqual(len(matches), 0)


class TestEnvVarRegex(unittest.TestCase):
    """Test ENV_VAR_RE pattern matching."""

    def test_api_key_pattern(self):
        match = ENV_VAR_RE.search("needs OPENAI_API_KEY to work")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "OPENAI_API_KEY")

    def test_token_pattern(self):
        match = ENV_VAR_RE.search("missing SLACK_BOT_TOKEN")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "SLACK_BOT_TOKEN")

    def test_short_names_excluded(self):
        # Names shorter than 3 chars shouldn't match
        match = ENV_VAR_RE.search("set AB to true")
        # AB is only 2 chars, should not match the pattern
        if match:
            self.assertNotEqual(match.group(1), "AB")


class TestVerificationTagRegex(unittest.TestCase):
    """Test VERIFICATION_TAG_RE matching."""

    def test_v1_tag(self):
        match = VERIFICATION_TAG_RE.search("[v1: checked file_read 2026-03-19]")
        self.assertIsNotNone(match)

    def test_v2_tag(self):
        match = VERIFICATION_TAG_RE.search("[v2: checked web_search 2026-03-20]")
        self.assertIsNotNone(match)

    def test_unverified_tag(self):
        match = VERIFICATION_TAG_RE.search("[unverified]")
        self.assertIsNotNone(match)

    def test_failed_tag(self):
        match = VERIFICATION_TAG_RE.search("[FAILED: file not found]")
        self.assertIsNotNone(match)

    def test_no_tag(self):
        match = VERIFICATION_TAG_RE.search("just a normal line")
        self.assertIsNone(match)


class TestBlockerRegex(unittest.TestCase):
    """Test BLOCKER_RE pattern matching."""

    def test_blocked_on(self):
        matches = BLOCKER_RE.findall("Audio blocked on OPENAI_API_KEY")
        self.assertTrue(len(matches) > 0)

    def test_needs(self):
        matches = BLOCKER_RE.findall("needs SUBSTACK_COOKIE to publish")
        self.assertTrue(len(matches) > 0)

    def test_waiting_for(self):
        matches = BLOCKER_RE.findall("waiting for API credentials")
        self.assertTrue(len(matches) > 0)

    def test_no_blocker(self):
        matches = BLOCKER_RE.findall("everything is working fine")
        self.assertEqual(len(matches), 0)


class TestPipelineStatusRegex(unittest.TestCase):
    """Test PIPELINE_STATUS_RE pattern matching."""

    def test_working(self):
        self.assertIsNotNone(PIPELINE_STATUS_RE.search("pipeline is working"))

    def test_broken(self):
        self.assertIsNotNone(PIPELINE_STATUS_RE.search("script is broken"))

    def test_running(self):
        self.assertIsNotNone(PIPELINE_STATUS_RE.search("service running"))

    def test_no_match(self):
        self.assertIsNone(PIPELINE_STATUS_RE.search("the project looks good"))


class TestCountRegex(unittest.TestCase):
    """Test COUNT_RE pattern matching."""

    def test_entries(self):
        matches = COUNT_RE.findall("361 entries in the journal")
        self.assertIn("361", matches)

    def test_tests(self):
        matches = COUNT_RE.findall("36 tests passing")
        self.assertIn("36", matches)

    def test_subscribers(self):
        matches = COUNT_RE.findall("500 subscribers")
        self.assertIn("500", matches)


class TestMetaRuleRegex(unittest.TestCase):
    """Test META_RULE_RE pattern matching."""

    def test_staleness_rule(self):
        self.assertIsNotNone(META_RULE_RE.match("Staleness rule: verify after 3 runs"))

    def test_size_rule(self):
        self.assertIsNotNone(META_RULE_RE.match("Size rules: keep under 50 lines"))

    def test_not_a_rule(self):
        self.assertIsNone(META_RULE_RE.match("Audio blocked on OPENAI_API_KEY"))


class TestOptionalFileRegex(unittest.TestCase):
    """Test OPTIONAL_FILE_RE matching."""

    def test_if_present(self):
        self.assertIsNotNone(OPTIONAL_FILE_RE.search("loads confab.toml if present"))

    def test_falls_back(self):
        self.assertIsNotNone(OPTIONAL_FILE_RE.search("or falls back to defaults"))

    def test_optionally(self):
        self.assertIsNotNone(OPTIONAL_FILE_RE.search("optionally reads config.yaml"))

    def test_normal_assertion(self):
        self.assertIsNone(OPTIONAL_FILE_RE.search("config.yaml exists and is configured"))


class TestHelperFunctions(unittest.TestCase):
    """Test claim extraction helper functions."""

    def test_extract_file_paths(self):
        paths = _extract_file_paths("`core/confab/config.py` is the config module")
        self.assertIn("core/confab/config.py", paths)

    def test_extract_file_paths_multiple(self):
        paths = _extract_file_paths("`a.py` and `b.json` are both needed")
        self.assertEqual(len(paths), 2)

    def test_is_assertion_context_positive(self):
        self.assertTrue(_is_assertion_context("the file exists and is ready"))
        self.assertTrue(_is_assertion_context("script is broken"))
        self.assertTrue(_is_assertion_context("pipeline operational"))

    def test_is_assertion_context_negative(self):
        self.assertFalse(_is_assertion_context("this is a regular comment"))

    def test_is_optional_reference(self):
        self.assertTrue(_is_optional_reference("loads config.toml if present"))
        self.assertFalse(_is_optional_reference("config.toml exists"))

    def test_is_config_assertion(self):
        self.assertTrue(_is_config_assertion(
            "`settings.json` is configured with key",
            ["settings.json"],
        ))
        self.assertFalse(_is_config_assertion(
            "`deploy.py` exists and is ready",
            ["deploy.py"],
        ))

    def test_extract_config_keys(self):
        keys = _extract_config_keys(
            "`settings.json` has `database_host` and `api_port` configured",
            ["settings.json"],
        )
        self.assertIn("database_host", keys)
        self.assertIn("api_port", keys)
        self.assertNotIn("settings.json", keys)


class TestExtractClaims(unittest.TestCase):
    """Test the main extract_claims function."""

    def test_blocker_claim_env_var(self):
        text = "Audio generation blocked on OPENAI_API_KEY"
        claims = extract_claims(text)
        self.assertTrue(len(claims) > 0)
        env_claim = next((c for c in claims if c.claim_type == ClaimType.ENV_VAR), None)
        self.assertIsNotNone(env_claim)
        self.assertIn("OPENAI_API_KEY", env_claim.extracted_env_vars)

    def test_file_exists_claim(self):
        text = "The `projects/synthesis/data/posts.json` file exists and is ready"
        claims = extract_claims(text)
        self.assertTrue(len(claims) > 0)
        file_claim = next((c for c in claims if c.claim_type == ClaimType.FILE_EXISTS), None)
        self.assertIsNotNone(file_claim)
        self.assertIn("projects/synthesis/data/posts.json", file_claim.extracted_paths)

    def test_pipeline_status_claim(self):
        text = "Notes pipeline is operational"
        claims = extract_claims(text)
        self.assertTrue(len(claims) > 0)
        status_claim = next(
            (c for c in claims if c.claim_type in (ClaimType.PIPELINE_WORKS, ClaimType.PIPELINE_BLOCKED)),
            None,
        )
        self.assertIsNotNone(status_claim)

    def test_count_claim(self):
        text = "There are 361 entries confirmed in the journal"
        claims = extract_claims(text)
        count_claims = [c for c in claims if c.claim_type == ClaimType.COUNT_CLAIM]
        self.assertTrue(len(count_claims) > 0)

    def test_directive_count_not_claimed(self):
        """Directive/constraint text with numbers should not become count claims.

        Regression: "1-2 entries per day maximum" was being extracted as a count
        claim and compared against the total posts.json count (399), producing
        false FAILED verdicts. Directives prescribe limits, not assert counts.
        """
        directives = [
            "**1-2 journal entries per day maximum.** The system was producing 8-12 entries/day",
            "**7 entries already published today (Mar 23)** (verified via git log)",
            "**When today's 1-2 entries are already published, redirect sprint cycles to:**",
            "Cap of 3 posts per session at most",
        ]
        for text in directives:
            claims = extract_claims(text)
            count_claims = [c for c in claims if c.claim_type == ClaimType.COUNT_CLAIM]
            self.assertEqual(len(count_claims), 0, f"Directive text should not produce count claim: {text[:60]}")

    def test_non_directive_count_still_extracted(self):
        """Legitimate count assertions should still be extracted."""
        text = "There are 361 entries confirmed in the journal"
        claims = extract_claims(text)
        count_claims = [c for c in claims if c.claim_type == ClaimType.COUNT_CLAIM]
        self.assertTrue(len(count_claims) > 0, "Legitimate count claim should still be extracted")

    def test_optional_file_not_claimed(self):
        """Optional file references should not be treated as existence claims."""
        text = "Loads confab.toml if present or falls back to defaults"
        claims = extract_claims(text)
        file_exists = [c for c in claims if c.claim_type == ClaimType.FILE_EXISTS]
        self.assertEqual(len(file_exists), 0)

    def test_meta_rule_skipped(self):
        """Meta-rules about claims should not be extracted as claims."""
        text = "Staleness rule: verify after 3 runs"
        claims = extract_claims(text)
        self.assertEqual(len(claims), 0)

    def test_headers_skipped(self):
        text = "# This is a header\n\nSome content"
        claims = extract_claims(text)
        header_claims = [c for c in claims if c.text.startswith("# ")]
        self.assertEqual(len(header_claims), 0)

    def test_source_file_propagated(self):
        text = "Script `deploy.py` is working"
        claims = extract_claims(text, source_file="test.md")
        for claim in claims:
            self.assertEqual(claim.source_file, "test.md")

    def test_verification_tag_extracted(self):
        text = "Audio pipeline working [v1: checked file_read 2026-03-19]"
        claims = extract_claims(text)
        tagged = [c for c in claims if c.verification_tag is not None]
        self.assertTrue(len(tagged) > 0)

    def test_sorted_by_verifiability(self):
        """Claims should be sorted: auto first, then semi, then manual."""
        text = (
            "Audio blocked on OPENAI_API_KEY\n"
            "361 entries in the journal\n"
        )
        claims = extract_claims(text)
        if len(claims) >= 2:
            priorities = [
                {"auto": 0, "semi": 1, "manual": 2}[c.verifiability.value]
                for c in claims
            ]
            self.assertEqual(priorities, sorted(priorities))

    def test_empty_text(self):
        self.assertEqual(extract_claims(""), [])

    def test_no_claims_text(self):
        self.assertEqual(extract_claims("Hello world, nothing to check here"), [])


class TestSectionExclusion(unittest.TestCase):
    """Test section-aware claim extraction filtering."""

    def test_excluded_section_skipped(self):
        """Claims in excluded sections should not be extracted."""
        text = (
            "## System Status\n"
            "Script `deploy.py` is working\n"
            "\n"
            "### Germinating threads\n"
            "**The Gain Controller** (idea-614) — Sustained 2 sessions. Crystalline.\n"
            "Script `broken.py` is broken\n"
            "\n"
            "## Next Steps\n"
            "Script `build.py` is running\n"
        )
        claims = extract_claims(text, exclude_sections=["Germinating threads"])
        claim_texts = [c.text for c in claims]
        # Should have claims from System Status and Next Steps, not Germinating threads
        self.assertTrue(any("deploy.py" in t for t in claim_texts))
        self.assertTrue(any("build.py" in t for t in claim_texts))
        self.assertFalse(any("broken.py" in t for t in claim_texts))
        self.assertFalse(any("Gain Controller" in t for t in claim_texts))

    def test_exclusion_ends_at_same_level_heading(self):
        """Exclusion should end when a heading at the same or higher level appears."""
        text = (
            "### Germinating threads\n"
            "Script `excluded.py` is broken\n"
            "\n"
            "### Active items\n"
            "Script `included.py` is running\n"
        )
        claims = extract_claims(text, exclude_sections=["Germinating threads"])
        claim_texts = [c.text for c in claims]
        self.assertFalse(any("excluded.py" in t for t in claim_texts))
        self.assertTrue(any("included.py" in t for t in claim_texts))

    def test_exclusion_ends_at_higher_level_heading(self):
        """A higher-level heading should also end the exclusion."""
        text = (
            "### Germinating threads\n"
            "Script `excluded.py` is broken\n"
            "\n"
            "## Top Level Section\n"
            "Script `included.py` is running\n"
        )
        claims = extract_claims(text, exclude_sections=["Germinating threads"])
        claim_texts = [c.text for c in claims]
        self.assertFalse(any("excluded.py" in t for t in claim_texts))
        self.assertTrue(any("included.py" in t for t in claim_texts))

    def test_deeper_subheading_stays_excluded(self):
        """A deeper heading within an excluded section should stay excluded."""
        text = (
            "### Germinating threads\n"
            "#### Sub-thread A\n"
            "Script `still_excluded.py` is broken\n"
            "\n"
            "### Active items\n"
            "Script `included.py` is running\n"
        )
        claims = extract_claims(text, exclude_sections=["Germinating threads"])
        claim_texts = [c.text for c in claims]
        self.assertFalse(any("still_excluded.py" in t for t in claim_texts))
        self.assertTrue(any("included.py" in t for t in claim_texts))

    def test_multiple_exclusion_patterns(self):
        """Multiple exclusion patterns should all be applied."""
        text = (
            "### Germinating threads\n"
            "Script `germ.py` is broken\n"
            "### For Next Dreamer\n"
            "Script `dreamer.py` is broken\n"
            "### Active items\n"
            "Script `active.py` is running\n"
        )
        claims = extract_claims(
            text,
            exclude_sections=["Germinating threads", "For Next Dreamer"],
        )
        claim_texts = [c.text for c in claims]
        self.assertFalse(any("germ.py" in t for t in claim_texts))
        self.assertFalse(any("dreamer.py" in t for t in claim_texts))
        self.assertTrue(any("active.py" in t for t in claim_texts))

    def test_regex_pattern_matching(self):
        """Exclusion patterns should support regex."""
        text = (
            "### Germinating threads (do not rush)\n"
            "Script `excluded.py` is broken\n"
            "### Active items\n"
            "Script `included.py` is running\n"
        )
        claims = extract_claims(text, exclude_sections=[r"Germinating threads"])
        claim_texts = [c.text for c in claims]
        self.assertFalse(any("excluded.py" in t for t in claim_texts))
        self.assertTrue(any("included.py" in t for t in claim_texts))

    def test_case_insensitive_matching(self):
        """Exclusion patterns should be case-insensitive."""
        text = (
            "### GERMINATING THREADS\n"
            "Script `excluded.py` is broken\n"
            "### Active items\n"
            "Script `included.py` is running\n"
        )
        claims = extract_claims(text, exclude_sections=["germinating threads"])
        claim_texts = [c.text for c in claims]
        self.assertFalse(any("excluded.py" in t for t in claim_texts))

    def test_empty_exclusion_list(self):
        """Empty exclusion list should not exclude anything."""
        text = (
            "### Germinating threads\n"
            "Script `germ.py` is broken\n"
        )
        claims = extract_claims(text, exclude_sections=[])
        claim_texts = [c.text for c in claims]
        self.assertTrue(any("germ.py" in t for t in claim_texts))

    def test_real_dreamer_false_positive(self):
        """The actual false positive case: germinating thread notes flagged as claims."""
        text = (
            "### Germinating threads (do not rush)\n"
            "- **The Gain Controller** (idea-614) — Sustained 2 sessions. Crystalline...\n"
            "- **The River Measurement** (idea-603) — Sustained 2 sessions.\n"
            "\n"
            "## System Status\n"
            "- **Audio WORKS** — [v2: confirmed Mar 13]\n"
            "- Notes pipeline operational. [v1: verified 2026-03-19]\n"
        )
        claims = extract_claims(
            text,
            exclude_sections=["Germinating threads"],
        )
        claim_texts = [c.text for c in claims]
        # Germinating thread notes should NOT appear
        self.assertFalse(any("Gain Controller" in t for t in claim_texts))
        self.assertFalse(any("River Measurement" in t for t in claim_texts))
        # System Status claims SHOULD appear
        self.assertTrue(any("pipeline" in t.lower() for t in claim_texts))

    def test_developing_topics_excluded(self):
        """Developing topic notes (theses, sources, status) should not be extracted."""
        text = (
            "## System Status\n"
            "- **All services RUNNING** [v1: verified 2026-03-25 6:00AM]\n"
            "- Notes pipeline operational. [v1: verified 2026-03-19]\n"
            "\n"
            "## Developing Topics\n"
            "\n"
            "### The Mortal Computation (seeded Mar 24)\n"
            "- **Thesis:** The field of consciousness research is crystallizing...\n"
            "- **Sources:** (1) COGITATE, Nature Apr 2025. (2) Milinkovic, Dec 2025.\n"
            "- **Status:** 9 sources. DEVELOPING — needs hook, one more session.\n"
            "\n"
            "### The Hard Limit (seeded Mar 24)\n"
            "- **Thesis:** Quantum computing may have a physics ceiling.\n"
            "- **Sources:** (1) Palmer, PNAS 2026. (2) Quantum Insider Mar 19.\n"
            "- **Status:** 8 sources, two independent predictions. DEVELOPING.\n"
        )
        claims = extract_claims(
            text,
            exclude_sections=["Developing Topics"],
        )
        claim_texts = [c.text for c in claims]
        # System Status claims SHOULD appear
        self.assertTrue(any("RUNNING" in t for t in claim_texts))
        self.assertTrue(any("pipeline" in t.lower() for t in claim_texts))
        # Developing topic notes should NOT appear
        self.assertFalse(any("consciousness" in t.lower() for t in claim_texts))
        self.assertFalse(any("COGITATE" in t for t in claim_texts))
        self.assertFalse(any("Quantum" in t for t in claim_texts))
        self.assertFalse(any("DEVELOPING" in t for t in claim_texts))
        self.assertEqual(len(claims), 2)


class TestProcessStatusDetection(unittest.TestCase):
    """Test process/service status claim detection."""

    def test_monitor_running(self):
        self.assertTrue(_is_process_status_claim(
            "Weather rewards monitor: running [v1: verified 2026-03-14]"
        ))

    def test_service_stopped(self):
        self.assertTrue(_is_process_status_claim(
            "weather-rewards service is stopped"
        ))

    def test_monitor_operational(self):
        self.assertTrue(_is_process_status_claim(
            "Slack monitor is operational"
        ))

    def test_process_crashed(self):
        self.assertTrue(_is_process_status_claim(
            "web server process crashed"
        ))

    def test_pipeline_not_matched(self):
        """Pipeline claims should not be classified as process status."""
        self.assertFalse(_is_process_status_claim(
            "Notes pipeline is running"
        ))

    def test_script_not_matched(self):
        self.assertFalse(_is_process_status_claim(
            "Script deploy.py is running"
        ))

    def test_unrelated_text(self):
        self.assertFalse(_is_process_status_claim(
            "The journal has 200 entries"
        ))

    def test_extraction_produces_process_status_type(self):
        """Extract claims should classify process status as PROCESS_STATUS."""
        text = "- Weather rewards monitor: running [v1: verified 2026-03-14]"
        claims = extract_claims(text)
        process_claims = [c for c in claims if c.claim_type == ClaimType.PROCESS_STATUS]
        self.assertTrue(len(process_claims) > 0, f"Expected PROCESS_STATUS claim, got: {[c.claim_type for c in claims]}")

    def test_extraction_auto_verifiable(self):
        text = "Weather rewards monitor: running"
        claims = extract_claims(text)
        process_claims = [c for c in claims if c.claim_type == ClaimType.PROCESS_STATUS]
        self.assertTrue(len(process_claims) > 0)
        self.assertEqual(process_claims[0].verifiability, VerifiabilityLevel.AUTO)

    def test_service_down_claim(self):
        text = "- web server: down since yesterday"
        claims = extract_claims(text)
        process_claims = [c for c in claims if c.claim_type == ClaimType.PROCESS_STATUS]
        self.assertTrue(len(process_claims) > 0)


class TestExtractClaimsFromFile(unittest.TestCase):
    """Test file-based claim extraction."""

    def test_nonexistent_file(self):
        claims = extract_claims_from_file("/nonexistent/path.md")
        self.assertEqual(claims, [])


class TestSummarizeClaims(unittest.TestCase):
    """Test claim summarization."""

    def test_empty_summary(self):
        summary = summarize_claims([])
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["auto_verifiable"], 0)

    def test_summary_counts(self):
        claims = [
            Claim(
                text="test",
                claim_type=ClaimType.FILE_EXISTS,
                verifiability=VerifiabilityLevel.AUTO,
            ),
            Claim(
                text="test2",
                claim_type=ClaimType.COUNT_CLAIM,
                verifiability=VerifiabilityLevel.SEMI,
            ),
        ]
        summary = summarize_claims(claims)
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["auto_verifiable"], 1)
        self.assertEqual(summary["by_type"]["file_exists"], 1)
        self.assertEqual(summary["by_type"]["count_claim"], 1)


class TestClaimToDict(unittest.TestCase):
    """Test Claim serialization."""

    def test_to_dict(self):
        claim = Claim(
            text="test claim",
            claim_type=ClaimType.ENV_VAR,
            verifiability=VerifiabilityLevel.AUTO,
            source_file="test.md",
            source_line=5,
            extracted_env_vars=["API_KEY"],
        )
        d = claim.to_dict()
        self.assertEqual(d["text"], "test claim")
        self.assertEqual(d["type"], "env_var")
        self.assertEqual(d["verifiability"], "auto")
        self.assertEqual(d["source_file"], "test.md")
        self.assertEqual(d["env_vars"], ["API_KEY"])


if __name__ == "__main__":
    unittest.main()
