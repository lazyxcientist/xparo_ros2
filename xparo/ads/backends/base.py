class DisplayBackend:
    """What AdManager needs from a player.

    plays_itself = True: AdManager decides what plays when and calls
    play(item, placement, on_finished) -- on_finished(status, error=None)
    must be called exactly once per play, with status one of completed /
    dismissed (closed by a viewer) / interrupted / error.

    plays_itself = False: the backend runs the whole schedule on its own
    (XP-shell); AdManager calls schedule_changed(items) and the backend
    reports plays through manager.record_external_play(record).
    """
    name = "base"
    plays_itself = True
    available = True

    def start(self, manager):
        self.manager = manager

    def play(self, item, placement, on_finished):
        raise NotImplementedError

    def schedule_changed(self, items):
        pass

    def stop_missing(self, live_ids):
        """Stop anything playing whose id is no longer in the schedule."""

    def stop_all(self):
        pass

    def close(self):
        pass


class OffBackend(DisplayBackend):
    name = "off"
    available = False

    def play(self, item, placement, on_finished):
        on_finished("error", "ads are turned off on this robot")
