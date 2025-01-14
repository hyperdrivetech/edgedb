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


"""EdgeQL compiler path scope helpers."""


from __future__ import annotations

from typing import *  # NoQA

from edb import errors

from edb.edgeql import qltypes
from edb.ir import ast as irast

from edb.schema import name as s_name
from edb.schema import objects as s_obj
from edb.schema import pointers as s_pointers
from edb.schema import types as s_types

from . import context
from . import stmtctx


def get_path_id(stype: s_obj.Object, *,
                typename: Optional[str]=None,
                ctx: context.ContextLevel) -> irast.PathId:
    return irast.PathId.from_type(
        ctx.env.schema, stype,
        typename=typename,
        namespace=ctx.path_id_namespace)


def get_tuple_indirection_path_id(
        tuple_path_id: irast.PathId, element_name: str,
        element_type: s_types.Type, *,
        ctx: context.ContextLevel) -> irast.PathId:
    return tuple_path_id.extend(
        ptrcls=irast.TupleIndirectionLink(element_name),
        direction=s_pointers.PointerDirection.Outbound,
        target=element_type,
        schema=ctx.env.schema
    )


def get_type_indirection_path_id(
        path_id: irast.PathId, target_type: s_types.Type, *,
        optional: bool, ancestral: bool, cardinality: qltypes.Cardinality,
        ctx: context.ContextLevel) -> irast.PathId:
    return path_id.extend(
        ptrcls=irast.TypeIndirectionLink(
            path_id.target, target_type,
            optional=optional,
            ancestral=ancestral,
            cardinality=cardinality),
        direction=s_pointers.PointerDirection.Outbound,
        target=target_type,
        schema=ctx.env.schema
    )


def get_expression_path_id(
        stype: s_types.Type, alias: Optional[str] = None, *,
        ctx: context.ContextLevel) -> irast.PathId:
    if alias is None:
        alias = ctx.aliases.get('expr')
    typename = s_name.Name(module='__derived__', name=alias)
    return get_path_id(stype, typename=typename, ctx=ctx)


def register_set_in_scope(
        ir_set: irast.Set, *,
        path_scope: irast.ScopeTreeNode=None,
        ctx: context.ContextLevel) -> None:
    if path_scope is None:
        path_scope = ctx.path_scope

    try:
        path_scope.attach_path(ir_set.path_id)
    except irast.InvalidScopeConfiguration as e:
        raise errors.QueryError(
            e.args[0], context=ir_set.context) from e


def assign_set_scope(
        ir_set: irast.Set, scope: Optional[irast.ScopeTreeNode], *,
        ctx: context.ContextLevel) -> irast.Set:
    if scope is None:
        ir_set.path_scope_id = None
    else:
        if scope.unique_id is None:
            scope.unique_id = ctx.scope_id_ctr.nextval()
        ir_set.path_scope_id = scope.unique_id
        if scope.find_child(ir_set.path_id):
            raise RuntimeError('scoped set must not contain itself')

    return ir_set


def get_set_scope(
        ir_set: irast.Set, *,
        ctx: context.ContextLevel) -> Optional[irast.ScopeTreeNode]:
    if ir_set.path_scope_id is None:
        return None
    else:
        return ctx.path_scope.root.find_by_unique_id(ir_set.path_scope_id)


def mark_path_as_optional(
        path_id: irast.PathId, *,
        ctx: context.ContextLevel) -> None:
    ctx.path_scope.mark_as_optional(path_id)


def extend_path_id(
        path_id: irast.PathId, *,
        ptrcls, direction=None, target=None, ns=None,
        ctx: context.ContextLevel) -> irast.PathId:

    result = path_id.extend(ptrcls=ptrcls, direction=direction, target=target,
                            ns=ns, schema=ctx.env.schema)

    ptrref = result.rptr()
    stmtctx.ensure_ptrref_cardinality(ptrcls, ptrref, ctx=ctx)

    return result


def ban_path(
        path_id: irast.PathId, *,
        ctx: context.ContextLevel) -> None:

    ctx.banned_paths.add(path_id.strip_weak_namespaces())


def path_is_banned(
        path_id: irast.PathId, *,
        ctx: context.ContextLevel) -> bool:

    s_path_id = path_id.strip_weak_namespaces()
    return s_path_id in ctx.banned_paths and ctx.path_scope.is_visible(path_id)
