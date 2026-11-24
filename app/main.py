"""ASGI entrypoint for the NOMAD OPTIMADE gateway.

Run with e.g. ``fastapi run`` (uses ``[tool.fastapi] entrypoint``) or
``uvicorn app.main:app``. Configuration is read from ``OPTIMADE_*`` environment
variables and the JSON/YAML file named by ``OPTIMADE_CONFIG_FILE``.
"""

import optimade
from fastapi import FastAPI
from optimade.models import (
    FileResource,
    LinksResource,
    ReferenceResource,
    StructureResource,
)
from optimade.server.exception_handlers import OPTIMADE_EXCEPTIONS
from optimade.server.mappers import (
    FileMapper,
    LinksMapper,
    ReferenceMapper,
    StructureMapper,
)
from optimade.server.mappers.entries import BaseResourceMapper
from optimade.server.routers import (
    files,
    info,
    landing,
    links,
    references,
    structures,
    versions,
)
from optimade.server.routers.utils import BASE_URL_PREFIXES, JSONAPIResponse

from app.config import CONFIG
from app.server.entry_collections.nomad_graph import NomadGraphCollection

app = FastAPI(
    root_path=CONFIG.root_path,
    title="OPTIMADE API",
    description="""The [Open Databases Integration for Materials Design (OPTIMADE) consortium](https://www.optimade.org/) aims to make materials databases interoperational by developing a common REST API.
    This specification is generated using [`optimade-python-tools`](https://github.com/Materials-Consortia/optimade-python-tools/tree/v{optimade.__version__}) v{optimade.__version__}.""",
    version=optimade.__api_version__,
    docs_url=f"{BASE_URL_PREFIXES['major']}/extensions/docs",
    openapi_url=f"{BASE_URL_PREFIXES['major']}/extensions/openapi.json",
    default_response_class=JSONAPIResponse,
    separate_input_output_schemas=False,
)


# Save the config in the app state for access in endpoints
app.state.config = CONFIG

# Back every OPTIMADE entry endpoint with the NOMAD Graph API.
# create entry collections and save in app state for access in endpoints
app.state.entry_collections = {
    "links": NomadGraphCollection(
        name=CONFIG.links_collection,
        resource_cls=LinksResource,
        resource_mapper=LinksMapper(CONFIG),
        config=CONFIG,
    ),
    "references": NomadGraphCollection(
        name=CONFIG.references_collection,
        resource_cls=ReferenceResource,
        resource_mapper=ReferenceMapper(CONFIG),
        config=CONFIG,
    ),
    "structures": NomadGraphCollection(
        name=CONFIG.structures_collection,
        resource_cls=StructureResource,
        resource_mapper=StructureMapper(CONFIG),
        config=CONFIG,
    ),
    "files": NomadGraphCollection(
        name="files",
        resource_cls=FileResource,
        resource_mapper=FileMapper(CONFIG),
        config=CONFIG,
    ),
}

# store also the BaseResourceMapper
app.state.base_resource_mapper = BaseResourceMapper()

# Landing page at base URL, with links to all endpoints
# @app.get("/", response_class=HTMLResponse, include_in_schema=False)
# async def root(request: Request):
#     return await landing(request)
app.include_router(landing.router)

# Add exception handlers
for exception, handler in OPTIMADE_EXCEPTIONS:
    app.add_exception_handler(exception, handler)

# Add various endpoints to unversioned URL
app.include_router(info.router)
app.include_router(links.router)
app.include_router(references.router)
app.include_router(structures.router)
app.include_router(files.router)
app.include_router(versions.router)

# Mandatory /vMajor base URL (all endpoints except /versions)
app.include_router(
    info.router, prefix=BASE_URL_PREFIXES["major"], include_in_schema=False
)
app.include_router(
    links.router, prefix=BASE_URL_PREFIXES["major"], include_in_schema=False
)
app.include_router(
    references.router, prefix=BASE_URL_PREFIXES["major"], include_in_schema=False
)
app.include_router(
    structures.router, prefix=BASE_URL_PREFIXES["major"], include_in_schema=False
)
app.include_router(
    files.router, prefix=BASE_URL_PREFIXES["major"], include_in_schema=False
)
