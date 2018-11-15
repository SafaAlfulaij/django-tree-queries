from __future__ import unicode_literals

from django.db import connections
from django.db.models.sql.compiler import SQLCompiler
from django.db.models.sql.query import Query


SEPARATOR = "\x1f"


class TreeQuery(Query):
    def get_compiler(self, using=None, connection=None):
        # Copied from django/db/models/sql/query.py
        if using is None and connection is None:
            raise ValueError("Need either using or connection")
        if using:
            connection = connections[using]
        # Difference: Not connection.ops.compiler, but our own compiler which
        # adds the CTE.
        return TreeCompiler(self, connection, using)


class TreeCompiler(SQLCompiler):
    CTE_POSTGRESQL = """
    WITH RECURSIVE __tree (
        "tree_depth",
        "tree_path",
        "tree_ordering",
        "tree_pk"
    ) AS (
        SELECT
            0 AS tree_depth,
            array[T.{pk}]::text[] AS tree_path,
            array[{order_by}]::text[] AS tree_ordering,
            T."{pk}"
        FROM {db_table} T
        WHERE T."{parent}" IS NULL

        UNION ALL

        SELECT
            __tree.tree_depth + 1 AS tree_depth,
            __tree.tree_path || T.{pk}::text,
            __tree.tree_ordering || {order_by}::text,
            T."{pk}"
        FROM {db_table} T
        JOIN __tree ON T."{parent}" = __tree.tree_pk
    )
    """

    CTE_MYSQL = """
    WITH RECURSIVE __tree(tree_depth, tree_path, tree_ordering, tree_pk) AS (
        SELECT
            0,
            -- Limit to max. 10 levels...
            CAST(CONCAT("{sep}", {pk}, "{sep}") AS char(1000)),
            CAST(CONCAT("{sep}", {order_by}, "{sep}") AS char(1000)),
            T.{pk}
        FROM {db_table} T
        WHERE T.{parent} IS NULL

        UNION ALL

        SELECT
            __tree.tree_depth + 1,
            CONCAT(__tree.tree_path, T2.{pk}, "{sep}"),
            CONCAT(__tree.tree_ordering, T2.{order_by}, "{sep}"),
            T2.{pk}
        FROM __tree, {db_table} T2
        WHERE __tree.tree_pk = T2.{parent}
    )
    """

    CTE_SQLITE3 = """
    WITH RECURSIVE __tree(tree_depth, tree_path, tree_ordering, tree_pk) AS (
        SELECT
            0 tree_depth,
            printf("{sep}%%s{sep}", {pk}) tree_path,
            printf("{sep}%%s{sep}", {order_by}) tree_ordering,
            T."{pk}" tree_pk
        FROM {db_table} T
        WHERE T."{parent}" IS NULL

        UNION ALL

        SELECT
            __tree.tree_depth + 1,
            __tree.tree_path || printf("%%s{sep}", T.{pk}),
            __tree.tree_ordering || printf("%%s{sep}", T.{order_by}),
            T."{pk}"
        FROM {db_table} T
        JOIN __tree ON T."{parent}" = __tree.tree_pk
    )
    """

    def as_sql(self, *args, **kwargs):
        # Summary queries are aggregates (not annotations)
        is_summary = self.query._annotations and any(  # pragma: no branch
            # OK if generator is not consumed completely
            annotation.is_summary
            for alias, annotation in self.query._annotations.items()
        )
        opts = self.query.model._meta

        params = {
            "parent": "parent_id",  # XXX Hardcoded.
            "pk": opts.pk.attname,
            "db_table": opts.db_table,
            "order_by": opts.ordering[0] if opts.ordering else opts.pk.attname,
            "sep": SEPARATOR,
        }

        if "__tree" not in self.query.extra_tables:  # pragma: no branch - unlikely
            self.query.add_extra(
                # Do not add extra fields to the select statement when it is a
                # summary query
                select={}
                if is_summary
                else {
                    "tree_depth": "__tree.tree_depth",
                    "tree_path": "__tree.tree_path",
                    "tree_ordering": "__tree.tree_ordering",
                },
                select_params=None,
                where=["__tree.tree_pk = {db_table}.{pk}".format(**params)],
                params=None,
                tables=["__tree"],
                order_by=(
                    []
                    # Do not add ordering for aggregates, or if the ordering
                    # has already been specified using .extra()
                    if is_summary or self.query.extra_order_by
                    else ["__tree.tree_ordering"]  # DFS is the only true way
                ),
            )

        sql = super(TreeCompiler, self).as_sql(*args, **kwargs)
        CTE = {
            "postgresql": self.CTE_POSTGRESQL,
            "sqlite": self.CTE_SQLITE3,
            "mysql": self.CTE_MYSQL,
        }[self.connection.vendor]
        return ("".join([CTE.format(**params), sql[0]]), sql[1])

    def get_converters(self, expressions):
        converters = super(TreeCompiler, self).get_converters(expressions)
        for i, expression in enumerate(expressions):
            if any(f in str(expression) for f in ("tree_path", "tree_ordering")):
                converters[i] = ([converter], expression)
        return converters


def converter(value, expression, connection, context=None):
    # context can be removed as soon as we only support Django>=2.0
    if isinstance(value, str):
        # MySQL/MariaDB and sqlite3 do not support arrays. Split the value on
        # the ASCII unit separator (chr(31)).
        # NOTE: The representation of array is NOT part of the API.
        value = value.split(SEPARATOR)[1:-1]

    try:
        # Either all values are convertible to int or don't bother
        return [int(v) for v in value]  # Maybe Field.to_python()?
    except ValueError:
        return value
