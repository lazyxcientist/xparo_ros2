"""Behaviour Tree redesign Phase 9: parses the same fragment shape stored in the
Django side's Project_Dashboard.aiml/custom_aiml -- the inner content of a
<BehaviorTree ID="MainTree">...</BehaviorTree> block, typically a single
root element (e.g. one <Sequence>) but not guaranteed to be. Mirrors the
wrap-on-failure convention frontend/src/behaviour-tree/persistence/
DownloadButton.js already uses for the same reason (this repo and the
Django one are separate git repos, so the logic is independently
duplicated, not shared) and engine.py's get_local_files, which strips
exactly this fragment out of a full BT.CPP XML document on save.
"""
import re
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


# A synced tree is the body of one <BehaviorTree ID="MainTree">, wrapped for
# disk -- unless it already is a whole BTCPP document (a Groot2 save: <?xml?>
# prolog, several trees, TreeNodesModel). Wrapping that once more put the
# prolog on line 3 and broke the robot's own file on "Import into project"
# (MEG-12), so a document is written exactly as it is (a UTF-8 BOM, as
# some Windows editors save it, included).
_TREE_DOCUMENT_START = re.compile(r'^\ufeff?\s*(?:<\?xml[^>]*\?>\s*)?(?:<!--.*?-->\s*)*<root[\s>]', re.DOTALL)
_XPARO_TREE_WRAPPER = re.compile(
    r'^\s*(<root\b[^>]*>\s*)?<BehaviorTree\s+ID="MainTree"\s*>(.*)</BehaviorTree>\s*(?(1)</root>\s*)$', re.DOTALL)


def is_tree_document(text):
    return bool(_TREE_DOCUMENT_START.match(text or ''))


def tree_file_content(tree):
    """What a synced tree (aiml / custom_aiml value) is written to disk as."""
    if is_tree_document(tree):
        return tree
    return f'''<root BTCPP_format="4" main_tree_to_execute="MainTree">
<BehaviorTree ID="MainTree">
{tree}
</BehaviorTree>
</root>'''


def tree_from_file_content(content):
    """The inverse, for what is sent to the server: the body of a file that
    is XPARO's own one-tree wrapper; any other file (more trees, a
    TreeNodesModel, a prolog or comments) is the tree itself, whole --
    cutting out the first MainTree used to drop the rest of a Groot2 file."""
    match = _XPARO_TREE_WRAPPER.match(content)
    if match is None:
        return content
    body = match.group(2)
    if is_tree_document(body):
        return body.strip()  # a document wrapped once more before MEG-12's fix
    if '<BehaviorTree' in body:
        return content
    return body.strip()
