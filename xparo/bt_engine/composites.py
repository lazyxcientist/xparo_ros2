"""Behaviour Tree redesign Phase 9: BT.CPP's <Parallel success_count="N"
failure_count="M"> has count-threshold semantics py_trees' own Parallel
doesn't support -- its tick() hardcodes isinstance checks against exactly
three built-in ParallelPolicy classes (SuccessOnAll/SuccessOnOne/
SuccessOnSelected, confirmed by reading py_trees.composites.Parallel.tick's
source directly) and raises RuntimeError on anything else, so a custom
ParallelPolicy subclass would not actually be consulted. Reusing Parallel's
child-management via subclassing and overriding tick() entirely is
simpler than duplicating Composite's bookkeeping from scratch.
"""
import py_trees
from py_trees import common


class CountingParallel(py_trees.composites.Parallel):
    def __init__(self, name, success_count, failure_count, children=None):
        super().__init__(name=name, policy=py_trees.common.ParallelPolicy.SuccessOnAll(), children=children)
        self.success_count = success_count
        self.failure_count = failure_count

    def tick(self):
        self.logger.debug(f"{self.__class__.__name__}.tick()")

        if self.status != common.Status.RUNNING:
            for child in self.children:
                if child.status != common.Status.INVALID:
                    child.stop(common.Status.INVALID)
            self.current_child = None
            self.initialise()

        if not self.children:
            self.current_child = None
            self.stop(common.Status.SUCCESS)
            yield self
            return

        for child in self.children:
            # Once a child has reached a final status, re-ticking it every
            # cycle would either restart it (SUCCESS/FAILURE reset back to
            # RUNNING on some behaviours) or just waste work -- freeze it,
            # matching Parallel's own synchronise=True behaviour.
            if child.status in (common.Status.SUCCESS, common.Status.FAILURE):
                continue
            yield from child.tick()

        successes = sum(1 for c in self.children if c.status == common.Status.SUCCESS)
        failures = sum(1 for c in self.children if c.status == common.Status.FAILURE)

        if failures >= self.failure_count:
            new_status = common.Status.FAILURE
        elif successes >= self.success_count:
            new_status = common.Status.SUCCESS
        else:
            new_status = common.Status.RUNNING

        self.current_child = self.children[-1] if self.children else None
        if new_status != common.Status.RUNNING:
            self.stop(new_status)
        self.status = new_status
        yield self
