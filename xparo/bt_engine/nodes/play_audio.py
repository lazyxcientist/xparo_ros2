"""STUB -- no audio-playback subsystem exists in this repo yet.
# TODO(hardware): wire to a real audio-playback subsystem (topic/service/
# action -- whichever this robot's actual audio stack expects).
See nodes/base.py's StubActionNode docstring for the honest-scoping
rationale shared by every stub in this package.
"""
from .base import StubActionNode


class PlayAudioNode(StubActionNode):
    TAG = "PlayAudio"
    REQUIRED_ATTRS = ("file_path",)
