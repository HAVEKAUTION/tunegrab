"""The common base for errors TuneGrab expects and explains.

``str(exc)`` of a :class:`TuneGrabError` is a sentence a user can read, so the CLI prints it
and the web app returns it as the response ``detail`` without a stack trace. Anything that
isn't a ``TuneGrabError`` is a bug and gets the generic "something unexpected" message.
"""

from __future__ import annotations


class TuneGrabError(Exception):
    """An expected failure with a message for the user."""
