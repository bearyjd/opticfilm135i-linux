/* Tiny standalone probe for sane/gl126_mark.{h,cpp} -- used only by
   tests/test_sane_mark.py. Not part of the SANE backend build.

   Usage:
     probe mark-path         -- print the effective mark path.
     probe mark-write <key>  -- write a Released mark: WROTE (0) / FAILED (1).
     probe mark-read         -- Released-only read: "KEY <key>" (0) or NONE (1).
     probe mark-clear        -- remove the mark: CLEARED.
     probe mark-write-kind <released|ejected|loaded|failed> <key>
     probe mark-read-kind    -- "KIND <kind> KEY <key>" (0) or NONE (1).

   The mark path comes from $SANE_GL126_MAGAZINE_MARK, as in the backend.
*/

#include "../sane/gl126_mark.h"

#include <cstdio>
#include <cstring>
#include <iostream>
#include <string>

using genesys::gl126::magazine_mark_clear;
using genesys::gl126::magazine_mark_path;
using genesys::gl126::magazine_mark_read;
using genesys::gl126::magazine_mark_write;

namespace {

// ------------------------------------------------ the magazine mark (WP-4)
//
// The "a release is pending" fact
// that has to survive a process, because `scanimage` loads in two
// invocations. Exercised here rather than through the built backend, so
// the file format and the ignore/clear rules are covered without needing
// a compiled sane-backends tree.

int do_mark_path()
{
    std::cout << magazine_mark_path() << std::endl;
    return 0;
}

int do_mark_write(const char* key)
{
    bool ok = magazine_mark_write(key);
    std::cout << (ok ? "WROTE" : "FAILED") << std::endl;
    return ok ? 0 : 1;
}

int do_mark_read()
{
    std::string key;
    if (!magazine_mark_read(&key)) {
        std::cout << "NONE" << std::endl;
        return 1;
    }
    std::cout << "KEY " << key << std::endl;
    return 0;
}

// WP-5 review finding E: the Loaded and Failed kinds (added alongside
// Released/Ejected for WP-5's one-button load and its own failure
// persistence) go through the SAME two-argument functions Ejected
// already used -- these two commands are the minimal addition needed to
// exercise them from this probe, without touching the one-argument
// commands above (mark-write/mark-read stay Released-only, unmodified,
// exactly as docs/sane-wp4-magazine.md section 10.1 requires).
int do_mark_write_kind(const char* kind_name, const char* key)
{
    genesys::gl126::MagazineMarkKind kind;
    if (std::strcmp(kind_name, "released") == 0) {
        kind = genesys::gl126::MagazineMarkKind::Released;
    } else if (std::strcmp(kind_name, "ejected") == 0) {
        kind = genesys::gl126::MagazineMarkKind::Ejected;
    } else if (std::strcmp(kind_name, "loaded") == 0) {
        kind = genesys::gl126::MagazineMarkKind::Loaded;
    } else if (std::strcmp(kind_name, "failed") == 0) {
        kind = genesys::gl126::MagazineMarkKind::Failed;
    } else {
        std::cerr << "unknown kind: " << kind_name << std::endl;
        return 2;
    }
    bool ok = genesys::gl126::magazine_mark_write(kind, key);
    std::cout << (ok ? "WROTE" : "FAILED") << std::endl;
    return ok ? 0 : 1;
}

int do_mark_read_kind()
{
    genesys::gl126::MagazineMarkKind kind;
    std::string key;
    if (!genesys::gl126::magazine_mark_read(&kind, &key)) {
        std::cout << "NONE" << std::endl;
        return 1;
    }
    std::cout << "KIND " << genesys::gl126::magazine_mark_kind_name(kind) << " KEY " << key
              << std::endl;
    return 0;
}

int do_mark_clear()
{
    magazine_mark_clear();
    std::cout << "CLEARED" << std::endl;
    return 0;
}

} // namespace

int main(int argc, char** argv)
{
    if (argc == 3 && std::strcmp(argv[1], "mark-write") == 0) {
        return do_mark_write(argv[2]);
    }
    if (argc == 4 && std::strcmp(argv[1], "mark-write-kind") == 0) {
        return do_mark_write_kind(argv[2], argv[3]);
    }
    if (argc == 2 && std::strcmp(argv[1], "mark-read-kind") == 0) {
        return do_mark_read_kind();
    }
    if (argc != 2) {
        std::cerr << "usage: " << argv[0]
                   << " mark-path|"
                      "mark-write <key>|mark-read|mark-clear|"
                      "mark-write-kind <released|ejected|loaded|failed> <key>|"
                      "mark-read-kind" << std::endl;
        return 2;
    }

    try {
        if (std::strcmp(argv[1], "mark-path") == 0) {
            return do_mark_path();
        }
        if (std::strcmp(argv[1], "mark-read") == 0) {
            return do_mark_read();
        }
        if (std::strcmp(argv[1], "mark-clear") == 0) {
            return do_mark_clear();
        }
    } catch (const std::exception& e) {
        std::cerr << "ERROR " << e.what() << std::endl;
        return 2;
    }

    std::cerr << "usage: " << argv[0]
               << " mark-path|mark-write <key>|mark-read|mark-clear" << std::endl;
    return 2;
}
