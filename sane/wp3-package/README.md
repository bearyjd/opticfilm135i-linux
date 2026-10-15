# WP-3 package — the exact SANE submission series, exported

**Status: revision v8 PREPARED offline 2026-10-15, NOT PUSHED, NOT run on
hardware.** The merge request (!1032,
<https://gitlab.com/sane-project/backends/-/merge_requests/1032>) holds v7
(pushed 2026-10-13; fork source branch `wp3-gl126-submission-v5`). v8 is v7
on current upstream plus the changes below; it is for the owner to push if
he decides to. The series is exactly the repository's `sane/` (the nine
`gl126_*` files and `gl126-integration.patch`) with the two exposure-time
edits described under v7, as five commits in two forms that recreate it
without manual reconstruction.

## Why v8 exists

The SANE maintainer asked why the backend takes a process lock shared with
an external (out-of-tree) driver. Decision (the owner's): drop the lock from
the submission entirely, and remove the other bench-specific names that had
leaked into upstream code. Changes against v7:

- **Process lock removed.** `gl126_lock.{h,cpp}` became `gl126_mark.{h,cpp}`
  and contains only the magazine mark (all its file hardening is unchanged:
  `O_NOFOLLOW`, `O_NONBLOCK`, regular-file and single-link check, atomic
  write by rename, a read failure means "no mark"). `sane_open_impl` and
  `sane_close_impl` no longer take or release anything; the lamp-off,
  `clear_halt` and no-`reset` handling at close is unchanged.
- **Mark path renamed:** `/tmp/sane-gl126-07b3-1436.magazine`, override
  `SANE_GL126_MAGAZINE_MARK` (a test facility). It was
  `$OF135I_LOCK_FILE` + `.magazine`.
- **Test env var renamed:** `OF135I_SANE_POLL_CAP_MS` ->
  `SANE_GL126_TEST_POLL_CAP_MS`; behaviour unchanged (only read when the
  interface is a mock and testing mode is on).
- **`.desc` status** `:basic` (v1 to v7: `:good`), and the comment ends
  "Experimental: developed and tested on a single unit." (commit 4).
- Base moved from `a8676a6f7` to `59d255866`; none of the new upstream
  commits touch `backend/genesys` or `backend/Makefile.am`.

Every GL126 file in the v8 tree is byte-identical to this repository's
`sane/` except `gl126.cpp`, which keeps the v7 divergence (the
`exposure_lperiod` assignment exists only in `sane/`, whose development
clone is based on the older upstream).

## What is in here

| file | what |
|---|---|
| `wp3-gl126-submission.bundle` | `git bundle` of the series, `59d255866..wp3-gl126-submission-v8`. Preserves the exact commit ids. |
| `0001-genesys-add-support-for-the-GL126-ASIC.patch` | commit 1 |
| `0002-genesys-add-the-Plustek-OpticFilm-135i-07b3-1436.patch` | commit 2 |
| `0003-genesys-frame-selection-and-magazine-handling-for-th.patch` | commit 3 |
| `0004-genesys-document-the-GL126-and-the-OpticFilm-135i.patch` | commit 4 |
| `0005-genesys-GL126-cold-start-restore-the-reg-0x32-write-.patch` | commit 5 |

## Revisions

Base: sane-backends `59d255866046e5909208b3da3a6911dd2896a7ec` — "Merge
branch 'various_fix_warnings' into 'master'", `origin/master` as fetched
2026-10-15 from `https://gitlab.com/sane-project/backends.git`.

| # | commit | tree | subject |
|---|---|---|---|
| 1 | `ac80e0a8263284c2ce97620b2bd1bd53ef420ef6` | `68b644b3f11daf7710dda8900f566d7cd9909923` | genesys: add support for the GL126 ASIC |
| 2 | `787f0371851deee74449750466a172bae2dade76` | `86e1f25b776afc6ff7fb9ea95658b79188d91f76` | genesys: add the Plustek OpticFilm 135i (07b3:1436) |
| 3 | `506f5898ee03ecc8dd254c28a1a15f6830c06303` | `fbf1a80ae26fd519d6d92175894cae75be7676b7` | genesys: frame selection and magazine handling for the OpticFilm 135i |
| 4 | `cfb390a6d9922d0b39fa7eb498b2ceef01497264` | `caef408a84ad66d95005275b83aac4aa5afd1795` | genesys: document the GL126 and the OpticFilm 135i |
| 5 | `94628f3f2f538b7bc215d90d65ef1f92ff5dae6a` | `ab97f45fcb35d812262216f815423f3716a0159a` | genesys: GL126 cold start, restore the reg 0x32 write between moves 2 and 3 |

The tip tree `ab97f45f…` is the identity of the package: any route below
that ends on that tree has recreated it exactly. v7 was tip
`cf21d4327b86…`, tree `f2a530c170b9…`; v6 tip `ec21b712f1c1…`, tree
`83568e8e8eaf…`.

### Hardware evidence, and whether it applies

**The v8 build has not been run on hardware.** The hardware tests
(`docs/test-log.md`, Tests 62 to 92, 98) and the `tstbackend -l 1` run
(Tests 93, 94) were made on earlier builds of the same GL126 sources. v8
removes the process lock, which sent nothing to the scanner, renames a mark
file path and a test-only environment variable, and changes documentation
metadata; no byte sent to the scanner differs from v7, so no new hardware
run is needed for these changes. Upstream's shared code merged since v7 was
not run against the scanner.

## History: why v7 existed (v7 is the series before this one)

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

## Recreate it

Either route needs a clone of sane-backends containing the base commit.

**Route A — the bundle (exact commit ids):**

```
git clone https://gitlab.com/sane-project/backends.git sane-backends
cd sane-backends
git fetch /path/to/wp3-gl126-submission.bundle wp3-gl126-submission-v8
git checkout -b wp3-gl126-submission FETCH_HEAD
git rev-parse HEAD            # 94628f3f2f538b7bc215d90d65ef1f92ff5dae6a
git rev-parse HEAD^{tree}     # ab97f45fcb35d812262216f815423f3716a0159a
```

**Route B — the patches (same trees, new commit ids):**

```
git clone https://gitlab.com/sane-project/backends.git sane-backends
cd sane-backends
git checkout -b wp3-gl126-submission 59d255866
git am /path/to/000[1-5]-*.patch
git rev-parse HEAD^{tree}     # ab97f45fcb35d812262216f815423f3716a0159a
```

The bundle's one prerequisite is `59d255866…` (`git bundle verify`, run
inside a clone that has it).

## Build

From the recreated branch, nothing from this repository is needed:

```
./autogen.sh
./configure --sysconfdir=/etc
make -j8 -C lib
make -j8 -C sanei
make -j8 -C backend libsane-genesys.la
nm -D backend/.libs/libsane-genesys.so | grep -c gl126     # 105
```

Note on the SONAME: sane-backends' `autogen.sh` patches libtool
(`patches/ltmain.sh.patch`) so that every backend plugin carries the
SONAME `libsane.so.1`; the v7 and v8 builds have it. The v6 and development
builds carry `libsane-genesys.so.1` (the patch had not taken there), as
does Fedora's packaged plugin. The installed backend is loaded by absolute
path, so the SONAME does not matter to the dll; it did matter to this
repository's probe-based tests, which link `-lsane-genesys` and would
resolve `libsane.so.1` to the system library. Since 2026-10-13 the tests
put a shim directory (`libsane.so.1` -> the built plugin) first on the
probe's rpath, so they test the build they were pointed at under either
SONAME.

## Verification of this export (v8, 2026-10-15)

Done offline, with no scanner contact. Results apply to the v8 build.

| check | result |
|---|---|
| Standalone build in a worktree on `59d255866` (`autogen`, `configure --sysconfdir=/etc`, `lib`, `sanei`, `backend/libsane-genesys.la`, `-j8`) | exit 0, **0 compiler warnings** |
| exported symbols | **105** mentioning `gl126` (v7: 111; the six removed are the process-lock functions) |
| `tests/test_sane_open_params.py` against that build | PASS, 7 passed |
| `tests/test_sane_calibration_cache.py` against that build | PASS, 6 passed |
| `tests/test_sane_magazine.py` against that build | PASS, 42 passed |
| `tests/test_sane_mark.py` (standalone build of `gl126_mark.cpp`; replaces `test_sane_lock.py`) | PASS, 7 passed, includes: mark readable/writable while the Python driver's `ProcessLock` is held |
| upstream `genesys_unit_tests` (`make -C testsuite/backend/genesys check`) | PASS (1/1) |
| `tools/release_check.py`, development build in `~/Dokument/Github/sane-backends` | FULL VERIFICATION PASSED, 393 tests, 0 skipped |
| Route A: fetch from the bundle in a clean clone | tip `94628f3f…`, tree `ab97f45f…`, identical |
| Route B: `git am` of the five patches onto `59d255866` in a clean clone | tree `ab97f45f…`, identical (tip `86a3af75…`) |
| upstream `.gitlab-ci.yml`; local replay of its jobs (podman, the project CI images) on a clean clone of the tip | `make-dist` replay exit 0; `fedora-39-clang` replay (clang 17.0.1, `-Werror`) `BUILD_EXIT=0`, `genesys_unit_tests` PASS, after which `make check` stops at `testsuite/backend/escl_test` (`escl.h: requires libcurl, libavahi and libxml2`, `CHECK_EXIT=2`) — the same stop as on v6 and v7, which is the local environment's, not the series' |
| symlinks in the recreated trees | 0, both routes |
| skipped | none |

Not run: `tstbackend` (any level), `scanimage`, and anything that touches
the scanner.

## What was pushed, and from where

Nothing from v8 is pushed. This package is built and exported locally and
committed to `cgillinger/opticfilm135i-linux` (a local branch). Nothing is
pushed to the SANE project's repository from the development clone, which
tracks the real upstream, and no automated session pushed anything to
GitLab.
