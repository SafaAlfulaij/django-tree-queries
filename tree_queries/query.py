from django.db import connections, models

from tree_queries.compiler import TreeQuery


__all__ = ("TreeQuerySet", "TreeManager", "TreeBase")


def pk(of):
    return of.pk if hasattr(of, "pk") else of


class TreeQuerySet(models.QuerySet):
    def with_tree_fields(self):
        self.query.__class__ = TreeQuery
        return self

    def ancestors(self, of, *, include_self=False):
        if not hasattr(of, "tree_path"):
            of = self.with_tree_fields().get(pk=pk(of))

        ids = of.tree_path if include_self else of.tree_path[:-1]
        return (
            self.with_tree_fields()  # TODO tree fields not strictly required
            .filter(id__in=ids)
            .order_by("__tree.tree_depth")
        )

    def descendants(self, of, *, include_self=False):
        connection = connections[self.db]
        if connection.vendor == "postgresql":
            queryset = self.with_tree_fields().extra(
                where=["{pk} = ANY(__tree.tree_path)".format(pk=pk(of))]
            )

        else:
            queryset = self.with_tree_fields().extra(
                where=['instr(__tree.tree_path, "x{:09x}") <> 0'.format(pk(of))]
            )

        if not include_self:
            return queryset.exclude(pk=pk(of))
        return queryset


class TreeManagerBase(models.Manager):
    def _ensure_parameters(self):
        # Compatibility with django-cte-forest
        pass


TreeManager = TreeManagerBase.from_queryset(TreeQuerySet)


class TreeBase(models.Model):
    objects = TreeManager()

    class Meta:
        abstract = True

    def ancestors(self, *, include_self=False):
        return self.__class__._default_manager.ancestors(
            self, include_self=include_self
        )

    def descendants(self, *, include_self=False):
        return self.__class__._default_manager.descendants(
            self, include_self=include_self
        )
