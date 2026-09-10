"""Background jobs.

Any endpoint can be called with ``?async=true``. The call returns immediately
with a job id, the work continues in the background, and the caller polls until
the outcome is known.

The point is not merely to return early. For playback especially, Spotify's
``204`` means *the command was accepted*, which is not the same as *audio is
playing* -- the device may be asleep, or may have ignored it. A job is not
finished until the effect has been confirmed.
"""

from __future__ import annotations
