"""xparo/task_call.py: the robot's AI task call offer -- Call now, Cancel,
and the 15 s window, the same as the dashboard's Tasks chat card. Breakage
review #5: an unanswered offer used to run the AI's guess by itself."""
from xparo.task_call import TaskCallOffer


class FakeTimer:
    def __init__(self, interval, fn, args=()):
        self.interval, self.fn, self.args, self.cancelled = interval, fn, args, False
        self.daemon = False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.fn(*self.args)


def _offer():
    runs, published, timers = [], [], []

    def factory(*a, **kw):
        timers.append(FakeTimer(*a, **kw))
        return timers[-1]
    return TaskCallOffer(lambda tid, params: runs.append((tid, params)), published.append, factory), runs, published, timers


CALL = {"task_id": "t1", "title": "deliver", "params": {"room": "4"}, "timeout_s": 15}


def test_call_now_runs_the_task():
    offer, runs, published, timers = _offer()
    offer.offer(CALL)
    assert timers[0].interval == 15
    assert offer.confirm() is True
    assert runs == [("t1", {"room": "4"})]
    assert [p["status"] for p in published] == ["pending", "called"]
    assert timers[0].cancelled


def test_cancel_never_runs_it():
    offer, runs, published, timers = _offer()
    offer.offer(CALL)
    assert offer.cancel() is True
    timers[0].fire()  # a timer that fired anyway must not run it
    assert runs == []
    assert published[-1]["status"] == "cancelled"
    assert offer.confirm() is False


def test_no_answer_means_nothing_runs():
    offer, runs, published, timers = _offer()
    offer.offer(CALL)
    timers[0].fire()
    assert runs == []
    assert published[-1]["status"] == "expired"
    assert offer.confirm() is False          # too late to call it now


def test_cancel_after_call_stops_the_run_it_started():
    runs, published, timers, stopped = [], [], [], []

    def factory(*a, **kw):
        timers.append(FakeTimer(*a, **kw))
        return timers[-1]
    offer = TaskCallOffer(lambda tid, params: runs.append(tid), published.append, factory,
                          cancel_task=lambda tid: stopped.append(tid) or ["run1"])
    offer.offer(CALL)
    offer.confirm()
    assert offer.cancel() is True
    assert stopped == ["t1"]
    assert published[-1]["status"] == "stopped"
    assert offer.cancel() is False           # nothing left to stop


def test_new_offer_replaces_the_old_one():
    offer, runs, published, timers = _offer()
    offer.offer(CALL)
    offer.offer({**CALL, "task_id": "t2"})
    timers[0].fire()                          # the replaced offer's timer: ignored
    assert runs == []
    offer.confirm()
    assert runs == [("t2", {"room": "4"})]
