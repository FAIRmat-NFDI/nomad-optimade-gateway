from typing import Any

from lark import v_args

from optimade.filtertransformers.base_transformer import BaseTransformer, Quantity

__all__ = ("NomadGraphTransformer",)


class NomadGraphTransformer(BaseTransformer):
    """Transforms OPTIMADE filter parse trees into NOMAD's own native search
    criteria dict (as accepted by `Metadata.query` / `/api/v1/entries/query` and,
    through it, the Graph API's `search` node), rather than an Elasticsearch DSL
    query. Compiling locally means the translation service no longer depends on
    NOMAD's `optimade_filter` query criterion, which itself queries the
    `OptimadeEntry` section slated for deprecation.

    NOMAD's query dict uses plain field names (optionally suffixed
    `:qualifier`, one of `lt`/`lte`/`gt`/`gte`/`all`/`any`/`none`) mapped to
    values, plus `and`/`or`/`not` keys for boolean composition. A bare list
    value with no qualifier is automatically treated as `all` (all values must
    match) by NOMAD's own query validation, which conveniently gives OPTIMADE's
    `HAS ALL` semantics for free.

    Some OPTIMADE operators have no equivalent in this query format, since it
    is a public, validated search API rather than raw Elasticsearch access
    (unlike the Mongo/Elasticsearch backends this class replaces): `IS KNOWN`/
    `IS UNKNOWN`, `CONTAINS`/`STARTS`/`ENDS`, `HAS ONLY`, and zipped/correlated
    list queries all raise `NotImplementedError`.
    """

    operator_map = {
        "<": "lt",
        "<=": "lte",
        ">": "gt",
        ">=": "gte",
    }

    # `structure_features` is served as a constant empty list (no NOMAD
    # equivalent exists; NOMAD's own embedded implementation also hardcodes
    # `[]`). Filters on it are translated consistently with that constant:
    # criteria that an empty list satisfies match everything, the rest match
    # nothing. Both constants are always-valid indexed criteria, so they
    # compose safely inside `and`/`or`/`not`. The bound is -1 rather than 0
    # because NOMAD's `Range` validation rejects falsy boundary values
    # ("At least one range boundary must be defined" for a legitimate 0 -
    # `any([lt, lte, gt, gte])` in nomad/app/v1/models/models.py).
    _MATCH_ALL = {"results.material.n_elements:gte": -1}
    _MATCH_NONE = {"results.material.n_elements:lt": -1}

    def _field(self, quantity: str | Quantity) -> str:
        """Resolve a `property`-rule result (a `Quantity` or a passed-through
        string, e.g. for another provider's field) to a backend field name.
        """
        if isinstance(quantity, str):
            if self.mapper and quantity in self.mapper.RELATIONSHIP_ENTRY_TYPES:
                raise NotImplementedError(
                    f"Unable to filter on relationships with type {quantity!r}"
                )
            return quantity

        return quantity.backend_field  # type: ignore[return-value]

    @staticmethod
    def _quantity_name(quantity: Quantity | str) -> str:
        return quantity if isinstance(quantity, str) else quantity.name

    def _query_op(
        self, quantity: Quantity | str, op: str, value: Any
    ) -> dict[str, Any]:
        """Build a single-criterion NOMAD query dict for the given quantity,
        comparison operator, and value.
        """
        if self._quantity_name(quantity) == "structure_features":
            # An empty list never equals or compares to a scalar value.
            return dict(self._MATCH_ALL if op == "!=" else self._MATCH_NONE)

        field = self._field(quantity)
        qualifier = self.operator_map.get(op)
        if qualifier:
            return {f"{field}:{qualifier}": value}

        if op in ("=", ""):
            return {field: value}

        if op == "!=":
            return {"not": {field: value}}

        raise NotImplementedError(f"Unsupported operator {op!r}.")

    def _has_query_op(
        self, quantity: Quantity | str, op: str, values: list[tuple[str, Any]]
    ) -> dict[str, Any]:
        """Build a NOMAD query dict for a `HAS`/`HAS ALL`/`HAS ANY`/`HAS ONLY`
        expression.
        """
        if self._quantity_name(quantity) == "structure_features":
            # The constant empty list contains nothing.
            return dict(self._MATCH_NONE)

        field = self._field(quantity)

        if op == "HAS":
            if len(values) != 1:
                raise NotImplementedError(
                    "HAS with multiple comma-separated values is not supported."
                )
            has_op, value = values[0]
            return self._query_op(quantity, has_op, value)

        if op in ("HAS ALL", "HAS ANY"):
            if any(has_op != "=" for has_op, _ in values):
                raise NotImplementedError(
                    f"{op} does not support comparison operators on individual values."
                )
            plain_values = [value for _, value in values]
            if op == "HAS ALL":
                # A bare list with no qualifier is treated as `all` by NOMAD's
                # own query validation (`_validate_criteria_value`).
                return {field: plain_values}
            return {f"{field}:any": plain_values}

        if op == "HAS ONLY":
            raise NotImplementedError(
                "HAS ONLY queries are not supported by the NOMAD Graph API backend."
            )

        raise NotImplementedError(f"Unrecognised operation {op!r}.")

    def __default__(self, data: Any, children: Any, meta: Any) -> Any:
        """Default behaviour for rules that only replace one symbol with another."""
        return children[0]

    def expression(self, args: list) -> Any:
        """expression: expression_clause ( OR expression_clause )*"""
        if len(args) == 1:
            return args[0]
        return {"or": list(args)}

    def expression_clause(self, args: list) -> Any:
        """expression_clause: expression_phrase ( AND expression_phrase )*

        Always builds an explicit `{"and": [...]}` list, rather than merging
        criteria dicts, to avoid silently dropping a criterion when the same
        field appears more than once (e.g. two separate `HAS` clauses).
        """
        if len(args) == 1:
            return args[0]
        return {"and": list(args)}

    def expression_phrase(self, args: list) -> Any:
        """expression_phrase: [ NOT ] ( comparison | "(" expression ")" )"""
        if args[0] == "NOT":
            return {"not": args[1]}
        return args[0]

    @v_args(inline=True)
    def property_first_comparison(self, quantity: Quantity | str, query) -> Any:
        """property_first_comparison: property ( value_op_rhs | known_op_rhs |
        fuzzy_string_op_rhs | set_op_rhs | set_zip_op_rhs | length_op_rhs )
        """
        return query(quantity)

    @v_args(inline=True)
    def constant_first_comparison(self, value: Any, op: str, quantity) -> Any:
        """constant_first_comparison: constant OPERATOR ( non_string_value |
        not_implemented_string )
        """
        return self._query_op(quantity, self._reversed_operator_map[op], value)

    @v_args(inline=True)
    def value_op_rhs(self, op: str, value: Any):
        """value_op_rhs: OPERATOR value"""
        return lambda quantity: self._query_op(quantity, op, value)

    def length_op_rhs(self, args: list):
        """length_op_rhs: LENGTH [ OPERATOR ] value"""
        value = args[-1]
        op = args[1] if len(args) == 3 else "="

        def query(quantity):
            if self._quantity_name(quantity) == "structure_features":
                # Evaluate the predicate against the constant length 0.
                predicates = {
                    "=": 0 == value,
                    "!=": 0 != value,
                    "<": 0 < value,
                    "<=": 0 <= value,
                    ">": 0 > value,
                    ">=": 0 >= value,
                }
                return dict(
                    self._MATCH_ALL if predicates[op] else self._MATCH_NONE
                )

            if isinstance(quantity, str):
                return self._query_op(quantity, op, value)

            if quantity.length_quantity is None:
                raise NotImplementedError(
                    f"LENGTH is not supported for {quantity.name!r}"
                )
            return self._query_op(quantity.length_quantity, op, value)

        return query

    @v_args(inline=True)
    def known_op_rhs(self, _is, value):
        """known_op_rhs: IS ( KNOWN | UNKNOWN )"""
        raise NotImplementedError(
            "IS KNOWN / IS UNKNOWN are not supported by the NOMAD Graph API "
            "backend: NOMAD's public search API exposes no exists-style query "
            "criterion."
        )

    def fuzzy_string_op_rhs(self, args: list):
        """fuzzy_string_op_rhs: CONTAINS value | STARTS [ WITH ] value | ENDS [ WITH ] value"""
        raise NotImplementedError(
            "CONTAINS/STARTS/ENDS are not supported by the NOMAD Graph API "
            "backend: NOMAD's public search API exposes no substring/prefix/"
            "suffix query criterion."
        )

    def set_op_rhs(self, args: list):
        """set_op_rhs: HAS ( [ OPERATOR ] value | ANY value_list | ALL value_list | ONLY value_list )"""
        values = args[-1]
        if isinstance(values, list):
            # ALL/ANY/ONLY keyword form: args = [HAS, <keyword>, value_list]
            set_op = f"HAS {args[1]}"
        else:
            # Single-value form, with an optional comparison operator:
            # args = [HAS, value] or [HAS, <operator>, value]
            op = args[1] if len(args) == 3 else "="
            values = [(op, values)]
            set_op = "HAS"
        return lambda quantity: self._has_query_op(quantity, set_op, values)

    def set_zip_op_rhs(self, args: list):
        """set_zip_op_rhs: property_zip_addon HAS (...)"""
        raise NotImplementedError(
            "Correlated (zipped) list queries are not supported by the NOMAD "
            "Graph API backend."
        )

    def property_zip_addon(self, args: list):
        """property_zip_addon: ":" property (":" property)*"""
        raise NotImplementedError(
            "Correlated (zipped) list queries are not supported by the NOMAD "
            "Graph API backend."
        )

    def value_zip(self, args: list):
        """value_zip: [ OPERATOR ] value ":" [ OPERATOR ] value (":" [ OPERATOR ] value)*"""
        raise NotImplementedError(
            "Correlated (zipped) list queries are not supported by the NOMAD "
            "Graph API backend."
        )

    def value_zip_list(self, args: list):
        """value_zip_list: value_zip ( "," value_zip )*"""
        raise NotImplementedError(
            "Correlated (zipped) list queries are not supported by the NOMAD "
            "Graph API backend."
        )

    def value_list(self, args: list) -> list[tuple[str, Any]]:
        """value_list: [ OPERATOR ] value ( "," [ OPERATOR ] value )*"""
        result = []
        op = "="
        for arg in args:
            if arg in ("<", "<=", ">", ">=", "!=", "="):
                op = arg
            else:
                result.append((op, arg))
                op = "="
        return result
