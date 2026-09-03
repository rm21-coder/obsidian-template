"""check_identity_leak: the pre-commit gate against publishing real identities.

This repo is public and gitleaks only covers credentials, so tenant domains and
colleagues' names had no gate at all. Turning this one on immediately found
four real names already published in docstrings and comments — which is the
argument for testing it rather than trusting it.

The invariant that matters most is the scan SCOPE. A new file is untracked
until it is staged, so a working-tree scan run while the file is being written
reports clean; the leak then rides in on the next `git add -A`. These tests
build real git repositories in tmp_path and stage into them, because that
off-by-one in scope is the failure this tool exists to close and it cannot be
observed by calling the matcher directly.
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent.parent.parent / "installers" / "lib"
SCANNER = LIB / "check_identity_leak.py"


def load_module(monkeypatch: pytest.MonkeyPatch, lib_dir: Path):
    """Import the scanner with its LIB_DIR pointed at a tmp copy.

    DENYLIST and ALLOWED_DOMAINS are module-level paths resolved from the
    script's own location, so a test that did not redirect them would read the
    operator's real deny-list — and pass or fail based on machine state.
    """
    spec = importlib.util.spec_from_file_location("cil", SCANNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "LIB_DIR", lib_dir)
    monkeypatch.setattr(mod, "DENYLIST", lib_dir / "identity-denylist.local")
    monkeypatch.setattr(mod, "ALLOWED_DOMAINS", lib_dir / "identity-allowed-domains.txt")
    return mod


@pytest.fixture
def lib(tmp_path: Path) -> Path:
    d = tmp_path / "lib"
    d.mkdir()
    (d / "identity-allowed-domains.txt").write_text(
        "example.com\n.example\n.test\nexample.edu\n# a comment\n", encoding="utf-8")
    (d / "identity-denylist.local").write_text(
        "# real values\nacme-tenant.edu\nAckerman, Dana\nre:internal-\\d+\n",
        encoding="utf-8")
    return d


@pytest.fixture
def repo(tmp_path: Path, allow_subprocess: None) -> Path:
    """A real git repo. `git diff --cached` has no meaningful fake.

    Requests `allow_subprocess` (see conftest) because the suite blocks real
    subprocess.run by default; git is the one thing here that cannot be faked
    without also faking the behaviour under test."""
    r = tmp_path / "repo"
    r.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "t@example.com"),
                 ("config", "user.name", "T")):
        subprocess.run(("git",) + args, cwd=r, check=True,
                       capture_output=True)
    return r


def stage(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    subprocess.run(("git", "add", name), cwd=repo, check=True,
                   capture_output=True)


class TestDenyList:

    def test_literal_values_match_case_insensitively(self, monkeypatch, lib) -> None:
        m = load_module(monkeypatch, lib)
        rules = m.load_denylist()
        hits = m.scan([("f.py", 1, "host = ACME-TENANT.EDU")], rules, set())
        assert len(hits) == 1
        assert "acme-tenant.edu" in hits[0][2]

    def test_regex_rules_are_honored(self, monkeypatch, lib) -> None:
        m = load_module(monkeypatch, lib)
        rules = m.load_denylist()
        assert m.scan([("f.py", 1, "ref internal-4821 here")], rules, set())

    def test_comments_and_blanks_are_not_rules(self, monkeypatch, lib) -> None:
        """A '#' line becoming a literal rule would match every Python comment
        in the repo and make the hook useless."""
        m = load_module(monkeypatch, lib)
        rules = m.load_denylist()
        assert not m.scan([("f.py", 1, "# an ordinary comment")], rules, set())

    def test_bad_regex_is_reported_not_fatal(self, monkeypatch, lib, capsys) -> None:
        (lib / "identity-denylist.local").write_text("re:[unclosed\ngood.tld\n",
                                                     encoding="utf-8")
        m = load_module(monkeypatch, lib)
        rules = m.load_denylist()
        err = capsys.readouterr().err
        assert "bad regex" in err and "[unclosed" in err
        # the valid rule after the broken one still loads
        assert m.scan([("f.py", 1, "x good.tld y")], rules, set())

    def test_missing_denylist_yields_no_rules(self, monkeypatch, lib) -> None:
        (lib / "identity-denylist.local").unlink()
        m = load_module(monkeypatch, lib)
        assert m.load_denylist() == []


class TestEmailRule:

    def test_allowed_domains_pass(self, monkeypatch, lib) -> None:
        m = load_module(monkeypatch, lib)
        allowed = m.load_allowed_domains()
        for addr in ("me@example.com", "a.b@example.edu",
                     "x@nimbuswidgets.example", "evil@attacker.test"):
            assert not m.scan([("f.md", 1, addr)], [], allowed), addr

    def test_real_looking_domain_is_flagged(self, monkeypatch, lib) -> None:
        m = load_module(monkeypatch, lib)
        allowed = m.load_allowed_domains()
        hits = m.scan([("f.md", 1, "contact person@realschool.edu")], [], allowed)
        assert len(hits) == 1
        assert "person@realschool.edu" in hits[0][2]

    def test_suffix_entries_cover_subdomains(self, monkeypatch, lib) -> None:
        m = load_module(monkeypatch, lib)
        allowed = m.load_allowed_domains()
        assert not m.scan([("f.md", 1, "a@deep.sub.example")], [], allowed)

    def test_trailing_dot_is_normalized(self, monkeypatch, lib) -> None:
        m = load_module(monkeypatch, lib)
        allowed = m.load_allowed_domains()
        assert not m.scan([("f.md", 1, "see me@example.com.")], [], allowed)


class TestSelfExclusion:
    """The deny-list and allow-list are the one place these strings belong."""

    def test_the_lists_themselves_are_skipped(self, monkeypatch, lib) -> None:
        m = load_module(monkeypatch, lib)
        assert m.skip_path("installers/lib/identity-denylist.local") is True
        assert m.skip_path("installers/lib/identity-allowed-domains.txt") is True
        assert m.skip_path("installers/lib/check_identity_leak.py") is False

    def test_this_test_file_is_excluded(self, monkeypatch, lib) -> None:
        """Deliberate and worth stating: this file holds leak-shaped fixtures
        so the rules can be proven to fire, so the scanner must skip it. The
        cost is that a real leak hidden in THIS file would pass."""
        m = load_module(monkeypatch, lib)
        assert m.skip_path("Templates/Scripts/tests/test_check_identity_leak.py") is True

    def test_binary_suffixes_are_skipped(self, monkeypatch, lib) -> None:
        m = load_module(monkeypatch, lib)
        assert m.skip_path("Z_attachments/photo.PNG") is True
        assert m.skip_path("docs/diagram.svg") is True
        assert m.skip_path("docs/notes.md") is False


class TestStagedScope:
    """The scope invariant: staged content is what gets published."""

    def test_newly_staged_file_is_scanned(self, monkeypatch, lib, repo,
                                          tmp_path) -> None:
        """The regression this tool exists for. The file is brand new, so a
        `git diff HEAD` scan sees nothing — staging is what makes it real."""
        m = load_module(monkeypatch, lib)
        monkeypatch.chdir(repo)
        stage(repo, "fixture.py", 'HOST = "acme-tenant.edu"\n')
        hits = m.scan(m.staged_added_lines(), m.load_denylist(),
                      m.load_allowed_domains())
        assert len(hits) == 1
        assert hits[0][0] == "fixture.py"

    def test_unstaged_edit_is_not_scanned(self, monkeypatch, lib, repo) -> None:
        """Only what is about to be committed. Blocking on working-tree scratch
        would make the hook fire on files the user never intends to commit."""
        m = load_module(monkeypatch, lib)
        monkeypatch.chdir(repo)
        stage(repo, "clean.py", "x = 1\n")
        (repo / "scratch.py").write_text('HOST = "acme-tenant.edu"\n',
                                         encoding="utf-8")
        assert not m.scan(m.staged_added_lines(), m.load_denylist(),
                          m.load_allowed_domains())

    def test_removed_lines_are_not_flagged(self, monkeypatch, lib, repo) -> None:
        """Deleting a leak must not be blocked by the leak it deletes."""
        m = load_module(monkeypatch, lib)
        monkeypatch.chdir(repo)
        stage(repo, "f.py", 'HOST = "acme-tenant.edu"\n')
        subprocess.run(("git", "commit", "-q", "-m", "seed", "--no-verify"),
                       cwd=repo, check=True, capture_output=True)
        stage(repo, "f.py", 'HOST = "example.com"\n')
        assert not m.scan(m.staged_added_lines(), m.load_denylist(),
                          m.load_allowed_domains())

    def test_line_numbers_point_at_the_hit(self, monkeypatch, lib, repo) -> None:
        m = load_module(monkeypatch, lib)
        monkeypatch.chdir(repo)
        stage(repo, "f.py", 'a = 1\nb = 2\nc = "Ackerman, Dana"\n')
        hits = m.scan(m.staged_added_lines(), m.load_denylist(),
                      m.load_allowed_domains())
        assert [(h[0], h[1]) for h in hits] == [("f.py", 3)]


class TestInit:

    def test_builds_rules_from_config_and_people(self, monkeypatch, lib,
                                                 tmp_path, capsys) -> None:
        m = load_module(monkeypatch, lib)
        vault = tmp_path / "vault"
        (vault / "People").mkdir(parents=True)
        (vault / "People" / "Ackerman, Dana.md").write_text("x", encoding="utf-8")
        (vault / "People" / "Solo.md").write_text("x", encoding="utf-8")
        cfg = tmp_path / "meeting_pull.json"
        cfg.write_text('{"tenant_domains": ["acme-tenant.edu"],'
                       ' "email": "someone@acme-tenant.edu",'
                       ' "display_name": "Some One"}', encoding="utf-8")
        assert m.init_denylist(vault, cfg) == 0
        body = m.DENYLIST.read_text(encoding="utf-8")
        assert "acme-tenant.edu" in body
        assert "Ackerman, Dana" in body
        assert "Dana Ackerman" in body           # both orderings
        assert "Some One" in body
        # A single-token People note has no distinctive full-name form and
        # would match ordinary prose, so it must not become a rule.
        assert "\nSolo\n" not in body

    def test_init_merges_and_keeps_hand_edits(self, monkeypatch, lib,
                                              tmp_path) -> None:
        """--init is re-run whenever People/ grows; it must not discard rules
        the operator added by hand."""
        m = load_module(monkeypatch, lib)
        m.DENYLIST.write_text("# mine\nhand-added.example\n", encoding="utf-8")
        vault = tmp_path / "vault"
        (vault / "People").mkdir(parents=True)
        cfg = tmp_path / "c.json"
        cfg.write_text('{"tenant_domains": ["acme-tenant.edu"]}', encoding="utf-8")
        m.init_denylist(vault, cfg)
        body = m.DENYLIST.read_text(encoding="utf-8")
        assert "hand-added.example" in body and "acme-tenant.edu" in body

    def test_init_reports_when_it_finds_nothing(self, monkeypatch, lib,
                                                tmp_path, capsys) -> None:
        """Silently writing an empty deny-list would look identical to a
        working one, so this must be loud and non-zero."""
        m = load_module(monkeypatch, lib)
        rc = m.init_denylist(tmp_path / "absent", tmp_path / "absent.json")
        assert rc == 1
        assert "found nothing to protect" in capsys.readouterr().err


class TestRepoIsClean:
    """The gate must pass on the committed tree, or it gets bypassed."""

    def test_committed_tree_has_no_real_addresses(self, allow_subprocess: None) -> None:
        """Runs the real scanner with its real allow-list over tracked files.
        No deny-list is involved: that is machine-local, and this assertion has
        to hold in any clone."""
        repo_root = SCANNER.parent.parent.parent
        r = subprocess.run(
            ("python3", str(SCANNER), "--worktree", "--quiet"),
            cwd=repo_root, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
