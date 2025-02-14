#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2008-present MagicStack Inc. and the EdgeDB authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


"""Query scope tree implementation."""

from __future__ import annotations

import textwrap
from typing import *  # NoQA
import weakref

from . import pathid


class InvalidScopeConfiguration(Exception):
    def __init__(self, msg: str, *,
                 offending_node: ScopeTreeNode,
                 existing_node: ScopeTreeNode) -> None:
        super().__init__(msg)
        self.offending_node = offending_node
        self.existing_node = existing_node


class ScopeTreeNode:
    unique_id: Optional[int]
    """A unique identifier used to map scopes on sets."""

    path_id: Optional[pathid.PathId]
    """Node path id, or None for branch nodes."""

    fenced: bool
    """Whether the subtree represents a SET OF argument."""

    protect_parent: bool
    """Whether the subtree represents a scope that must not affect parents."""

    unnest_fence: bool
    """Prevent unnesting in parents."""

    optional: bool
    """Whether this node represents an optional path."""

    children: Set[ScopeTreeNode]
    """A set of child nodes."""

    namespaces: Set[str]
    """A set of namespaces used by paths in this branch.

    When a path node is pulled up from this branch,
    and its namespace matches anything in `namespaces`,
    the namespace will be stripped.  This is used to
    implement "semi-detached" semantics used by
    views declared in a WITH block."""

    def __init__(self, *, path_id: Optional[pathid.PathId]=None,
                 fenced: bool=False, unique_id: Optional[int]=None):
        self.unique_id = unique_id
        self.path_id = path_id
        self.fenced = fenced
        self.protect_parent = False
        self.unnest_fence = False
        self.optional = False
        self.children = set()
        self.namespaces = set()
        self._parent = None

    def __repr__(self):
        name = 'ScopeFenceNode' if self.fenced else 'ScopeTreeNode'
        return (f'<{name} {self.path_id!r} at {id(self):0x}>')

    def _copy(self, parent: ScopeTreeNode) -> ScopeTreeNode:
        cp = self.__class__(
            path_id=self.path_id,
            fenced=self.fenced)
        cp.optional = self.optional
        cp.unnest_fence = self.unnest_fence
        cp.namespaces = set(self.namespaces)
        cp.unique_id = self.unique_id
        cp._set_parent(parent)

        for child in self.children:
            child._copy(parent=cp)

        return cp

    @property
    def name(self):
        return self._name(debug=False)

    def _name(self, debug):
        if self.path_id is None:
            return f'FENCE' if self.fenced else f'BRANCH'
        else:
            pid = self.path_id.pformat_internal(debug=debug)
            return f'{pid}{" [OPT]" if self.optional else ""}'

    def debugname(self, fuller=False):
        parts = [f'{self._name(debug=fuller)}']
        if self.unique_id:
            parts.append(f'uid:{self.unique_id}')
        if self.namespaces:
            parts.append(','.join(self.namespaces))
        if self.unnest_fence:
            parts.append('no-unnest')
        parts.append(f'0x{id(self):0x}')
        return ' '.join(parts)

    @property
    def ancestors(self) -> Iterator[ScopeTreeNode]:
        """An iterator of node's ancestors, including self."""
        node = self
        while node is not None:
            yield node
            node = node.parent

    @property
    def strict_ancestors(self) -> Iterator[ScopeTreeNode]:
        """An iterator of node's ancestors, not including self."""
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    @property
    def ancestors_and_namespaces(self) \
            -> Iterator[Tuple[ScopeTreeNode, FrozenSet[str]]]:
        """An iterator of node's ancestors and namespaces, including self."""
        namespaces = frozenset()
        node = self
        while node is not None:
            namespaces |= node.namespaces
            yield node, namespaces
            node = node.parent

    @property
    def path_children(self) -> Iterator[ScopeTreeNode]:
        """An iterator of node's children that have path ids."""
        return filter(lambda p: p.path_id is not None, self.children)

    def get_all_paths(self):
        paths = set()

        if self.path_id:
            paths.add(self.path_id)
        else:
            paths.update(p.path_id for p in self.path_children)

        return paths

    @property
    def descendants(self) -> Iterator[ScopeTreeNode]:
        """An iterator of node's descendants including self top-first."""
        yield self
        yield from self.strict_descendants

    @property
    def strict_descendants(self) -> Iterator[ScopeTreeNode]:
        """An iterator of node's descendants not including self top-first."""
        for child in tuple(self.children):
            yield child
            yield from child.strict_descendants

    @property
    def strict_descendants_and_namespaces(self) \
            -> Iterator[Tuple[ScopeTreeNode, FrozenSet[str]]]:
        """An iterator of node's descendants and namespaces.

        Does not include self. Top-first.
        """
        for child in tuple(self.children):
            yield child, child.namespaces
            desc_ns = child.strict_descendants_and_namespaces
            for desc, desc_namespaces in desc_ns:
                yield desc, child.namespaces | desc_namespaces

    @property
    def path_descendants(self) -> Iterator[ScopeTreeNode]:
        """An iterator of node's descendants that have path ids."""
        return filter(lambda p: p.path_id is not None, self.descendants)

    def get_all_path_nodes(self, *, include_subpaths: bool=True):  # XXX
        return list(self.path_descendants)

    @property
    def descendant_namespaces(self) -> Set[str]:
        """An set of namespaces declared by descendants."""
        namespaces = set()
        for child in self.descendants:
            namespaces.update(child.namespaces)

        return namespaces

    @property
    def unfenced_descendants(self) -> Iterator[ScopeTreeNode]:
        """An iterator of node's unfenced descendants including self."""
        yield self
        for child in tuple(self.children):
            if not child.fenced:
                yield from child.unfenced_descendants

    @property
    def strict_unfenced_descendants(self) -> Iterator[ScopeTreeNode]:
        """An iterator of node's unfenced descendants."""
        for child in tuple(self.children):
            if not child.fenced:
                yield from child.unfenced_descendants

    @property
    def fence(self) -> ScopeTreeNode:
        """The nearest ancestor fence (or self, if fence)."""
        if self.fenced:
            return self
        else:
            return self.parent_fence

    @property
    def parent(self) -> Optional[ScopeTreeNode]:
        """The parent node."""
        if self._parent is None:
            return None
        else:
            return self._parent()

    @property
    def parent_fence(self) -> Optional[ScopeTreeNode]:
        """The nearest strict ancestor fence."""
        for ancestor in self.strict_ancestors:
            if ancestor.fenced:
                return ancestor

        return None

    @property
    def root(self) -> ScopeTreeNode:
        """The root of this tree."""
        node = self
        while node.parent is not None:
            node = node.parent
        return node

    def attach_child(self, node: ScopeTreeNode) -> None:
        """Attach a child node to this node.

        This is a low-level operation, no tree validation is
        performed.  For safe tree modification, use attach_subtree()""
        """
        if node.path_id is not None:
            for child in self.children:
                if child.path_id == node.path_id:
                    raise InvalidScopeConfiguration(
                        f'{node.path_id} is already present in {self!r}')

        if node.unique_id is not None:
            for child in self.children:
                if child.unique_id == node.unique_id:
                    return

        node._set_parent(self)

    def attach_fence(self) -> ScopeTreeNode:
        """Create and attach an empty fenced node."""
        fence = ScopeTreeNode(fenced=True)
        self.attach_child(fence)
        return fence

    def attach_branch(self) -> ScopeTreeNode:
        """Create and attach an empty branch node."""
        fence = ScopeTreeNode()
        self.attach_child(fence)
        return fence

    def attach_path(self, path_id: pathid.PathId) -> None:
        """Attach a scope subtree representing *path_id*."""

        subtree = parent = ScopeTreeNode(fenced=True)
        is_lprop = False

        for prefix in reversed(list(path_id.iter_prefixes(include_ptr=True))):
            if prefix.is_ptr_path():
                is_lprop = True
                continue

            new_child = ScopeTreeNode(path_id=prefix)
            parent.attach_child(new_child)

            if not (is_lprop or prefix.is_linkprop_path()):
                parent = new_child

            is_lprop = False

        self.attach_subtree(subtree)

    def attach_subtree(self, node: ScopeTreeNode) -> None:
        """Attach a subtree to this node.

        *node* is expected to be a balanced scope tree and may be modified
        by this function.

        If *node* is not a path node (path_id is None), it is discared,
        and it's descendants are attached directly.  The tree balance is
        maintained.
        """
        if node.path_id is not None:
            # Wrap path node
            wrapper_node = ScopeTreeNode(fenced=True)
            wrapper_node.attach_child(node)
            node = wrapper_node

        dns = node.descendant_namespaces

        for descendant in node.path_descendants:
            path_id = descendant.path_id.strip_namespace(dns)
            visible = self.find_visible(path_id)
            if visible is not None:
                # This path is already present in the tree, discard,
                # but keep its OPTIONAL status, if any.
                descendant.remove()
                if descendant.optional:
                    visible.optional = True

            elif descendant.parent_fence is node:
                # Unfenced path.
                # First, find any existing descendant with the same path_id.
                # If not found, find any _unfenced_ node that is a child of
                # any of our ancestors.
                # If found, attach the node directly to its parent fence
                # and remove all other occurrences.
                existing, existing_ns = self.find_descendant_and_ns(path_id)
                unnest_fence = False
                parent_fence = None
                if existing is None:
                    existing, unnest_fence = self.find_unfenced(path_id)
                    if existing is not None:
                        parent_fence = existing.parent_fence
                else:
                    parent_fence = self.fence

                if existing is not None:
                    if parent_fence.find_child(path_id) is None:
                        if (unnest_fence
                                and parent_fence.find_child(
                                    path_id, in_branches=True) is None):
                            if descendant.parent.path_id:
                                offending_node = descendant.parent
                            else:
                                offending_node = descendant
                            raise InvalidScopeConfiguration(
                                f'reference to '
                                f'{offending_node.path_id.pformat()!r} '
                                f'changes the interpretation of '
                                f'{existing.path_id.pformat()!r} '
                                f'elsewhere in the query',
                                offending_node=offending_node,
                                existing_node=existing
                            )

                        parent_fence.remove_descendants(path_id)
                        existing.path_id = existing.path_id.strip_namespace(
                            existing_ns)
                        parent_fence.attach_child(existing)

                    # Discard the node from the subtree being attached.
                    existing.fuse_subtree(descendant)

        for descendant in tuple(node.children):
            # Attach whatever is remaining in the subtree.
            for pd in descendant.path_descendants:
                if pd.path_id.namespace:
                    to_strip = set(pd.path_id.namespace) & node.namespaces
                    pd.path_id = pd.path_id.strip_namespace(to_strip)

            self.attach_child(descendant)

    def fuse_subtree(self, node):
        node.remove()

        if node.path_id is not None:
            if node.optional:
                self.optional = True
            subtree = ScopeTreeNode(fenced=True)
            for child in tuple(node.children):
                subtree.attach_child(child)
        else:
            subtree = node

        self.attach_subtree(subtree)

    def remove_subtree(self, node):
        """Remove the given subtree from this node."""
        if node not in self.children:
            raise KeyError(f'{node} is not a child of {self}')

        node._set_parent(None)

    def remove_descendants(self, path_id: pathid.PathId) -> None:
        """Remove all descendant nodes matching *path_id*."""

        matching = set()

        for node in self.descendants:
            if _paths_equal_to_shortest_ns(node.path_id, path_id):
                matching.add(node)

        for node in matching:
            node.remove()

    def mark_as_optional(self, path_id: pathid.PathId) -> None:
        """Indicate that *path_id* is used as an OPTIONAL argument."""
        node = self.find_visible(path_id)
        if node is not None:
            node.optional = True

    def is_optional(self, path_id) -> bool:
        node = self.find_visible(path_id)
        if node is not None:
            return node.optional
        else:
            return False

    def add_namespaces(self, namespaces):
        # Make sure we don't add namespaces that already appear
        # in on of the ancestors.
        namespaces = frozenset(namespaces) - self.get_effective_namespaces()
        self.namespaces.update(namespaces)

    def get_effective_namespaces(self):
        namespaces = set()

        for _node, ans in self.ancestors_and_namespaces:
            namespaces |= ans

        return namespaces

    def remove(self):
        """Remove this node from the tree (subtree becomes independent)."""
        parent = self.parent
        if parent is not None:
            parent.remove_subtree(self)

    def collapse(self):
        """Remove the node, reattaching the children to the parent."""
        parent = self.parent
        if parent is None:
            raise ValueError('cannot collapse the root node')

        if self.path_id is not None:
            subtree = ScopeTreeNode()

            for child in self.children:
                subtree.attach_child(child)
        else:
            subtree = self

        self.remove()
        parent.attach_subtree(subtree)

    def unfence(self):
        """Remove the node, reattaching the children as an unfenced branch."""
        parent = self.parent
        if parent is None:
            raise ValueError('cannot unfence the root node')

        subtree = ScopeTreeNode()

        for child in list(self.children):
            subtree.attach_child(child)

        self.remove()

        parent_subtree = ScopeTreeNode(fenced=True)
        parent_subtree.attach_child(subtree)

        parent.attach_subtree(parent_subtree)

        return subtree

    def is_empty(self):
        if self.path_id is not None:
            return False
        else:
            return (
                not self.children or
                all(c.is_empty() for c in self.children)
            )

    def get_all_visible(self) -> Set[pathid.PathId]:
        paths = set()

        for node in self.ancestors:
            if node.path_id:
                paths.add(node.path_id)
            else:
                for c in node.children:
                    if c.path_id:
                        paths.add(c.path_id)

        return paths

    def find_visible(self, path_id: pathid.PathId) \
            -> Optional[ScopeTreeNode]:
        """Find the visible node with the given *path_id*."""
        namespaces = set()

        for node, ans in self.ancestors_and_namespaces:
            if _paths_equal(node.path_id, path_id, namespaces):
                return node

            for child in node.children:
                if _paths_equal(child.path_id, path_id, namespaces):
                    return child

            namespaces |= ans

        return None

    def is_visible(self, path_id: pathid.PathId) -> bool:
        return self.find_visible(path_id) is not None

    def is_any_prefix_visible(self, path_id: pathid.PathId) -> bool:
        for prefix in reversed(list(path_id.iter_prefixes())):
            if self.find_visible(prefix) is not None:
                return True

        return False

    def find_child(self, path_id: pathid.PathId, in_branches: bool = False) \
            -> Optional[ScopeTreeNode]:
        for child in self.children:
            if child.path_id == path_id:
                return child
            if in_branches and child.path_id is None and not child.fenced:
                desc = child.find_child(path_id, in_branches=True)
                if desc is not None:
                    return desc

        return None

    def find_descendant(self, path_id: pathid.PathId) \
            -> Optional[ScopeTreeNode]:
        for descendant, dns in self.strict_descendants_and_namespaces:
            if _paths_equal(descendant.path_id, path_id, dns):
                return descendant

        return None

    def find_descendant_and_ns(self, path_id: pathid.PathId) \
            -> Tuple[
                Optional[ScopeTreeNode],
                FrozenSet[str]]:
        for descendant, dns in self.strict_descendants_and_namespaces:
            if _paths_equal(descendant.path_id, path_id, dns):
                return descendant, dns

        return None, frozenset()

    def find_unfenced(self, path_id: pathid.PathId) \
            -> Tuple[Optional[ScopeTreeNode], bool]:
        """Find the unfenced node with the given *path_id*."""
        namespaces = set()
        unnest_fence_seen = False

        for node, ans in self.ancestors_and_namespaces:
            for descendant in node.unfenced_descendants:
                if _paths_equal(descendant.path_id, path_id, namespaces):
                    return descendant, unnest_fence_seen

            namespaces |= ans
            unnest_fence_seen = unnest_fence_seen or node.unnest_fence

        return None, unnest_fence_seen

    def find_by_unique_id(self, unique_id: int) \
            -> Optional[ScopeTreeNode]:
        for node in self.descendants:
            if node.unique_id == unique_id:
                return node

        return None

    def copy(self) -> ScopeTreeNode:
        """Return a complete copy of this subtree."""
        return self._copy(parent=None)

    def pformat(self):
        if self.children:
            child_formats = []
            for c in self.children:
                cf = c.pformat()
                if cf:
                    child_formats.append(cf)

            if child_formats:
                child_formats = sorted(child_formats)
                children = textwrap.indent(',\n'.join(child_formats), '    ')
                return f'"{self.name}": {{\n{children}\n}}'

        if self.path_id is not None:
            return f'"{self.name}"'
        else:
            return ''

    def pdebugformat(self, fuller=False):
        if self.children:
            child_formats = []
            for c in self.children:
                cf = c.pdebugformat()
                if cf:
                    child_formats.append(cf)

            child_formats = sorted(child_formats)
            children = textwrap.indent(',\n'.join(child_formats), '    ')
            return f'"{self.debugname(fuller=fuller)}": {{\n{children}\n}}'
        else:
            return f'"{self.debugname(fuller=fuller)}"'

    def _set_parent(self, parent):
        current_parent = self.parent
        if parent is current_parent:
            return

        if current_parent is not None:
            # Make sure no other node refers to us.
            current_parent.children.remove(self)

        if parent is not None:
            self._parent = weakref.ref(parent)
            parent.children.add(self)
        else:
            self._parent = None


def _paths_equal(path_id_1: pathid.PathId, path_id_2: pathid.PathId,
                 namespaces: Set[str]) -> bool:
    if path_id_1 is None or path_id_2 is None:
        return False

    if namespaces:
        path_id_1 = path_id_1.strip_namespace(namespaces)
        path_id_2 = path_id_2.strip_namespace(namespaces)

    return path_id_1 == path_id_2


def _paths_equal_to_shortest_ns(path_id_1: pathid.PathId,
                                path_id_2: pathid.PathId) -> bool:
    if path_id_1 is None or path_id_2 is None:
        return False

    ns1 = path_id_1.namespace or set()
    ns2 = path_id_2.namespace or set()

    if not ns1 and not ns2:
        return path_id_1 == path_id_2
    else:
        extra_in_1 = ns1 - ns2
        extra_in_2 = ns2 - ns1

        if extra_in_1 and extra_in_2:
            # neither namespace is a proper subset of another
            return False
        else:
            path_id_1 = path_id_1.replace_namespace(None)
            path_id_2 = path_id_2.replace_namespace(None)

            return path_id_1 == path_id_2
