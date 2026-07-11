"""P2-10B: Verification tests for the article_fetcher.py module split.

These tests confirm that:
1. The new ``article_fetcher`` module exists and exports the expected names.
2. ``tools.py`` re-exports the same names for backward compatibility.
3. No duplicate definitions exist.
4. ``article_fetcher.py`` only depends on the standard library and ``.utils``
   (no qwen_agent or other third-party imports).
5. The re-exported objects are identical (same function/class objects).
"""

from __future__ import annotations

import ast
import pathlib
import unittest

from daily_report.src.stock_daily_agent import article_fetcher, tools


_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
_ARTICLE_FETCHER_PATH = _PROJECT_ROOT / "daily_report" / "src" / "stock_daily_agent" / "article_fetcher.py"
_TOOLS_PATH = _PROJECT_ROOT / "daily_report" / "src" / "stock_daily_agent" / "tools.py"


class TestArticleFetcherModuleExists(unittest.TestCase):
    """Confirm article_fetcher.py exists and is importable."""

    def test_module_file_exists(self) -> None:
        self.assertTrue(_ARTICLE_FETCHER_PATH.is_file(),
                        f"Expected article_fetcher.py at {_ARTICLE_FETCHER_PATH}")

    def test_module_is_importable(self) -> None:
        # If we got here, the import at the top of the file succeeded.
        self.assertIsNotNone(article_fetcher)


class TestArticleFetcherExports(unittest.TestCase):
    """Verify that article_fetcher exports all expected public names."""

    def test_exports_security_error(self) -> None:
        self.assertTrue(hasattr(article_fetcher, "ArticleFetchSecurityError"))

    def test_exports_fetch_function(self) -> None:
        self.assertTrue(hasattr(article_fetcher, "_fetch_article_text"))

    def test_exports_validate_url(self) -> None:
        self.assertTrue(hasattr(article_fetcher, "_validate_article_url"))

    def test_exports_is_public_ip(self) -> None:
        self.assertTrue(hasattr(article_fetcher, "_is_public_ip"))

    def test_exports_enrich_function(self) -> None:
        self.assertTrue(hasattr(article_fetcher, "_enrich_evidence_with_articles"))

    def test_exports_open_pinned_request(self) -> None:
        self.assertTrue(hasattr(article_fetcher, "_open_pinned_article_request"))

    def test_exports_article_text_parser(self) -> None:
        self.assertTrue(hasattr(article_fetcher, "_ArticleTextParser"))

    def test_exports_pinned_https_connection(self) -> None:
        self.assertTrue(hasattr(article_fetcher, "_PinnedHTTPSConnection"))

    def test_exports_tool_error(self) -> None:
        self.assertTrue(hasattr(article_fetcher, "ToolError"))

    def test_security_error_inherits_tool_error(self) -> None:
        self.assertTrue(issubclass(article_fetcher.ArticleFetchSecurityError,
                                   article_fetcher.ToolError))


class TestToolsReExport(unittest.TestCase):
    """Verify that tools.py re-exports the article fetcher names."""

    def test_tools_has_fetch_function(self) -> None:
        self.assertTrue(hasattr(tools, "_fetch_article_text"))

    def test_tools_has_security_error(self) -> None:
        self.assertTrue(hasattr(tools, "ArticleFetchSecurityError"))

    def test_tools_has_validate_url(self) -> None:
        self.assertTrue(hasattr(tools, "_validate_article_url"))

    def test_tools_has_is_public_ip(self) -> None:
        self.assertTrue(hasattr(tools, "_is_public_ip"))

    def test_tools_has_enrich_function(self) -> None:
        self.assertTrue(hasattr(tools, "_enrich_evidence_with_articles"))

    def test_tools_has_open_pinned_request(self) -> None:
        self.assertTrue(hasattr(tools, "_open_pinned_article_request"))

    def test_re_exported_functions_are_identical(self) -> None:
        """The re-exported objects must be the same objects (not copies)."""
        self.assertIs(tools._fetch_article_text, article_fetcher._fetch_article_text)
        self.assertIs(tools.ArticleFetchSecurityError, article_fetcher.ArticleFetchSecurityError)
        self.assertIs(tools._validate_article_url, article_fetcher._validate_article_url)
        self.assertIs(tools._is_public_ip, article_fetcher._is_public_ip)
        self.assertIs(tools._enrich_evidence_with_articles, article_fetcher._enrich_evidence_with_articles)
        self.assertIs(tools._open_pinned_article_request, article_fetcher._open_pinned_article_request)


class TestNoDuplicateDefinitions(unittest.TestCase):
    """Verify that article-fetching functions are NOT defined in tools.py."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tools_source = _TOOLS_PATH.read_text(encoding="utf-8")

    def test_no_article_text_parser_class_in_tools(self) -> None:
        self.assertNotIn("class _ArticleTextParser(HTMLParser):", self._tools_source)

    def test_no_fetch_article_text_def_in_tools(self) -> None:
        # The function should only appear as an import, not a def.
        lines = self._tools_source.splitlines()
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("def _fetch_article_text("):
                self.fail("_fetch_article_text should not be defined in tools.py")

    def test_no_validate_article_url_def_in_tools(self) -> None:
        lines = self._tools_source.splitlines()
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("def _validate_article_url("):
                self.fail("_validate_article_url should not be defined in tools.py")

    def test_no_is_public_ip_def_in_tools(self) -> None:
        lines = self._tools_source.splitlines()
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("def _is_public_ip("):
                self.fail("_is_public_ip should not be defined in tools.py")

    def test_no_enrich_evidence_def_in_tools(self) -> None:
        lines = self._tools_source.splitlines()
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("def _enrich_evidence_with_articles("):
                self.fail("_enrich_evidence_with_articles should not be defined in tools.py")

    def test_no_pinned_https_connection_class_in_tools(self) -> None:
        self.assertNotIn("class _PinnedHTTPSConnection(", self._tools_source)

    def test_import_from_article_fetcher_exists(self) -> None:
        self.assertIn("from .article_fetcher import (", self._tools_source)


class TestArticleFetcherOnlyStdlib(unittest.TestCase):
    """Verify that article_fetcher.py does not import third-party packages."""

    @classmethod
    def setUpClass(cls) -> None:
        source = _ARTICLE_FETCHER_PATH.read_text(encoding="utf-8")
        cls._tree = ast.parse(source)

    def test_no_qwen_agent_import(self) -> None:
        """qwen_agent (BaseTool) must NOT be imported in article_fetcher."""
        for node in ast.walk(self._tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(
                        alias.name.startswith("qwen_agent"),
                        f"article_fetcher.py must not import qwen_agent, found: {alias.name}",
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    self.assertFalse(
                        node.module.startswith("qwen_agent"),
                        f"article_fetcher.py must not import from qwen_agent, found: {node.module}",
                    )

    def test_only_utils_internal_import(self) -> None:
        """The only relative import should be from .utils (for ToolError)."""
        relative_imports: list[str] = []
        for node in ast.walk(self._tree):
            if isinstance(node, ast.ImportFrom) and node.level > 0:
                relative_imports.append(node.module or "")
        # Must import ToolError from .utils, and may lazily import from .tools.
        # At top level, only .utils is allowed.
        self.assertIn("utils", relative_imports)
        # .tools should NOT be a top-level import (it's a lazy import inside functions).
        # Check the AST: ImportFrom nodes at module level only.
        for node in ast.iter_child_nodes(self._tree):
            if isinstance(node, ast.ImportFrom) and node.level > 0:
                self.assertNotEqual(
                    node.module, "tools",
                    "article_fetcher.py must not import from .tools at module level (use lazy import)",
                )

    def test_no_third_party_imports(self) -> None:
        """All top-level imports must be from the standard library or .utils."""
        stdlib_prefixes = {
            "http", "ipaddress", "os", "re", "socket", "ssl",
            "html", "urllib", "typing", "__future__", "email",
            "pathlib", "json", "time", "collections", "functools",
            "datetime", "collections.abc",
        }
        for node in ast.iter_child_nodes(self._tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    self.assertTrue(
                        top in stdlib_prefixes,
                        f"article_fetcher.py imports non-stdlib '{alias.name}' at module level",
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    top = node.module.split(".")[0]
                    self.assertTrue(
                        top in stdlib_prefixes,
                        f"article_fetcher.py imports non-stdlib '{node.module}' at module level",
                    )


class TestHelperFunctionsStayInTools(unittest.TestCase):
    """Verify that shared helper functions are NOT moved to article_fetcher."""

    def test_source_domain_still_in_tools(self) -> None:
        self.assertTrue(hasattr(tools, "_source_domain"),
                        "_source_domain must remain in tools.py")

    def test_article_text_quality_ok_still_in_tools(self) -> None:
        self.assertTrue(hasattr(tools, "_article_text_quality_ok"),
                        "_article_text_quality_ok must remain in tools.py")

    def test_is_blocked_or_consent_text_still_in_tools(self) -> None:
        self.assertTrue(hasattr(tools, "_is_blocked_or_consent_text"),
                        "_is_blocked_or_consent_text must remain in tools.py")

    def test_source_quality_score_still_in_tools(self) -> None:
        self.assertTrue(hasattr(tools, "_source_quality_score"),
                        "_source_quality_score must remain in tools.py")

    def test_helpers_not_in_article_fetcher(self) -> None:
        """These helpers should NOT be defined in article_fetcher (they stay in tools)."""
        self.assertFalse(
            hasattr(article_fetcher, "_source_domain"),
            "_source_domain should not be in article_fetcher",
        )
        self.assertFalse(
            hasattr(article_fetcher, "_article_text_quality_ok"),
            "_article_text_quality_ok should not be in article_fetcher",
        )
        self.assertFalse(
            hasattr(article_fetcher, "_source_quality_score"),
            "_source_quality_score should not be in article_fetcher",
        )


if __name__ == "__main__":
    unittest.main()
