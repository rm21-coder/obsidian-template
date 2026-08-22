# Vault Lint (Optional)

A weekly content-hygiene sweep over the vault. Standard-library Python 3 (no
dependencies), **read-only on its schedule**, and makes **no network calls**.
Installed by component `55-vault-lint`.

It is the counterpart to the [security harness](Security-Harness.md), not an
overlap with it. `integrity_monitor.py` watches for *hostile* change — script
hashes, LaunchAgents, bulk deletion. This watches for *entropy*: the tag that
drifted off your taxonomy, the article you clipped twice, the wikilink pointing
at a note you renamed, the vault script still running last month's code.

Entropy is the failure mode automation actually produces. A tagger with an
off-by-one bug doesn't crash — it quietly writes a duplicate tag on every note
it touches for two months.

## The seven checks

**Fixable** — rewritten by `--apply`, which writes a rollback manifest first:

| Check | Finds |
| --- | --- |
| `dup-tags` | The same tag listed twice in one note's frontmatter |
| `bad-tags` | Malformed tags (`"#Golf"`, a stray `- ` prefix, two tags collapsed onto one line) and high-confidence remaps onto your allowlist |

**Report-only** — these need a human call, so the tool never acts on them:

| Check | Finds |
| --- | --- |
| `dup-notes` | Near-identical notes, confirmed by body similarity |
| `taxonomy` | Off-allowlist tags, split into auto-fixable / promotion candidates / strays; unused allowlist entries; entries breaking your own stated conventions |
| `script-drift` | Vault `Templates/Scripts` vs your clone of this repo |
| `schema` | Notes missing frontmatter keys their folder's template defines |
| `links` | Wikilinks to notes that don't exist; unreferenced People/Groups |

Two extra fixers sit outside `--apply` because they rewrite more than a tag
list: `--fix-malformed` and `--fix-links`. Both are described under
[Fixing](#fixing).

## Configure it first

The lint has no opinion of its own about what your vault should look like. Three
places tell it:

1. **`Knowledge/Tag Taxonomy.md`** — the canonical tag allowlist, the same file
   the [semantic auto-tagger](Semantic%20Auto-Tagger%20Setup.md) reads. Every tag
   used but not listed here is reported. Without this file the three tag checks
   no-op rather than inventing a standard for you; the other four still work.
2. **`REQUIRED_KEYS`** in `vault_lint.py` — the frontmatter contract per folder.
   Only *missing* keys are reported; extra keys are fine.
3. **`EXPECTED_DRIFT`** in `vault_lint.py` — vault/repo differences you intend
   to keep (see below).

## What it deliberately does not flag

Most of the work in a lint like this is suppressing true-but-useless findings.
Ignore the wrong things and the report is noise; ignore too much and it misses
real problems. The rules it applies:

- **Author bylines.** Clippings link their byline as a wikilink. A byline author
  with no People note is normal, not a broken link. Counted, not listed.
- **Documentation examples.** A wikilink inside a code fence, inside inline
  backticks, or containing a template placeholder like `${selected}` is
  documentation showing the syntax. Rewriting those would corrupt the very docs
  that explain the system. Everything under `Templates/` is skipped outright.
- **Shared source URLs.** Twenty-eight YouTube notes share `youtube.com/watch`;
  a course's lessons share one product URL. Duplicate detection therefore
  confirms with body similarity and never on URL equality alone.
- **Trailing disambiguators.** `2026-07-14 1500-2` is a *different* meeting from
  `2026-07-14 1500`, and `Report 3` a different report from `Report`. Spelling
  repair never collapses two distinct entities.
- **Installer-rendered values.** A `YOUR_USERNAME` placeholder on one side and
  the real path on the other is the installer working.
- **LaunchAgent plists**, which are compared by `<key>` structure rather than
  text, since the installed copy legitimately carries real paths and
  per-machine `EnvironmentVariables` the template can't.

## Script drift, and why it earns its place

The check that pays for the whole tool. A vault script can fall behind its repo
version and keep working — silently running last month's logic. Hash-based
integrity monitoring cannot catch this, because it pins each file against
*itself*: stale-but-unchanged code looks perfectly healthy.

Findings are classified rather than dumped:

- **Behavioral** — the two copies do different things. Python files are compared
  by AST with docstrings stripped, so a reworded comment doesn't cry wolf and a
  changed default value doesn't hide.
- **Comments/docs only** — same behavior, different prose. Worth knowing (a
  docstring documenting the wrong default is a real if minor bug) but not urgent.
- **Expected** — matches `EXPECTED_DRIFT`.

`EXPECTED_DRIFT` maps a filename to regexes. A changed line matching one stays
quiet; **every other changed line in that same file still reports.** That
distinction is the point — an accepted exception must not become a hiding place
for a real regression:

```python
_GATEWAY = [r"MY_ORG_API_KEY", r"api\.ai\.example\.edu", r"ANTHROPIC_API_KEY",
            r"base_url", r"anthropic\.Anthropic\("]
EXPECTED_DRIFT = {"tag_clippings.py": _GATEWAY, "voice_cleanup.py": _GATEWAY}
```

Point it at your clone with `VAULT_LINT_REPO=/path/to/obsidian-template` if it
isn't at `~/dev/repos/obsidian-template`. The check skips itself cleanly when
the path doesn't exist.

## Running it

```bash
python3 Templates/Scripts/vault_lint.py                  # dry-run, all checks
python3 Templates/Scripts/vault_lint.py --only taxonomy  # one check (repeatable)
python3 Templates/Scripts/vault_lint.py --skip links     # all but one (repeatable)
python3 Templates/Scripts/vault_lint.py --verbose        # full lists, not samples
python3 Templates/Scripts/vault_lint.py --json report.json
```

Exit status is 1 when anything is reported, so a CI job or a shell gate can act
on it.

## Fixing

```bash
python3 Templates/Scripts/vault_lint.py --apply           # dup-tags + bad-tags
python3 Templates/Scripts/vault_lint.py --fix-malformed   # spliced frontmatter
python3 Templates/Scripts/vault_lint.py --fix-links       # broken wikilinks
python3 Templates/Scripts/vault_lint.py --rollback vault_lint_manifest_<ts>.json
```

Every write records the complete original file in a manifest next to the
script, so `--rollback` restores byte-for-byte.

**`--apply`** touches only the frontmatter `tags:` block. Bodies, other keys,
indentation, and quoting are left byte-for-byte unchanged.

**`--fix-malformed`** repairs one specific injury: a tag written into the middle
of an `updated:` timestamp, stranding the seconds on their own line as invalid
YAML. It drops the stranded fragment rather than reconstructing it (the split
point varies, and Obsidian rewrites `updated:` anyway) and folds any welded tag
back into the tags block. Anything outside that exact signature is left alone.

Note that `--apply` **refuses** to touch a note whose frontmatter has an
unexplained line. Tidying its tags would leave the real corruption in place,
looking clean. Fix those with `--fix-malformed` or by hand.

**`--fix-links`** relinks what resolves unambiguously — a stray bracket, a
diacritic or casing slip, a reversed `First Last` / `Last, First`, or a single
very-close spelling match — and unlinks the rest, keeping the display text. When
a target is genuinely ambiguous (four notes named `King, *` for a `[[king]]`
link) it unlinks rather than guessing an attribution.

## The weekly job

`com.obsidian.vault-lint` runs Mondays 07:00, logging to
`~/Library/Logs/vault-lint.log`. It passes **no fixing flags** — unattended
frontmatter rewrites are the wrong default.

It runs with `--exit-zero`. Without that the job exits 1 whenever it finds
anything, and `morning_dashboard.py` reads launchd's `LastExitStatus`, so a
vault with two stray tags would render as a *failed pipeline*. With it, exit
status means "the lint ran" and the findings live in the log. A non-zero status
on this job therefore means the lint itself broke.

It also runs without `--verbose`, because the full listing runs to hundreds of
lines of unreferenced People notes and buries what needs action.

> **Editing the plist:** XML comments cannot contain a double hyphen, so flag
> names appear in its comments without their leading dashes. Adding them makes
> the file unparseable by `plistlib` — even though `plutil -lint` and launchd
> both still accept it — and the dashboard silently drops the job from pipeline
> health.

## Footprint

- One LaunchAgent, one weekly run, one log file.
- No dependencies, no network, no state directory. The rollback manifests are
  the only files it writes outside the vault's notes.
- A full sweep over ~2,500 notes takes a couple of seconds.

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.obsidian.vault-lint.plist
rm ~/Library/LaunchAgents/com.obsidian.vault-lint.plist
```

`./uninstall.sh` removes it along with everything else.
