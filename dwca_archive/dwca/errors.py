"""Exception hierarchy for the DwC-A import module.

Two error families are distinguished on purpose:

* :class:`DescriptorError` -- the *archive descriptor* (``meta.xml``) or the
  archive's member layout is invalid.  Nothing can be imported; no database
  is created.
* :class:`ArchiveDataError` -- rows that fail while reading table data.
  Such rows are kept, with a reason, in the quarantine area of the database
  rather than dropped silently.
"""

from __future__ import annotations


class DwcaError(Exception):
    """Base class for every error raised by this package."""


class DescriptorError(DwcaError):
    """``meta.xml`` (or the archive member layout) violates the supported
    DwC-A subset.

    The import is rejected outright: no output database is created and no
    partially written member is left behind.
    """


class ArchiveDataError(DwcaError):
    """A fatal data-level failure that cannot be represented as a quarantined
    row (for example a member that cannot be decoded at all)."""
