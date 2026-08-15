"""Behaviour Tree redesign Phase 12/13: import a custom action/condition
node from a path already on the robot's own filesystem (Phase 12), or one
authored inline in the BT editor and shipped to a project-scoped folder on
this same robot (Phase 13) -- both use this exact same loader, just
pointed at different directories with a different trust posture on the
Django side (Phase 13's inline-code path is opt-in + audit-logged; this
one is not, since it only ever points at code the robot operator already
put on disk themselves). No source code transits the network through this
mechanism -- only a path (or, for Phase 13, code that was already written
to a robot-local file by a *separate* sync step before this ever runs).
"""
import importlib
import importlib.util
import inspect
import os
import sys

import py_trees


class CustomBTNode(py_trees.behaviour.Behaviour):
    """Base class for every custom action/condition node loaded through
    this module. Subclasses MUST set XML_TAG (the tag a tree references
    it by -- e.g. XML_TAG = "MyCustomAction") and implement update() like
    any other py_trees Behaviour. Constructor matches nodes/base.py's
    StubActionNode (attrs/blackboard) plus ros_node, since a real plugin
    is exactly the kind of node likely to need live ROS access the way
    Phase 10's ParamSet does.
    """
    XML_TAG = None

    def __init__(self, name, attrs, blackboard, ros_node=None):
        super().__init__(name=name)
        self.attrs = attrs
        self.blackboard = blackboard
        self.ros_node = ros_node


def _load_module_from_file(path):
    # A unique-per-path module name -- two plugin files with the same
    # basename (e.g. two different projects each shipping their own
    # nodes.py) must not clobber each other in sys.modules.
    module_name = f"xparo_bt_plugin__{abs(hash(path))}__{os.path.splitext(os.path.basename(path))[0]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load a module spec from {path!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_module(path_or_dotted):
    """A real filesystem path (contains a path separator, or ends in
    .py) is loaded directly from that file; anything else is treated as
    an installed dotted module path (importlib.import_module) -- e.g. a
    plugin shipped as part of a real installed Python package rather than
    a loose file on this specific robot's disk.
    """
    if os.path.sep in path_or_dotted or path_or_dotted.endswith(".py"):
        return _load_module_from_file(path_or_dotted)
    return importlib.import_module(path_or_dotted)


def load_plugins(paths):
    """Returns {XML_TAG: class} for every CustomBTNode subclass found
    across every given path. A path that fails to load, or loads cleanly
    but defines no CustomBTNode subclass, contributes nothing -- never
    raises -- so one bad or irrelevant entry in a project's plugin list
    doesn't take down every other plugin.
    """
    registry = {}
    for path in paths:
        try:
            module = _load_module(path)
        except Exception as e:
            print(f"[bt_engine plugin_loader] failed to load {path!r}: {e}")
            continue
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj is CustomBTNode:
                continue
            if issubclass(obj, CustomBTNode) and obj.XML_TAG:
                registry[obj.XML_TAG] = obj
    return registry


def register_plugins(paths):
    """load_plugins() + wires each result into node_registry.NODE_REGISTRY
    using the same calling convention every other builder there already
    uses (see node_registry.py's own module docstring) -- callers outside
    this module only ever need this one function, not load_plugins
    directly (that's exposed mainly for the loader's own tests).
    """
    from .node_registry import NODE_REGISTRY

    loaded = load_plugins(paths)
    for tag, cls in loaded.items():
        NODE_REGISTRY[tag] = lambda name, attrs, blackboard, children, ros_node, cls=cls: cls(
            name=name, attrs=attrs, blackboard=blackboard, ros_node=ros_node,
        )
    return loaded
