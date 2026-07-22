"""
Unit tests for the pure, deterministic functions in github_context.py.

These deliberately avoid any network calls (parse_github_url, path filtering,
dependency-list regex parsing, and the path-extraction fallback logic are all
pure functions of their inputs) so the suite runs instantly and needs no
GitHub API access or rate-limit budget.

Run with: pytest test_github_context.py -v
"""

import github_context as gc


# ---------------------------------------------------------------------------
# parse_github_url
# ---------------------------------------------------------------------------

class TestParseGithubUrl:
    def test_basic_url(self):
        assert gc.parse_github_url("https://github.com/octocat/Hello-World") == ("octocat", "Hello-World")

    def test_trailing_git_suffix(self):
        assert gc.parse_github_url("https://github.com/octocat/Hello-World.git") == ("octocat", "Hello-World")

    def test_trailing_slash_and_extra_path(self):
        assert gc.parse_github_url("https://github.com/octocat/Hello-World/tree/main") == ("octocat", "Hello-World")

    def test_url_with_whitespace(self):
        assert gc.parse_github_url("  https://github.com/octocat/Hello-World  ") == ("octocat", "Hello-World")

    def test_not_a_github_url(self):
        assert gc.parse_github_url("https://gitlab.com/octocat/Hello-World") is None

    def test_missing_repo_segment(self):
        assert gc.parse_github_url("https://github.com/octocat") is None

    def test_empty_string(self):
        assert gc.parse_github_url("") is None

    def test_query_string_and_fragment_stripped(self):
        assert gc.parse_github_url("https://github.com/octocat/Hello-World?tab=readme#section") == (
            "octocat", "Hello-World",
        )


# ---------------------------------------------------------------------------
# _is_excluded
# ---------------------------------------------------------------------------

class TestIsExcluded:
    def test_excluded_dir_present(self):
        assert gc._is_excluded("src/node_modules/foo.js") is True

    def test_excluded_dir_at_root(self):
        assert gc._is_excluded("build/index.html") is True

    def test_no_excluded_dir(self):
        assert gc._is_excluded("src/components/Button.jsx") is False

    def test_substring_but_not_exact_dir_name_is_not_excluded(self):
        # "distribution" should NOT be excluded just because "dist" is a substring —
        # matching must be on whole path segments only.
        assert gc._is_excluded("src/distribution/report.py") is False


# ---------------------------------------------------------------------------
# build_path_listing
# ---------------------------------------------------------------------------

class TestBuildPathListing:
    def _files(self, paths_and_sizes):
        return [{"path": p, "size": s, "type": "blob"} for p, s in paths_and_sizes]

    def test_filters_excluded_dirs(self):
        tree = self._files([
            ("src/App.jsx", 100),
            ("node_modules/react/index.js", 50),
        ])
        listing, truncated = gc.build_path_listing(tree)
        assert listing == ["src/App.jsx"]
        assert truncated is False

    def test_filters_by_extension(self):
        tree = self._files([
            ("README.md", 100),
            ("src/App.jsx", 100),
            ("image.png", 100),
        ])
        listing, _ = gc.build_path_listing(tree)
        assert listing == ["src/App.jsx"]

    def test_filters_by_size_cap(self):
        tree = self._files([
            ("src/Big.jsx", 70000),
            ("src/Small.jsx", 100),
        ])
        listing, _ = gc.build_path_listing(tree)
        assert listing == ["src/Small.jsx"]

    def test_sorted_shortest_first(self):
        tree = self._files([
            ("src/components/deeply/nested/File.jsx", 10),
            ("App.jsx", 10),
        ])
        listing, _ = gc.build_path_listing(tree)
        assert listing[0] == "App.jsx"

    def test_truncation_flag(self):
        tree = self._files([(f"file{i}.py", 10) for i in range(5)])
        listing, truncated = gc.build_path_listing(tree, max_paths=3)
        assert len(listing) == 3
        assert truncated is True


# ---------------------------------------------------------------------------
# extract_picked_paths (item 1: hardened path extraction with fallback signal)
# ---------------------------------------------------------------------------

class TestExtractPickedPaths:
    def test_strict_backtick_paths(self):
        text = "Look at `src/App.jsx` and also `src/utils/helpers.py` for patterns."
        paths, used_fallback = gc.extract_picked_paths(text)
        assert paths == ["src/App.jsx", "src/utils/helpers.py"]
        assert used_fallback is False

    def test_dedupes_while_preserving_order(self):
        text = "`src/App.jsx` is relevant. Also see `src/App.jsx` again and `src/B.jsx`."
        paths, used_fallback = gc.extract_picked_paths(text)
        assert paths == ["src/App.jsx", "src/B.jsx"]
        assert used_fallback is False

    def test_falls_back_when_no_backticks_present(self):
        # Model didn't follow instructions to use backticks.
        text = "You should inspect src/App.jsx and src/utils/helpers.py closely."
        paths, used_fallback = gc.extract_picked_paths(text)
        assert "src/App.jsx" in paths
        assert "src/utils/helpers.py" in paths
        assert used_fallback is True

    def test_no_paths_at_all_returns_empty_and_no_fallback_flag(self):
        # When there's genuinely nothing path-like, used_fallback should be False
        # (there's nothing to "fall back" to finding) rather than falsely True.
        text = "This appears to be net-new functionality with no existing counterpart."
        paths, used_fallback = gc.extract_picked_paths(text)
        assert paths == []
        assert used_fallback is False

    def test_empty_string_input(self):
        paths, used_fallback = gc.extract_picked_paths("")
        assert paths == []
        assert used_fallback is False


# ---------------------------------------------------------------------------
# fetch_files_by_paths (only its validation logic — no real network doubles needed
# since invalid paths are filtered before any request would be made)
# ---------------------------------------------------------------------------

class TestFetchFilesByPathsValidation:
    def test_rejects_paths_not_in_real_tree(self, monkeypatch):
        tree_files = [{"path": "src/App.jsx", "type": "blob"}]

        # Ensure no network call is attempted for a hallucinated path.
        def _fail_if_called(*args, **kwargs):
            raise AssertionError("fetch_raw_file should not be called for an invalid path")

        monkeypatch.setattr(gc, "fetch_raw_file", _fail_if_called)

        result = gc.fetch_files_by_paths(
            "owner", "repo", "main",
            ["src/DoesNotExist.jsx"], tree_files,
        )
        assert result == []

    def test_accepts_and_fetches_valid_path(self, monkeypatch):
        tree_files = [{"path": "src/App.jsx", "type": "blob"}]

        monkeypatch.setattr(gc, "fetch_raw_file", lambda owner, repo, branch, path, max_chars=1500: "code here")

        result = gc.fetch_files_by_paths(
            "owner", "repo", "main",
            ["src/App.jsx"], tree_files,
        )
        assert len(result) == 1
        assert "src/App.jsx" in result[0]
        assert "code here" in result[0]

    def test_strips_backticks_from_path_before_lookup(self, monkeypatch):
        tree_files = [{"path": "src/App.jsx", "type": "blob"}]
        monkeypatch.setattr(gc, "fetch_raw_file", lambda owner, repo, branch, path, max_chars=1500: "ok")

        result = gc.fetch_files_by_paths(
            "owner", "repo", "main",
            ["`src/App.jsx`"], tree_files,
        )
        assert len(result) == 1

    def test_respects_max_files_cap(self, monkeypatch):
        tree_files = [{"path": f"file{i}.py", "type": "blob"} for i in range(10)]
        monkeypatch.setattr(gc, "fetch_raw_file", lambda owner, repo, branch, path, max_chars=1500: "ok")

        result = gc.fetch_files_by_paths(
            "owner", "repo", "main",
            [f"file{i}.py" for i in range(10)], tree_files,
            max_files=3,
        )
        assert len(result) == 3


# ---------------------------------------------------------------------------
# detect_tech_stack dependency-list regex parsing
# ---------------------------------------------------------------------------

class TestDetectTechStackParsing:
    def test_package_json_dependencies_extracted(self, monkeypatch):
        fake_package_json = """
        {
          "name": "demo",
          "dependencies": {
            "react": "^18.0.0",
            "@scope/pkg": "^1.2.3"
          },
          "devDependencies": {
            "eslint": "^8.0.0"
          }
        }
        """
        monkeypatch.setattr(
            gc, "fetch_raw_file",
            lambda owner, repo, branch, path, max_chars=4000: fake_package_json,
        )
        tree_files = [{"path": "package.json", "type": "blob"}]
        result = gc.detect_tech_stack("owner", "repo", "main", tree_files)
        assert "react" in result
        assert "@scope/pkg" in result
        assert "eslint" in result

    def test_requirements_txt_parsed(self, monkeypatch):
        fake_requirements = "flask==2.0.0\n# a comment\nrequests>=2.0\n\n"
        monkeypatch.setattr(
            gc, "fetch_raw_file",
            lambda owner, repo, branch, path, max_chars=4000: fake_requirements,
        )
        tree_files = [{"path": "requirements.txt", "type": "blob"}]
        result = gc.detect_tech_stack("owner", "repo", "main", tree_files)
        assert "flask==2.0.0" in result
        assert "requests>=2.0" in result
        assert "# a comment" not in result

    def test_no_manifest_found(self):
        tree_files = [{"path": "src/App.jsx", "type": "blob"}]
        result = gc.detect_tech_stack("owner", "repo", "main", tree_files)
        assert "No recognized manifest file found" in result


# ---------------------------------------------------------------------------
# resolve_imported_dependencies
# ---------------------------------------------------------------------------

class TestResolveImportedDependencies:
    def test_resolves_js_relative_imports(self, monkeypatch):
        tree_files = [
            {"path": "src/components/Form.jsx", "type": "blob"},
            {"path": "src/services/api.js", "type": "blob"},
            {"path": "src/components/helpers.js", "type": "blob"},
        ]
        snippets = [(
            "src/components/Form.jsx",
            "import { postData } from '../services/api';\nimport { formatDate } from './helpers';"
        )]

        def _fake_fetch(owner, repo, branch, path, max_chars=2500):
            if path == "src/services/api.js":
                return "export const postData = () => {};"
            if path == "src/components/helpers.js":
                return "export const formatDate = () => {};"
            return None

        monkeypatch.setattr(gc, "fetch_raw_file", _fake_fetch)
        extras = gc.resolve_imported_dependencies(
            snippets, "owner", "repo", "main", tree_files, max_extra_files=3
        )
        assert len(extras) == 2
        assert "src/services/api.js" in extras[0] or "src/services/api.js" in extras[1]
        assert "src/components/helpers.js" in extras[0] or "src/components/helpers.js" in extras[1]

    def test_respects_max_paths_cap_and_depth_sorting(self):
        tree_files = [
            {"path": f"dir{i}/file{i}.js", "type": "blob"} for i in range(700)
        ]
        listing, truncated = gc.build_path_listing(tree_files, max_paths=600)
        assert len(listing) == 600
        assert truncated is True