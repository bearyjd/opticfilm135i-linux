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

#include "gl126_mark.h"

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <ctime>

namespace genesys {
namespace gl126 {

namespace {

std::string now_iso8601_utc()
{
    std::time_t t = std::time(nullptr);
    std::tm tm_buf{};
    gmtime_r(&t, &tm_buf);
    char buf[32];
    std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%S+00:00", &tm_buf);
    return std::string(buf);
}

} // namespace

// ------------------------------------------------ the magazine mark

std::string magazine_mark_path()
{
    const char* env = std::getenv("SANE_GL126_MAGAZINE_MARK");
    if (env != nullptr && env[0] != '\0') {
        return std::string(env);
    }
    return std::string("/tmp/sane-gl126-07b3-1436.magazine");
}

const char* magazine_mark_kind_name(MagazineMarkKind kind)
{
    switch (kind) {
    case MagazineMarkKind::Released: return "released";
    case MagazineMarkKind::Ejected:  return "ejected";
    case MagazineMarkKind::Loaded:   return "loaded";
    case MagazineMarkKind::Failed:   return "failed";
    }
    return "released";
}

bool magazine_mark_write(MagazineMarkKind kind, const std::string& device_key)
{
    // Written atomically via a temp file + rename(), never by truncating
    // whatever already sits at the mark path in place: a truncate-in-
    // place would follow a symlink planted at the mark path straight
    // into its target, and would leave a half-written file visible to a
    // concurrent reader. rename() replaces the mark path's directory
    // entry itself, including a symlink entry, without ever opening
    // or following what that entry used to point to.
    std::string mark_path = magazine_mark_path();
    std::string tmp_path = mark_path + ".tmp." + std::to_string(static_cast<long>(getpid()));

    // O_EXCL already refuses a pre-existing FIFO/device at the temp path
    // (the path is fresh, PID-suffixed); O_NONBLOCK is added for the same
    // uniformity as every other open() in this file, it costs nothing
    // here since O_EXCL guarantees this process created the file.
    int fd = ::open(tmp_path.c_str(),
                    O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC, 0666);
    if (fd < 0) {
        return false;
    }

    // The device key goes on its own line: it is whatever string the SANE
    // frontend uses to name the device, and it can contain spaces (the
    // backend's own test mode produces "test device:0x07b3:0x1436"), so a
    // space-delimited field would truncate it and silently look like a
    // mark for some other device. First line stays human-readable, since
    // `cat` on this file should say what it is, and its first word is
    // the kind ("released" or "ejected"), not a fixed constant any more.
    std::string line = std::string(magazine_mark_kind_name(kind)) + " " +
                       now_iso8601_utc() + " (sane genesys gl126)\n" + device_key + "\n";
    ssize_t written = ::write(fd, line.data(), line.size());
    ::close(fd);
    if (written != static_cast<ssize_t>(line.size())) {
        ::unlink(tmp_path.c_str());
        return false;
    }
    if (::rename(tmp_path.c_str(), mark_path.c_str()) != 0) {
        ::unlink(tmp_path.c_str());
        return false;
    }
    return true;
}

bool magazine_mark_write(const std::string& device_key)
{
    return magazine_mark_write(MagazineMarkKind::Released, device_key);
}

bool magazine_mark_read(MagazineMarkKind* kind, std::string* device_key)
{
    // O_NONBLOCK: this path is read unconditionally before every load, so
    // the open() itself must never block, a FIFO planted here (no
    // writer attached) would otherwise hang the open() before the
    // regular-file check below is ever reached. On the regular file this
    // function actually expects, O_NONBLOCK has no effect on the read().
    int fd = ::open(magazine_mark_path().c_str(),
                    O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0) {
        return false;
    }
    {
        struct stat st{};
        if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) || st.st_nlink != 1) {
            // Not a mark we wrote (symlink, directory, FIFO, hard link to
            // a victim file, ...): treat exactly like "no mark", this
            // function must never throw.
            ::close(fd);
            return false;
        }
    }
    char buf[256] = {0};
    ssize_t n = ::read(fd, buf, sizeof(buf) - 1);
    ::close(fd);
    if (n <= 0) {
        return false;
    }
    // Line 1: "<kind> <timestamp> (sane genesys gl126)", kind being the
    // first word, "released", "ejected" or "loaded". Line 2: the device
    // key, whole, spaces and all.
    std::string text(buf, static_cast<std::size_t>(n));
    std::size_t eol = text.find('\n');
    if (eol == std::string::npos) {
        return false;
    }
    std::string first_line = text.substr(0, eol);
    std::size_t space = first_line.find(' ');
    std::string word = space == std::string::npos ? first_line : first_line.substr(0, space);
    MagazineMarkKind found_kind;
    if (word == "released") {
        found_kind = MagazineMarkKind::Released;
    } else if (word == "ejected") {
        found_kind = MagazineMarkKind::Ejected;
    } else if (word == "loaded") {
        found_kind = MagazineMarkKind::Loaded;
    } else if (word == "failed") {
        found_kind = MagazineMarkKind::Failed;
    } else {
        // No known kind (garbage, or a future/older format): treat
        // exactly like "no mark", same as the old fixed "released" check.
        return false;
    }
    std::size_t key_end = text.find('\n', eol + 1);
    std::string key = text.substr(eol + 1,
                                  key_end == std::string::npos
                                      ? std::string::npos : key_end - eol - 1);
    if (key.empty()) {
        return false;
    }
    if (kind != nullptr) {
        *kind = found_kind;
    }
    if (device_key != nullptr) {
        *device_key = key;
    }
    return true;
}

bool magazine_mark_read(std::string* device_key)
{
    MagazineMarkKind kind = MagazineMarkKind::Released;
    if (!magazine_mark_read(&kind, device_key)) {
        return false;
    }
    // Back-compat: this overload only ever meant a Released mark, so an
    // Ejected one reads as "no mark" through it, exactly as it would
    // have before that kind existed.
    return kind == MagazineMarkKind::Released;
}

MagazineMarkClearResult magazine_mark_clear()
{
    if (::unlink(magazine_mark_path().c_str()) == 0) {
        return MagazineMarkClearResult::Removed;
    }
    if (errno == ENOENT) {
        return MagazineMarkClearResult::NonePresent;
    }
    return MagazineMarkClearResult::Error;
}

} // namespace gl126
} // namespace genesys
