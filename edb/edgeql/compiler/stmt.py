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


"""EdgeQL statement compilation routines."""


from __future__ import annotations

from typing import *  # NoQA

from edb import errors

from edb.ir import ast as irast
from edb.ir import typeutils as irtyputils

from edb.schema import ddl as s_ddl
from edb.schema import links as s_links
from edb.schema import lproperties as s_lprops
from edb.schema import name as s_name
from edb.schema import objects as s_obj
from edb.schema import objtypes as s_objtypes
from edb.schema import pointers as s_pointers
from edb.schema import types as s_types

from edb.edgeql import ast as qlast
from edb.edgeql import qltypes

from . import astutils
from . import clauses
from . import context
from . import dispatch
from . import inference
from . import pathctx
from . import setgen
from . import viewgen
from . import schemactx
from . import stmtctx


@dispatch.compile.register(qlast.SelectQuery)
def compile_SelectQuery(
        expr: qlast.SelectQuery, *, ctx: context.ContextLevel) -> irast.Set:
    if astutils.is_degenerate_select(expr) and ctx.toplevel_stmt is not None:
        # Compile implicit "SELECT Path" as "Path"
        with ctx.new() as sctx:
            process_with_block(expr, ctx=sctx, parent_ctx=ctx)
            sctx.aliased_views = ctx.aliased_views.new_child()
            sctx.modaliases = ctx.modaliases.copy()
            sctx.anchors = ctx.anchors.copy()
            result = compile_result_clause(
                expr.result,
                view_scls=ctx.view_scls,
                view_rptr=ctx.view_rptr,
                view_name=ctx.toplevel_result_view_name,
                ctx=sctx)
            result = fini_stmt(result, expr, ctx=sctx, parent_ctx=ctx)

        return result

    with ctx.subquery() as sctx:
        stmt = irast.SelectStmt()
        init_stmt(stmt, expr, ctx=sctx, parent_ctx=ctx)
        if expr.implicit:
            # Make sure path prefix does not get blown away by
            # implicit subqueries.
            sctx.partial_path_prefix = ctx.partial_path_prefix

        stmt.result = compile_result_clause(
            expr.result,
            view_scls=ctx.view_scls,
            view_rptr=ctx.view_rptr,
            result_alias=expr.result_alias,
            view_name=ctx.toplevel_result_view_name,
            ctx=sctx)

        clauses.compile_where_clause(
            stmt, expr.where, ctx=sctx)

        stmt.orderby = clauses.compile_orderby_clause(
            expr.orderby, ctx=sctx)

        stmt.offset = clauses.compile_limit_offset_clause(
            expr.offset, ctx=sctx)

        stmt.limit = clauses.compile_limit_offset_clause(
            expr.limit, ctx=sctx)

        result = fini_stmt(stmt, expr, ctx=sctx, parent_ctx=ctx)

    return result


@dispatch.compile.register(qlast.ForQuery)
def compile_ForQuery(
        qlstmt: qlast.ForQuery, *, ctx: context.ContextLevel) -> irast.Set:
    with ctx.subquery() as sctx:
        stmt = irast.SelectStmt()
        init_stmt(stmt, qlstmt, ctx=sctx, parent_ctx=ctx)

        if qlstmt.offset is not None or qlstmt.limit is not None:
            # LIMIT and OFFSET are infix operators with both
            # operands being SET OF, so we need to compile
            # the body of the statement behind a fence.
            sctx.path_scope = sctx.path_scope.attach_fence()

        with sctx.newscope(fenced=True) as scopectx:
            iterator = qlstmt.iterator
            if isinstance(iterator, qlast.Set) and len(iterator.elements) == 1:
                iterator = iterator.elements[0]

            iterator_view = stmtctx.declare_view(
                iterator, qlstmt.iterator_alias, ctx=scopectx)

            stmt.iterator_stmt = setgen.new_set_from_set(
                iterator_view, ctx=scopectx)

            iterator_scope = scopectx.path_scope_map.get(iterator_view)

        pathctx.register_set_in_scope(stmt.iterator_stmt, ctx=sctx)
        node = sctx.path_scope.find_descendant(stmt.iterator_stmt.path_id)
        node.attach_subtree(iterator_scope)

        stmt.result = compile_result_clause(
            qlstmt.result,
            view_scls=ctx.view_scls,
            view_rptr=ctx.view_rptr,
            result_alias=qlstmt.result_alias,
            view_name=ctx.toplevel_result_view_name,
            forward_rptr=True,
            ctx=sctx)

        clauses.compile_where_clause(
            stmt, qlstmt.where, ctx=sctx)

        stmt.orderby = clauses.compile_orderby_clause(
            qlstmt.orderby, ctx=sctx)

        if qlstmt.offset is not None or qlstmt.limit is not None:
            sctx.path_scope = sctx.path_scope.parent

            stmt.offset = clauses.compile_limit_offset_clause(
                qlstmt.offset, ctx=sctx)

            stmt.limit = clauses.compile_limit_offset_clause(
                qlstmt.limit, ctx=sctx)

        result = fini_stmt(stmt, qlstmt, ctx=sctx, parent_ctx=ctx)

    return result


@dispatch.compile.register(qlast.GroupQuery)
def compile_GroupQuery(
        expr: qlast.Base, *, ctx: context.ContextLevel) -> irast.Set:

    raise errors.UnsupportedFeatureError(
        "'GROUP' statement is not currently implemented",
        context=expr.context)

    with ctx.subquery() as ictx:
        stmt = irast.GroupStmt()
        init_stmt(stmt, expr, ctx=ictx, parent_ctx=ctx)

        typename = s_name.Name(
            module='__group__', name=ctx.aliases.get('Group'))
        obj = ctx.env.get_track_schema_object('std::Object')
        stmt.group_path_id = pathctx.get_path_id(
            obj, typename=typename, ctx=ictx)

        pathctx.register_set_in_scope(stmt.group_path_id, ctx=ictx)

        with ictx.newscope(fenced=True) as subjctx:
            subjctx.clause = 'input'

            subject_set = setgen.scoped_set(
                dispatch.compile(expr.subject, ctx=subjctx), ctx=subjctx)

            alias = expr.subject_alias or subject_set.path_id.target_name_hint
            stmt.subject = stmtctx.declare_inline_view(
                subject_set, alias, ctx=ictx)

            with subjctx.new() as grpctx:
                stmt.groupby = compile_groupby_clause(
                    expr.groupby, ctx=grpctx)

        with ictx.subquery() as isctx, isctx.newscope(fenced=True) as sctx:
            o_stmt = sctx.stmt = irast.SelectStmt()

            o_stmt.result = compile_result_clause(
                expr.result,
                view_scls=ctx.view_scls,
                view_rptr=ctx.view_rptr,
                result_alias=expr.result_alias,
                view_name=ctx.toplevel_result_view_name,
                ctx=sctx)

            clauses.compile_where_clause(
                o_stmt, expr.where, ctx=sctx)

            o_stmt.orderby = clauses.compile_orderby_clause(
                expr.orderby, ctx=sctx)

            o_stmt.offset = clauses.compile_limit_offset_clause(
                expr.offset, ctx=sctx)

            o_stmt.limit = clauses.compile_limit_offset_clause(
                expr.limit, ctx=sctx)

            stmt.result = setgen.scoped_set(o_stmt, ctx=sctx)

        result = fini_stmt(stmt, expr, ctx=ictx, parent_ctx=ctx)

    return result


@dispatch.compile.register(qlast.InsertQuery)
def compile_InsertQuery(
        expr: qlast.InsertQuery, *, ctx: context.ContextLevel) -> irast.Set:
    with ctx.subquery() as ictx:
        ictx.implicit_id_in_shapes = False
        ictx.implicit_tid_in_shapes = False

        stmt = irast.InsertStmt()
        init_stmt(stmt, expr, ctx=ictx, parent_ctx=ctx)

        subject = dispatch.compile(expr.subject, ctx=ictx)
        assert isinstance(subject, irast.Set)

        # Self-references in INSERT are prohibited.
        pathctx.ban_path(subject.path_id, ctx=ictx)

        subject_stype = setgen.get_set_type(subject, ctx=ictx)
        if subject_stype.get_is_abstract(ctx.env.schema):
            raise errors.QueryError(
                f'cannot insert into abstract '
                f'{subject_stype.get_verbosename(ctx.env.schema)}',
                context=expr.subject.context)

        if subject_stype.is_view(ctx.env.schema):
            raise errors.QueryError(
                f'cannot insert into view '
                f'{subject_stype.get_shortname(ctx.env.schema)!r}',
                context=expr.subject.context)

        stmt.subject = compile_query_subject(
            subject,
            shape=expr.shape,
            view_rptr=ctx.view_rptr,
            compile_views=True,
            result_alias=expr.subject_alias,
            is_insert=True,
            ctx=ictx)

        stmt_subject_stype = setgen.get_set_type(subject, ctx=ictx)

        result = setgen.class_set(
            stmt_subject_stype.material_type(ctx.env.schema),
            path_id=stmt.subject.path_id, ctx=ctx)

        stmt.result = compile_query_subject(
            result,
            view_scls=ctx.view_scls,
            view_name=ctx.toplevel_result_view_name,
            compile_views=ictx.stmt is ictx.toplevel_stmt,
            ctx=ictx)

        result = fini_stmt(stmt, expr, ctx=ictx, parent_ctx=ctx)

    return result


@dispatch.compile.register(qlast.UpdateQuery)
def compile_UpdateQuery(
        expr: qlast.UpdateQuery, *, ctx: context.ContextLevel) -> irast.Set:
    with ctx.subquery() as ictx:
        ictx.implicit_id_in_shapes = False
        ictx.implicit_tid_in_shapes = False

        stmt = irast.UpdateStmt()
        init_stmt(stmt, expr, ctx=ictx, parent_ctx=ctx)

        subject = dispatch.compile(expr.subject, ctx=ictx)
        assert isinstance(subject, irast.Set)

        subj_type = inference.infer_type(subject, ictx.env)
        if not isinstance(subj_type, s_objtypes.ObjectType):
            raise errors.QueryError(
                f'cannot update non-ObjectType objects',
                context=expr.subject.context
            )

        ictx.partial_path_prefix = subject

        clauses.compile_where_clause(
            stmt, expr.where, ctx=ictx)

        stmt.subject = compile_query_subject(
            subject,
            shape=expr.shape,
            view_rptr=ctx.view_rptr,
            compile_views=True,
            result_alias=expr.subject_alias,
            is_update=True,
            ctx=ictx)

        stmt_subject_stype = setgen.get_set_type(subject, ctx=ictx)

        result = setgen.class_set(
            stmt_subject_stype.material_type(ctx.env.schema),
            path_id=stmt.subject.path_id, ctx=ctx)

        stmt.result = compile_query_subject(
            result,
            view_scls=ctx.view_scls,
            view_name=ctx.toplevel_result_view_name,
            compile_views=ictx.stmt is ictx.toplevel_stmt,
            ctx=ictx)

        result = fini_stmt(stmt, expr, ctx=ictx, parent_ctx=ctx)

    return result


@dispatch.compile.register(qlast.DeleteQuery)
def compile_DeleteQuery(
        expr: qlast.DeleteQuery, *, ctx: context.ContextLevel) -> irast.Set:
    with ctx.subquery() as ictx:
        ictx.implicit_id_in_shapes = False
        ictx.implicit_tid_in_shapes = False

        stmt = irast.DeleteStmt()
        # Expand the DELETE from sugar into full DELETE (SELECT ...)
        # form, if there's any additional clauses.
        if any([expr.where, expr.orderby, expr.offset, expr.limit]):
            if expr.offset or expr.limit:
                subjql = qlast.SelectQuery(
                    result=qlast.SelectQuery(
                        result=expr.subject,
                        result_alias=expr.subject_alias,
                        where=expr.where,
                        orderby=expr.orderby,
                        context=expr.context,
                        implicit=True,
                    ),
                    limit=expr.limit,
                    offset=expr.offset,
                    context=expr.context,
                )
            else:
                subjql = qlast.SelectQuery(
                    result=expr.subject,
                    result_alias=expr.subject_alias,
                    where=expr.where,
                    orderby=expr.orderby,
                    offset=expr.offset,
                    limit=expr.limit,
                    context=expr.context,
                )

            expr = qlast.DeleteQuery(
                aliases=expr.aliases,
                context=expr.context,
                subject=subjql,
            )

        init_stmt(stmt, expr, ctx=ictx, parent_ctx=ctx)

        # DELETE Expr is a delete(SET OF X), so we need a scope fence.
        with ictx.newscope(fenced=True) as scopectx:
            subject = setgen.scoped_set(
                dispatch.compile(expr.subject, ctx=scopectx), ctx=scopectx)

        subj_type = inference.infer_type(subject, ictx.env)
        if not isinstance(subj_type, s_objtypes.ObjectType):
            raise errors.QueryError(
                f'cannot delete non-ObjectType objects',
                context=expr.subject.context
            )

        stmt.subject = compile_query_subject(subject, shape=None, ctx=ictx)

        stmt_subject_stype = setgen.get_set_type(subject, ctx=ictx)
        result = setgen.class_set(
            stmt_subject_stype.material_type(ctx.env.schema),
            path_id=stmt.subject.path_id, ctx=ctx)

        stmt.result = compile_query_subject(
            result,
            view_scls=ctx.view_scls,
            view_name=ctx.toplevel_result_view_name,
            compile_views=ictx.stmt is ictx.toplevel_stmt,
            ctx=ictx)

        result = fini_stmt(stmt, expr, ctx=ictx, parent_ctx=ctx)

    return result


@dispatch.compile.register
def compile_DescribeStmt(
        ql: qlast.DescribeStmt, *, ctx: context.ContextLevel) -> irast.Set:
    with ctx.subquery() as ictx:
        stmt = irast.SelectStmt()
        init_stmt(stmt, ql, ctx=ictx, parent_ctx=ctx)

        if not ql.object:
            if ql.language is qltypes.DescribeLanguage.DDL:
                # DESCRIBE SCHEMA
                text = s_ddl.ddl_text_from_schema(ctx.env.schema)
            else:
                raise errors.QueryError(
                    f'cannot describe full schema as {ql.language}')
        else:
            modules = []
            items = []
            referenced_classes: List[s_obj.ObjectMeta] = []

            objref = ql.object

            if objref.itemclass is qltypes.SchemaObjectClass.MODULE:
                modules.append(objref.name)
            else:
                itemtypes = None

                if objref.itemclass is not None:
                    itemtypes = (
                        s_obj.ObjectMeta.get_schema_metaclass_for_ql_class(
                            objref.itemclass),
                    )

                obj = schemactx.get_schema_object(
                    objref,
                    item_types=itemtypes,
                    ctx=ictx,
                )
                items.append(obj.get_name(ictx.env.schema))

            if ql.language is qltypes.DescribeLanguage.DDL:
                method = s_ddl.ddl_text_from_schema
            elif ql.language is qltypes.DescribeLanguage.SDL:
                method = s_ddl.sdl_text_from_schema
            elif ql.language is qltypes.DescribeLanguage.TEXT:
                method = s_ddl.descriptive_text_from_schema
                if not ql.verbose:
                    referenced_classes = [s_links.Link, s_lprops.Property]
            else:
                raise errors.InternalServerError(
                    f'cannot handle describe language {ql.language}'
                )

            text = method(
                ctx.env.schema,
                included_modules=modules,
                included_items=items,
                included_ref_classes=referenced_classes,
                include_module_ddl=False,
                include_std_ddl=True,
            )

        ct = irtyputils.type_to_typeref(
            ctx.env.schema,
            ctx.env.get_track_schema_type('std::str'),
        )

        stmt.result = setgen.ensure_set(
            irast.StringConstant(value=text, typeref=ct),
            ctx=ictx,
        )

        result = fini_stmt(stmt, ql, ctx=ictx, parent_ctx=ctx)

    return result


@dispatch.compile.register(qlast.Shape)
def compile_Shape(
        shape: qlast.Shape, *, ctx: context.ContextLevel) -> irast.Set:
    expr = setgen.ensure_set(dispatch.compile(shape.expr, ctx=ctx), ctx=ctx)
    expr_stype = setgen.get_set_type(expr, ctx=ctx)
    view_type = viewgen.process_view(
        stype=expr_stype, path_id=expr.path_id,
        elements=shape.elements, ctx=ctx)

    return setgen.ensure_set(expr, type_override=view_type, ctx=ctx)


def init_stmt(
        irstmt: irast.Stmt, qlstmt: qlast.Statement, *,
        ctx: context.ContextLevel, parent_ctx: context.ContextLevel) -> None:

    ctx.stmt = irstmt
    if ctx.toplevel_stmt is None:
        parent_ctx.toplevel_stmt = ctx.toplevel_stmt = irstmt
        if parent_ctx.path_scope is None:
            parent_ctx.path_scope = ctx.path_scope = irast.new_scope_tree()
    else:
        ctx.path_scope = parent_ctx.path_scope.attach_fence()

    pending_own_ns = parent_ctx.pending_stmt_own_path_id_namespace
    if pending_own_ns:
        ctx.path_scope.add_namespaces(pending_own_ns)

    pending_full_ns = parent_ctx.pending_stmt_full_path_id_namespace
    if pending_full_ns:
        ctx.path_id_namespace |= pending_full_ns

    metadata = ctx.stmt_metadata.get(qlstmt)
    if metadata is not None and metadata.is_unnest_fence:
        ctx.path_scope.unnest_fence = True

    irstmt.parent_stmt = parent_ctx.stmt

    process_with_block(qlstmt, ctx=ctx, parent_ctx=parent_ctx)


def fini_stmt(
        irstmt: Union[irast.Stmt, irast.Set],
        qlstmt: qlast.Statement, *,
        ctx: context.ContextLevel,
        parent_ctx: context.ContextLevel) -> irast.Set:

    view_name = parent_ctx.toplevel_result_view_name
    t = inference.infer_type(irstmt, ctx.env)

    if t.get_name(ctx.env.schema) == view_name:
        # The view statement did contain a view declaration and
        # generated a view class with the requested name.
        view = t
        path_id = pathctx.get_path_id(view, ctx=parent_ctx)
    elif view_name is not None:
        # The view statement did _not_ contain a view declaration,
        # but we still want the correct path_id.
        view = ctx.env.schema.get(view_name, None)
        if view is None:
            view = schemactx.derive_view(
                t, derived_name=view_name, preserve_shape=True, ctx=parent_ctx)
        path_id = pathctx.get_path_id(view, ctx=parent_ctx)
    else:
        view = None
        path_id = None

    type_override = view if view is not None else None
    result = setgen.scoped_set(
        irstmt, type_override=type_override, path_id=path_id, ctx=ctx)

    if view is not None:
        parent_ctx.view_sets[view] = result

    return result


def process_with_block(
        edgeql_tree: qlast.Statement, *,
        ctx: context.ContextLevel, parent_ctx: context.ContextLevel) -> None:
    for with_entry in edgeql_tree.aliases:
        if isinstance(with_entry, qlast.ModuleAliasDecl):
            ctx.modaliases[with_entry.alias] = with_entry.module

        elif isinstance(with_entry, qlast.AliasedExpr):
            with ctx.new() as scopectx:
                scopectx.expr_exposed = False
                stmtctx.declare_view(
                    with_entry.expr, with_entry.alias, ctx=scopectx)

        else:
            raise RuntimeError(
                f'unexpected expression in WITH block: {with_entry}')


def compile_result_clause(
        result: qlast.Base, *,
        view_scls: Optional[s_types.Type]=None,
        view_rptr: Optional[context.ViewRPtr]=None,
        view_name: Optional[s_name.SchemaName]=None,
        result_alias: Optional[str]=None,
        forward_rptr: bool=False,
        ctx: context.ContextLevel) -> irast.Set:
    with ctx.new() as sctx:
        sctx.clause = 'result'
        if sctx.stmt is ctx.toplevel_stmt:
            sctx.toplevel_clause = sctx.clause
            sctx.expr_exposed = True

        if forward_rptr:
            sctx.view_rptr = view_rptr
            # sctx.view_scls = view_scls

        result_expr: qlast.Base
        shape: Optional[Sequence[qlast.ShapeElement]]

        if isinstance(result, qlast.Shape):
            result_expr = result.expr
            shape = result.elements
        else:
            result_expr = result
            shape = None

        if result_alias:
            # `SELECT foo := expr` is largely equivalent to
            # `WITH foo := expr SELECT foo` with one important exception:
            # the scope namespace does not get added to the current query
            # path scope.  This is needed to handle FOR queries correctly.
            with sctx.newscope(temporary=True, fenced=True) as scopectx:
                stmtctx.declare_view(
                    result_expr, alias=result_alias,
                    temporary_scope=False, ctx=scopectx)

            result_expr = qlast.Path(
                steps=[qlast.ObjectRef(name=result_alias)]
            )

        if (view_rptr is not None and
                (view_rptr.is_insert or view_rptr.is_update) and
                view_rptr.ptrcls is not None) and False:
            # If we have an empty set assigned to a pointer in an INSERT
            # or UPDATE, there's no need to explicitly specify the
            # empty set type and it can be assumed to match the pointer
            # target type.
            target_t = view_rptr.ptrcls.get_target(ctx.env.schema)

            if astutils.is_ql_empty_set(result_expr):
                expr = setgen.new_empty_set(
                    stype=target_t,
                    alias=ctx.aliases.get('e'),
                    ctx=sctx,
                    srcctx=result_expr.context,
                )
            else:
                with sctx.new() as exprctx:
                    exprctx.empty_result_type_hint = target_t
                    expr = setgen.ensure_set(
                        dispatch.compile(result_expr, ctx=exprctx),
                        ctx=exprctx)
        else:
            if astutils.is_ql_empty_set(result_expr):
                expr = setgen.new_empty_set(
                    stype=sctx.empty_result_type_hint,
                    alias=ctx.aliases.get('e'),
                    ctx=sctx,
                    srcctx=result_expr.context,
                )
            else:
                expr = setgen.ensure_set(
                    dispatch.compile(result_expr, ctx=sctx), ctx=sctx)

        ctx.partial_path_prefix = expr

        ir_result = compile_query_subject(
            expr, shape=shape, view_rptr=view_rptr, view_name=view_name,
            result_alias=result_alias,
            view_scls=view_scls,
            compile_views=ctx.stmt is ctx.toplevel_stmt,
            ctx=sctx)

        ctx.partial_path_prefix = ir_result

    return ir_result


def compile_query_subject(
        expr: irast.Set, *,
        shape: Optional[List[qlast.ShapeElement]]=None,
        view_rptr: Optional[context.ViewRPtr]=None,
        view_name: Optional[s_name.SchemaName]=None,
        result_alias: Optional[str]=None,
        view_scls: Optional[s_types.Type]=None,
        compile_views: bool=True,
        is_insert: bool=False,
        is_update: bool=False,
        ctx: context.ContextLevel) -> irast.Set:

    expr_stype = setgen.get_set_type(expr, ctx=ctx)
    expr_rptr = expr.rptr

    while isinstance(expr_rptr, irast.TypeIndirectionPointer):
        expr_rptr = expr_rptr.source.rptr

    is_ptr_alias = (
        view_rptr is not None
        and view_rptr.ptrcls is None
        and view_rptr.ptrcls_name is not None
        and expr_rptr is not None
        and expr_rptr.direction is s_pointers.PointerDirection.Outbound
        and (
            view_rptr.ptrcls_is_linkprop
            == (expr_rptr.ptrref.parent_ptr is not None)
        )
    )

    if is_ptr_alias:
        assert view_rptr is not None
        # We are inside an expression that defines a link alias in
        # the parent shape, ie. Spam { alias := Spam.bar }, so
        # `Spam.alias` should be a subclass of `Spam.bar` inheriting
        # its properties.
        view_rptr.base_ptrcls = irtyputils.ptrcls_from_ptrref(
            expr_rptr.ptrref, schema=ctx.env.schema)
        view_rptr.ptrcls_is_alias = True

    if (ctx.expr_exposed
            and viewgen.has_implicit_tid(
                expr_stype, is_mutation=is_insert or is_update, ctx=ctx)
            and shape is None
            and expr_stype not in ctx.env.view_shapes):
        # Force the subject to be compiled as a view if a __tid__
        # insertion is anticipated (the actual decision is taken
        # by the compile_view_shapes() flow).
        shape = []

    if shape is not None and view_scls is None:
        if (view_name is None and
                isinstance(result_alias, s_name.SchemaName)):
            view_name = result_alias

        view_scls = viewgen.process_view(
            stype=expr_stype, path_id=expr.path_id,
            elements=shape, view_rptr=view_rptr,
            view_name=view_name, is_insert=is_insert,
            is_update=is_update, ctx=ctx)

    if view_scls is not None:
        expr = setgen.ensure_set(expr, type_override=view_scls, ctx=ctx)
        expr_stype = view_scls

    if compile_views:
        rptr = view_rptr.rptr if view_rptr is not None else None
        viewgen.compile_view_shapes(expr, rptr=rptr, ctx=ctx)

    if (shape is not None or view_scls is not None) and len(expr.path_id) == 1:
        ctx.class_view_overrides[expr.path_id.target.id] = expr_stype

    return expr


def compile_groupby_clause(
        groupexprs: Iterable[qlast.Base], *,
        ctx: context.ContextLevel) -> List[irast.Set]:
    result: List[irast.Set] = []
    if not groupexprs:
        return result

    with ctx.new() as sctx:
        sctx.clause = 'groupby'
        if sctx.stmt.parent_stmt is None:
            sctx.toplevel_clause = sctx.clause

        ir_groupexprs = []
        for groupexpr in groupexprs:
            with sctx.newscope(fenced=True) as scopectx:
                ir_groupexpr = setgen.scoped_set(
                    dispatch.compile(groupexpr, ctx=scopectx), ctx=scopectx)
                ir_groupexpr.context = groupexpr.context
                ir_groupexprs.append(ir_groupexpr)

    return ir_groupexprs
