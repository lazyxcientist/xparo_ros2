"""STUB -- no text-to-speech subsystem exists in this repo yet.
# TODO(hardware): wire to a real TTS subsystem (topic/service/action --
# whichever this robot's actual speech stack expects).
See nodes/base.py's StubActionNode docstring for the honest-scoping
rationale shared by every stub in this package.
"""
from .base import StubActionNode


class SpeakTextNode(StubActionNode):
    TAG = "SpeakText"
    REQUIRED_ATTRS = ("text",)
