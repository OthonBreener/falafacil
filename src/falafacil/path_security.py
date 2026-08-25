"""Shared write-permission checks for installation and update validators."""

from __future__ import annotations

import grp
import os
import pwd
import stat


def _lookup_group_uids(gid: int) -> frozenset[int] | None:
    """Return every UID that can write through `gid`.

    Returns None when the membership cannot be determined; callers must treat
    that as unsafe.
    """
    try:
        entry = grp.getgrgid(gid)
    except (KeyError, OverflowError, OSError):
        return None

    uids: set[int] = set()
    for member in entry.gr_mem:
        try:
            uids.add(pwd.getpwnam(member).pw_uid)
        except (KeyError, OSError):
            return None

    try:
        uids.update(account.pw_uid for account in pwd.getpwall() if account.pw_gid == gid)
    except OSError:
        return None

    return frozenset(uids)


def has_foreign_write(st: os.stat_result) -> bool:
    """Report whether anyone other than the owner may write the inspected path.

    World-writable is always foreign. Group-writable is foreign only when the
    owning group grants write access to some UID other than the owner: a private
    per-user group, the default on Ubuntu and on every Homebrew prefix (`0o775`
    directories and `0o664` files under umask `002`), widens nothing beyond the
    owner. An undeterminable group membership fails closed.
    """
    if (st.st_mode & stat.S_IWOTH) != 0:
        return True
    if (st.st_mode & stat.S_IWGRP) == 0:
        return False

    group_uids = _lookup_group_uids(st.st_gid)
    if group_uids is None:
        return True
    return bool(group_uids - {st.st_uid})
