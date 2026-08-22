# Security-controls test suite

A pytest suite covering the security-relevant runtime scripts and the
LaunchAgent plists that make up the vault's security posture.

## What's tested

| File under test                | Test file                    | Headline coverage |
|--------------------------------|------------------------------|-------------------|
| `integrity_monitor.py`         | `test_integrity_monitor.py`  | scan_dir hashing, state_dir scope, diff_dir, BULK_DELETE threshold, save/load round-trip, end-to-end drift detection |
| `plugin_integrity_check.py`    | `test_plugin_integrity.py`   | HMAC envelope round-trip, ALLOWLIST_TAMPER detection (state mutation, hmac swap, key swap, malformed envelope), legacy migration, trust-anchor helpers |
| `youtube_summarize.py`         | `test_youtube_summarize.py`  | SSRF guard parity with url_safety, caption-track SSRF, retry-with-backoff on 429/5xx, header-based API auth, AST invariants |
| `security_common.py`           | `test_security_common.py`    | state_dir resolution, `restrict_file` (chmod 0600 / icacls ACL), DPAPI protect-unprotect round-trip, key stability across runs, corrupt-blob and short-key regeneration, Keychain branch, notify never raising |
| `whisper_onnx.py`              | `test_whisper_onnx.py`       | Slaney-vs-HTK mel scale, filter area normalisation, time-domain vs feature-space padding, decoder-prompt construction across both special-token key spellings, repo resolution, ffmpeg boundary errors |
| All of the above + plists      | `test_static.py`             | py_compile, AST security invariants (getaddrinfo / allow_redirects=False / hmac.compare_digest), plistlib parse, integrity plist WatchPaths |

`security_common.py` previously had no
dedicated test file. That mattered most for `security_common.py`: the other
suites monkeypatch it out via `fake_keychain`, so its Windows branches —
DPAPI and the icacls hardening — were covered nowhere at all.

Platform coverage: tests for a branch that calls an OS API existing on only
one platform are marked for that platform and skip elsewhere (the Keychain
cases skip on Windows; the DPAPI cases skip on macOS). Everything else runs
everywhere. Run with `-rs` to see the skip reasons.

## Running

```sh
cd Templates/Scripts/tests
./run_tests.sh
```

First run installs whichever of `pytest`, `pytest-cov` and `numpy` are missing,
via `pip3 install --break-system-packages`. Subsequent runs skip the install
step. `numpy` is needed because `test_whisper_onnx.py` imports
`whisper_onnx.py`, which imports numpy at module scope — without it the whole
suite fails at collection, not just that file. (`requirements.txt` doesn't name
numpy: the ONNX backend is imported lazily and only on Windows ARM64, where
`onnxruntime` brings numpy along.)

**On Windows**, `run_tests.sh` doesn't apply (`pip3
--break-system-packages`). Install the test deps once, then run from the
repo root — no `PYTHONPATH` needed, `conftest.py` puts `Templates/Scripts/`
on `sys.path` itself:

```powershell
.\Templates\Scripts\.venv\Scripts\python.exe -m pip install pytest pytest-cov numpy
.\Templates\Scripts\.venv\Scripts\python.exe -m pytest Templates\Scripts\tests
```

Output ends with a coverage table per module and an HTML report at
`coverage_html/index.html` (gitignored — regenerate locally as needed).

### Common flags

```sh
./run_tests.sh -v            # verbose output
./run_tests.sh -k tamper     # run only tests whose name contains "tamper"
./run_tests.sh --no-cov      # skip coverage (faster iteration)
```

## Isolation guarantees

The suite is safe to run repeatedly on a live Mac or Windows box. It does not:

- read or write the real macOS Keychain (`security_common.get_or_create_hmac_key`
  is monkeypatched to a dict-backed fake in `conftest.py`; the real
  `obsidian-allowlist-hmac` entry is never touched)
- write to `~/.local/share/obsidian-security/` (each test gets its
  own `tmp_path`-rooted state directory; the module-level `STATE_DIR`,
  `ALLOWLIST_PATH`, `STATE_PATH`, `ALERT_LOG` constants are
  monkeypatched per-test)
- shell out to `/usr/bin/security`, `osascript`, or `log` (an autouse
  `block_unmocked_subprocess` fixture wraps `subprocess.run` and
  raises if a test calls it without explicitly opting in via the
  `allow_subprocess` fixture, which is reserved for tests that are
  themselves mocking `subprocess.run` to a canned response)
- resolve a real hostname (`block_external_dns` is autouse and fails
  any test that reaches `socket.getaddrinfo` for a non-loopback name;
  a test that needs a resolvable public host takes the `public_dns`
  fixture instead). Enforcement was added after seven tests were found
  depending on live DNS — two failed offline, and five *passed* offline
  for the wrong reason: the SSRF parity cases agreed on `False` because
  neither predicate could resolve, asserting parity while testing
  nothing
- make real network requests (`requests.get` is monkeypatched to
  return synthesized `Mock` responses; `socket.getaddrinfo` is
  monkeypatched to return controlled IP addresses for DNS-rebinding
  scenarios)
- modify the real Obsidian vault (synthetic vault layouts are built
  in `tmp_path` for plugin scanner and integrity counts)

## Test categories

**Static.** `test_static.py::TestPyCompile` (every `.py` compiles),
`TestClipArticleAST` / `TestPluginIntegrityAST` / `TestIntegrityMonitorAST`
(AST-level assertions of the security invariants), `TestPlists` (plistlib
parses every security-control plist; integrity plist WatchPaths covers the
three expected targets), `TestShellScripts` (`bash -n` on `run_tests.sh`).

**Unit.** Per-function correctness for `is_safe_url`, `safe_filename`,
`yaml_escape`, `host_tag`, `trim_trailing_punctuation`,
`is_known_paywall`, `looks_like_paywall_page`, `sha256_file`,
`scan_dir`, `scan_state_dir`, `count_vault_md`, `diff_dir`,
`diff_md_count`, `_canonical_state_bytes`, `_compute_hmac`,
`scan_plugins`, `diff`.

**Functional.** `TestEndToEnd` for `integrity_monitor` (baseline →
clean check → mutate → drift), `TestEndToEndMain` for
`plugin_integrity_check` (baseline → clean check → bundle swap → fires),
`TestFetchersRouteThroughUrlSafety` for the fetchers (every one routes
through the shared SSRF guard rather than carrying its own copy).

**Security.** The headline attack scenarios:

- DNS rebinding (`evil.test` resolving to `127.0.0.1`, `10.0.0.1`,
  `169.254.169.254`, `::1`) — `is_safe_url` rejects.
- Mixed-resolution attack (DNS returns one public + one private IP) —
  rejected if any resolved IP is internal.
- DNS resolution failure — rejected (conservative posture).
- 302-to-private-IP redirect — `safe_fetch` rejects the Location
  header before issuing the second request; verified via
  request-call-count assertion.
- Redirect chain longer than `MAX_REDIRECTS` — terminates.
- Body size larger than `MAX_BODY_BYTES` — rejected mid-stream.
- Allowlist state mutation — `load_allowlist` fires `ALLOWLIST_TAMPER`,
  notifies, logs to alerts.log, exits non-zero.
- Allowlist HMAC field swap — same.
- Allowlist envelope substituted with a forgery under an attacker's
  key — same (HMAC verification uses the real trust-anchor key, which
  the attacker doesn't control).
- Malformed envelope — same.
- Truncated trust-anchor key (sub-128-bit) — treated as missing,
  regenerated.

## Adding tests

When you add a fix or a new control, the pattern is:

1. Add a test class to the relevant `test_<module>.py` file.
2. If the new code touches the Keychain/DPAPI, network, vault state, or
   the unified log, write the test against the existing fixtures
   (`fake_keychain`, `silent_notify`, `tmp_state_dir`, `sample_vault`,
   `monkeypatch` of `requests.get` / `socket.getaddrinfo` /
   `subprocess.run`). Do not relax the `block_unmocked_subprocess`
   guard.
3. If the fix introduces a new security invariant (e.g. "X must
   always be set to Y"), add an AST-level assertion to `test_static.py`
   so a future revert surfaces immediately.
4. Run `./run_tests.sh` and confirm coverage on the new module
   doesn't drop.
