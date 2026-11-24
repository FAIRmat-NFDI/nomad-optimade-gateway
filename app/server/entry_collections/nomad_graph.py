import re
from collections import Counter
from typing import Any, Optional

import requests

from app.filtertransformer import NomadGraphTransformer
from optimade.models import EntryResource
from optimade.server.config import ServerConfig
from optimade.server.entry_collections import EntryCollection, PaginationMechanism
from optimade.server.logger import get_logger
from optimade.server.mappers import BaseResourceMapper
from optimade.server.query_params import EntryListingQueryParams, SingleEntryQueryParams

_METER_TO_ANGSTROM = 1e10

_SPECIES_LABEL_RE = re.compile(r"([A-Z][a-z]?)")

_ARCHIVE_REQUIRED = {
    "results": {"properties": {"structures": {"structure_original": "*"}}}
}

# OPTIMADE fields whose values exist only in the archive's
# `results.properties.structures.structure_original` section: they carry no
# Elasticsearch annotation in NOMAD's `results.py`, so they are not search
# quantities and cannot be aliased (an alias would push them into
# `required.include` and 422 the whole search). They are served through a
# second `/entries/archive/query` fetch instead.
_ARCHIVE_ONLY_FIELDS = {
    "lattice_vectors",
    "cartesian_site_positions",
    "species_at_sites",
    "dimension_types",
    "species",
}

# `elements_ratios` responses are computed from the fetched `species_at_sites`
# (the same derivation NOMAD's own `OptimadeNormalizer` used), so it shares the
# archive fetch; its *filters* go through the config alias to the indexed
# `results.material.elemental_composition.atomic_fraction` leaf instead.
_ARCHIVE_COMPUTED_FIELDS = {"elements_ratios"}


class NomadGraphCollection(EntryCollection):
    """Queries NOMAD's own Graph API (`POST {nomad_base_url}/graph/query`) instead of
    a private Elasticsearch index.

    OPTIMADE filters are compiled locally into NOMAD's own native search criteria
    dict by `NomadGraphTransformer`, targeting `results.material` (NOMAD's regular,
    actively-maintained materials data) rather than NOMAD's `optimade_filter` query
    criterion, which queries the `OptimadeEntry` section slated for deprecation.
    """

    pagination_mechanism = PaginationMechanism("page_offset")

    def __init__(
        self,
        name: str,
        resource_cls: type[EntryResource],
        resource_mapper: BaseResourceMapper,
        config: ServerConfig,
        session: Optional["requests.Session"] = None,
    ):
        super().__init__(
            resource_cls=resource_cls,
            resource_mapper=resource_mapper,
            transformer=NomadGraphTransformer(mapper=resource_mapper),
            config=config,
        )

        self.config = config
        self.name = name

        self.session = session or requests.Session()
        if config.nomad_api_token:
            self.session.headers["Authorization"] = f"Bearer {config.nomad_api_token}"

        get_logger().info(
            "Using: NOMAD Graph API backend at %s", config.nomad_base_url
        )

    def handle_query_params(
        self, params: EntryListingQueryParams | SingleEntryQueryParams
    ) -> dict[str, Any]:
        criteria = super().handle_query_params(params)
        # `find()` pops `criteria["fields"]` before `_run_db_query` sees it;
        # keep a copy so the archive fetch can be skipped when no archive-only
        # field was requested.
        criteria["response_fields"] = set(criteria.get("fields") or ())
        return criteria

    def count(self, *args: Any, **kwargs: Any) -> int:
        raise NotImplementedError

    def __len__(self) -> int:
        response = self._graph_search(
            {
                "owner": "public",
                "query": {},
                "pagination": {"page_size": 1},
                "required": {"include": ["entry_id"]},
            }
        )
        m_response, _ = self._split_search_response(response)
        return m_response.get("pagination", {}).get("total", 0)

    def insert(self, data: list[EntryResource | dict]) -> None:
        raise NotImplementedError(
            "NomadGraphCollection is a read-only translation of NOMAD's own API "
            "and cannot insert data."
        )

    def _graph_search(self, metadata_query: dict[str, Any]) -> dict[str, Any]:
        """POSTs a `search`-rooted request to NOMAD's Graph API and returns the
        `search` node of the response: a dict mapping matched `entry_id`s to their
        (metadata-only) documents, plus a sibling `m_response` key carrying
        response metadata (including `pagination`) - confirmed against a live
        NOMAD instance, not assumed.
        """
        response = self.session.post(
            f"{self.config.nomad_base_url}/graph/query",
            json={
                "search": {
                    "m_request": {"query": metadata_query, "directive": "plain"}
                }
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()["search"]

    @staticmethod
    def _split_search_response(
        search_response: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Splits a `_graph_search()` result into its `m_response` metadata
        (containing `pagination`) and the list of matched entry documents.
        """
        m_response = search_response.get("m_response", {})
        entries = [
            doc for key, doc in search_response.items() if key != "m_response"
        ]
        return m_response, entries

    @classmethod
    def _flatten(cls, doc: dict[str, Any], prefix: str = "") -> dict[str, Any]:
        """Flattens nested dicts into dotted keys (`{"results": {"material":
        {"elements": [...]}}}` -> `{"results.material.elements": [...]}`), which
        is the shape `map_back()`'s aliases match against. Lists and scalars are
        kept as-is. Already-flat docs pass through unchanged.
        """
        flat: dict[str, Any] = {}
        for key, value in doc.items():
            dotted = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                flat.update(cls._flatten(value, dotted))
            else:
                flat[dotted] = value
        return flat

    def _fetch_archives(self, entry_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetches the `structure_original` archive section for the given page of
        entries in one `/entries/archive/query` call. Returns entry_id ->
        `structure_original` dict (empty when an entry has no such section).
        """
        response = self.session.post(
            f"{self.config.nomad_base_url}/entries/archive/query",
            json={
                "owner": "public",
                "query": {"entry_id:any": entry_ids},
                "pagination": {"page_size": len(entry_ids)},
                "required": _ARCHIVE_REQUIRED,
            },
            timeout=60,
        )
        response.raise_for_status()

        archives: dict[str, dict[str, Any]] = {}
        for item in response.json().get("data", []):
            structure = (
                (item.get("archive") or {})
                .get("results", {})
                .get("properties", {})
                .get("structures", {})
                .get("structure_original", {})
            )
            archives[item["entry_id"]] = structure or {}
        return archives

    def _augment_from_archive(
        self, results: list[dict[str, Any]], requested_fields: set[str]
    ) -> None:
        """Merges archive-only structural fields into the search-result docs,
        under their bare OPTIMADE names (no alias exists for them, so
        `map_back()` passes them through into `attributes` unchanged).
        """
        entry_ids = [doc["entry_id"] for doc in results if doc.get("entry_id")]
        if not entry_ids:
            return
        archives = self._fetch_archives(entry_ids)

        ratios_key = self.resource_mapper.get_backend_field("elements_ratios")

        for doc in results:
            structure = archives.get(doc.get("entry_id"), {})

            # Archives store SI units; OPTIMADE structures use angstrom.
            for field in ("lattice_vectors", "cartesian_site_positions"):
                value = structure.get(field)
                if field in requested_fields and value is not None:
                    doc[field] = [
                        [coord * _METER_TO_ANGSTROM for coord in row]
                        for row in value
                    ]

            for field in ("species_at_sites", "dimension_types"):
                if field in requested_fields and structure.get(field) is not None:
                    doc[field] = structure[field]

            if "species" in requested_fields and structure.get("species"):
                doc["species"] = [
                    {
                        key: sp[key]
                        for key in ("name", "chemical_symbols", "concentration")
                        if key in sp
                    }
                    for sp in structure["species"]
                ]

            if "elements_ratios" in requested_fields:
                ratios = self._elements_ratios(structure.get("species_at_sites"))
                if ratios is not None:
                    # Overwrite the aliased backend key: the raw
                    # `atomic_fraction` list retrieved via the search index has
                    # no guaranteed ordering relative to `elements`.
                    doc[ratios_key] = ratios

    @staticmethod
    def _elements_ratios(species_at_sites: list[str] | None) -> list[float] | None:
        """Element ratios from per-site species labels, ordered to match the
        alphabetically sorted `elements` list (the OPTIMADE convention).
        """
        if not species_at_sites:
            return None
        symbols = []
        for label in species_at_sites:
            match = _SPECIES_LABEL_RE.match(label)
            symbols.append(match.group(1) if match else label)
        counts = Counter(symbols)
        total = sum(counts.values())
        return [counts[element] / total for element in sorted(counts)]

    def _run_db_query(
        self, criteria: dict[str, Any], single_entry: bool = False
    ) -> tuple[list[dict[str, Any]], int | None, bool]:
        # `criteria["filter"]` is already a NOMAD-native query dict, compiled by
        # `NomadGraphTransformer` (or `{}` if no filter was given).
        query: dict[str, Any] = criteria.get("filter") or {}

        limit = criteria.get("limit", self.config.page_limit)
        page_offset = criteria.get("skip", 0) or 0
        pagination: dict[str, Any] = {"page_size": limit, "page_offset": page_offset}

        sort = list(criteria.get("sort") or [])
        if sort:
            field, sort_dir = sort[0]
            pagination["order_by"] = field
            pagination["order"] = "desc" if sort_dir == -1 else "asc"

        # As with the Mongo/Elasticsearch backends: fetch all fields known to the
        # mapper and let `EntryCollection.find()` trim to the requested subset.
        # Unlike Mongo/ES though, NOMAD validates `required.include` against its own
        # registered search quantities and rejects anything else with a 422
        # ("X is not a doc quantity") - confirmed live. `self.all_fields` includes
        # every standard OPTIMADE attribute (`immutable_id`, `species`,
        # `structure_features`, optional `space_group_*` fields, etc.), most of which
        # have no configured alias and would be sent as their bare OPTIMADE name -
        # never a real NOMAD path. So only fields with an actual alias/mapping
        # (`get_backend_field(field) != field`) are included; `type`/`relationships`/
        # `links` are also excluded outright, since they're pure OPTIMADE/JSON:API
        # concepts with no backend quantity at all (`type` is synthesized in
        # `map_back()`; `relationships`/`links` aren't populated by this mapper).
        non_backend_fields = self.resource_mapper.TOP_LEVEL_NON_ATTRIBUTES_FIELDS - {
            "id"
        }
        all_aliased_fields = sorted(
            {
                self.resource_mapper.get_backend_field(field)
                for field in self.all_fields
                if field not in non_backend_fields
                and self.resource_mapper.get_backend_field(field) != field
            }
        )

        response = self._graph_search(
            {
                "owner": "public",
                "query": query,
                "pagination": pagination,
                "required": {"include": all_aliased_fields},
            }
        )

        m_response, results = self._split_search_response(response)
        pagination_response = m_response.get("pagination", {})

        # NOMAD returns nested JSON; map_back's aliases match dotted keys.
        results = [self._flatten(doc) for doc in results]

        requested_fields: set[str] = criteria.get("response_fields") or set()

        if results and requested_fields & (
            _ARCHIVE_ONLY_FIELDS | _ARCHIVE_COMPUTED_FIELDS
        ):
            self._augment_from_archive(results, requested_fields)

        if "structure_features" in requested_fields:
            # No NOMAD equivalent exists; NOMAD's own embedded OPTIMADE
            # implementation also serves a constant empty list
            # (nomad/normalizing/optimade.py). The filter side is handled
            # consistently in NomadGraphTransformer.
            for doc in results:
                doc["structure_features"] = []

        data_returned = pagination_response.get("total")
        if data_returned is None:
            data_returned = len(results)

        if single_entry:
            more_data_available = False
        else:
            more_data_available = page_offset + limit < data_returned

        return results, data_returned, more_data_available
