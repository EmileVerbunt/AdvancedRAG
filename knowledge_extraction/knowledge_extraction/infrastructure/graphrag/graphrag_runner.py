"""Microsoft GraphRAG runner: scaffolds Azure-aware config and runs the indexer.

graphrag (>=2.x) consumes raw `input/*.txt` files, does its own chunking,
extraction, and community detection — so we feed it the chunk text we already
have and let it build community summaries. The pre-extracted ontology graph we
produce ourselves is a *separate* artifact.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import textwrap
from collections.abc import Iterable
from pathlib import Path

from knowledge_extraction.config.settings import AzureAuthMode, Settings
from knowledge_extraction.domain import Chunk, OntologyVersion

log = logging.getLogger(__name__)


class GraphRagRunner:
    """Wraps the official `graphrag` CLI with Azure OpenAI settings derived from our config."""

    def __init__(self, root: Path, settings: Settings) -> None:
        self._root = root
        self._settings = settings

    def workdir(self, version: OntologyVersion) -> Path:
        d = self._root / version.version
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_inputs(self, version: OntologyVersion, chunks: Iterable[Chunk]) -> Path:
        wd = self.workdir(version)

        # 1) Scaffold prompts / .env / settings.yaml via the official `graphrag init`.
        proc = asyncio.run(self._init(wd))
        if proc != 0:
            raise RuntimeError(f"graphrag init failed with exit code {proc}")

        # 2) Overwrite settings.yaml with our Azure-aware config.
        (wd / "settings.yaml").write_text(self._azure_settings_yaml(), encoding="utf-8")

        # 3) Write .env with API key. graphrag 2.7 requires api_key auth for chat models,
        #    so in CREDENTIAL mode we fetch a short-lived bearer token from DefaultAzureCredential.
        api_key = self._resolve_api_key()
        (wd / ".env").write_text(f"GRAPHRAG_API_KEY={api_key}\n", encoding="utf-8")

        # 4) Write chunk texts as input/*.txt; clear stale text inputs first.
        in_dir = wd / "input"
        in_dir.mkdir(parents=True, exist_ok=True)
        for stale in in_dir.glob("*.txt"):
            stale.unlink()
        # Drop legacy parquets from older runner version.
        for stale in in_dir.glob("*.parquet"):
            stale.unlink()
        n = 0
        for c in chunks:
            (in_dir / f"{c.id}.txt").write_text(c.text or "", encoding="utf-8")
            n += 1

        log.info("graphrag inputs written", extra={
            "event": "graphrag.write_inputs",
            "workdir": str(wd),
            "chunks": n,
            "auth": self._settings.azure_auth_mode.value,
        })
        return wd

    def _resolve_api_key(self) -> str:
        """Return an Azure OpenAI API key — either the configured static key, or a
        short-lived AAD bearer token when running in CREDENTIAL mode.

        graphrag 2.7 currently rejects `auth_type: azure_managed_identity` for chat
        models, so we feed the bearer token through the api_key channel. The token
        has a ~1h TTL — fine for a single `graphrag index` run.
        """
        if self._settings.azure_auth_mode == AzureAuthMode.KEY:
            if not self._settings.azure_openai_api_key:
                raise RuntimeError(
                    "AZURE_AUTH_MODE=key but AZURE_OPENAI_API_KEY is empty."
                )
            return self._settings.azure_openai_api_key

        # CREDENTIAL mode: fetch bearer token via DefaultAzureCredential.
        from azure.identity import DefaultAzureCredential

        cred = DefaultAzureCredential()
        token = cred.get_token("https://cognitiveservices.azure.com/.default")
        return token.token

    async def _init(self, wd: Path) -> int:
        # `graphrag init` refuses if files exist; use --force to refresh prompts cleanly.
        proc = await asyncio.create_subprocess_exec(
            "graphrag", "init", "--root", str(wd), "--force",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await proc.communicate()
        if proc.returncode != 0:
            log.error("graphrag init failed",
                      extra={"err": err.decode("utf-8", "replace")[-2000:]})
        return proc.returncode or 0

    async def index(self, version: OntologyVersion) -> int:
        wd = self.workdir(version)
        # Wipe previous output so re-runs are deterministic.
        for sub in ("output", "cache", "logs"):
            p = wd / sub
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)

        # The Azure preflight (validate_config) occasionally returns a transient 401
        # ("Key based authentication is disabled for this resource") even when local
        # auth is enabled. Retry a few times with backoff before giving up.
        rc = 0
        last_err = b""
        for attempt in range(3):
            proc = await asyncio.create_subprocess_exec(
                "graphrag", "index", "--root", str(wd),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            log.info(
                "graphrag stdout",
                extra={"out": out.decode("utf-8", "replace")[-2000:], "attempt": attempt + 1},
            )
            rc = proc.returncode or 0
            last_err = err
            if rc == 0:
                return 0
            transient = b"Key based authentication is disabled" in err or b"validate_config" in err
            if not transient or attempt == 2:
                break
            log.warning("graphrag transient failure, retrying", extra={"attempt": attempt + 1})
            await asyncio.sleep(2 ** attempt)
        log.error("graphrag failed", extra={"err": last_err.decode("utf-8", "replace")[-2000:]})
        return rc

    # ----------------------------------------------------------------------- #
    # settings.yaml builder                                                    #
    # ----------------------------------------------------------------------- #

    def _azure_settings_yaml(self) -> str:
        s = self._settings
        endpoint = s.azure_openai_endpoint.rstrip("/")
        api_version = s.azure_openai_api_version
        chat_model = s.azure_openai_extraction_model
        embed_model = s.azure_openai_embedding_model
        # graphrag 2.7 only supports api_key auth for chat/embedding models.
        # When in CREDENTIAL mode the api key is a bearer token (see _resolve_api_key).
        auth_type = "api_key"
        api_key_line = "    api_key: ${GRAPHRAG_API_KEY}\n"

        return textwrap.dedent(f"""\
            # Generated by knowledge_extraction.GraphRagRunner — do not edit manually.
            models:
              default_chat_model:
                type: chat
                model_provider: azure
                auth_type: {auth_type}
            {api_key_line.rstrip()}
                api_base: {endpoint}
                api_version: "{api_version}"
                deployment_name: {chat_model}
                model: {chat_model}
                model_supports_json: true
                concurrent_requests: 4
                async_mode: threaded
                retry_strategy: exponential_backoff
                max_retries: 6
                tokens_per_minute: null
                requests_per_minute: null
              default_embedding_model:
                type: embedding
                model_provider: azure
                auth_type: {auth_type}
            {api_key_line.rstrip()}
                api_base: {endpoint}
                api_version: "{api_version}"
                deployment_name: {embed_model}
                model: {embed_model}
                concurrent_requests: 4
                async_mode: threaded
                retry_strategy: exponential_backoff
                max_retries: 6
                tokens_per_minute: null
                requests_per_minute: null

            input:
              storage:
                type: file
                base_dir: "input"
              file_type: text

            chunks:
              size: 1200
              overlap: 100
              group_by_columns: [id]

            output:
              type: file
              base_dir: "output"

            cache:
              type: file
              base_dir: "cache"

            reporting:
              type: file
              base_dir: "logs"

            vector_store:
              default_vector_store:
                type: lancedb
                db_uri: output/lancedb
                container_name: default

            embed_text:
              model_id: default_embedding_model
              vector_store_id: default_vector_store

            extract_graph:
              model_id: default_chat_model
              prompt: "prompts/extract_graph.txt"
              entity_types: [organization, person, geo, event]
              max_gleanings: 1

            summarize_descriptions:
              model_id: default_chat_model
              prompt: "prompts/summarize_descriptions.txt"
              max_length: 500

            extract_graph_nlp:
              text_analyzer:
                extractor_type: regex_english

            cluster_graph:
              max_cluster_size: 10

            extract_claims:
              enabled: false
              model_id: default_chat_model
              prompt: "prompts/extract_claims.txt"
              description: "Any claims or facts that could be relevant to information discovery."
              max_gleanings: 1

            community_reports:
              model_id: default_chat_model
              graph_prompt: "prompts/community_report_graph.txt"
              text_prompt: "prompts/community_report_text.txt"
              max_length: 2000
              max_input_length: 8000

            embed_graph:
              enabled: false

            umap:
              enabled: false

            snapshots:
              graphml: true
              embeddings: false

            local_search:
              chat_model_id: default_chat_model
              embedding_model_id: default_embedding_model
              prompt: "prompts/local_search_system_prompt.txt"

            global_search:
              chat_model_id: default_chat_model
              map_prompt: "prompts/global_search_map_system_prompt.txt"
              reduce_prompt: "prompts/global_search_reduce_system_prompt.txt"
              knowledge_prompt: "prompts/global_search_knowledge_system_prompt.txt"
        """)
