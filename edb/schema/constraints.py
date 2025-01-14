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


from __future__ import annotations

from edb import errors

from edb import edgeql
from edb.edgeql import ast as qlast
from edb.edgeql import qltypes as ft

from . import abc as s_abc
from . import annos as s_anno
from . import delta as sd
from . import expr as s_expr
from . import functions as s_func
from . import inheriting
from . import name as sn
from . import objects as so
from . import pseudo as s_pseudo
from . import referencing
from . import utils


class Constraint(referencing.ReferencedInheritingObject,
                 s_func.CallableObject, s_abc.Constraint,
                 qlkind=ft.SchemaObjectClass.CONSTRAINT):

    expr = so.SchemaField(
        s_expr.Expression, default=None, compcoef=0.909,
        coerce=True, allow_ddl_set=True)

    subjectexpr = so.SchemaField(
        s_expr.Expression,
        default=None, compcoef=0.833, coerce=True)

    orig_subjectexpr = so.SchemaField(
        str, default=None, coerce=True, compcoef=0.909,
        allow_ddl_set=True
    )

    finalexpr = so.SchemaField(
        s_expr.Expression,
        default=None, compcoef=0.909, coerce=True)

    subject = so.SchemaField(
        so.Object, default=None, inheritable=False)

    args = so.SchemaField(
        s_expr.ExpressionList,
        default=None, coerce=True, inheritable=False,
        compcoef=0.875)

    delegated = so.SchemaField(
        bool,
        default=False,
        inheritable=False,
        compcoef=0.9,
    )

    errmessage = so.SchemaField(
        str, default=None, compcoef=0.971, allow_ddl_set=True)

    def get_verbosename(self, schema, *, with_parent: bool=False) -> str:
        is_abstract = self.generic(schema)
        vn = super().get_verbosename(schema)
        if is_abstract:
            return f'abstract {vn}'
        else:
            if with_parent:
                pvn = self.get_subject(schema).get_verbosename(
                    schema, with_parent=True)
                return f'{vn} of {pvn}'
            else:
                return vn

    def generic(self, schema):
        return self.get_subject(schema) is None

    @classmethod
    def _dummy_subject(cls, schema):
        # Point subject placeholder to a dummy pointer to make EdgeQL
        # pipeline happy.
        return s_pseudo.Any.instance

    @classmethod
    def get_concrete_constraint_attrs(
            cls, schema, subject, *, name, subjectexpr=None,
            sourcectx=None, args=None, modaliases=None, **kwargs):
        from edb.edgeql import parser as qlparser
        from edb.edgeql import utils as qlutils

        constr_base = schema.get(name, module_aliases=modaliases)
        module_aliases = {}

        orig_subjectexpr = subjectexpr
        orig_subject = subject
        base_subjectexpr = constr_base.get_field_value(schema, 'subjectexpr')
        if subjectexpr is None:
            subjectexpr = base_subjectexpr
        elif (base_subjectexpr is not None
                and subjectexpr.text != base_subjectexpr.text):
            raise errors.InvalidConstraintDefinitionError(
                'subjectexpr is already defined for ' +
                f'{str(name)!r}')

        if subjectexpr is not None:
            subject_ql = subjectexpr.qlast
            if subject_ql is None:
                subject_ql = qlparser.parse(subjectexpr.text, module_aliases)

            subject = subject_ql

        expr: s_expr.Expression = constr_base.get_field_value(schema, 'expr')
        if not expr:
            raise errors.InvalidConstraintDefinitionError(
                f'missing constraint expression in {name!r}')

        expr_ql = qlparser.parse(expr.text, module_aliases)

        if not args:
            args = constr_base.get_field_value(schema, 'args')

        attrs = dict(kwargs)
        inherited = dict()
        if orig_subjectexpr is not None:
            attrs['subjectexpr'] = orig_subjectexpr
        else:
            base_subjectexpr = constr_base.get_subjectexpr(schema)
            if base_subjectexpr is not None:
                attrs['subjectexpr'] = base_subjectexpr
                inherited['subjectexpr'] = True

        errmessage = attrs.get('errmessage')
        if not errmessage:
            errmessage = constr_base.get_errmessage(schema)
            inherited['errmessage'] = True

        attrs['errmessage'] = errmessage

        if subject is not orig_subject:
            # subject has been redefined
            qlutils.inline_anchors(expr_ql, anchors={qlast.Subject: subject})
            subject = orig_subject

        args_map = None
        if args:
            args_ql = [
                qlast.Path(steps=[qlast.Subject()]),
            ]

            args_ql.extend(
                qlparser.parse(arg.text, module_aliases) for arg in args
            )

            args_map = qlutils.index_parameters(
                args_ql,
                parameters=constr_base.get_params(schema),
                schema=schema)

            qlutils.inline_parameters(expr_ql, args_map)

            args_map = {name: edgeql.generate_source(val, pretty=False)
                        for name, val in args_map.items()}

            args_map['__subject__'] = '{__subject__}'
            attrs['errmessage'] = attrs['errmessage'].format(**args_map)
            inherited.pop('errmessage', None)

        attrs['args'] = args

        if expr == '__subject__':
            expr_context = sourcectx
        else:
            expr_context = None

        final_expr = s_expr.Expression.compiled(
            s_expr.Expression.from_ast(expr_ql, schema, module_aliases),
            schema=schema,
            modaliases=module_aliases,
            anchors={qlast.Subject: subject},
        )

        bool_t = schema.get('std::bool')
        expr_type = final_expr.irast.stype
        if not expr_type.issubclass(schema, bool_t):
            raise errors.InvalidConstraintDefinitionError(
                f'{name} constraint expression expected '
                f'to return a bool value, got '
                f'{expr_type.get_name(schema).name!r}',
                context=expr_context
            )

        attrs['return_type'] = constr_base.get_return_type(schema)
        attrs['return_typemod'] = constr_base.get_return_typemod(schema)
        attrs['finalexpr'] = final_expr
        attrs['params'] = constr_base.get_params(schema)

        return constr_base, attrs, inherited

    def format_error_message(self, schema):
        errmsg = self.get_errmessage(schema)
        subject = self.get_subject(schema)
        titleattr = subject.get_annotation(schema, 'std::title')

        if not titleattr:
            subjname = subject.get_shortname(schema)
            subjtitle = subjname.name
        else:
            subjtitle = titleattr

        formatted = errmsg.format(__subject__=subjtitle)

        return formatted

    @classmethod
    def delta_properties(cls, delta, old, new, *, context=None,
                         old_schema, new_schema):
        super().delta_properties(
            delta, old, new, context=context,
            old_schema=old_schema, new_schema=new_schema)

        if new is not None and new.get_subject(new_schema) is not None:
            new_params = new.get_params(new_schema)
            if old is None or new_params != old.get_params(old_schema):
                delta.add(
                    sd.AlterObjectProperty(
                        property='params',
                        new_value=new_params,
                        source='inheritance',
                    )
                )

    @classmethod
    def get_root_classes(cls):
        return (
            sn.Name(module='std', name='constraint'),
        )

    @classmethod
    def get_default_base_name(self):
        return sn.Name('std::constraint')


class ConsistencySubject(inheriting.InheritingObject):
    constraints_refs = so.RefDict(
        attr='constraints',
        ref_cls=Constraint)

    constraints = so.SchemaField(
        so.ObjectIndexByFullname,
        inheritable=False, ephemeral=True, coerce=True, compcoef=0.887,
        default=so.ObjectIndexByFullname)

    def add_constraint(self, schema, constraint, replace=False):
        return self.add_classref(
            schema, 'constraints', constraint, replace=replace)


class ConsistencySubjectCommandContext:
    # context mixin
    pass


class ConsistencySubjectCommand(inheriting.InheritingObjectCommand):
    pass


class ConstraintCommandContext(sd.ObjectCommandContext,
                               s_anno.AnnotationSubjectCommandContext):
    pass


class ConstraintCommand(
        referencing.ReferencedInheritingObjectCommand,
        s_func.CallableCommand,
        schema_metaclass=Constraint, context_class=ConstraintCommandContext,
        referrer_context_class=ConsistencySubjectCommandContext):

    @classmethod
    def _validate_subcommands(cls, astnode):
        # check that 'subject' and 'subjectexpr' are not set as annotations
        for command in astnode.commands:
            cname = command.name
            if cls._is_special_name(cname):
                raise errors.InvalidConstraintDefinitionError(
                    f'{cname.name} is not a valid constraint annotation',
                    context=command.context)

    @classmethod
    def _is_special_name(cls, astnode):
        # check that 'subject' and 'subjectexpr' are not set as annotations
        return (astnode.name in {'subject', 'subjectexpr'} and
                not astnode.module)

    @classmethod
    def _classname_quals_from_ast(cls, schema, astnode, base_name,
                                  referrer_name, context):
        if isinstance(astnode, qlast.CreateConstraint):
            return ()

        exprs = []
        args = cls._constraint_args_from_ast(schema, astnode, context)
        for arg in args:
            exprs.append(arg.text)

        subjexpr_text = None

        # check if "orig_subjectexpr" field is set
        for node in astnode.commands:
            if isinstance(node, qlast.SetField):
                if (node.name.module is None and
                        node.name.name == 'orig_subjectexpr'):
                    subjexpr_text = node.value.value
                    break

        if subjexpr_text is None and astnode.subjectexpr:
            # if not, then use the origtext directly from the expression
            expr = s_expr.Expression.from_ast(
                astnode.subjectexpr, schema, context.modaliases)
            subjexpr_text = expr.origtext

        if subjexpr_text:
            exprs.append(subjexpr_text)

        return (cls._name_qual_from_exprs(schema, exprs),)

    @classmethod
    def _constraint_args_from_ast(cls, schema, astnode, context):
        args = []

        if astnode.args:
            for arg in astnode.args:
                arg_expr = s_expr.Expression.from_ast(
                    arg, schema, context.modaliases)
                args.append(arg_expr)

        return args

    def compile_expr_field(self, schema, context, field, value):
        from edb.edgeql import compiler as qlcompiler

        if field.name in ('expr', 'subjectexpr'):
            if isinstance(self, CreateConstraint):
                params = self._get_params(schema, context)
            else:
                params = self.scls.get_params(schema)
            anchors, _ = (
                qlcompiler.get_param_anchors_for_callable(
                    params, schema, inlined_defaults=False)
            )
            referrer_ctx = self.get_referrer_context(context)
            if referrer_ctx is not None:
                anchors['__subject__'] = referrer_ctx.op.scls

            return s_expr.Expression.compiled(
                value,
                schema=schema,
                modaliases=context.modaliases,
                anchors=anchors,
                func_params=params,
                allow_generic_type_output=True,
                parent_object_type=self.get_schema_metaclass(),
            )
        else:
            return super().compile_expr_field(schema, context, field, value)


class CreateConstraint(ConstraintCommand,
                       s_func.CreateCallableObject,
                       referencing.CreateReferencedInheritingObject):

    astnode = [qlast.CreateConcreteConstraint, qlast.CreateConstraint]
    referenced_astnode = qlast.CreateConcreteConstraint

    @classmethod
    def _get_param_desc_from_ast(cls, schema, modaliases, astnode, *,
                                 param_offset: int=0):

        if not hasattr(astnode, 'params'):
            # Concrete constraint.
            return []

        params = super()._get_param_desc_from_ast(
            schema, modaliases, astnode, param_offset=param_offset + 1)

        params.insert(0, s_func.ParameterDesc(
            num=param_offset,
            name='__subject__',
            default=None,
            type=s_pseudo.Any.instance,
            typemod=ft.TypeModifier.SINGLETON,
            kind=ft.ParameterKind.POSITIONAL,
        ))

        return params

    def _create_begin(self, schema, context):
        referrer_ctx = self.get_referrer_context(context)
        if referrer_ctx is None:
            return super()._create_begin(schema, context)

        subject = referrer_ctx.scls
        if subject.is_scalar() and subject.is_enum(schema):
            raise errors.UnsupportedFeatureError(
                f'constraints cannot be defined on an enumerated type',
                context=self.source_context,
            )

        if self.get_attribute_value('orig_subjectexpr') is None:
            subjexpr = self.get_local_attribute_value('subjectexpr')
            if subjexpr:
                self.set_attribute_value('orig_subjectexpr', subjexpr.origtext)

        if not context.canonical:
            schema, props = self._get_create_fields(schema, context)
            props.pop('name')
            props.pop('subject', None)
            fullname = self.classname
            shortname = sn.shortname_from_fullname(fullname)

            constr_base, attrs, inh = Constraint.get_concrete_constraint_attrs(
                schema,
                subject,
                name=shortname,
                sourcectx=self.source_context,
                **props)

            for k, v in attrs.items():
                inherited = inh.get(k)
                self.set_attribute_value(k, v, inherited=inherited)

            self.set_attribute_value('subject', subject)

        return super()._create_begin(schema, context)

    @classmethod
    def as_inherited_ref_cmd(cls, schema, context, astnode, parents):
        cmd = super().as_inherited_ref_cmd(schema, context, astnode, parents)

        args = cls._constraint_args_from_ast(schema, astnode, context)
        if args:
            cmd.set_attribute_value('args', args)

        subj_expr = parents[0].get_subjectexpr(schema)
        if subj_expr is not None:
            cmd.set_attribute_value('subjectexpr', subj_expr)

        cmd.set_attribute_value(
            'bases', so.ObjectList.create(schema, parents))

        return cmd

    @classmethod
    def as_inherited_ref_ast(cls, schema, context, name, parent):
        refctx = cls.get_referrer_context(context)
        astnode_cls = cls.referenced_astnode

        if sn.Name.is_qualified(name):
            nref = qlast.ObjectRef(
                name=name.name,
                module=name.module,
            )
        else:
            nref = qlast.ObjectRef(
                name=name,
                module=refctx.op.classname.module,
            )

        args = []

        parent_args = parent.get_args(schema)
        if parent_args:
            for arg_expr in parent.get_args(schema):
                arg = edgeql.parse_fragment(arg_expr.text)
                args.append(arg)

        subj_expr = parent.get_subjectexpr(schema)
        if subj_expr is not None:
            subj_expr_ql = edgeql.parse_fragment(subj_expr.text)
        else:
            subj_expr_ql = None

        astnode = astnode_cls(name=nref, args=args, subjectexpr=subj_expr_ql)

        return astnode

    @classmethod
    def _cmd_tree_from_ast(cls, schema, astnode, context):
        cmd = super()._cmd_tree_from_ast(schema, astnode, context)

        if isinstance(astnode, qlast.CreateConcreteConstraint):
            if astnode.delegated:
                cmd.set_attribute_value('delegated', astnode.delegated)

            args = cls._constraint_args_from_ast(schema, astnode, context)
            if args:
                cmd.add(
                    sd.AlterObjectProperty(
                        property='args',
                        new_value=args
                    )
                )

        elif isinstance(astnode, qlast.CreateConstraint):
            params = cls._get_param_desc_from_ast(
                schema, context.modaliases, astnode)

            for param in params:
                if param.get_kind(schema) is ft.ParameterKind.NAMED_ONLY:
                    raise errors.InvalidConstraintDefinitionError(
                        'named only parameters are not allowed '
                        'in this context',
                        context=astnode.context)

                if param.get_default(schema) is not None:
                    raise errors.InvalidConstraintDefinitionError(
                        'constraints do not support parameters '
                        'with defaults',
                        context=astnode.context)

        if cmd.get_attribute_value('return_type') is None:
            cmd.add(sd.AlterObjectProperty(
                property='return_type',
                new_value=utils.reduce_to_typeref(
                    schema, schema.get('std::bool')
                )
            ))

        if cmd.get_attribute_value('return_typemod') is None:
            cmd.add(sd.AlterObjectProperty(
                property='return_typemod',
                new_value=ft.TypeModifier.SINGLETON,
            ))

        # 'subjectexpr' can be present in either astnode type
        if astnode.subjectexpr:
            subjectexpr = s_expr.Expression.from_ast(
                astnode.subjectexpr,
                schema,
                context.modaliases,
            )

            cmd.add(sd.AlterObjectProperty(
                property='subjectexpr',
                new_value=subjectexpr
            ))

        cls._validate_subcommands(astnode)

        return cmd

    def _apply_field_ast(self, schema, context, node, op):
        subjectexpr = self.get_local_attribute_value('subjectexpr')
        if subjectexpr is not None:
            # add subjectexpr to the node
            node.subjectexpr = subjectexpr.qlast

        if op.property == 'delegated':
            if isinstance(node, qlast.CreateConcreteConstraint):
                node.delegated = op.new_value
            else:
                node.commands.append(
                    qlast.SetSpecialField(
                        name=qlast.ObjectRef(name='delegated'),
                        value=op.new_value,
                    )
                )
        elif op.property == 'args':
            node.args = [arg.qlast for arg in op.new_value]
        else:
            super()._apply_field_ast(schema, context, node, op)

    @classmethod
    def _classbases_from_ast(cls, schema, astnode, context):
        if isinstance(astnode, qlast.CreateConcreteConstraint):
            classname = cls._classname_from_ast(schema, astnode, context)
            base_name = sn.shortname_from_fullname(classname)
            base = schema.get(base_name)
            return so.ObjectList.create(
                schema, [utils.reduce_to_typeref(schema, base)])
        else:
            return super()._classbases_from_ast(schema, astnode, context)


class RenameConstraint(ConstraintCommand, sd.RenameObject):
    pass


class AlterConstraint(ConstraintCommand,
                      referencing.AlterReferencedInheritingObject):
    astnode = [qlast.AlterConcreteConstraint, qlast.AlterConstraint]
    referenced_astnode = qlast.AlterConcreteConstraint

    @classmethod
    def _cmd_tree_from_ast(cls, schema, astnode, context):
        cmd = super()._cmd_tree_from_ast(schema, astnode, context)

        if isinstance(astnode, (qlast.CreateConcreteConstraint,
                                qlast.AlterConcreteConstraint)):
            subject_ctx = context.get(ConsistencySubjectCommandContext)
            new_subject_name = None

            if getattr(astnode, 'delegated', False):
                cmd.set_attribute_value('delegated', astnode.delegated)

            for op in subject_ctx.op.get_subcommands(
                    type=sd.RenameObject):
                new_subject_name = op.new_name

            if new_subject_name is not None:
                cmd.add(
                    sd.AlterObjectProperty(
                        property='subject',
                        new_value=so.ObjectRef(
                            classname=new_subject_name
                        )
                    )
                )

            new_name = None
            for op in cmd.get_subcommands(type=RenameConstraint):
                new_name = op.new_name

            if new_name is not None:
                cmd.add(
                    sd.AlterObjectProperty(
                        property='name',
                        new_value=new_name
                    )
                )

        cls._validate_subcommands(astnode)

        return cmd

    def _apply_field_ast(self, schema, context, node, op):
        if op.property == 'delegated':
            node.delegated = op.new_value
        else:
            super()._apply_field_ast(schema, context, node, op)


class DeleteConstraint(ConstraintCommand, s_func.DeleteCallableObject):
    astnode = [qlast.DropConcreteConstraint, qlast.DropConstraint]
    referenced_astnode = qlast.DropConcreteConstraint
