# WP-3 package — the exact SANE submission series, exported

**Status: revision v7 PUSHED by the author 2026-10-13 to merge request
!1032 (<https://gitlab.com/sane-project/backends/-/merge_requests/1032>),
whose fork source branch keeps the name `wp3-gl126-submission-v5` and now
holds the five v7 commits. The conflict the merge request showed against
upstream is gone. The merge request's GitLab pipeline on v7 passed all eight jobs (2943836952) and the merge request is mergeable; still under review, not merged.** v7 is v6 rebased onto current upstream plus one new
commit. The series is exactly the repository's `sane/` (the nine
`gl126_*` files and `gl126-integration.patch`) with the two edits
described below, as five commits in two forms that recreate it without
manual reconstruction.

## Why v7 exists

Upstream moved to `a8676a6f7`, 34 commits past `7103e09b0`. Among them is
a revert (`675c285e0`, `be064801b`) of the "scan exposure time" feature:
`Genesys_Settings::exposure_lperiod`, `SetupParams::exposure_lperiod` and
the frontend option `scan-exposure-time` (`OPT_EXPOSURE_TIME`) no longer
exist. The series touched all three, so the rebase has two parts.

- **One conflict**, `backend/genesys/settings.h` (commit 1): both sides
  added a field after `expiration_time`. Resolved by keeping our
  `unsigned frame = 1;` with its comment and not re-adding
  `exposure_lperiod`, following upstream's revert. Every other file
  merged without conflict (the OpticFilm 7600i changes `90a16470f`,
  `908b0cb7b` touched other model entries).
- **Two edits that the revert forced, not conflicts**, made inside the
  commits that introduced the lines:
  - commit 1, `backend/genesys/gl126.cpp`: the assignment
    `session.params.exposure_lperiod = dev->settings.exposure_lperiod;`
    is removed (the field is gone; the value was always 0 for GL126) and
    the comment above it no longer mentions the exposure field. Nothing
    else in `gl126.cpp` differs from v6.
  - commit 3, `backend/genesys/genesys.cpp`: the line
    `DISABLE(OPT_EXPOSURE_TIME);` in the GL126 block that deactivates
    options doing nothing on this chip is removed (the option is gone;
    nothing is left to deactivate), and the comment's mention of
    "scan-exposure-time" with it. This was found by the first v7 build,
    which failed to compile on the missing enumerator.
- **A fifth commit** carries the cold-start fix: the hand-written cold
  homing round had dropped a read-modify-write of reg 0x32 (set bit 0x02)
  that the vendor driver performs between motor moves 2 and 3. The change
  is in the generated `gl126_tables.cpp` only (three steps, one per
  homing round; `cold_init` 218 -> 224 transfers). Hardware evidence:
  `docs/test-log.md` Test 98 (both the companion driver and the backend).

v6 (2026-10-02): base `7103e09b0`, four commits, tip
`ec21b712f1c120c6cc6d041f98385dcdb1876dde`, tip tree
`83568e8e8eaf8d77add24174b2b473e819198c0a`. Earlier revisions are
described in `docs/sane-wp3-submission.md`.

## What is in here

| file | what |
|---|---|
| `wp3-gl126-submission.bundle` | `git bundle` of the series, `a8676a6f7..wp3-gl126-submission-v7`. Preserves the exact commit ids. |
| `0001-genesys-add-support-for-the-GL126-ASIC.patch` | commit 1 |
| `0002-genesys-add-the-Plustek-OpticFilm-135i-07b3-1436.patch` | commit 2 |
| `0003-genesys-frame-selection-and-magazine-handling-for-th.patch` | commit 3 |
| `0004-genesys-document-the-GL126-and-the-OpticFilm-135i.patch` | commit 4 |
| `0005-genesys-GL126-cold-start-restore-the-reg-0x32-write-.patch` | commit 5 |

## Revisions

Base: sane-backends `a8676a6f7d21adf5137936a3b406fbde70795220` — "Merge
branch 'xerox_mfp-tcp-partial-read' into 'master'", `origin/master` as
fetched 2026-10-13 from `https://gitlab.com/sane-project/backends.git`.

| # | commit | tree | subject |
|---|---|---|---|
| 1 | `f97e65cf805bc3c327c3830f5bbf3637ad15969a` | `8a92b9ad1cd6009f3d3179634767b71383120ac3` | genesys: add support for the GL126 ASIC |
| 2 | `8a7fbd9f2b7e64b456d5ac4c6492d3be8c74e316` | `30fdefa99ca2bde974f7a30a21f77f9566047284` | genesys: add the Plustek OpticFilm 135i (07b3:1436) |
| 3 | `232a71bb118c6e57faf78819e8fb88e2135bc31a` | `8d2362b4438bb12995c8afc7de03e9331ff43aee` | genesys: frame selection and magazine handling for the OpticFilm 135i |
| 4 | `6c7e30b3ecccb4833e9451cb3803a9a28f15d371` | `ce9b4e58055cb0838bc92f2ea39d0557399d7cbb` | genesys: document the GL126 and the OpticFilm 135i |
| 5 | `cf21d4327b86ec768c1dbfda25451052c994ee47` | `f2a530c170b91065c7b5f8b60b71e4aa73af4257` | genesys: GL126 cold start, restore the reg 0x32 write between moves 2 and 3 |

The tip tree `f2a530c1…` is the identity of the package: any route below
that ends on that tree has recreated it exactly.

### Hardware evidence, and whether it applies

The hardware tests (`docs/test-log.md`, Tests 62 to 92, 98) and the
`tstbackend -l 1` run (Tests 93, 94) were made on earlier builds of the
same GL126 sources. **The v7 build has not been run on hardware.** The
cold-start table change itself (commit 5) was run on hardware in the
development build (Test 98: settle read 0x1f on the first poll, the three
warnings gone). The `gl126.cpp` and `genesys.cpp` edits remove an
assignment of a value that was always 0 and the deactivation of an option
that no longer exists; they do not change what is sent to the scanner.
Upstream's shared code merged in since v6 (OpticFilm 7600i entries, the
exposure-time revert, other unrelated backends) was not run against the
scanner.

## Recreate it

Either route needs a clone of sane-backends containing the base commit.

**Route A — the bundle (exact commit ids):**

```
git clone https://gitlab.com/sane-project/backends.git sane-backends
cd sane-backends
git fetch /path/to/wp3-gl126-submission.bundle wp3-gl126-submission-v7
git checkout -b wp3-gl126-submission FETCH_HEAD
git rev-parse HEAD            # cf21d4327b86ec768c1dbfda25451052c994ee47
git rev-parse HEAD^{tree}     # f2a530c170b91065c7b5f8b60b71e4aa73af4257
```

**Route B — the patches (same trees, new commit ids):**

```
git clone https://gitlab.com/sane-project/backends.git sane-backends
cd sane-backends
git checkout -b wp3-gl126-submission a8676a6f7
git am /path/to/000[1-5]-*.patch
git rev-parse HEAD^{tree}     # f2a530c170b91065c7b5f8b60b71e4aa73af4257
```

The bundle's one prerequisite is `a8676a6f7…` (`git bundle verify`, run
inside a clone that has it).

## Build

From the recreated branch, nothing from this repository is needed:

```
./autogen.sh
./configure --sysconfdir=/etc
make -j8 -C lib
make -j8 -C sanei
make -j8 -C backend libsane-genesys.la
nm -D backend/.libs/libsane-genesys.so | grep -c gl126     # 111
```

Note on the SONAME: sane-backends' `autogen.sh` patches libtool
(`patches/ltmain.sh.patch`) so that every backend plugin carries the
SONAME `libsane.so.1`; the v7 build has it. The v6 and development
builds carry `libsane-genesys.so.1` (the patch had not taken there), as
does Fedora's packaged plugin. The installed backend is loaded by absolute
path, so the SONAME does not matter to the dll; it did matter to this
repository's probe-based tests, which link `-lsane-genesys` and would
resolve `libsane.so.1` to the system library. Since 2026-10-13 the tests
put a shim directory (`libsane.so.1` -> the built plugin) first on the
probe's rpath, so they test the build they were pointed at under either
SONAME.

## Verification of this export (v7, 2026-10-13)

Done offline, with no scanner contact. Results apply to the v7 build.

| check | result |
|---|---|
| Standalone build in a worktree on `a8676a6f7` (`autogen`, `configure --sysconfdir=/etc`, `lib`, `sanei`, `backend/libsane-genesys.la`, `-j8`) | exit 0, **0 compiler warnings** from any source file (the first attempt, before the `OPT_EXPOSURE_TIME` line was removed from commit 3, failed to compile; the series was amended and rebuilt) |
| exported symbols | **111** mentioning `gl126` (same as v6) |
| `tests/test_sane_open_params.py` against that build | 7 passed |
| `tests/test_sane_calibration_cache.py` against that build | 6 passed |
| `tests/test_sane_magazine.py` against that build | 42 passed (after the test change described below; the test as of v6 failed one assertion against v7) |
| upstream `genesys_unit_tests` (`make -C testsuite/backend/genesys check`) | PASS (1/1) |
| `tests/test_sane_ops.py` | 47 passed; this suite does not use `SANE_BACKENDS_DIR`: it compiles this repository's `sane/gl126_ops.cpp` and tables (the cold-start table of commit 5 included), not the v7 build |
| `tools/release_check.py`, default development build in `~/Dokument/Github/sane-backends` (not v7) | FULL VERIFICATION PASSED, 401 tests, 0 skipped |
| Route A: fetch from the bundle in a clean clone | tip `cf21d432…`, tree `f2a530c1…`, identical |
| Route B: `git am` of the five patches onto `a8676a6f7` in a clean clone | tree `f2a530c1…`, identical (tip `4f8f8b94…`) |
| upstream CI `.gitlab-ci.yml`; local replay of its jobs in the project's CI images | `make-dist` job replay exit 0; `fedora-39-clang` replay (clang 17.0.1, `-Werror`) `BUILD_EXIT=0`, no `error:`/`warning:` lines in the series' files, `genesys_unit_tests` PASS, after which `make check` stops at `testsuite/backend/escl_test` (`escl.h: requires libcurl, libavahi and libxml2`, `CHECK_EXIT=2`) — the same stop as on v6, which is the local environment's, not the series' |
| symlinks in the recreated trees | 0 (`git ls-files -s`, mode 120000), both routes |

Two test changes were needed in this repository (2026-10-13), both in the
tests, not in the series. (1) The SONAME shim described above; without it
the probes loaded the system `libsane.so.1` and failed with an undefined
symbol. (2) `test_sane_magazine.py` asserted that the option
`scan-exposure-time` is inactive on GL126; upstream removed that option,
so the assertion failed against v7 (`AssertionError:
('scan-exposure-time', [...])`). The test now checks that option only
where it exists, so the same test passes against the development clone
(older upstream, option present) and against v7. All three suites were
re-run after the changes: 7 / 6 / 42 against v7 and 7 / 6 / 42 against the
development build.

Not run: `tstbackend` (any level), `scanimage`, and anything that touches
the scanner.

## What was pushed, and from where

Nothing from v7 is pushed. This package is built and exported locally and
committed to `cgillinger/opticfilm135i-linux` (a local branch, not
pushed at the time of writing). The owner pushed the v5 branch to his own
fork himself, from the v5 worktree, to open the merge request, and v6 to
the same merge request on 2026-10-02; v7 is for him to push if he decides
to. Nothing is pushed to the SANE project's repository from the
development clone, which tracks the real upstream, and no automated
session pushed anything to GitLab.
