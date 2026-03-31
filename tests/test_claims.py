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
    ENV_VAR_STATUS_RE,
    STATUS_ERROR_RE,
    FACT_CLAIM_RE,
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


class TestEnvVarStatusRegex(unittest.TestCase):
    """Test ENV_VAR_STATUS_RE pattern matching for natural-language env var claims."""

    def test_is_not_set(self):
        match = ENV_VAR_STATUS_RE.search("OPENAI_API_KEY is not set in the environment")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "OPENAI_API_KEY")

    def test_not_configured(self):
        match = ENV_VAR_STATUS_RE.search("ANTHROPIC_API_KEY not configured")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "ANTHROPIC_API_KEY")

    def test_missing(self):
        match = ENV_VAR_STATUS_RE.search("DATABASE_URL missing from .env")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "DATABASE_URL")

    def test_absent(self):
        match = ENV_VAR_STATUS_RE.search("SLACK_BOT_TOKEN is absent")
        self.assertIsNotNone(match)

    def test_unset(self):
        match = ENV_VAR_STATUS_RE.search("GITHUB_TOKEN unset")
        self.assertIsNotNone(match)

    def test_not_available(self):
        match = ENV_VAR_STATUS_RE.search("GOOGLE_API_KEY is not available")
        self.assertIsNotNone(match)

    def test_no_match_on_normal_text(self):
        match = ENV_VAR_STATUS_RE.search("the pipeline is working fine")
        self.assertIsNone(match)


class TestEnvVarStatusExtraction(unittest.TestCase):
    """Test env var claim extraction from natural language."""

    def test_key_is_not_set(self):
        claims = extract_claims("OPENAI_API_KEY is not set in the environment")
        env_claims = [c for c in claims if c.claim_type == ClaimType.ENV_VAR]
        self.assertEqual(len(env_claims), 1)
        self.assertIn("OPENAI_API_KEY", env_claims[0].extracted_env_vars)

    def test_key_missing(self):
        claims = extract_claims("ANTHROPIC_API_KEY missing from .env file")
        env_claims = [c for c in claims if c.claim_type == ClaimType.ENV_VAR]
        self.assertEqual(len(env_claims), 1)

    def test_key_not_configured(self):
        claims = extract_claims("SLACK_BOT_TOKEN is not configured")
        env_claims = [c for c in claims if c.claim_type == ClaimType.ENV_VAR]
        self.assertEqual(len(env_claims), 1)

    def test_unknown_var_with_suffix(self):
        """Env vars ending in common suffixes should be caught even if not in known list."""
        claims = extract_claims("CUSTOM_API_KEY is not set")
        env_claims = [c for c in claims if c.claim_type == ClaimType.ENV_VAR]
        self.assertEqual(len(env_claims), 1)

    def test_unknown_var_without_suffix_ignored(self):
        """Random uppercase words should not be caught as env vars."""
        claims = extract_claims("CONFAB is not set up correctly")
        env_claims = [c for c in claims if c.claim_type == ClaimType.ENV_VAR]
        self.assertEqual(len(env_claims), 0)


class TestStatusErrorRegex(unittest.TestCase):
    """Test STATUS_ERROR_RE pattern matching for error codes and expiry."""

    def test_returned_403(self):
        self.assertIsNotNone(STATUS_ERROR_RE.search("returned a 403 error"))

    def test_got_500(self):
        self.assertIsNotNone(STATUS_ERROR_RE.search("got 500 error"))

    def test_received_401_status(self):
        self.assertIsNotNone(STATUS_ERROR_RE.search("received a 401 status"))

    def test_has_expired(self):
        self.assertIsNotNone(STATUS_ERROR_RE.search("cookie has expired"))

    def test_expired(self):
        self.assertIsNotNone(STATUS_ERROR_RE.search("The token expired"))

    def test_timed_out(self):
        self.assertIsNotNone(STATUS_ERROR_RE.search("request timed out"))

    def test_returned_error(self):
        self.assertIsNotNone(STATUS_ERROR_RE.search("returned an error"))

    def test_no_match_normal_text(self):
        self.assertIsNone(STATUS_ERROR_RE.search("the system works perfectly"))


class TestStatusErrorExtraction(unittest.TestCase):
    """Test status/error claim extraction."""

    def test_cookie_expired(self):
        claims = extract_claims("The Substack cookie has expired")
        status_claims = [c for c in claims if c.claim_type == ClaimType.STATUS_CLAIM]
        self.assertEqual(len(status_claims), 1)

    def test_403_error(self):
        claims = extract_claims("The responder returned a 403 error yesterday")
        status_claims = [c for c in claims if c.claim_type == ClaimType.STATUS_CLAIM]
        self.assertEqual(len(status_claims), 1)

    def test_timeout(self):
        claims = extract_claims("The API request timed out during deployment")
        status_claims = [c for c in claims if c.claim_type == ClaimType.STATUS_CLAIM]
        self.assertEqual(len(status_claims), 1)


class TestFactClaimRegex(unittest.TestCase):
    """Test FACT_CLAIM_RE pattern matching for numeric factual claims."""

    def test_cpi_percentage(self):
        self.assertIsNotNone(FACT_CLAIM_RE.search("CPI is at 3.5%"))

    def test_rate_percentage(self):
        self.assertIsNotNone(FACT_CLAIM_RE.search("rate is 4.2%"))

    def test_dropped_to(self):
        self.assertIsNotNone(FACT_CLAIM_RE.search("GDP dropped to 2.1%"))

    def test_rose_to(self):
        self.assertIsNotNone(FACT_CLAIM_RE.search("inflation rose to 4.8%"))

    def test_stands_at(self):
        self.assertIsNotNone(FACT_CLAIM_RE.search("unemployment stands at 4.1%"))

    def test_no_match_without_percent(self):
        """Plain numbers without % should not match (those are counts, not facts)."""
        self.assertIsNone(FACT_CLAIM_RE.search("CPI is at 3"))


class TestFactClaimExtraction(unittest.TestCase):
    """Test fact claim extraction."""

    def test_cpi_claim(self):
        claims = extract_claims("CPI is at 3.5% currently")
        fact_claims = [c for c in claims if c.claim_type == ClaimType.FACT_CLAIM]
        self.assertEqual(len(fact_claims), 1)

    def test_gdp_claim(self):
        claims = extract_claims("GDP dropped to 2.1% in Q4")
        fact_claims = [c for c in claims if c.claim_type == ClaimType.FACT_CLAIM]
        self.assertEqual(len(fact_claims), 1)

    def test_fact_claim_verifiability(self):
        """Fact claims should be manual verifiability (need external data)."""
        claims = extract_claims("CPI is at 3.5% currently")
        fact_claims = [c for c in claims if c.claim_type == ClaimType.FACT_CLAIM]
        self.assertEqual(fact_claims[0].verifiability, VerifiabilityLevel.MANUAL)


class TestCountClaimExpanded(unittest.TestCase):
    """Test count claim extraction with expanded assertion context."""

    def test_tests_passing(self):
        """'passing' should now be recognized as assertion context."""
        claims = extract_claims("Confab framework has 512 tests passing")
        count_claims = [c for c in claims if c.claim_type == ClaimType.COUNT_CLAIM]
        self.assertEqual(len(count_claims), 1)
        self.assertIn("512", count_claims[0].extracted_numbers)

    def test_tests_failed(self):
        claims = extract_claims("3 tests failed in the last run")
        count_claims = [c for c in claims if c.claim_type == ClaimType.COUNT_CLAIM]
        self.assertEqual(len(count_claims), 1)

    def test_tests_passed(self):
        claims = extract_claims("All 42 tests passed")
        count_claims = [c for c in claims if c.claim_type == ClaimType.COUNT_CLAIM]
        self.assertEqual(len(count_claims), 1)


class TestDreamerGapIntegration(unittest.TestCase):
    """Integration test: the 8-claim sample from the dreamer's analysis."""

    def test_catches_at_least_6_of_7(self):
        """The extractor should catch at least 6 of the 7 test lines."""
        text = (
            "The audio pipeline is working.\n"
            "File at scripts/weather_config.json should be updated.\n"
            "CPI is at 3.5% currently.\n"
            "Confab framework has 512 tests passing.\n"
            "The Substack cookie has expired.\n"
            "OPENAI_API_KEY is not set in the environment.\n"
            "The responder returned a 403 error yesterday."
        )
        claims = extract_claims(text)
        self.assertGreaterEqual(len(claims), 6)

    def test_catches_all_7(self):
        """Verify all 7 lines produce claims."""
        text = (
            "The audio pipeline is working.\n"
            "File at scripts/weather_config.json should be updated.\n"
            "CPI is at 3.5% currently.\n"
            "Confab framework has 512 tests passing.\n"
            "The Substack cookie has expired.\n"
            "OPENAI_API_KEY is not set in the environment.\n"
            "The responder returned a 403 error yesterday."
        )
        claims = extract_claims(text)
        types = {c.claim_type for c in claims}
        self.assertIn(ClaimType.PIPELINE_WORKS, types)
        self.assertIn(ClaimType.FILE_EXISTS, types)
        self.assertIn(ClaimType.FACT_CLAIM, types)
        self.assertIn(ClaimType.COUNT_CLAIM, types)
        self.assertIn(ClaimType.STATUS_CLAIM, types)
        self.assertIn(ClaimType.ENV_VAR, types)
        self.assertEqual(len(claims), 7)


class TestConfidenceScoring(unittest.TestCase):
    """Tests for claim confidence scoring."""

    def test_file_exists_high_confidence(self):
        """File existence claims with paths should score high."""
        from confab.claims import score_confidence
        claim = Claim(
            text="Config at `core/config.py` is ready",
            claim_type=ClaimType.FILE_EXISTS,
            verifiability=VerifiabilityLevel.AUTO,
            extracted_paths=["core/config.py"],
        )
        score = score_confidence(claim)
        self.assertGreaterEqual(score, 0.8)

    def test_env_var_high_confidence(self):
        """Env var claims with extracted vars should score high."""
        from confab.claims import score_confidence
        claim = Claim(
            text="Blocked on OPENAI_API_KEY",
            claim_type=ClaimType.ENV_VAR,
            verifiability=VerifiabilityLevel.AUTO,
            extracted_env_vars=["OPENAI_API_KEY"],
        )
        score = score_confidence(claim)
        self.assertGreaterEqual(score, 0.8)

    def test_subjective_low_confidence(self):
        """Subjective claims should score low."""
        from confab.claims import score_confidence
        claim = Claim(
            text="The system feels slow today",
            claim_type=ClaimType.SUBJECTIVE,
            verifiability=VerifiabilityLevel.MANUAL,
        )
        score = score_confidence(claim)
        self.assertLessEqual(score, 0.6)

    def test_fact_claim_medium_confidence(self):
        """Fact claims without artifacts score medium."""
        from confab.claims import score_confidence
        claim = Claim(
            text="CPI at 3.5%",
            claim_type=ClaimType.FACT_CLAIM,
            verifiability=VerifiabilityLevel.MANUAL,
        )
        score = score_confidence(claim)
        self.assertGreater(score, 0.4)
        self.assertLess(score, 0.7)

    def test_age_penalty_reduces_confidence(self):
        """Older unverified claims should lose confidence."""
        from confab.claims import score_confidence
        young = Claim(
            text="Pipeline is working",
            claim_type=ClaimType.PIPELINE_WORKS,
            verifiability=VerifiabilityLevel.SEMI,
            age_builds=0,
        )
        old = Claim(
            text="Pipeline is working",
            claim_type=ClaimType.PIPELINE_WORKS,
            verifiability=VerifiabilityLevel.SEMI,
            age_builds=5,
        )
        self.assertGreater(score_confidence(young), score_confidence(old))

    def test_verification_tag_boosts_confidence(self):
        """Claims with verification tags should score higher."""
        from confab.claims import score_confidence
        untagged = Claim(
            text="Script runs fine",
            claim_type=ClaimType.SCRIPT_RUNS,
            verifiability=VerifiabilityLevel.AUTO,
        )
        tagged = Claim(
            text="Script runs fine",
            claim_type=ClaimType.SCRIPT_RUNS,
            verifiability=VerifiabilityLevel.AUTO,
            verification_tag="[v1: tested 2026-03-29]",
        )
        self.assertGreater(score_confidence(tagged), score_confidence(untagged))

    def test_v2_tag_higher_than_v1(self):
        """v2 verification should score higher than v1."""
        from confab.claims import score_confidence
        v1 = Claim(
            text="Monitor running",
            claim_type=ClaimType.PROCESS_STATUS,
            verifiability=VerifiabilityLevel.AUTO,
            verification_tag="[v1: checked 2026-03-29]",
        )
        v2 = Claim(
            text="Monitor running",
            claim_type=ClaimType.PROCESS_STATUS,
            verifiability=VerifiabilityLevel.AUTO,
            verification_tag="[v2: checked 2026-03-29]",
        )
        self.assertGreater(score_confidence(v2), score_confidence(v1))

    def test_confidence_in_to_dict(self):
        """Confidence should appear in to_dict output."""
        claim = Claim(
            text="test",
            claim_type=ClaimType.FILE_EXISTS,
            verifiability=VerifiabilityLevel.AUTO,
            extracted_paths=["test.py"],
            confidence=0.85,
        )
        d = claim.to_dict()
        self.assertIn("confidence", d)
        self.assertEqual(d["confidence"], 0.85)

    def test_confidence_bounds(self):
        """Confidence should always be in [0.0, 1.0]."""
        from confab.claims import score_confidence
        # Max everything
        maxed = Claim(
            text="test",
            claim_type=ClaimType.FILE_EXISTS,
            verifiability=VerifiabilityLevel.AUTO,
            extracted_paths=["a.py"],
            extracted_env_vars=["KEY"],
            extracted_config_keys=["k"],
            extracted_numbers=["5"],
            verification_tag="[v2: checked 2026-03-29]",
        )
        self.assertLessEqual(score_confidence(maxed), 1.0)
        # Min everything
        minimal = Claim(
            text="maybe something",
            claim_type=ClaimType.SUBJECTIVE,
            verifiability=VerifiabilityLevel.MANUAL,
            age_builds=10,
        )
        self.assertGreaterEqual(score_confidence(minimal), 0.0)

    def test_extraction_assigns_confidence(self):
        """extract_claims should populate confidence on all claims."""
        text = "Pipeline `scripts/test.py` is working\nBlocked on OPENAI_API_KEY"
        claims = extract_claims(text, source_file="test.md")
        for c in claims:
            self.assertGreater(c.confidence, 0.0)
            self.assertLessEqual(c.confidence, 1.0)

    def test_summary_includes_confidence_stats(self):
        """summarize_claims should include confidence statistics."""
        from confab.claims import summarize_claims
        claims = [
            Claim(text="a", claim_type=ClaimType.FILE_EXISTS,
                  verifiability=VerifiabilityLevel.AUTO,
                  extracted_paths=["x.py"], confidence=0.9),
            Claim(text="b", claim_type=ClaimType.SUBJECTIVE,
                  verifiability=VerifiabilityLevel.MANUAL,
                  confidence=0.4),
        ]
        s = summarize_claims(claims)
        self.assertIn("avg_confidence", s)
        self.assertIn("high_confidence", s)
        self.assertIn("low_confidence", s)
        self.assertEqual(s["avg_confidence"], 0.65)
        self.assertEqual(s["high_confidence"], 1)
        self.assertEqual(s["low_confidence"], 1)


class TestDateExpiryClaims(unittest.TestCase):
    """Test date-expiry claim detection."""

    def test_expires_day_name(self):
        """'expires Mon' should be detected as DATE_EXPIRY."""
        text = "Gas contracts at 99¢ verified — no action needed, expire Mon."
        claims = extract_claims(text)
        expiry_claims = [c for c in claims if c.claim_type == ClaimType.DATE_EXPIRY]
        self.assertEqual(len(expiry_claims), 1)

    def test_expiry_uppercase(self):
        """'EXPIRY MON.' should be detected as DATE_EXPIRY."""
        text = "| Gas >$3.70 | 35 | 99¢ | +$10.15 | EXPIRY MON. AAA=$3.98, near-certain YES. |"
        claims = extract_claims(text)
        expiry_claims = [c for c in claims if c.claim_type == ClaimType.DATE_EXPIRY]
        self.assertEqual(len(expiry_claims), 1)

    def test_resolve_with_month_day(self):
        """'resolve Apr 5' should be detected as DATE_EXPIRY."""
        text = "| Tesla 330K NO | 96 | 11¢ | -$29.76 | Let resolve Apr 5. |"
        claims = extract_claims(text)
        expiry_claims = [c for c in claims if c.claim_type == ClaimType.DATE_EXPIRY]
        self.assertEqual(len(expiry_claims), 1)

    def test_key_date_line(self):
        """'Mon Mar 31: Gas contracts expire' should be DATE_EXPIRY."""
        text = "- **Mon Mar 31:** Gas contracts expire"
        claims = extract_claims(text)
        expiry_claims = [c for c in claims if c.claim_type == ClaimType.DATE_EXPIRY]
        self.assertEqual(len(expiry_claims), 1)

    def test_deadline_with_date(self):
        """'deadline Wed Apr 2' should be DATE_EXPIRY."""
        text = "NIST NCCoE comments deadline Wed Apr 2."
        claims = extract_claims(text)
        expiry_claims = [c for c in claims if c.claim_type == ClaimType.DATE_EXPIRY]
        self.assertEqual(len(expiry_claims), 1)

    def test_no_date_no_match(self):
        """'expires eventually' without a date reference should NOT match."""
        text = "This cookie expires eventually."
        claims = extract_claims(text)
        expiry_claims = [c for c in claims if c.claim_type == ClaimType.DATE_EXPIRY]
        self.assertEqual(len(expiry_claims), 0)

    def test_due_with_iso_date(self):
        """'due 2026-04-02' should be DATE_EXPIRY."""
        text = "Report due 2026-04-02 for compliance review."
        claims = extract_claims(text)
        expiry_claims = [c for c in claims if c.claim_type == ClaimType.DATE_EXPIRY]
        self.assertEqual(len(expiry_claims), 1)

    def test_exemptions_expire_date(self):
        """'USMCA exemptions expire' with date should be DATE_EXPIRY."""
        text = "- **Wed Apr 2:** USMCA exemptions expire. Full 15% tariff on Canada/Mexico."
        claims = extract_claims(text)
        expiry_claims = [c for c in claims if c.claim_type == ClaimType.DATE_EXPIRY]
        self.assertEqual(len(expiry_claims), 1)


class TestFlagStaleVtags(unittest.TestCase):
    """Test verification tag staleness detection."""

    def test_stale_vtag_detected(self):
        """Claims with vtags older than threshold should be flagged."""
        from datetime import datetime, timezone, timedelta
        from confab.claims import flag_stale_vtags

        old_time = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)
        now = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)  # 48h later

        claim = Claim(
            text="Audio pipeline: WORKING",
            claim_type=ClaimType.PIPELINE_WORKS,
            verifiability=VerifiabilityLevel.AUTO,
            verification_tag="[v2: verified 2026-03-28]",
        )
        stale = flag_stale_vtags([claim], max_age_hours=24.0, now=now)
        self.assertEqual(len(stale), 1)
        self.assertGreater(stale[0][1], 24.0)

    def test_fresh_vtag_not_flagged(self):
        """Claims with recent vtags should NOT be flagged."""
        from datetime import datetime, timezone
        from confab.claims import flag_stale_vtags

        now = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)

        claim = Claim(
            text="Substack cookie: WORKING",
            claim_type=ClaimType.STATUS_CLAIM,
            verifiability=VerifiabilityLevel.SEMI,
            verification_tag="[v2: verified 2026-03-30]",
        )
        stale = flag_stale_vtags([claim], max_age_hours=24.0, now=now)
        self.assertEqual(len(stale), 0)

    def test_behavior_claims_shorter_ttl(self):
        """Behavior claims should use half the TTL threshold."""
        from datetime import datetime, timezone
        from confab.claims import flag_stale_vtags

        now = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)

        # 18h old — fresh for 24h threshold, but stale for behavior (12h)
        claim = Claim(
            text="Weather monitor: RUNNING",
            claim_type=ClaimType.PROCESS_STATUS,
            verifiability=VerifiabilityLevel.AUTO,
            verification_tag="[v1: verified 2026-03-29 18:00]",
        )
        stale = flag_stale_vtags([claim], max_age_hours=24.0, now=now)
        self.assertEqual(len(stale), 1)

    def test_no_vtag_not_flagged(self):
        """Claims without vtags should NOT be flagged."""
        from confab.claims import flag_stale_vtags

        claim = Claim(
            text="Something happened",
            claim_type=ClaimType.STATUS_CLAIM,
            verifiability=VerifiabilityLevel.SEMI,
        )
        stale = flag_stale_vtags([claim], max_age_hours=24.0)
        self.assertEqual(len(stale), 0)

    def test_unverified_tag_not_flagged(self):
        """[unverified] tags should NOT be flagged for staleness."""
        from confab.claims import flag_stale_vtags

        claim = Claim(
            text="Something claimed",
            claim_type=ClaimType.STATUS_CLAIM,
            verifiability=VerifiabilityLevel.SEMI,
            verification_tag="[unverified]",
        )
        stale = flag_stale_vtags([claim], max_age_hours=24.0)
        self.assertEqual(len(stale), 0)


class TestGenericAgentOutput(unittest.TestCase):
    """Tests for generic (non-ia-specific) agent output claim extraction.

    These cover the sentence splitting, generic fractional counts,
    positive env var status, and inline file path patterns added
    to support external agent output.
    """

    def test_acceptance_case_4_claims(self):
        """The dreamer's acceptance test: 4 claims from one line."""
        text = (
            "The config at /tmp/test.yaml is deployed. "
            "Tests pass (42/42). "
            "The server at port 8080 is running. "
            "ENV var DATABASE_URL is set."
        )
        claims = extract_claims(text)
        self.assertEqual(len(claims), 4)
        types = {c.claim_type for c in claims}
        self.assertIn(ClaimType.PROCESS_STATUS, types)
        self.assertIn(ClaimType.COUNT_CLAIM, types)
        self.assertIn(ClaimType.ENV_VAR, types)
        # File path claim (config_present or file_exists)
        self.assertTrue(
            any(c.claim_type in (ClaimType.CONFIG_PRESENT, ClaimType.FILE_EXISTS) for c in claims)
        )

    # --- Sentence splitting ---

    def test_sentence_splitting_basic(self):
        """Multi-sentence lines are split into independent claims."""
        text = "File exists. Service is running."
        claims = extract_claims(text)
        # Should produce at least 1 claim per sentence that matches a pattern
        self.assertGreaterEqual(len(claims), 1)

    def test_sentence_splitting_preserves_single_line(self):
        """Single-sentence lines are not affected by splitting."""
        text = "The server at port 8080 is running."
        claims = extract_claims(text)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].claim_type, ClaimType.PROCESS_STATUS)

    def test_sentence_splitting_headings_not_split(self):
        """Headings should not be split even if they contain periods."""
        text = "## Status v1.2. Updated.\nSome content."
        claims = extract_claims(text)
        # Heading should be treated as one unit, not split
        # No assertions on count — just no crash
        self.assertIsInstance(claims, list)

    # --- Generic fractional counts ---

    def test_generic_frac_parenthesized(self):
        """Parenthesized fractions like (42/42) are extracted."""
        text = "Tests pass (42/42)."
        claims = extract_claims(text)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].claim_type, ClaimType.COUNT_CLAIM)
        self.assertIn("42", claims[0].extracted_numbers)

    def test_generic_frac_bare(self):
        """Bare fractions like 10/10 in assertion context are extracted."""
        text = "Checks completed 10/10."
        claims = extract_claims(text)
        count_claims = [c for c in claims if c.claim_type == ClaimType.COUNT_CLAIM]
        self.assertGreaterEqual(len(count_claims), 1)

    def test_generic_frac_no_false_positive_on_dates(self):
        """Date-like patterns 03/31 should not match as fractional counts
        when in directive context."""
        text = "The limit is 5 per day as of 03/31."
        claims = extract_claims(text)
        # Should not produce a count claim (directive context)
        count_claims = [c for c in claims if c.claim_type == ClaimType.COUNT_CLAIM]
        self.assertEqual(len(count_claims), 0)

    def test_generic_frac_test_results(self):
        """Various test result formats produce count claims."""
        for text in [
            "All tests pass (100/100).",
            "Build status: 5/5 passing.",
            "Lint: 0/42 errors found.",
        ]:
            claims = extract_claims(text)
            count_claims = [c for c in claims if c.claim_type == ClaimType.COUNT_CLAIM]
            self.assertGreaterEqual(
                len(count_claims), 1,
                f"Expected count claim for: {text}"
            )

    # --- Positive env var status ---

    def test_env_var_positive_set(self):
        """'ENV var DATABASE_URL is set' produces an ENV_VAR claim."""
        text = "ENV var DATABASE_URL is set."
        claims = extract_claims(text)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].claim_type, ClaimType.ENV_VAR)
        self.assertIn("DATABASE_URL", claims[0].extracted_env_vars)

    def test_env_var_positive_configured(self):
        """'ANTHROPIC_API_KEY is configured' produces an ENV_VAR claim."""
        text = "ANTHROPIC_API_KEY is configured."
        claims = extract_claims(text)
        env_claims = [c for c in claims if c.claim_type == ClaimType.ENV_VAR]
        self.assertEqual(len(env_claims), 1)
        self.assertIn("ANTHROPIC_API_KEY", env_claims[0].extracted_env_vars)

    def test_env_var_positive_with_prefix(self):
        """'ENV variable GITHUB_TOKEN is present' produces a claim."""
        text = "ENV variable GITHUB_TOKEN is present."
        claims = extract_claims(text)
        env_claims = [c for c in claims if c.claim_type == ClaimType.ENV_VAR]
        self.assertEqual(len(env_claims), 1)

    def test_env_var_positive_unknown_var_with_suffix(self):
        """Unknown env vars with standard suffixes are still extracted."""
        text = "MY_CUSTOM_API_KEY is set."
        claims = extract_claims(text)
        env_claims = [c for c in claims if c.claim_type == ClaimType.ENV_VAR]
        self.assertEqual(len(env_claims), 1)
        self.assertIn("MY_CUSTOM_API_KEY", env_claims[0].extracted_env_vars)

    def test_env_var_positive_ignores_random_words(self):
        """Regular uppercase words like HELLO should not match."""
        text = "HELLO is set to greet users."
        claims = extract_claims(text)
        env_claims = [c for c in claims if c.claim_type == ClaimType.ENV_VAR]
        self.assertEqual(len(env_claims), 0)

    # --- Multi-claim single line ---

    def test_multi_claim_line_all_types(self):
        """A single line with 4 different claim types produces 4 claims."""
        text = (
            "Config at /tmp/app.yaml is ready. "
            "Tests: 50/50. "
            "Worker service is running. "
            "ENV var SECRET_KEY is set."
        )
        claims = extract_claims(text)
        self.assertEqual(len(claims), 4)

    def test_multi_claim_preserves_line_number(self):
        """All claims from a split line share the original line number."""
        text = "File deployed. Server is running."
        claims = extract_claims(text)
        if len(claims) >= 2:
            # Both should reference line 1
            for c in claims:
                self.assertEqual(c.source_line, 1)


if __name__ == "__main__":
    unittest.main()
