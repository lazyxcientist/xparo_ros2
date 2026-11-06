"""xparo/task_call.py: the robot's AI task call offer -- Call now, Cancel,
and the 15 s auto-call, the same as the dashboard's Tasks chat card."""
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


def test_timeout_calls_it():
    offer, runs, published, timers = _offer()
    offer.offer(CALL)
    timers[0].fire()
    assert runs == [("t1", {"room": "4"})]
    assert published[-1]["status"] == "called"


def test_new_offer_replaces_the_old_one():
    offer, runs, published, timers = _offer()
    offer.offer(CALL)
    offer.offer({**CALL, "task_id": "t2"})
    timers[0].fire()
    assert runs == []
    offer.confirm()
    assert runs == [("t2", {"room": "4"})]
