/* sane - Scanner Access Now Easy.

   Copyright (C) 2026 Christian Gillinger

   This file is part of the SANE package.

   This program is free software; you can redistribute it and/or
   modify it under the terms of the GNU General Public License as
   published by the Free Software Foundation; either version 2 of the
   License, or (at your option) any later version.

   This program is distributed in the hope that it will be useful, but
   WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
   General Public License for more details.

   You should have received a copy of the GNU General Public License
   along with this program.  If not, see <https://www.gnu.org/licenses/>.
*/

/* GL126 magazine mark: a small state file that lets a magazine load span
   two processes.

   The magazine flow can span two steps with the operator in between: the
   "Load film" option releases the magazine, the person takes it out and
   reseats it to the stop, and the load itself waits for that reseat before
   running. Inside one frontend that keeps the device open (digiKam) a fact
   like "released" can live in memory; `scanimage` cannot, because each
   invocation is a fresh process. The mark is therefore also written to a
   file. It is only a hint: the hardware is re-verified before every motor
   move (see the comment at the magazine mark API below), so a stale or
   missing mark can never authorise anything, the worst case is asking the
   operator to reseat the magazine.

   Path: `/tmp/sane-gl126-07b3-1436.magazine`, or the file named by the
   environment variable `SANE_GL126_MAGAZINE_MARK` when it is set (a test
   facility, so a test run can use a private path; it is not meant for
   production use). There is only one such unit per host, so the path is
   not per-device; the device name is stored inside the file.

   File handling: the path is deliberately predictable and lives in a
   directory (/tmp) any local user can write to, and the file is created
   mode 0666, because scanimage, saned and a desktop frontend may run as
   different users and still need to see the same mark. That combination
   (fixed path, shared directory, open permissions) is exactly the setup a
   symlink, hard-link, or FIFO/device-node attack targets, so every open()
   of the mark passes:
     - O_NOFOLLOW, so a symlink planted at the path fails the open()
       itself with ELOOP rather than being followed onto whatever it
       points at;
     - O_NONBLOCK, so the open() itself can never block: a FIFO (or
       certain device nodes) dropped at the path would otherwise hang
       an O_RDONLY open with no writer attached, before any later check
       ever runs; it has no effect on a regular file's later read()/
       write();
   and the resulting fd is then checked to be a regular file with
   exactly one hard link (S_ISREG && st_nlink == 1) before anything is
   read, refused otherwise (magazine_mark_read() returns false, since it
   runs before every load and must never fail the session). That check
   catches what O_NOFOLLOW/O_NONBLOCK do not: a directory (an O_RDONLY
   open of a directory succeeds), a device node, or a hard link onto some
   other file this process can write to (not a symlink at all: st_nlink
   climbs to 2 or more, which a mark this code created never has). The
   mark is additionally never modified in place: magazine_mark_write()
   writes a private temp file next to it and rename()s that over the mark
   path, so the write can never land inside whatever the mark path used to
   point to. A mark that cannot be written or read is by design not a
   failure.

   What this is not: a security boundary against a privileged or same-user
   attacker who can also write to /tmp at will. It is a hint, hardened
   against the ordinary local hazards of that spot (stale symlinks, FIFOs,
   hard links) rather than against a determined adversary who races the
   open() or has broader filesystem control.

   Deliberately free of genesys headers (no genesys.h, no Genesys_Device)
   so it can be compiled and exercised standalone, without pulling in the
   rest of the backend, using a small probe program that links only this
   file. */

#ifndef BACKEND_GENESYS_GL126_MARK_H
#define BACKEND_GENESYS_GL126_MARK_H

#include <string>

namespace genesys {
namespace gl126 {

// ------------------------------------------------ the magazine mark

/* The magazine flow can span two steps with the operator in between: the
   "Load film" option releases the magazine (the vendor's jog), the
   person takes it out and reseats it to the stop, and the load itself
   waits for that reseat before running. Inside one frontend that holds
   the device open (digiKam) a fact like "released" can live in memory;
   `scanimage` cannot, each invocation is a new process, and a load
   without the jog before it, in the same power cycle, fails.

   So the fact is also written to the mark file described at the top of
   this header, holding the device it applies to. It is never
   trusted on its own: before a load the backend re-reads the hardware
   (reg 0x01 idle-homed, the loader-sensor bit set, the device-open
   register state), and a power cycle both re-enumerates the unit under a
   new address and leaves reg 0x01 cold, so a stale mark cannot authorise
   anything. The mark is a hint that survives a process, not a state.

   The mark carries one of four kinds. "released" (the original: Load
   film's jog already ran, only the bare "load" program is needed) and
   "ejected" (an eject completed; the next scan must also replay the
   device-open table first, since nothing has written it since) are both
   read from the mark's first word, which used to be the constant
   "released" and is now whichever name applies, so an old-format mark
   still reads back byte-identically to a new one.

   A third kind, "loaded", covers the case where "Load film" runs the
   whole flow (release, wait for the reseat edge, and LOAD) in one button
   press: a load can complete in a process that then exits
   (`scanimage -n --load-film`) before the scan that uses it runs in a
   separate process, so a cross-process "the magazine is loaded" fact is
   needed the same way "a release/eject is pending" already is.
   `load_document()` never runs LOAD itself: it only checks this mark (or
   the in-process state) to decide whether to let a scan through.

   A fourth kind, "failed", covers a magazine sequence that fails and
   leaves the transport in a state nobody can name: that fact must not be
   dropped the moment the failing process exits, or a following
   `scanimage` invocation would have no way to know the previous one had
   failed, and would try a scan (or another magazine action) against a
   transport whose state was never established. `MagazineFailGuard`
   (sane/gl126.cpp) writes this mark on any failure instead of clearing
   whatever was there; every entry point that already refuses on an
   in-process Failed state (Load film, Eject film, load_document())
   refuses the same way on a matching `failed` mark from Unknown. Only a
   cold reg 0x01 read clears it, the one event that actually
   re-establishes a known transport state (a power cycle), checked at
   Load film's own start and by Check status. */

/** The four things a magazine mark can mean. */
enum class MagazineMarkKind { Released, Ejected, Loaded, Failed };

/** "released", "ejected", "loaded" or "failed", also the word the mark
    file leads with. */
const char* magazine_mark_kind_name(MagazineMarkKind kind);

/** Path of the magazine mark: $SANE_GL126_MAGAZINE_MARK if set and
    non-empty, else "/tmp/sane-gl126-07b3-1436.magazine". */
std::string magazine_mark_path();

/** Record that `device_key` (the SANE device name, e.g.
    "libusb:001:007", it carries the USB address, which a power cycle
    changes) is waiting for a load of the given kind. Returns false if the
    file could not be written; a mark that cannot be written is not fatal
    (the in-process record still works for a frontend that stays open),
    so callers log and continue. */
bool magazine_mark_write(MagazineMarkKind kind, const std::string& device_key);

/** Back-compat convenience: writes a Released mark, byte-identical to
    what this function always produced before the Ejected kind existed.
    Existing callers of this form must keep working unmodified. */
bool magazine_mark_write(const std::string& device_key);

/** The kind and device key of a pending mark, or false when there is none
    (or it could not be read). */
bool magazine_mark_read(MagazineMarkKind* kind, std::string* device_key);

/** Back-compat convenience: true only for a Released mark, what this
    function always meant before the Ejected kind existed. An Ejected
    mark reads as "no mark" through this overload, exactly as it would
    have before that kind existed (kept for the same callers as the
    write overload above). */
bool magazine_mark_read(std::string* device_key);

/** What magazine_mark_clear() actually did, so a caller that logs the
    outcome (sane/gl126.cpp's clear_magazine_mark()) can say which. */
enum class MagazineMarkClearResult { Removed, NonePresent, Error };

/** Remove the mark. Removing nothing (there was none) is not an error. */
MagazineMarkClearResult magazine_mark_clear();

} // namespace gl126
} // namespace genesys

#endif // BACKEND_GENESYS_GL126_MARK_H
