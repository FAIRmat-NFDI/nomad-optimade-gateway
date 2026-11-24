from typing import Annotated

from optimade.server.config import ServerConfig as OptimadeConfig
from pydantic import Field


class ServerConfig(OptimadeConfig):

    nomad_base_url: Annotated[
        str,
        Field(
            description=(
                "Base URL of the NOMAD API to query through the `nomad_graph` "
                "database backend. Note NOMAD's default `api_base_path` is "
                "`/nomad-oasis` (nomad.config.models.config.Config.api_base_path), "
                "e.g. 'http://localhost:8000/nomad-oasis/api/v1' - confirmed against "
                "a live instance, not just '/api/v1'."
            )
        ),
    ] = "http://localhost:8000/api/v1"

    nomad_api_token: Annotated[
        str | None,
        Field(
            description=(
                "Optional bearer token used to authenticate against `nomad_base_url` "
                "when using the `nomad_graph` database backend."
            )
        ),
    ] = None


CONFIG = ServerConfig()

