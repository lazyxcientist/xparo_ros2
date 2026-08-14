"""Behaviour Tree redesign Phase 9 (see /home/scientist/.claude/plans/
breezy-splashing-koala.md): parses the same fragment shape stored in the
Django side's Project_Dashboard.aiml/custom_aiml -- the inner content of a
<BehaviorTree ID="MainTree">...</BehaviorTree> block, typically a single
root element (e.g. one <Sequence>) but not guaranteed to be. Mirrors the
wrap-on-failure convention frontend/src/behaviour-tree/persistence/
DownloadButton.js already uses for the same reason (this repo and the
Django one are separate git repos, so the logic is independently
duplicated, not shared) and engine.py's get_local_files, which strips
exactly this fragment out of a full BT.CPP XML document on save.
"""
import xml.etree.ElementTree as ET


class TreeParseError(Exception):
    pass


def parse_fragment(xml_fragment):
    """Returns the root ET.Element for a BT XML fragment. Comments are
    skipped by ElementTree's default parser, which is the behaviour this
    engine wants: a commented-out node's attributes (including any
    blackboard "{name}" references) must never affect execution or
    parameter resolution.
    """
    if not xml_fragment or not xml_fragment.strip():
        raise TreeParseError("empty behaviour tree fragment")
    try:
        return ET.fromstring(xml_fragment)
    except ET.ParseError:
        pass
    try:
        return ET.fromstring(f"<root>{xml_fragment}</root>")
    except ET.ParseError as e:
        raise TreeParseError(f"could not parse behaviour tree XML: {e}") from e


def single_root_child(root):
    """A wrapped <root> may have exactly one real child (the common case --
    a single top-level <Sequence>) or several (rare, but not invalid XML).
    tree_builder.py needs exactly one node to build from; if wrapping
    produced more than one, that's a genuinely ambiguous tree (BT.CPP
    itself requires a single root under <BehaviorTree>) -- fail loudly
    rather than silently picking the first one and dropping the rest.
    """
    if root.tag != "root":
        return root
    children = list(root)
    if len(children) != 1:
        raise TreeParseError(
            f"expected exactly one root node in the behaviour tree, found {len(children)}"
        )
    return children[0]
